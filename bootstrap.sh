#!/usr/bin/env bash
# 一键把 MiniMax 签到装成 GitHub Actions（电脑不用开机）
#
#   ./bootstrap.sh                    # 交互式，缺什么问什么
#   ./bootstrap.sh --repo my/minimax-checkin
#
# 需要：git、gh（已登录）、python3
set -euo pipefail

REPO=""
REPO_NAME="minimax-checkin"
PRIVATE="--public"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="$2"; shift 2 ;;
    --private) PRIVATE="--private"; shift ;;
    --name) REPO_NAME="$2"; shift 2 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "未知参数: $1" >&2; exit 1 ;;
  esac
done

say() { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die() { printf '\033[1;31m✗ %s\033[0m\n' "$*" >&2; exit 1; }

command -v git  >/dev/null || die "没找到 git"
command -v gh   >/dev/null || die "没找到 gh（GitHub CLI），请先装：https://cli.github.com"
command -v python3 >/dev/null || die "没找到 python3"

gh auth status >/dev/null 2>&1 || die "gh 还没登录，请先执行：gh auth login"

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

say "准备仓库内容"
cp -r "$(dirname "$0")/." "$WORKDIR/"
cd "$WORKDIR"
rm -f bootstrap.sh
chmod +x minimax_checkin.py

# ── 1. 登录拿凭据 ────────────────────────────────────────────────
CRED_FILE="$HOME/.minimax/checkin/credentials.json"
if [[ -f "$CRED_FILE" ]]; then
  say "检测到已有凭据：$CRED_FILE"
else
  say "没有本机凭据，现在登录（浏览器里点确认即可，手机也行）"
  python3 minimax_checkin.py login
fi

[[ -f "$CRED_FILE" ]] || die "登录后仍未找到凭据文件"

say "导出凭据"
CRED_JSON="$(python3 minimax_checkin.py export --raw)"

# ── 2. 建仓库并推送 ──────────────────────────────────────────────
if [[ -z "$REPO" ]]; then
  say "创建 GitHub 仓库：$REPO_NAME"
  gh repo create "$REPO_NAME" $PRIVATE --source=. --remote=origin --push \
    || die "创建仓库失败（仓库名可能已被占用，换个 --name）"
else
  say "推送到已有仓库：$REPO"
  git init -q
  git add -A
  git commit -q -m "feat: MiniMax 每日自动签到"
  gh repo create "$REPO" $PRIVATE --source=. --remote=origin --push \
    || { git remote add origin "https://github.com/$REPO.git" 2>/dev/null || true
         git push -u origin HEAD; }
fi

# ── 3. 设置 Secret ───────────────────────────────────────────────
say "设置仓库 Secret: MINIMAX_CREDENTIALS"
printf '%s' "$CRED_JSON" | gh secret set MINIMAX_CREDENTIALS
say "可选：设置推送地址 Secret: MINIMAX_NOTIFY_URL"
read -r -p "推送地址（留空跳过）: " NOTIFY || true
if [[ -n "${NOTIFY:-}" ]]; then
  printf '%s' "$NOTIFY" | gh secret set MINIMAX_NOTIFY_URL
fi

cat <<EOF

✅ 完成。

仓库：$REPO_NAME
仓库里**不带定时器** —— 手动点 Run workflow 就能签。

想改成每天自动的话，调一次这个即可：
  curl -sS -X POST -H "Authorization: Bearer \$GITHUB_PAT" \
    -H "Accept: application/vnd.github+json" \
    https://api.github.com/repos/你的仓库/actions/workflows/checkin.yml/dispatches \
    -d '{"ref":"main"}'

现在可以手动触发一次验证：
  gh workflow run checkin.yml

看日志：
  gh run list
  gh run view --log

想改时间：改外部调度器的时间（工作流里已经没有 cron 字段了）。
EOF
