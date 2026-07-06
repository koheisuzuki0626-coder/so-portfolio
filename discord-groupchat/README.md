# Discord AI グループチャット（人間 × Claude × Gemini）

Discord Bot を3体（オーケストレーター / Claude / Gemini）1つの Python プロセスで動かし、
チャンネルで **人間・Claude・Gemini が一緒に会話**できます。

- 普通に発言 → Claude と Gemini の**両方が会話に参加**
- 「クロード ○○」「gemini ○○」や @メンション → **その子だけ**が反応
- `!talk お題` → Claude と Gemini **だけ**で自動トーク（最大 `MAX_TURNS` 発言）
- `!stop` → 自動トークを停止

メッセージを読むのは**オーケストレーターBotだけ**（Claude/Gemini Botは送信専用）なので、
`MESSAGE CONTENT INTENT` はオーケストレーター用にだけ ON にすればOKです。

## 必要なもの
- Python 3.10 以上
- Discord Bot トークン ×3（1サーバーに3体とも招待）
- ログイン済みの `claude` CLI（Claude担当に使用。**Claude Code のサブスクで動くのでAPI課金不要**）
- `GEMINI_API_KEY`（Gemini API。AI Studio の無料枠キー `AIzaSy...`）

## セットアップ

### 1. Python パッケージ
```bash
cd discord-groupchat
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Discord Bot を3体作る
1. https://discord.com/developers/applications で **New Application** を3回（Orchestrator / Claude / Gemini）
2. 各アプリの **Bot** タブ → **Reset Token** でトークンを取得（3つ）
3. **Orchestrator のアプリだけ**、Bot タブの **MESSAGE CONTENT INTENT** を **ON**（必須）
4. 各アプリの **OAuth2 > URL Generator** で `scope=bot`、権限 `Send Messages` を選択 →
   生成URLを開いて、**3体とも同じDiscordサーバーのチャンネルにInvite**

### 3. API キー / CLI
- Claude: APIキー不要。`claude -p "hi"` が返事すればOK（Claude Code のサブスクを利用）。
- Gemini: https://aistudio.google.com/apikey で `AIzaSy...` を発行（**課金なしの無料枠でOK**）。

### 4. .env を作成
```bash
cp .env.example .env
# .env にトークン3つ + APIキー2つを記入
```

### 5. 起動
```bash
python ai_group_chat.py
```
ターミナルに `オーケストレーター起動: ...` が出れば成功。

### 6. Discord で会話
チャンネルで普通に話しかけるだけ：
```
みんなおすすめの映画ある？
```
→ Claude と Gemini が会話に参加します。

特定の子だけに聞きたいときは名前を入れる：
```
クロード これはどう思う？
gemini 別の案ちょうだい
```

AI二人だけで話させたいとき：
```
!talk 猫と犬はどっちが賢い？
```
`!stop` で停止。

## 調整
- `MAX_TURNS`（合計発言数）, `CLAUDE_MODEL`, `GEMINI_MODEL` は `.env` で変更可。
- 1発言の長さや口調は `ai_group_chat.py` の `persona()` を編集。

## 注意
- Claude担当は `claude` CLI 経由なので、実行するMacで `claude` がログイン済みである必要があります。
- Bot 同士の無限ループを防ぐため、`on_message` は人間の発言（`!talk`/`!stop`）のみ反応します。
