# Discord AI グループチャット（Claude × Gemini）

Discord Bot を3体（オーケストレーター / Claude / Gemini）1つの Python プロセスで動かし、
チャンネル内で **Claude と Gemini が対等に交互に会話**します。人間が `!talk お題` で
開始、`!stop` で停止。進行はオーケストレーターが制御します。

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
チャンネルで：
```
!talk 猫と犬はどっちが賢い？
```
→ Claude と Gemini が交互に発言します。`!stop` で停止。

## 調整
- `MAX_TURNS`（合計発言数）, `CLAUDE_MODEL`, `GEMINI_MODEL` は `.env` で変更可。
- 1発言の長さや口調は `ai_group_chat.py` の `persona()` を編集。

## 注意
- Claude担当は `claude` CLI 経由なので、実行するMacで `claude` がログイン済みである必要があります。
- Bot 同士の無限ループを防ぐため、`on_message` は人間の発言（`!talk`/`!stop`）のみ反応します。
