#!/usr/bin/env bash
# Discordボットを再起動するスクリプト。
# 使い方:  bash restart.sh
#   1) 既存のボットプロセスを停止
#   2) 最新コードを取得（失敗しても続行）
#   3) venv の Python でボットを起動

cd "$(dirname "$0")" || exit 1

echo "🛑 既存のボットを停止…"
pkill -f ai_group_chat.py 2>/dev/null || true
sleep 1

echo "⬇️  最新コードに同期（GitHubに強制一致）…"
BRANCH=claude/line-webhook-claude-integration-l3hff3
if git -C .. fetch origin "$BRANCH"; then
    git -C .. reset --hard "origin/$BRANCH" || echo "(reset に失敗)"
else
    echo "(fetch に失敗。ネット確認)"
fi

echo "📦 依存を確認/インストール…"
venv/bin/pip install -q -r requirements.txt || echo "(pip install に失敗。ネット確認)"

echo "🚀 ボットを起動…（止めるときは Ctrl+C）"
exec venv/bin/python ai_group_chat.py
