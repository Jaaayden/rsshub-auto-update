#!/usr/bin/env bash
set -euo pipefail
[[ ${EUID} -eq 0 ]] || { echo '请使用 sudo bash 或 root' >&2; exit 1; }
for tool in curl python3; do
    command -v "$tool" >/dev/null || { echo "缺少依赖：$tool" >&2; exit 1; }
done
umask 077
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
# RSSHUB_AUTO_UPDATE_REF 可固定到分支、标签或完整 commit，默认 main。
ref=${RSSHUB_AUTO_UPDATE_REF:-main}
[[ $ref =~ ^[A-Za-z0-9._/-]+$ ]] || { echo 'RSSHUB_AUTO_UPDATE_REF 含有不允许的字符' >&2; exit 1; }
slug=Jaaayden/rsshub-auto-update
commit=''
# 优先用 GitHub API；未认证请求有限流（同一出口 IP 每小时 60 次），失败时退回 git ls-remote。
if curl -fsSL --proto '=https' --tlsv1.2 "https://api.github.com/repos/$slug/commits/$ref" -o "$work/commit.json" 2>/dev/null; then
    commit=$(python3 -c 'import json,sys,re; s=json.load(open(sys.argv[1]))["sha"]; assert re.fullmatch("[0-9a-f]{40}",s); print(s)' "$work/commit.json" 2>/dev/null) || commit=''
fi
if [[ -z $commit ]] && command -v git >/dev/null; then
    # 排序后注解标签的解引用行（^{}）在最后，取最后一行即得到 commit。
    commit=$(git ls-remote "https://github.com/$slug.git" "refs/heads/$ref" "refs/tags/$ref" "refs/tags/$ref^{}" 2>/dev/null | tail -n 1 | cut -f 1) || commit=''
fi
[[ $commit =~ ^[0-9a-f]{40}$ ]] || { echo "无法解析 $ref 对应的 commit（GitHub API 可能限流，可稍后重试或设置 RSSHUB_AUTO_UPDATE_REF 为完整 commit）" >&2; exit 1; }
base="https://raw.githubusercontent.com/$slug/$commit"
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
