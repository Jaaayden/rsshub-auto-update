#!/usr/bin/env bash
set -euo pipefail
work=$(mktemp -d)
project="rsshub-update-ci-${RANDOM}"
cleanup() {
    docker compose -p "$project" -f "$work/compose.json" down -v --remove-orphans || true
    rm -rf "$work"
}
trap cleanup EXIT
# Build two deterministic local versions. Never contact a business deployment.
printf 'FROM alpine:3.20\nRUN echo old > /version\nCMD ["sleep", "infinity"]\n' > "$work/Dockerfile"
docker build -t "$project:old" "$work"
printf 'FROM alpine:3.20\nRUN echo new > /version\nCMD ["sleep", "infinity"]\n' > "$work/Dockerfile"
docker build -t "$project:new" "$work"
python3 - "$work" "$project" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1]); project = sys.argv[2]
services = {n: {'image': project + ':old', 'healthcheck': {'test': ['CMD', 'test', '-f', '/version'], 'interval': '1s', 'timeout': '1s', 'retries': 2}} for n in ('rsshub','redis','browserless')}
(p/'compose.json').write_text(json.dumps({'services': services}))
PY
docker compose -p "$project" -f "$work/compose.json" up -d --wait --wait-timeout 30
printf '%s\n' 'https://example.invalid/test-key' > "$work/bark-url"
sudo python3 updater.py install --project "$project" --compose-file "$work/compose.json" --bark-url-file "$work/bark-url"
sudo python3 updater.py install --project "$project" --compose-file "$work/compose.json"
sudo test "$(sudo stat -c '%a' "/etc/rsshub-auto-update/$project.json")" = 600
systemctl is-enabled "rsshub-auto-update@$project.timer"
sudo python3 updater.py uninstall "$project"
test "$(docker compose -p "$project" -f "$work/compose.json" ps -q | wc -l)" -eq 3
python3 - "$work" "$project" <<'PY'
import json, pathlib, sys
from unittest.mock import patch
import updater as u
p=pathlib.Path(sys.argv[1]); project=sys.argv[2]
c={'project': project, 'files':[str(p/'compose.json')], 'directory':str(p), 'bark_url':'https://example.invalid/key'}
old=u.snapshot(c,u.model(c))
original=u.compose
# Emulate registry moving a tag during pull; all inspect/up/health/rollback calls are real Docker.
def compose(c,*args,**kw):
    if args[0]=='pull':
        u.run(['docker','tag',project+':new',project+':old'])
        return ''
    return original(c,*args,**kw)
with patch.object(u,'STATE',p/'state'), patch.object(u,'cookie_lock',return_value=None), patch.object(u,'compose',side_effect=compose), patch.object(u,'send') as send:
    assert u.update(c)==0
    send.assert_not_called()
assert all(v != old[n] for n,v in u.snapshot(c,u.model(c)).items())
# A new image violates the unchanged healthcheck: updater must restore the old image.
u.run(['docker','tag',project+':old',project+':good'])
u.run(['docker','pull','alpine:3.20'])
def fail_compose(c,*args,**kw):
    if args[0]=='pull':
        u.run(['docker','tag','alpine:3.20',project+':old'])
        return ''
    return original(c,*args,**kw)
with patch.object(u,'STATE',p/'state'), patch.object(u,'cookie_lock',return_value=None), patch.object(u,'compose',side_effect=fail_compose), patch.object(u,'send') as send:
    assert u.update(c)==1
    assert '成功' in send.call_args.args[1]['rollback']
u.snapshot(c,u.model(c))
PY
