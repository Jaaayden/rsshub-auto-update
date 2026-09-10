#!/usr/bin/env bash
set -euo pipefail
[[ ${EUID} -eq 0 ]] || { echo '请使用 sudo bash 或 root' >&2; exit 1; }
for tool in curl python3; do
    command -v "$tool" >/dev/null || { echo "缺少依赖：$tool" >&2; exit 1; }
done
umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
repo=https://api.github.com/repos/Jaaayden/rsshub-auto-update
curl -fsSL --proto '=https' --tlsv1.2 "$repo/commits/main" -o "$work/commit.json"
commit=$(python3 -c 'import json,sys,re; s=json.load(open(sys.argv[1]))["sha"]; assert re.fullmatch("[0-9a-f]{40}",s); print(s)' "$work/commit.json")
base="https://raw.githubusercontent.com/Jaaayden/rsshub-auto-update/$commit"
for file in updater.py SHA256SUMS; do
    curl -fsSL --proto '=https' --tlsv1.2 "$base/$file" -o "$work/$file"
done
python3 - "$work" <<'PY'
import hashlib, pathlib, sys
p = pathlib.Path(sys.argv[1])
expected, name = (p / 'SHA256SUMS').read_text().split()
assert name == 'updater.py'
assert hashlib.sha256((p / name).read_bytes()).hexdigest() == expected, '下载校验失败'
PY
python3 "$work/updater.py" install "$@"
