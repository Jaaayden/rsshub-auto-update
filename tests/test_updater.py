import contextlib
import io
import json
from pathlib import Path
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

    def scenario(self, changed=True, pull_fail=False, up_fail=0):
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
        def run(args, **kw):
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

    def test_timer(self):
        text = u.unit_text('test', '04:00', 'Asia/Shanghai')
        self.assertIn('04:00:00 Asia/Shanghai', text)
        self.assertIn('Persistent=false', text)

if __name__ == '__main__':
    unittest.main()
