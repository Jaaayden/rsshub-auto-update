#!/usr/bin/env python3
"""RSSHub Compose updater. No third-party Python dependencies."""
import argparse
import contextlib
import datetime
import fcntl
import getpass
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import uuid

BASE = Path('/etc/rsshub-auto-update')
STATE = Path('/var/lib/rsshub-auto-update')
LIB = Path('/usr/local/lib/rsshub-auto-update')
UNITS = Path('/etc/systemd/system')

class Failure(Exception):
    pass

def run(args, cwd=None, timeout=600):
    env = {k: v for k, v in os.environ.items() if not k.startswith('COMPOSE_')}
    # Avoid inheriting arbitrary interpolation values from installer / systemd.
    env = {k: v for k, v in env.items() if k in ('PATH', 'HOME', 'LANG', 'DOCKER_HOST', 'DOCKER_CONTEXT')}
    p = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise Failure('命令失败：' + ' '.join(args[:2]))
    return p.stdout

def save(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def read(path):
    return json.loads(Path(path).read_text())

def compose(c, *args, override=None):
    cmd = ['docker', 'compose', '--project-directory', c['directory'], '-p', c['project']]
    for file in c['files']:
        cmd += ['-f', file]
    if override:
        cmd += ['-f', str(override)]
    return run(cmd + list(args), cwd=c['directory'])

def model(c):
    data = json.loads(compose(c, 'config', '--format', 'json'))
    services = {n: s for n, s in data['services'].items() if not s.get('profiles')}
    if not services:
        raise Failure('没有默认启用的服务')
    for n, s in services.items():
        if s.get('build') or not s.get('image'):
            raise Failure('仅支持镜像部署：' + n)
        if s.get('scale', 1) != 1 or s.get('deploy', {}).get('replicas', 1) != 1:
            raise Failure('首版要求每个服务一个容器：' + n)
        identity = (n + ' ' + s['image']).lower()
        if any(x in identity for x in ('rsshub', 'redis', 'browserless')):
            h = s.get('healthcheck', {})
            if h.get('disable') or not h.get('test') or h['test'][0] == 'NONE':
                raise Failure('服务缺少健康检查：' + n)
    return services

def snapshot(c, services):
    result = {}
    for n, s in services.items():
        ids = compose(c, 'ps', '-a', '-q', n).split()
        if len(ids) != 1:
            raise Failure('服务必须有一个运行容器：' + n)
        obj = json.loads(run(['docker', 'inspect', ids[0]]))[0]
        state = obj['State']
        if not state.get('Running') or state.get('Health', {}).get('Status', 'healthy') != 'healthy':
            raise Failure('服务未就绪：' + n)
        result[n] = obj['Image']
    return result

def discover(objects):
    found = {}
    for o in objects:
        labels = o.get('Config', {}).get('Labels') or {}
        if 'rsshub' not in (o.get('Config', {}).get('Image', '') + labels.get('com.docker.compose.service', '')).lower():
            continue
        p = labels.get('com.docker.compose.project')
        files = labels.get('com.docker.compose.project.config_files')
        directory = labels.get('com.docker.compose.project.working_dir')
        if p and files and directory:
            found[(p, files, directory)] = {'project': p, 'files': files.split(','), 'directory': directory}
    return list(found.values())

def cookie_lock(path=Path('/etc/rsshub-cookie-sync/config.json')):
    if path.exists():
        try:
            lock = read(path)['deployment']['lock_file']
            if not isinstance(lock, str) or not Path(lock).is_absolute():
                raise ValueError()
            return lock
        except Exception:
            raise Failure('无法解析 Cookie Sync deployment.lock_file') from None
    if Path('/usr/local/sbin/rsshub-cookie-sync-apply').exists():
        raise Failure('检测到 Cookie Sync，但配置缺失')
    return None

@contextlib.contextmanager
def lock(path):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with p.open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield

def bark_url(value):
    u = urllib.parse.urlsplit(value.strip())
    if u.scheme != 'https' or not u.hostname or u.username or u.password or u.query or u.fragment or not u.path.strip('/'):
        raise Failure('Bark 地址必须是包含设备密钥的 HTTPS 地址')
    return value.strip().rstrip('/')

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None

def send(c, state):
    body = '\n'.join([f"主机：{socket.gethostname()}", f"项目：{c['project']}",
                      f"阶段：{state['stage']}", f"时间：{state['time']}",
                      f"回退：{state.get('rollback', '未执行')}",
                      f"日志：journalctl -u rsshub-auto-update@{c['project']}.service"])
    data = json.dumps({'title': 'RSSHub 自动更新失败' if state['stage'] != '测试通知' else 'RSSHub 测试通知',
                       'body': body, 'group': 'rsshub-auto-update'}, ensure_ascii=False).encode()
    request = urllib.request.Request(bark_url(c['bark_url']), data=data, headers={'Content-Type': 'application/json'}, method='POST')
    for attempt in range(3):
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
                if json.loads(response.read(65536)).get('code') != 200:
                    raise Failure('Bark 拒绝请求')
            return True
        except Exception:
            if attempt < 2:
                time.sleep(2)
    print('Bark 通知发送失败（敏感详情已隐藏）', flush=True)
    return False

def notify(c, state, directory):
    with lock(directory / 'notify.lock'):
        marker = directory / ('notified-' + state['id'])
        if marker.exists():
            return
        # Claim before network I/O: service and OnFailure cannot duplicate a run.
        marker.touch(mode=0o600)
        send(c, state)

def update(c):
    directory = STATE / c['project']
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    state = {'id': os.environ.get('INVOCATION_ID') or uuid.uuid4().hex,
             'time': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'stage': '更新前检查', 'rollback': '未执行'}
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(lock(directory / 'update.lock'))
            save(directory / 'run.json', state)
            current_lock = cookie_lock()
            if current_lock:
                stack.enter_context(lock(current_lock))
            services = model(c)
            old = snapshot(c, services)
            save(directory / 'previous-images.json', old)
            # Protect images against ordinary dangling-image cleanup.
            for n, image in old.items():
                run(['docker', 'image', 'tag', image, f"rsshub-auto-update-backup/{c['project']}/{n}:previous"])
            state['stage'] = '拉取镜像'
            save(directory / 'run.json', state)
            compose(c, 'pull', *services)
            changed = []
            for n, s in services.items():
                image = run(['docker', 'image', 'inspect', s['image'], '--format', '{{.Id}}']).strip()
                if image != old[n]:
                    changed.append(n)
                    print(f'{n}: {old[n]} -> {image}', flush=True)
            if not changed:
                print('没有新镜像，不重启', flush=True)
                return 0
            state['stage'] = '更新服务'
            save(directory / 'run.json', state)
            override = directory / 'rollback.json'
            save(override, {'services': {n: {'image': image, 'pull_policy': 'never'} for n, image in old.items()}})
            try:
                compose(c, 'up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '300', *services)
            except Exception:
                state['rollback'] = '进行中'
                save(directory / 'run.json', state)
                try:
                    compose(c, 'up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '300', *services, override=override)
                    state['rollback'] = '成功（仅镜像）'
                except Exception:
                    state['rollback'] = '失败，需人工恢复'
                raise Failure('更新失败') from None
            print('整套服务更新成功', flush=True)
            return 0
    except BlockingIOError:
        print('操作锁占用，跳过本次更新', flush=True)
        return 0
    except Exception:
        save(directory / 'run.json', state)
        print(f"失败阶段：{state['stage']}；回退：{state['rollback']}", flush=True)
        notify(c, state, directory)
        return 1

def fail_notify(project):
    c = read(BASE / (project + '.json'))
    d = STATE / project
    invocation = run(['systemctl', 'show', f'rsshub-auto-update@{project}.service', '-p', 'InvocationID', '--value']).strip()
    state = read(d / 'run.json') if (d / 'run.json').exists() else {}
    if not state or (invocation and state['id'] != invocation):
        state = {'id': invocation or uuid.uuid4().hex, 'time': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'stage': '任务异常退出', 'rollback': '未知，检查服务状态'}
    notify(c, state, d)

def unit_text(project, hour, zone):
    return f'''[Unit]
Description=Daily Compose update for {project}
[Timer]
OnCalendar=*-*-* {hour}:00 {zone}
Persistent=false
AccuracySec=1min
Unit=rsshub-auto-update@{project}.service
[Install]
WantedBy=timers.target
'''

def install(args):
    if sys.version_info < (3, 9):
        raise Failure('需要 Python 3.9+')
    if sys.platform != 'linux' or not Path('/run/systemd/system').exists():
        raise Failure('需要运行 systemd 的 Linux')
    for cmd in ('docker', 'curl', 'flock', 'systemctl', 'systemd-analyze'):
        if not shutil.which(cmd):
            raise Failure('缺少依赖：' + cmd)
    help_text = run(['docker', 'compose', 'up', '--help'])
    if '--wait-timeout' not in help_text or '--pull' not in help_text:
        raise Failure('需要支持 --wait-timeout 的 Docker Compose v2+')
    ids = run(['docker', 'ps', '-q']).split()
    objects = json.loads(run(['docker', 'inspect', *ids])) if ids else []
    candidates = discover(objects)
    if args.compose_file:
        files = [str(Path(p).resolve(strict=True)) for p in args.compose_file]
        if not args.project:
            raise Failure('显式配置文件必须同时指定 --project')
        c = {'project': args.project, 'files': files, 'directory': str(Path(files[0]).parent)}
    else:
        candidates = [c for c in candidates if not args.project or c['project'] == args.project]
        if len(candidates) != 1:
            print(json.dumps(candidates, ensure_ascii=False, indent=2))
            raise Failure('请使用 --project NAME --compose-file /absolute/compose.yml 明确目标')
        c = candidates[0]
    project = c['project']
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', project):
        raise Failure('项目名称不符合要求')
    existing = read(BASE / (project + '.json')) if (BASE / (project + '.json')).exists() else {}
    hour = args.time or existing.get('time', '04:00')
    zone = args.timezone or existing.get('timezone', 'Asia/Shanghai')
    if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', hour):
        raise Failure('时间必须是 HH:MM')
    if not re.fullmatch(r'[A-Za-z0-9_+/-]+', zone) or '..' in zone or not (Path('/usr/share/zoneinfo') / zone).is_file():
        raise Failure('无效时区')
    run(['systemd-analyze', 'calendar', f'*-*-* {hour}:00 {zone}'])
    cookie_lock()
    # Reject interpolation that differs from the running deployment (including secrets).
    config = model(c)
    snapshot(c, config)
    for obj in objects:
        labels = obj['Config'].get('Labels') or {}
        if labels.get('com.docker.compose.project') != project:
            continue
        service = labels.get('com.docker.compose.service')
        if service not in config:
            continue
        expected_hash = compose(c, 'config', '--hash', service).strip().split()[-1]
        if labels.get('com.docker.compose.config-hash') != expected_hash:
            raise Failure('Compose 配置与运行容器不同；请先用原配置重新部署再安装')
        env = dict(x.split('=', 1) for x in obj['Config'].get('Env', []) if '=' in x)
        for k, v in config[service].get('environment', {}).items():
            if v is not None and str(v) != env.get(k):
                raise Failure('Compose 环境与运行容器不同；请先还原原部署环境')
    if args.bark_url_file:
        url = Path(args.bark_url_file).read_text().strip()
    elif existing.get('bark_url'):
        url = existing['bark_url']
    else:
        try:
            with open('/dev/tty', 'r+') as tty:
                url = getpass.getpass('Bark HTTPS 地址（隐藏输入）：', stream=tty)
        except (OSError, EOFError):
            raise Failure('无终端，请提供 --bark-url-file') from None
    c.update(time=hour, timezone=zone, bark_url=bark_url(url))
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    LIB.mkdir(mode=0o755, parents=True, exist_ok=True)
    source = Path(__file__).resolve()
    if source != LIB / 'updater.py':
        shutil.copyfile(source, LIB / 'updater.py')
    os.chmod(LIB / 'updater.py', 0o755)
    save(BASE / (project + '.json'), c)
    (UNITS / 'rsshub-auto-update@.service').write_text('''[Unit]
Description=RSSHub Compose automatic update (%i)
Wants=network-online.target
After=network-online.target docker.service
OnFailure=rsshub-auto-update-failure@%i.service
[Service]
Type=oneshot
UMask=0077
ExecStart=/usr/bin/python3 /usr/local/lib/rsshub-auto-update/updater.py update %i
TimeoutStartSec=30min
''')
    (UNITS / 'rsshub-auto-update-failure@.service').write_text('''[Unit]
Description=RSSHub update failure notification (%i)
[Service]
Type=oneshot
UMask=0077
ExecStart=/usr/bin/python3 /usr/local/lib/rsshub-auto-update/updater.py failure %i
TimeoutStartSec=60
''')
    timer = UNITS / f'rsshub-auto-update@{project}.timer'
    timer.write_text(unit_text(project, hour, zone))
    launcher = Path('/usr/local/bin/rsshub-auto-update')
    launcher.write_text('#!/bin/sh\nexec /usr/bin/python3 /usr/local/lib/rsshub-auto-update/updater.py "$@"\n')
    launcher.chmod(0o755)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', timer.name])
    run(['systemctl', 'restart', timer.name])
    print(f'已安装 {project}：每天 {hour} {zone}；未执行更新或发送通知')

def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    i = sub.add_parser('install')
    for name in ('project', 'time', 'timezone', 'bark-url-file'):
        i.add_argument('--' + name)
    i.add_argument('--compose-file', action='append')
    for name in ('update', 'run', 'status', 'logs', 'test-notify', 'failure', 'uninstall'):
        sub.add_parser(name).add_argument('project')
    args = p.parse_args()
    if os.geteuid() != 0:
        raise Failure('请使用 root 或 sudo')
    if args.command == 'install':
        install(args)
        return 0
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', args.project):
        raise Failure('无效项目名')
    c = read(BASE / (args.project + '.json'))
    unit = f'rsshub-auto-update@{args.project}'
    if args.command == 'update':
        return update(c)
    if args.command == 'failure':
        fail_notify(args.project)
    elif args.command == 'test-notify':
        return 0 if send(c, {'stage': '测试通知', 'time': datetime.datetime.now(datetime.timezone.utc).isoformat()}) else 1
    elif args.command == 'run':
        print(run(['systemctl', 'start', unit + '.service']))
    elif args.command == 'status':
        subprocess.run(['systemctl', 'status', unit + '.timer', unit + '.service'], check=False)
    elif args.command == 'logs':
        subprocess.run(['journalctl', '-u', unit + '.service', '-u', f'rsshub-auto-update-failure@{args.project}.service', '-n', '100', '--no-pager'], check=False)
    elif args.command == 'uninstall':
        run(['systemctl', 'disable', '--now', unit + '.timer'])
        run(['systemctl', 'stop', unit + '.service', f'rsshub-auto-update-failure@{args.project}.service'])
        (UNITS / (unit + '.timer')).unlink(missing_ok=True)
        (BASE / (args.project + '.json')).unlink()
        shutil.rmtree(STATE / args.project, ignore_errors=True)
        if not list(BASE.glob('*.json')):
            for name in ('rsshub-auto-update@.service', 'rsshub-auto-update-failure@.service'):
                (UNITS / name).unlink(missing_ok=True)
            Path('/usr/local/bin/rsshub-auto-update').unlink(missing_ok=True)
            shutil.rmtree(LIB)
        run(['systemctl', 'daemon-reload'])
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except Failure as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    except Exception:
        print('操作失败；敏感详情已隐藏，请检查依赖与配置', file=sys.stderr)
        sys.exit(1)
