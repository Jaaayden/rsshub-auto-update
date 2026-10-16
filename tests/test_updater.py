import contextlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import updater as u

class Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.c = {'project': 'test', 'directory': '/tmp', 'files': ['/tmp/a.yml', '/tmp/b.yml'], 'bark_url': 'https://example.invalid/test-key'}

    def test_discovery_dedup_and_ambiguity(self):
        def obj(p):
            return {'Config': {'Image': 'diygod/rsshub', 'Labels': {'com.docker.compose.project': p, 'com.docker.compose.project.config_files': '/a,/b', 'com.docker.compose.project.working_dir': '/'}}}
        self.assertEqual(len(u.discover([obj('a'), obj('a')])), 1)
        self.assertEqual(len(u.discover([obj('a'), obj('b')])), 2)
        self.assertEqual(u.discover([obj('a')])[0]['files'], ['/a', '/b'])

    def test_compose_order(self):
        with patch.object(u, 'run', return_value='') as r:
            u.compose(self.c, 'pull', override='/c')
        self.assertEqual(r.call_args.args[0][-7:], ['-f', '/tmp/a.yml', '-f', '/tmp/b.yml', '-f', '/c', 'pull'])

    def test_model_profiles_health_build(self):
        services = {'rsshub': {'image': 'x', 'healthcheck': {'test': ['CMD', 'true']}}, 'optional': {'build': '.', 'profiles': ['extra']}}
        with patch.object(u, 'compose', return_value=json.dumps({'services': services})):
            self.assertEqual(list(u.model(self.c)), ['rsshub'])
        for service in ({'image': 'x'}, {'image': 'x', 'build': '.'}):
            with patch.object(u, 'compose', return_value=json.dumps({'services': {'rsshub': service}})):
                with self.assertRaises(u.Failure):
                    u.model(self.c)

    def scenario(self, changed=True, pull_fail=False, up_fail=0, backup=None, rm_fails=False):
        services = {n: {'image': n + ':latest'} for n in ('rsshub', 'redis', 'browserless')}
        old = {n: 'sha256:old' for n in services}
        calls = []
        def compose(c, *args, **kw):
            calls.append((args, kw))
            if args[0] == 'pull' and pull_fail:
                raise u.Failure('secret-error')
            if args[0] == 'up' and len([x for x in calls if x[0][0] == 'up']) <= up_fail:
                raise u.Failure('secret-error')
            return ''
        self.run_calls = []
        def run(args, **kw):
            self.run_calls.append(args)
            if args[:3] == ['docker', 'image', 'inspect'] and args[3].startswith(u.BACKUP_PREFIX):
                if backup is None:
                    raise u.Failure('no backup tag')
                return backup
            if args[:3] == ['docker', 'image', 'rm'] and rm_fails:
                raise u.Failure('in use')
            return 'sha256:new' if changed else 'sha256:old'
        with patch.object(u, 'STATE', self.root), patch.object(u, 'cookie_lock', return_value=None), patch.object(u, 'model', return_value=services), patch.object(u, 'snapshot', return_value=old), patch.object(u, 'compose', side_effect=compose), patch.object(u, 'run', side_effect=run), patch.object(u, 'send', return_value=True) as send, contextlib.redirect_stdout(io.StringIO()) as output:
            result = u.update(self.c)
        self.assertNotIn('secret-error', output.getvalue())
        return result, calls, send

    def test_no_change(self):
        result, calls, send = self.scenario(changed=False)
        self.assertEqual(result, 0)
        self.assertEqual([x[0][0] for x in calls], ['pull'])
        send.assert_not_called()

    def test_pull_failure_no_recreate(self):
        result, calls, send = self.scenario(pull_fail=True)
        self.assertEqual(result, 1)
        self.assertEqual(len(calls), 1)
        send.assert_called_once()

    def test_whole_project_update(self):
        result, calls, send = self.scenario()
        self.assertEqual(result, 0)
        self.assertEqual(calls[1][0][-3:], ('rsshub', 'redis', 'browserless'))
        send.assert_not_called()

    def test_rollback(self):
        for failures, expected in ((1, '成功'), (2, '失败')):
            with self.subTest(failures=failures):
                result, calls, send = self.scenario(up_fail=failures)
                self.assertEqual(result, 1)
                self.assertIn('override', calls[-1][1])
                self.assertIn(expected, send.call_args.args[1]['rollback'])
                self.assertEqual(len(u.read(self.root / 'test/rollback.json')['services']), 3)

    def test_lock_skip(self):
        with u.lock(self.root / 'test/update.lock'), patch.object(u, 'STATE', self.root), patch.object(u, 'send') as send:
            self.assertEqual(u.update(self.c), 0)
        send.assert_not_called()

    def test_notify_dedup(self):
        state = {'id': 'one', 'stage': 'test', 'time': 'now'}
        with patch.object(u, 'send') as send:
            u.notify(self.c, state, self.root)
            u.notify(self.c, state, self.root)
        send.assert_called_once()

    def test_bark_post_retry_and_redaction(self):
        opener = unittest.mock.MagicMock()
        opener.open.side_effect = RuntimeError('sensitive-url')
        with patch.object(u.urllib.request, 'build_opener', return_value=opener), patch.object(u.time, 'sleep'), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertFalse(u.send(self.c, {'stage': '失败', 'time': 'now'}))
        self.assertEqual(opener.open.call_count, 3)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, 'POST')
        self.assertIn('失败', json.loads(request.data)['body'])
        self.assertNotIn('sensitive-url', out.getvalue())
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 10)

    def test_bark_validation(self):
        for value in ('http://example.org/key', 'https://user:pass@example.org/key', 'https://example.org/', 'https://example.org/key?secret=x'):
            with self.assertRaises(u.Failure):
                u.bark_url(value)

    def test_permissions_and_cookie(self):
        path = self.root / 'config.json'
        u.save(path, {'deployment': {'lock_file': '/var/lib/example/lock'}})
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(u.cookie_lock(path), '/var/lib/example/lock')
        u.save(path, {})
        with self.assertRaises(u.Failure):
            u.cookie_lock(path)

    def test_hash_mismatch_is_diagnostic(self):
        obj = {'Image': 'sha256:old', 'Config': {'Env': ['PATH=/bin', 'COOKIE=private-value'], 'Labels': {
            'com.docker.compose.project': 'test', 'com.docker.compose.service': 'rsshub',
            'com.docker.compose.config-hash': 'legacy'}}}
        config = {'rsshub': {'environment': {'COOKIE': 'private-value'}}}
        with patch.object(u, 'run', return_value=json.dumps([{'Config': {'Env': ['PATH=/bin']}}])), patch.object(u, 'compose', return_value='rsshub current'), contextlib.redirect_stdout(io.StringIO()) as out:
            u.validate_running_config(self.c, config, [obj])
        self.assertIn('环境检查已通过', out.getvalue())
        self.assertNotIn('private-value', out.getvalue())

    def test_environment_drift_still_blocks(self):
        obj = {'Image': 'sha256:old', 'Config': {'Env': ['PATH=/bin', 'COOKIE=private-value'], 'Labels': {
            'com.docker.compose.project': 'test', 'com.docker.compose.service': 'rsshub'}}}
        for env in ({}, {'COOKIE': 'different-value'}, {'COOKIE': None}):
            with self.subTest(env=env), patch.object(u, 'run', return_value=json.dumps([{'Config': {'Env': ['PATH=/bin']}}])):
                with self.assertRaises(u.Failure) as error:
                    u.validate_running_config(self.c, {'rsshub': {'environment': env}}, [obj])
                self.assertIn('COOKIE', str(error.exception))
                self.assertNotIn('private-value', str(error.exception))
                self.assertNotIn('different-value', str(error.exception))

    def test_timer(self):
        text = u.unit_text('test', '04:00', 'Asia/Shanghai')
        self.assertIn('04:00:00 Asia/Shanghai', text)
        self.assertIn('Persistent=false', text)

    # --- diagnostics, environment and timeouts -------------------------------------------------
    def test_run_environment_and_redacted_diagnostics(self):
        done = subprocess.CompletedProcess(['x'], 3, '', 'boom\npull https://user:pw@registry.example/v2 denied\n')
        env = {'PATH': '/bin', 'COMPOSE_PROJECT_NAME': 'x', 'SECRET_TOKEN': 's', 'DOCKER_CONFIG': '/d'}
        with patch.dict(os.environ, env, clear=True), patch.object(u.subprocess, 'run', return_value=done) as r, \
                contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(u.Failure) as error:
                u.run(['docker', 'compose', 'x'], label='docker compose pull')
        passed = r.call_args.kwargs['env']
        self.assertEqual(passed['DOCKER_CONFIG'], '/d')
        self.assertIn('HOME', passed)
        self.assertNotIn('SECRET_TOKEN', passed)
        self.assertNotIn('COMPOSE_PROJECT_NAME', passed)
        self.assertIn('docker compose pull', str(error.exception))
        self.assertIn('退出码 3', err.getvalue())
        self.assertIn('registry.example', err.getvalue())
        self.assertNotIn('pw', err.getvalue())

    def test_run_quiet_and_timeout(self):
        done = subprocess.CompletedProcess(['x'], 1, '', 'noise')
        with patch.object(u.subprocess, 'run', return_value=done), contextlib.redirect_stderr(io.StringIO()) as err:
            with self.assertRaises(u.Failure):
                u.run(['docker', 'image', 'inspect', 'x'], quiet=True)
        self.assertEqual(err.getvalue(), '')
        with patch.object(u.subprocess, 'run', side_effect=subprocess.TimeoutExpired('x', 1)), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(u.Failure) as error:
                u.run(['docker', 'compose', 'x'], timeout=1, label='docker compose pull')
        self.assertIn('超时', str(error.exception))

    def test_compose_label_and_pull_timeout(self):
        with patch.object(u, 'run', return_value='') as r:
            u.compose(self.c, 'pull', timeout=u.PULL_TIMEOUT)
        self.assertEqual(r.call_args.kwargs['label'], 'docker compose pull')
        self.assertEqual(r.call_args.kwargs['timeout'], u.PULL_TIMEOUT)
        result, calls, _ = self.scenario(changed=False)
        self.assertEqual(calls[0][1]['timeout'], u.PULL_TIMEOUT)

    # --- notifications ------------------------------------------------------------------------
    def test_bark_retry_backoff(self):
        opener = unittest.mock.MagicMock()
        opener.open.side_effect = RuntimeError('down')
        with patch.object(u.urllib.request, 'build_opener', return_value=opener), patch.object(u.time, 'sleep') as sleep, \
                contextlib.redirect_stdout(io.StringIO()):
            u.send(self.c, {'stage': '失败', 'time': 'now'})
        self.assertEqual([x.args[0] for x in sleep.call_args_list], [5, 20])

    def test_failed_delivery_releases_claim(self):
        state = {'id': 'run', 'stage': 'x', 'time': 'now'}
        with patch.object(u, 'send', side_effect=[False, True]) as send:
            u.notify(self.c, state, self.root)       # Bark down: claim released
            self.assertFalse((self.root / 'notified-run').exists())
            u.notify(self.c, state, self.root)       # OnFailure fallback delivers
            u.notify(self.c, state, self.root)       # now deduplicated
        self.assertEqual(send.call_count, 2)

    def test_markers_are_pruned(self):
        for index in range(25):
            marker = self.root / f'notified-old{index}'
            marker.touch()
            os.utime(marker, (1000 + index, 1000 + index))
        with patch.object(u, 'send', return_value=True):
            u.notify(self.c, {'id': 'fresh', 'stage': 'x', 'time': 'now'}, self.root)
        names = {p.name for p in self.root.glob('notified-*')}
        self.assertEqual(len(names), u.KEEP_MARKERS)
        self.assertIn('notified-fresh', names)
        self.assertIn('notified-old24', names)
        self.assertNotIn('notified-old0', names)

    def test_warning_title(self):
        opener = unittest.mock.MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = b'{"code": 200}'
        with patch.object(u.urllib.request, 'build_opener', return_value=opener):
            self.assertTrue(u.send(self.c, {'stage': 's', 'time': 'now', 'warning': True}))
        self.assertEqual(json.loads(opener.open.call_args.args[0].data)['title'], 'RSSHub 自动更新警告')

    # --- repeated lock skips ------------------------------------------------------------------
    def test_skip_alert_schedule(self):
        due = [n for n in range(1, 25) if u.skip_alert_due(n)]
        self.assertEqual(due, [3, 10, 17, 24])

    def test_persistent_lock_skip_warns_once_and_resets(self):
        with patch.object(u, 'STATE', self.root), patch.object(u, 'cookie_lock', return_value=None), patch.object(u, 'send', return_value=True) as send, \
                contextlib.redirect_stdout(io.StringIO()):
            with u.lock(self.root / 'test/update.lock'):
                for _ in range(2):
                    self.assertEqual(u.update(self.c), 0)
                send.assert_not_called()
                self.assertEqual(u.update(self.c), 0)
                self.assertEqual(send.call_count, 1)
                state = send.call_args.args[1]
                self.assertTrue(state['warning'])
                self.assertIn('3', state['stage'])
                self.assertEqual(u.update(self.c), 0)
                self.assertEqual(send.call_count, 1)
        self.assertTrue((self.root / 'test/skipped.json').exists())
        self.scenario(changed=False)
        self.assertFalse((self.root / 'test/skipped.json').exists())

    # --- backup tags and image pruning ----------------------------------------------------------
    def test_backup_tag_is_valid_docker_name(self):
        part = r'[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*'
        self.assertEqual(u.backup_tag('rsshub', 'redis'), 'rsshub-auto-update-backup/rsshub/redis:previous')
        self.assertEqual(u.backup_tag('my_proj', 'a.b-c'), 'rsshub-auto-update-backup/my_proj/a.b-c:previous')
        for service in ('Web', '_x', 'x_', 'A..B', '-'):
            tag = u.backup_tag('p', service)
            self.assertRegex(tag, rf'^{re.escape(u.BACKUP_PREFIX)}/p/{part}:previous$')
        self.assertNotEqual(u.backup_tag('p', 'Web'), u.backup_tag('p', 'web'))

    def test_displaced_backup_removed_after_success(self):
        result, _, _ = self.scenario(backup='sha256:older')
        self.assertEqual(result, 0)
        self.assertIn(['docker', 'image', 'rm', 'sha256:older'], self.run_calls)
        self.assertEqual([x for x in self.run_calls if x[:3] == ['docker', 'image', 'rm']], [['docker', 'image', 'rm', 'sha256:older']])
        self.assertFalse((self.root / 'test/stale-images.json').exists())

    def test_no_change_also_prunes(self):
        self.scenario(changed=False, backup='sha256:older')
        self.assertIn(['docker', 'image', 'rm', 'sha256:older'], self.run_calls)

    def test_running_images_are_never_pruned(self):
        self.scenario(backup='sha256:old')   # displaced backup equals the image that was running
        self.assertEqual([x for x in self.run_calls if x[:3] == ['docker', 'image', 'rm']], [])

    def test_failed_update_keeps_candidates_for_later(self):
        result, _, _ = self.scenario(up_fail=2, backup='sha256:older')
        self.assertEqual(result, 1)
        self.assertEqual([x for x in self.run_calls if x[:3] == ['docker', 'image', 'rm']], [])
        self.assertEqual(u.load_stale(self.root / 'test'), {'sha256:older'})

    def test_prune_failure_does_not_fail_update(self):
        result, _, send = self.scenario(backup='sha256:older', rm_fails=True)
        self.assertEqual(result, 0)
        send.assert_not_called()

    def test_prune_can_be_disabled(self):
        self.c['prune'] = False
        result, _, _ = self.scenario(backup='sha256:older')
        self.assertEqual(result, 0)
        self.assertEqual([x for x in self.run_calls if x[:3] in (['docker', 'image', 'rm'],)], [])
        self.assertFalse((self.root / 'test/stale-images.json').exists())

    # --- installation artifacts -----------------------------------------------------------------
    def test_units_use_installer_interpreter(self):
        text = u.service_text('/opt/py/bin/python3')
        self.assertIn('ExecStart=/opt/py/bin/python3 /usr/local/lib/rsshub-auto-update/updater.py update %i', text)
        self.assertIn('TimeoutStartSec=45min', text)
        self.assertIn('/opt/py/bin/python3 /usr/local/lib/rsshub-auto-update/updater.py failure %i', u.failure_service_text('/opt/py/bin/python3'))
        self.assertIn('exec /opt/py/bin/python3 /usr/local/lib/rsshub-auto-update/updater.py "$@"', u.launcher_text('/opt/py/bin/python3'))
        self.assertIn('RandomizedDelaySec=5min', u.unit_text('test', '04:00', 'Asia/Shanghai'))

    def test_interpreter_must_be_absolute_and_simple(self):
        with patch.object(u.sys, 'executable', '/bin/sh'):
            self.assertEqual(u.interpreter(), '/bin/sh')
        for value in ('python3', '/does/not/exist', '', None):
            with self.subTest(value=value), patch.object(u.sys, 'executable', value):
                with self.assertRaises(u.Failure):
                    u.interpreter()

    def test_write_atomic_mode_ignores_umask(self):
        target = self.root / 'unit.service'
        old = os.umask(0o077)
        try:
            u.write_atomic(target, b'one', 0o644)
            u.write_atomic(target, b'two', 0o644)
        finally:
            os.umask(old)
        self.assertEqual(target.read_bytes(), b'two')
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertEqual([p.name for p in self.root.iterdir()], ['unit.service'])

    def test_prune_option_parsing(self):
        parse = u.build_parser().parse_args
        self.assertIsNone(parse(['install']).prune)
        self.assertFalse(parse(['install', '--no-prune']).prune)
        self.assertTrue(parse(['install', '--prune']).prune)

    # --- uninstall ------------------------------------------------------------------------------
    def layout(self):
        paths = {name: self.root / name for name in ('units', 'base', 'state', 'lib', 'bin')}
        for path in paths.values():
            path.mkdir(exist_ok=True)
        files = [paths['units'] / n for n in ('rsshub-auto-update@test.timer', 'rsshub-auto-update@.service', 'rsshub-auto-update-failure@.service')]
        files += [paths['base'] / 'test.json', paths['lib'] / 'updater.py', paths['bin'] / 'launcher']
        for file in files:
            file.write_text('x')
        (paths['state'] / 'test').mkdir(exist_ok=True)
        return paths, files

    def uninstall_patches(self, paths):
        return (patch.object(u, 'UNITS', paths['units']), patch.object(u, 'BASE', paths['base']), patch.object(u, 'STATE', paths['state']),
                patch.object(u, 'LIB', paths['lib']), patch.object(u, 'LAUNCHER', paths['bin'] / 'launcher'), patch.object(u, 'best_effort'))

    def test_uninstall_is_idempotent_and_cleans_shared_files(self):
        paths, files = self.layout()
        with contextlib.ExitStack() as stack:
            for p in self.uninstall_patches(paths):
                stack.enter_context(p)
            with patch.object(u, 'run', return_value='inactive\n') as r, contextlib.redirect_stdout(io.StringIO()):
                u.uninstall('test')
                u.uninstall('test')          # leftovers already gone: must not raise
        self.assertEqual([f for f in files if f.exists()], [])
        self.assertFalse((paths['state'] / 'test').exists())
        self.assertFalse(paths['lib'].exists())
        self.assertIn(['systemctl', 'daemon-reload'], [x.args[0] for x in r.call_args_list])

    def test_uninstall_keeps_shared_files_for_other_projects(self):
        paths, files = self.layout()
        (paths['base'] / 'other.json').write_text('x')
        with contextlib.ExitStack() as stack:
            for p in self.uninstall_patches(paths):
                stack.enter_context(p)
            with patch.object(u, 'run', return_value='inactive\n'), contextlib.redirect_stdout(io.StringIO()):
                u.uninstall('test')
        self.assertTrue((paths['lib'] / 'updater.py').exists())
        self.assertTrue((paths['units'] / 'rsshub-auto-update@.service').exists())

    def test_uninstall_refuses_while_update_runs(self):
        for activity in ('activating', 'active', 'deactivating'):
            paths, files = self.layout()
            with self.subTest(activity=activity), contextlib.ExitStack() as stack:
                for p in self.uninstall_patches(paths):
                    stack.enter_context(p)
                with patch.object(u, 'run', return_value=activity + '\n'):
                    with self.assertRaises(u.Failure):
                        u.uninstall('test')
                self.assertTrue(all(f.exists() for f in files))

if __name__ == '__main__':
    unittest.main()
