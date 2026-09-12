#!/bin/bash
# ボットをmacOSに常駐登録する（ログイン時に自動起動＋落ちても自動復活）。
#
# なぜ要るか:
#   これまで nohup で起動していたが、ターミナルを閉じる・Macを再起動する・
#   プロセスが落ちる、のいずれでもボットが消えていた。消えるとDiscordから
#   「再起動して」が届かず、Macを触るまで復旧できない。実際に何度も起きた。
#   launchd に任せれば、macOS 自身が起動と復活を面倒みてくれる。
#
# 使い方（Macで一度だけ）:
#   cd ~/so-portfolio/discord-groupchat && bash install_autostart.sh
#
# 解除:
#   launchctl bootout gui/$(id -u)/com.marusadi.discordbot
set -eu

DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="com.marusadi.discordbot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLI
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$DIR/run_bot.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$DIR/launchd.log</string>
  <key>StandardErrorPath</key><string>$DIR/launchd.log</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
</dict>
</plist>
PLI

# 取り残しを片付けてから登録し直す
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
pkill -f ai_group_chat.py 2>/dev/null || true
pkill -f run_bot.sh 2>/dev/null || true
rm -f "$DIR/run_bot.pid"
sleep 1

launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo "✅ 常駐登録しました。"
echo "   ・Macのログイン時に自動で起動します"
echo "   ・落ちても macOS が自動で起動し直します"
echo "   ・ターミナルを閉じても止まりません"
echo "   状態: launchctl print gui/$(id -u)/$LABEL | head -20"
