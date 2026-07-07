# Discord AI グループチャット（人間 × Claude × Gemini）

Discord Bot を3体（オーケストレーター / Claude / Gemini）1つの Python プロセスで動かし、
チャンネルで **人間・Claude・Gemini が一緒に会話**できます。

- 普通に発言 → Claude と Gemini の**両方が会話に参加**
- 「クロード ○○」「gemini ○○」や @メンション → **その子だけ**が反応
- `!talk お題` → Claude と Gemini **だけ**で自動トーク（最大 `MAX_TURNS` 発言）
- `!stop` → 自動トークを停止

メッセージを読むのは**オーケストレーターBotだけ**（Claude/Gemini Botは送信専用）なので、
`MESSAGE CONTENT INTENT` はオーケストレーター用にだけ ON にすればOKです。

### オーケストレーターの3層構造
`@オーケストレーター` / 宛先なしの発言は、次の3層で処理されます：
1. **ルーティング** … 要求を分類し、簡単なら得意モデル1つで即答（コスト節約）、複雑なら多段化
2. **ディベート** … Claude と Gemini が独立に回答 → 互いの回答を見せて批判・修正
3. **統合** … 司令塔が合意点/対立点を整理し、単一の最終回答にまとめる

Gemini が使えない場合は自動的に Claude 単独へ縮退します。

### Web検索（ボット内蔵・APIキー不要）
最新情報が要る質問では、ボット自身が DuckDuckGo で検索し、その結果を AI に渡して回答します
（Claude CLI の権限プロンプトに依存しないので常に使えます）。
- ルーターが「最新情報が必要」と判断したら自動で検索
- `!search 調べたいこと` で明示的に検索させることも可能

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

### 映像制作パイプライン（構成案 → 絵コンテ → 編集チェック×3）
Higgsfield を使った承認フロー。各段階でテキスト返信して進めます。

```
!project 犬が主役の30秒CM
```
1. **構成案**（Claudeが作成）→ `OK` で承認、直したいときは指示を返信
2. **絵コンテ**（Higgsfieldで画像生成）→ `OK` or 修正指示
3. **編集チェック×3**（Higgsfieldで動画化）→ 最大3回まで修正、`OK` で完成

`!cancel` で中止。

> ⚠️ 絵コンテ・編集の生成には Higgsfield のクレジット（有料）が必要です。
> `.env` に `HIGGSFIELD_API_KEY` / `HIGGSFIELD_API_SECRET` を設定してください。
> クレジットが無いと該当段階で「not_enough_credits」となります（構成案までは無料）。

## 調整
- `MAX_TURNS`（合計発言数）, `CLAUDE_MODEL`, `GEMINI_MODEL` は `.env` で変更可。
- 1発言の長さや口調は `ai_group_chat.py` の `persona()` を編集。

## 注意
- Claude担当は `claude` CLI 経由なので、実行するMacで `claude` がログイン済みである必要があります。
- Bot 同士の無限ループを防ぐため、`on_message` は人間の発言（`!talk`/`!stop`）のみ反応します。
