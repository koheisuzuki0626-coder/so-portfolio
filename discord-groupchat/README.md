# Discord AI グループチャット（人間 × Claude × Gemini）

Discord Bot を3体（オーケストレーター / Claude / Gemini）1つの Python プロセスで動かし、
チャンネルで **人間・Claude・Gemini が一緒に会話**できます。

- 普通に発言（@メンションなし）→ **常にオーケストレーター**が Claude と Gemini を
  使い分けて統合回答
- @メンション → **メンションした子だけ**が反応（@Claude と @Gemini 両方で二人が個別に返答）
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
2. **絵コンテ**（**Gemini画像生成・無料枠 約500枚/日**。失敗時はHiggsfieldにフォールバック）→ `OK` or 修正指示
3. **編集チェック×3**（Higgsfieldで動画化）→ 最大3回まで修正、`OK` で完成

絵コンテを Higgsfield で作りたい場合は `.env` に `IMAGE_GEN_ENGINE=higgsfield` を設定。

**生成モデルは Discord の発言で切り替え可能**（設定は再起動後も保持）：
- 「**クリング使って**」「**動画はklingで**」→ 動画生成が Kling に
- 「**ナノバナナがいい**」→ 画像生成が Gemini（Nano Banana・無料枠）に
- 「**シードリームにして**」「**fluxで生成して**」→ 画像生成が Higgsfield の該当モデルに
- 対応: kling / seedance / hailuo(minimax) / dop（動画）、nano banana / seedream / flux（画像）
- 「**今のモデル設定教えて**」で現在の設定を確認
- 「クリングで犬のCM作って」のように制作依頼と同時に指定してもOK

**各種モデルで生成（Higgsfield MCP経由）**：
「**seedanceで動画作って**」「**veoで〜作りたい**」「**クリング3で〜生成して**」のように
モデル名を添えて頼むと、そのモデルで生成→完成したら動画/画像URLを自動投稿します
（投入→自動監視→URL投稿まで全自動。再起動しても完了は届きます）。
- 対応（動画）: seedance / veo / sora / wan / kling / kling3 / kling turbo / gemini omni
- 対応（画像）: nano banana / soul / seedream
- 画像や動画を添付すると参照メディアとして渡します
- 「**できた？**」でいつでも生成状況を確認できます

**モーションコントロール（動きの転写・1発生成）**：
参照動画（mp4/mov・2〜60秒・720p/1080p）を添付して
「**この動きでモーションコントロールで生成して**」と頼むと、Kling が参照動画の動きを
キャラクターに転写した動画を1発で生成します（構成案フローは通しません）。
- キャラ画像も一緒に添付 → その見た目で生成
- 画像なし → 依頼文からキャラ画像を自動生成（Gemini無料枠→Higgsfieldの順）
- モデルは `.env` の `HIGGSFIELD_MOTION_APP` で変更可（既定: kling-video/v2.6/pro/motion-control）
※動画生成は Gemini API（Veo）だと無料枠が無く従量課金（$0.15〜0.60/秒）のため、Higgsfield のままです。

`!cancel` で中止。

### エージェントモード（プラン承認型・追加SDK不要）
```
!agent test.txt を作って「hello」と書いて
```
1. Claude が **実行プラン**（やる手順）を提示
2. Discord に **[✅ 許可] [❌ 拒否]** ボタン → 実行した本人のみ操作可、5分で自動却下
3. **[✅許可]** を押すと、その計画を **Mac上で実際に実行**して結果を返信

承認（=ボタン）がそのまま実行（Claude Code）に伝わります。`claude -p` だけで動くので
Python 3.9 でもOK、追加SDK不要です。

> ⚠️ 許可すると **フル権限**でコマンド/編集が実行されます。**プランを確認してから**
> [✅許可] を押してください。信頼できる相手のいるサーバーでのみ使うこと。

> ⚠️ 絵コンテ・編集の生成には Higgsfield のクレジット（有料）が必要です。
> `.env` に `HIGGSFIELD_API_KEY` / `HIGGSFIELD_API_SECRET` を設定してください。
> クレジットが無いと該当段階で「not_enough_credits」となります（構成案までは無料）。

### 会話の記憶（永続化＋長期記憶＋全ログ参照）
- 全発言は `history/{チャンネルID}.jsonl` に自動保存。**再起動しても消えません**。
- プロンプトには「長期記憶の要約 + 直近40件」を使用。
- 直近枠から溢れた古い会話は自動で要約に圧縮され、`history/{チャンネルID}_summary.txt` に保存。
- **過去ログの全文参照**：「前に話した〜」「昨日決めた〜」のような質問のときは、
  Gemini（大容量コンテキスト・無料枠）が全ログを読んで関連情報を抽出し、回答に使います。
- **導入前の履歴も取込**：初回メッセージ時に、Discordのチャンネル履歴（最大2000件）を
  一度だけ遡って取り込みます。※Botに「メッセージ履歴を読む」権限が必要。
- `.env` で調整可: `HISTORY_LIMIT`（直近件数, 既定40）, `SUMMARIZE_BATCH`（要約単位, 既定12）,
  `IMPORT_LIMIT`（過去ログ取込上限, 既定2000）, `RECALL_MAX_CHARS`（全ログ参照の上限, 既定15万字）

