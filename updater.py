#!/usr/bin/env python3
"""RSSHub Compose updater. No third-party Python dependencies."""
import argparse
import contextlib
import datetime
import fcntl
import getpass
import hashlib
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
import warnings

BASE = Path('/etc/rsshub-auto-update')
STATE = Path('/var/lib/rsshub-auto-update')
LIB = Path('/usr/local/lib/rsshub-auto-update')
UNITS = Path('/etc/systemd/system')
LAUNCHER = Path('/usr/local/bin/rsshub-auto-update')

BACKUP_PREFIX = 'rsshub-auto-update-backup'
# Only these variables reach docker/compose; arbitrary values would leak into interpolation.
ENV_KEEP = ('PATH', 'HOME', 'LANG', 'DOCKER_HOST', 'DOCKER_CONTEXT',
            'DOCKER_CONFIG', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH')
PULL_TIMEOUT = 1200      # large images (Chromium) on slow links
SKIP_ALERT_AFTER = 3     # consecutive lock skips before a warning is sent
SKIP_ALERT_REPEAT = 7    # ... and then once per this many further skips
KEEP_MARKERS = 20        # notified-* markers kept per project
BARK_RETRY_DELAYS = (5, 20)

class Failure(Exception):
    pass

def redact(line):
    """Drop URL credentials (scheme://user:pass@host) from diagnostic text."""
    return re.sub(r'(?<=//)[^/@\s]+@', '', line)

def diagnose(label, code, stderr):
    """Write a short stderr tail to the local journal only (never to Bark)."""
    print(f'命令失败：{label}（退出码 {code}）', file=sys.stderr, flush=True)
    for line in (stderr or '').strip().splitlines()[-10:]:
        print('  ' + redact(line)[:300], file=sys.stderr, flush=True)

def run(args, cwd=None, timeout=600, label=None, quiet=False):
    env = {k: v for k, v in os.environ.items() if k in ENV_KEEP}
    env.setdefault('HOME', str(Path('~').expanduser()))
    label = label or ' '.join(args[:3])
    try:
        p = subprocess.run(args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        if not quiet:
            print(f'命令超时：{label}（{timeout} 秒）', file=sys.stderr, flush=True)
        raise Failure('命令超时：' + label) from None
    if p.returncode:
        if not quiet:
            diagnose(label, p.returncode, p.stderr)
        raise Failure('命令失败：' + label)
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

def write_atomic(path, data, mode):
    """Replace path in one step so a concurrent reader never sees a partial file."""
    path = Path(path)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)

def compose(c, *args, override=None, timeout=600):
    cmd = ['docker', 'compose', '--project-directory', c['directory'], '-p', c['project']]
    for file in c['files']:
        cmd += ['-f', file]
    if override:
        cmd += ['-f', str(override)]
    return run(cmd + list(args), cwd=c['directory'], timeout=timeout, label='docker compose ' + args[0])

def model(c):
    data = json.loads(compose(c, 'config', '--format', 'json'))
    services = {n: s for n, s in data['services'].items() if not s.get('profiles')}
    if not services:
        raise Failure('没有默认启用的服务')
    for name, svc in services.items():
        if svc.get('build') or not svc.get('image'):
            raise Failure('仅支持镜像部署：' + name)
        if svc.get('scale', 1) != 1 or svc.get('deploy', {}).get('replicas', 1) != 1:
            raise Failure('首版要求每个服务一个容器：' + name)
        identity = (name + ' ' + svc['image']).lower()
        if any(x in identity for x in ('rsshub', 'redis', 'browserless')):
            h = svc.get('healthcheck', {})
            if h.get('disable') or not h.get('test') or h['test'][0] == 'NONE':
                raise Failure('服务缺少健康检查：' + name)
    return services

def snapshot(c, services):
    result = {}
    for name in services:
        ids = compose(c, 'ps', '-a', '-q', name).split()
        if len(ids) != 1:
            raise Failure('服务必须有一个运行容器：' + name)
        obj = json.loads(run(['docker', 'inspect', ids[0]]))[0]
        state = obj['State']
        if not state.get('Running') or state.get('Health', {}).get('Status', 'healthy') != 'healthy':
            raise Failure('服务未就绪：' + name)
        result[name] = obj['Image']
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
    if state['stage'] == '测试通知':
        title = 'RSSHub 测试通知'
    elif state.get('warning'):
        title = 'RSSHub 自动更新警告'
    else:
        title = 'RSSHub 自动更新失败'
    data = json.dumps({'title': title, 'body': body, 'group': 'rsshub-auto-update'}, ensure_ascii=False).encode()
    request = urllib.request.Request(bark_url(c['bark_url']), data=data, headers={'Content-Type': 'application/json'}, method='POST')
    for attempt in range(len(BARK_RETRY_DELAYS) + 1):
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=10) as response:
                if json.loads(response.read(65536)).get('code') != 200:
                    raise Failure('Bark 拒绝请求')
            return True
        except Exception:
            if attempt < len(BARK_RETRY_DELAYS):
                time.sleep(BARK_RETRY_DELAYS[attempt])
    print('Bark 通知发送失败（敏感详情已隐藏）', flush=True)
    return False

