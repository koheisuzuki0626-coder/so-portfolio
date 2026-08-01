#!/bin/bash
# ボットを「落ちても勝手に復活する」形で動かす常駐スクリプト。
#
# なぜ要るか:
#   ボットのプロセスが落ちると、Discordから「再起動して」と言っても届かない。
#   復旧のたびにMacでコマンドを打つ必要があり、実際にそれで詰まった。
#   この形なら、落ちても数秒で起動し直すので、Discordだけで完結し続けられる。
#
# 使い方（Macで一度だけ）:
#   cd ~/so-portfolio/discord-groupchat
#   chmod +x run_bot.sh
#   ./run_bot.sh
#
#   ターミナルを閉じても動かし続けたい場合:
#   nohup ./run_bot.sh > /dev/null 2>&1 &
#
# 止めるとき:
#   touch ~/so-portfolio/discord-groupchat/STOP   （作ると終了する）
#   または  pkill -f run_bot.sh; pkill -f ai_group_chat.py
set -u

cd "$(dirname "$0")" || exit 1
BRANCH="${BOT_BRANCH:-claude/line-webhook-claude-integration-l3hff3}"
LOG="bot.log"
STOP_FILE="STOP"
PID_FILE="run_bot.pid"

# --- 二重起動の防止 -------------------------------------------------------
# 見張り役が2つ動くと、ボットを止めてもそれぞれが起動し直すので必ず2体に戻る。
# 実際に「pkill しても2つ動く」状態になった。先着1つだけを通す。
if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
    echo "すでに見張り役が動いています（PID $(cat "$PID_FILE")）。二重起動はしません。"
    echo "止めたい場合: touch $STOP_FILE"
    exit 0
fi
echo $$ > "$PID_FILE"
trap 'rm -f "$PID_FILE"' EXIT

# 取り残されたボットがいたら片付けてから始める（ここを通れる見張り役は1つだけ）
if pgrep -f "ai_group_chat.py" > /dev/null 2>&1; then
    echo "$(date '+%F %T') 既存のボットを停止します（重複防止）" | tee -a "$LOG"
    pkill -f "ai_group_chat.py"
    sleep 2
fi

# gitを非対話にする（認証情報が無い時に入力待ちで固まらせない）
export GIT_TERMINAL_PROMPT=0
export GIT_ASKPASS=echo
export SSH_ASKPASS=echo

echo "=== 監視付きで起動します（停止するには $STOP_FILE を作成）===" | tee -a "$LOG"

fails=0
while true; do
    if [ -f "$STOP_FILE" ]; then
        echo "$(date '+%F %T') STOP ファイルを検出したので終了します" | tee -a "$LOG"
        rm -f "$STOP_FILE"
        exit 0
    fi

    # 起動のたびに最新コードへ揃える（ローカルの差分は退避してから）
    git -C .. fetch origin "$BRANCH" --quiet 2>>"$LOG" && \
    git -C .. reset --hard "origin/$BRANCH" --quiet 2>>"$LOG" && \
    echo "$(date '+%F %T') 最新コードに更新しました" >> "$LOG"

    if [ -d venv ]; then
        # shellcheck disable=SC1091
        source venv/bin/activate
    fi

    echo "$(date '+%F %T') ボットを起動します" | tee -a "$LOG"
    python ai_group_chat.py >> "$LOG" 2>&1
    code=$?
    echo "$(date '+%F %T') ボットが終了しました (exit=$code)" | tee -a "$LOG"

    # 自己再起動（os.execv）は同じプロセスのまま入れ替わるので、ここには来ない。
    # ここに来たのは異常終了なので、少し待ってから起動し直す。
    if [ "$code" -eq 0 ]; then
        fails=0
    else
        fails=$((fails + 1))
    fi
    # 連続で落ち続ける時は間隔を空ける（起動失敗のループで暴走させない）
    wait_sec=5
    [ "$fails" -ge 3 ] && wait_sec=30
    [ "$fails" -ge 6 ] && wait_sec=120
    echo "$(date '+%F %T') ${wait_sec}秒後に再起動します（連続失敗 ${fails} 回）" | tee -a "$LOG"
    sleep "$wait_sec"
done