### 添付ファイルの内容理解（Gemini・無料枠）
チャンネルに添付したファイルの中身を AI が「人間が見た/読んだ/聴いた」のと同じレベルで理解します（上限 20MB）。
- **画像**（png/jpg/gif/webp）… OCR＋構図・要素の分析
- **音声**（mp3/wav/m4a/aac/ogg/opus/flac・ボイスメッセージ）… 自動で書き起こして投稿。
  複数話者は「話者A:」「話者B:」で区別し、末尾に【要約】が付きます。
  音声だけ添付 → 書き起こしのみ投稿。テキストを添えると内容を踏まえてAIが回答。
- **動画**（mp4/webm/mov/avi）… 映像の流れ・会話の文字起こし・画面内テキストまで読み取り
- **PDF** … 文書の構造を保って全文読み取り（表・図の説明・要約付き）
- **テキスト系**（txt/md/csv/json/py など）… そのまま読み込み（`TEXT_ATTACHMENT_MAX_CHARS`, 既定1.2万字）

読み取った内容は会話履歴に残るので、後から「さっきのPDFの〜」と質問できます。

### 自己改修＆自己再起動（Discord内で完結・自然言語OK）
ボット自身の修正・機能追加・再起動が、**Discordで話しかけるだけ**で完結します。
コマンドを覚える必要はありません（`!restart` も一応使えます）。
- 「**返答もっと短くして**」「**!trendの視聴本数を増やして**」「**〜という機能つけて**」
  → Claude Code がボット自身のコードを修正 → 構文＋起動チェックで検証 →
  修正内容を提示 → [✅適用して再起動] / [❌元に戻す] のボタンで選択
- 「**再起動して**」→ その場で自己再起動（Mac操作不要。完了すると ✅ を報告）
- 安全装置：適用前に自動バックアップ。検証に失敗したら**自動で元のコードに戻します**。
  適用時はgitにも記録（プッシュできる環境ならプッシュ）
- 大規模な機能追加は精度の面で Claude Code（チャット/Web）推奨。日常の微調整はDiscordで

### パーソナライズ（人物プロファイルの自動学習）
発言が一定数（既定20件）たまるごとに、AIがあなたの発言から**性格・口調・好み・興味・
価値観**を学習してプロファイルを自動更新します（`history/profile_名前.md` に保存）。
- プロファイルは全AI（オーケストレーター/Claude/Gemini）の応答に常時反映され、
  あなたに合わせた口調・話題選び・提案をするようになります
- `!profile` で現在の学習内容を確認できます
- 内容を直したいときは `history/profile_名前.md` を直接編集してもOK
- `.env` で調整可: `PROFILE_UPDATE_EVERY`（更新間隔・発言数, 既定20）,
  `PROFILE_MAX_CHARS`（プロファイル上限, 既定1500字）

### YouTubeリンクの内容理解
チャンネルに YouTube のリンクを貼ると、Gemini がその動画を**実際に視聴**して内容を理解します
（URL読み込み機能を使うためダウンロード不要。通常動画・ショート・live URLに対応、1メッセージ最大2本）。
- **リンクだけ**貼る → 内容まとめ（ジャンル・要約・見どころ・画面内の情報）を投稿
- **テキストを添えて**貼る（例:「この動画の編集手法どう思う？」）→ 動画の内容を踏まえてAIが回答
- 内容は会話の記憶に残るので、後から「さっきの動画の〜」と質問できます

### YouTube急上昇の自動リサーチ（毎日）
毎日決まった時刻に、YouTubeの急上昇TOP100を取得し、上位数本を Gemini が実際に「視聴」して
**演出・カット割り・カメラワーク・顔の動きや表情の見せ方・CG/VFX・テロップ・音の使い方**を分析します。
- ダイジェストを指定チャンネルに投稿し、**会話の記憶にも追加**（雑談や `!project` のアイデアに使える）
- 全文レポートは `insights/日付.md` に保存。分析済み動画はスキップされ、知見が毎日蓄積される
- `!trend` でいつでも手動実行できます
- **お題を指定して絞り込みも可能**：`!trend 料理系Vlogの編集`、`!trend ゲーム実況のオープニング演出` のように
  書くと、そのお題で検索した人気動画（直近90日・再生数順、`TREND_SEARCH_DAYS`で調整可）を、
  お題の観点を最優先にして視聴・分析します

セットアップ：
1. [Google Cloud Console](https://console.cloud.google.com/apis/library/youtube.googleapis.com) で
   **YouTube Data API v3** を有効化し、APIキーを発行（無料。Geminiキーとは別物）
2. `.env` に `YOUTUBE_API_KEY` と `TREND_CHANNEL_ID`（投稿先チャンネルID）を設定
3. `.env` で調整可: `TREND_HOUR`（実行時刻JST, 既定8時）, `TREND_DEEP_COUNT`（視聴する本数/日, 既定5）,
   `TREND_MAX_MINUTES`（視聴する動画の最大分数, 既定20）, `TREND_REGION`（既定JP）

※ 動画の「視聴」は Gemini API の YouTube URL 読み込み機能を使うためダウンロード不要です。
　無料枠の制約（動画処理は1日あたり合計約8時間まで）があるため、TOP100全部ではなく
　上位数本を深掘りする方式です。`TREND_DEEP_COUNT` を増やせば本数を増やせます。

## 調整
- `MAX_TURNS`（合計発言数）, `CLAUDE_MODEL`, `GEMINI_MODEL` は `.env` で変更可。
- 1発言の長さや口調は `ai_group_chat.py` の `persona()` を編集。

## 注意
- Claude担当は `claude` CLI 経由なので、実行するMacで `claude` がログイン済みである必要があります。
- Bot 同士の無限ループを防ぐため、`on_message` は人間の発言（`!talk`/`!stop`）のみ反応します。