def prune_markers(directory, keep=KEEP_MARKERS):
    markers = sorted(directory.glob('notified-*'), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in markers[keep:]:
        old.unlink(missing_ok=True)

def notify(c, state, directory):
    with lock(directory / 'notify.lock'):
        marker = directory / ('notified-' + state['id'])
        if marker.exists():
            return
        # Claim before network I/O: service and OnFailure cannot duplicate a run.
        marker.touch(mode=0o600)
        prune_markers(directory)
        if not send(c, state):
            # Release the claim so the OnFailure unit gets one more delivery attempt.
            marker.unlink(missing_ok=True)

def docker_name(value):
    """Map an arbitrary Compose name to a valid Docker repository path component."""
    if re.fullmatch(r'[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*', value):
        return value
    clean = re.sub(r'[^a-z0-9]+', '-', value.lower()).strip('-') or 'service'
    return clean + '-' + hashlib.sha1(value.encode()).hexdigest()[:6]

def backup_tag(project, service):
    return f'{BACKUP_PREFIX}/{docker_name(project)}/{docker_name(service)}:previous'

def backup_ids(c, services):
    """Image IDs currently held by the backup tags; they are displaced by the next re-tag."""
    found = set()
    for name in services:
        try:
            found.add(run(['docker', 'image', 'inspect', backup_tag(c['project'], name), '--format', '{{.Id}}'], quiet=True).strip())
        except Failure:
            pass
    return found

def load_stale(directory):
    try:
        return {x for x in read(directory / 'stale-images.json') if isinstance(x, str)}
    except Exception:
        return set()

def remember_images(c, directory, services, old):
    """Persist rollback data and protect the running images with backup tags."""
    save(directory / 'previous-images.json', old)
    stale = set()
    if c.get('prune', True):
        # Images that stop being the backup after the re-tag below are cleanup candidates.
        stale = load_stale(directory) | backup_ids(c, services)
        save(directory / 'stale-images.json', sorted(stale))
    for name, image in old.items():
        run(['docker', 'image', 'tag', image, backup_tag(c['project'], name)])
    return stale

def prune_images(directory, stale, protected):
    """Remove displaced backup images that have no repository tag left.

    A tag that still names the image (for example a pinned version) is the
    user's, so such images are kept. Docker also refuses in-use images.
    """
    for image in sorted(stale - protected):
        try:
            tags = json.loads(run(['docker', 'image', 'inspect', image, '--format', '{{json .RepoTags}}'], quiet=True) or 'null')
            if tags:
                continue
            run(['docker', 'image', 'rm', image], quiet=True)
            print(f'已清理旧镜像：{image[:19]}', flush=True)
        except (Failure, ValueError):
            pass
    (directory / 'stale-images.json').unlink(missing_ok=True)

def finish(c, directory, stale, old, current):
    if not c.get('prune', True):
        return
    try:
        prune_images(directory, stale, set(old.values()) | set(current.values()))
    except Exception:
        print('旧镜像清理失败（不影响更新结果）', flush=True)

def record_skip(directory):
    path = directory / 'skipped.json'
    try:
        count = int(read(path)['count'])
    except Exception:
        count = 0
    count += 1
    save(path, {'count': count})
    return count

def skip_alert_due(count):
    return count == SKIP_ALERT_AFTER or (count > SKIP_ALERT_AFTER and (count - SKIP_ALERT_AFTER) % SKIP_ALERT_REPEAT == 0)

def compare_images(services, old):
    current, changed = {}, []
    for name, svc in services.items():
        current[name] = run(['docker', 'image', 'inspect', svc['image'], '--format', '{{.Id}}']).strip()
        if current[name] != old[name]:
            changed.append(name)
            print(f'{name}: {old[name]} -> {current[name]}', flush=True)
    return current, changed

def apply_update(c, directory, state, services, old):
    override = directory / 'rollback.json'
    save(override, {'services': {n: {'image': image, 'pull_policy': 'never'} for n, image in old.items()}})
    up = ['up', '-d', '--pull', 'never', '--wait', '--wait-timeout', '300', *services]
    try:
        compose(c, *up)
    except Exception:
        state['rollback'] = '进行中'
        save(directory / 'run.json', state)
        try:
            compose(c, *up, override=override)
            state['rollback'] = '成功（仅镜像）'
        except Exception:
            state['rollback'] = '失败，需人工恢复'
        raise Failure('更新失败') from None

def update(c):
    directory = STATE / c['project']
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    invocation = os.environ.get('INVOCATION_ID') if os.environ.get('SYSTEMD_EXEC_PID') == str(os.getpid()) else None
    state = {'id': invocation or uuid.uuid4().hex,
             'time': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'stage': '更新前检查', 'rollback': '未执行'}
    try:
        with contextlib.ExitStack() as stack:
            stack.enter_context(lock(directory / 'update.lock'))
            save(directory / 'run.json', state)
            current_lock = cookie_lock()
            if current_lock:
                stack.enter_context(lock(current_lock))
            (directory / 'skipped.json').unlink(missing_ok=True)
            services = model(c)
            old = snapshot(c, services)
            stale = remember_images(c, directory, services, old)
            state['stage'] = '拉取镜像'
            save(directory / 'run.json', state)
            compose(c, 'pull', *services, timeout=PULL_TIMEOUT)
            current, changed = compare_images(services, old)
            if not changed:
                print('没有新镜像，不重启', flush=True)
                finish(c, directory, stale, old, current)
                return 0
            state['stage'] = '更新服务'
            save(directory / 'run.json', state)
            apply_update(c, directory, state, services, old)
            print('整套服务更新成功', flush=True)
            finish(c, directory, stale, old, current)
            return 0
    except BlockingIOError:
        count = record_skip(directory)
        print(f'操作锁占用，跳过本次更新（已连续跳过 {count} 次）', flush=True)
        if skip_alert_due(count):
            notify(c, {'id': state['id'] + '-skip', 'time': state['time'], 'warning': True, 'rollback': '未执行',
                       'stage': f'连续 {count} 次因操作锁被占用而跳过更新'}, directory)
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
RandomizedDelaySec=5min
Persistent=false
AccuracySec=1min
Unit=rsshub-auto-update@{project}.service
[Install]
WantedBy=timers.target
'''

def service_text(python):
    return f'''[Unit]
Description=RSSHub Compose automatic update (%i)
Wants=network-online.target
After=network-online.target docker.service
OnFailure=rsshub-auto-update-failure@%i.service
[Service]
Type=oneshot
UMask=0077
ExecStart={python} {LIB}/updater.py update %i
TimeoutStartSec=45min
'''

def failure_service_text(python):
    return f'''[Unit]
Description=RSSHub update failure notification (%i)
[Service]
Type=oneshot
UMask=0077
ExecStart={python} {LIB}/updater.py failure %i
TimeoutStartSec=5min
'''

def launcher_text(python):
    return f'#!/bin/sh\nexec {python} {LIB}/updater.py "$@"\n'

def interpreter():
    """The interpreter that ran the installer is the one the timer will use."""
    python = sys.executable
    if not re.fullmatch(r'/[A-Za-z0-9_./+-]+', python or '') or not os.access(python, os.X_OK):
        raise Failure('无法确定 Python 解释器的绝对路径')
    return python

def validate_running_config(c, config, objects):
    """Hashes are diagnostics, not portable proof of configuration drift.

    Compare effective environment to the running image defaults to detect lost
    shell interpolation and runtime overrides without exposing any values.
    """
    image_defaults = {}
    for obj in objects:
        labels = obj['Config'].get('Labels') or {}
        if labels.get('com.docker.compose.project') != c['project']:
            continue
        service = labels.get('com.docker.compose.service')
        if service not in config:
            continue
        image_id = obj['Image']
        if image_id not in image_defaults:
            image = json.loads(run(['docker', 'image', 'inspect', image_id]))[0]
            image_defaults[image_id] = dict(x.split('=', 1) for x in image.get('Config', {}).get('Env', []) or [] if '=' in x)
        expected_env = dict(image_defaults[image_id])
        for key, value in config[service].get('environment', {}).items():
            if value is None:
                expected_env.pop(key, None)
            else:
                expected_env[key] = str(value)
        actual_env = dict(x.split('=', 1) for x in obj['Config'].get('Env', []) or [] if '=' in x)
        different = sorted(k for k in expected_env.keys() | actual_env.keys()
                           if expected_env.get(k) != actual_env.get(k))
        if different:
            raise Failure(f"服务 {service} 的运行环境与 Compose/.env 不一致（字段：{', '.join(different)}）；请将启动时使用的变量还原到 .env 或 env_file")
        expected_hash = compose(c, 'config', '--hash', service).strip().split()[-1]
        if labels.get('com.docker.compose.config-hash') != expected_hash:
            print(f'提示：{service} 配置哈希不同，环境检查已通过；后续更新以当前 Compose 文件为准', flush=True)


def prompt_bark_url():
    try:
        # Text r+ streams require seeking; terminals cannot seek. getpass opens
        # its own unbuffered read/write tty, so this stream is only for output.
        with open('/dev/tty', 'w') as tty, warnings.catch_warnings():
            warnings.simplefilter('error', getpass.GetPassWarning)
            return getpass.getpass('Bark HTTPS 地址（隐藏输入）：', stream=tty)
    except (OSError, EOFError, getpass.GetPassWarning):
        raise Failure('无法安全读取终端；无人值守安装请提供 --bark-url-file') from None


def install(args):
    if sys.version_info < (3, 9):
        raise Failure('需要 Python 3.9+')
    if sys.platform != 'linux' or not Path('/run/systemd/system').exists():
        raise Failure('需要运行 systemd 的 Linux')
    for cmd in ('docker', 'curl', 'flock', 'systemctl', 'systemd-analyze'):
        if not shutil.which(cmd):
            raise Failure('缺少依赖：' + cmd)
    python = interpreter()
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
    prune = args.prune if args.prune is not None else existing.get('prune', True)
    if not re.fullmatch(r'(?:[01][0-9]|2[0-3]):[0-5][0-9]', hour):
        raise Failure('时间必须是 HH:MM')
    if not re.fullmatch(r'[A-Za-z0-9_+/-]+', zone) or '..' in zone or not (Path('/usr/share/zoneinfo') / zone).is_file():
        raise Failure('无效时区')
    run(['systemd-analyze', 'calendar', f'*-*-* {hour}:00 {zone}'])
    cookie_lock()
    # Reject interpolation that differs from the running deployment (including secrets).
    config = model(c)
    snapshot(c, config)
    validate_running_config(c, config, objects)
    if args.bark_url_file:
        url = Path(args.bark_url_file).read_text().strip()
    elif existing.get('bark_url'):
        url = existing['bark_url']
    else:
        url = prompt_bark_url()
    c.update(time=hour, timezone=zone, bark_url=bark_url(url), prune=prune)
    BASE.mkdir(mode=0o700, parents=True, exist_ok=True)
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    LIB.mkdir(mode=0o755, parents=True, exist_ok=True)
    LIB.chmod(0o755)  # the installer runs with umask 077
    source = Path(__file__).resolve()
    if source != LIB / 'updater.py':
        write_atomic(LIB / 'updater.py', source.read_bytes(), 0o755)
    os.chmod(LIB / 'updater.py', 0o755)
    save(BASE / (project + '.json'), c)
    write_atomic(UNITS / 'rsshub-auto-update@.service', service_text(python).encode(), 0o644)
    write_atomic(UNITS / 'rsshub-auto-update-failure@.service', failure_service_text(python).encode(), 0o644)
    timer = UNITS / f'rsshub-auto-update@{project}.timer'
    write_atomic(timer, unit_text(project, hour, zone).encode(), 0o644)
    write_atomic(LAUNCHER, launcher_text(python).encode(), 0o755)
    run(['systemctl', 'daemon-reload'])
    run(['systemctl', 'enable', '--now', timer.name])
    run(['systemctl', 'restart', timer.name])
    print(f'已安装 {project}：每天 {hour}（最多随机延后 5 分钟）{zone}；旧镜像清理{"开启" if prune else "关闭"}；未执行更新或发送通知')


def best_effort(args):
    subprocess.run(args, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def uninstall(project):
    unit = f'rsshub-auto-update@{project}'
    activity = run(['systemctl', 'show', unit + '.service', '-p', 'ActiveState', '--value']).strip()
    if activity in ('active', 'activating', 'reloading', 'deactivating'):
        raise Failure('更新正在运行；强行卸载会中断 compose up 且不会回退，请等待结束后再试')
    # Idempotent: every step tolerates leftovers from an earlier partial uninstall.
    best_effort(['systemctl', 'disable', '--now', unit + '.timer'])
    best_effort(['systemctl', 'stop', unit + '.service', f'rsshub-auto-update-failure@{project}.service'])
    (UNITS / (unit + '.timer')).unlink(missing_ok=True)
    (BASE / (project + '.json')).unlink(missing_ok=True)
    shutil.rmtree(STATE / project, ignore_errors=True)
    if not list(BASE.glob('*.json')):
        for name in ('rsshub-auto-update@.service', 'rsshub-auto-update-failure@.service'):
            (UNITS / name).unlink(missing_ok=True)
        LAUNCHER.unlink(missing_ok=True)
        shutil.rmtree(LIB, ignore_errors=True)
    run(['systemctl', 'daemon-reload'])
    print(f'已卸载 {project}；备份镜像标签 {BACKUP_PREFIX}/{docker_name(project)}/* 未删除，可用 docker image rm 手工清理')


def build_parser():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    i = sub.add_parser('install')
    for name in ('project', 'time', 'timezone', 'bark-url-file'):
        i.add_argument('--' + name)
    i.add_argument('--compose-file', action='append')
    i.add_argument('--prune', action=argparse.BooleanOptionalAction, help='成功更新后清理被替换的旧备份镜像（默认开启）')
    for name in ('update', 'run', 'status', 'logs', 'test-notify', 'failure', 'uninstall'):
        sub.add_parser(name).add_argument('project')
    return p


def main():
    args = build_parser().parse_args()
    if os.geteuid() != 0:
        raise Failure('请使用 root 或 sudo')
    if args.command == 'install':
        install(args)
        return 0
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', args.project):
        raise Failure('无效项目名')
    if args.command == 'uninstall':
        uninstall(args.project)
        return 0
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
