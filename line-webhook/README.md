# LINE × Claude CLI Webhook（画像・動画対応）

LINE Messaging API の Webhook サーバーです。許可したユーザーとの間で、テキストに加えて
**生成した画像・動画のやり取り**ができます。

- **テキスト**: 届いたテキストを `claude "<text>"` に渡し、標準出力を返信。
- **送信（生成物 → LINE）**: claude が生成した画像/動画を検出し、LINE に画像/動画メッセージとして返信。
- **受信（LINE → claude）**: あなたが送った画像/動画を保存し、次のテキスト指示と一緒に claude に渡す（例:「この画像を〜して」）。

## 仕組み

```
[テキスト]
LINE → /webhook → 署名検証 → ユーザー照合 → claude 実行
     → stdout を返信 ＋ 生成された画像/動画を検出して送信

[画像/動画を送る]
LINE → /webhook → Content API でDL → inbound/ に保存 → 「保留中の添付」に登録
次にテキストを送ると、その指示＋保存パスを claude に渡す
```

### 生成物の検出（2方式の併用）
1. **ディレクトリ走査**: claude 実行の前後で作業ディレクトリ（`WORK_DIR`）を走査し、`mtime` が新しい画像/動画ファイルを検出。
2. **stdout パス抽出**: claude の標準出力に含まれるファイルパス（`.png/.jpg/.mp4` など）を拾う。

検出したファイルは `public-media/` にコピーし、`PUBLIC_BASE_URL/media/...` の公開URLとして LINE に送ります（LINEは公開HTTPS URLしか送れないため）。

## セットアップ

```bash
cd line-webhook
npm install
cp .env.example .env   # 値を編集
npm start
```

### 環境変数

| 変数 | 必須 | 説明 |
| --- | --- | --- |
| `LINE_CHANNEL_ACCESS_TOKEN` | ✓ | Messaging API のチャネルアクセストークン |
| `LINE_CHANNEL_SECRET` | ✓ | チャネルシークレット（署名検証） |
| `ALLOWED_USER_IDS` | ✓ | 反応を許可する LINE ユーザーID（カンマ区切り） |
| `PUBLIC_BASE_URL` | 送信に必須 | LINE から到達できる公開HTTPSベースURL。未設定だとテキストのみ |
| `PORT` | | 待ち受けポート（既定 3000） |
| `CLAUDE_BIN` | | `claude` CLI のパス（PATHにない場合） |
| `COMMAND_TIMEOUT_MS` | | コマンドのタイムアウト（既定 300000ms） |
| `WORK_DIR` | | claude の作業ディレクトリ兼生成物走査先（既定 `./workspace`） |
| `INBOUND_DIR` | | 受信メディアの保存先（既定 `./inbound`） |
| `MEDIA_SERVE_DIR` | | 配信用にコピーする先（既定 `./public-media`） |

## LINE 側の設定
1. Messaging API チャネルで Webhook URL に `https://<ドメイン>/webhook` を設定し Webhook をオン。
2. 自動応答メッセージはオフ（二重返信防止）。

## 使い方の例
- **画像生成**: 「猫の画像を作って ~/workspace/cat.png に保存して」→ 生成された画像が返ってくる。
- **画像編集**: 画像を送る → 「背景を夜空にして」→ 編集後の画像が返ってくる。
- **動画**: 動画を送る／生成させる → 動画メッセージとして返信（プレビューは ffmpeg があれば先頭フレーム）。

## メディアの制約（LINE仕様）
- 画像: JPEG/PNG、10MB以下。
- 動画: MP4、200MB以下。プレビュー画像が必須（ffmpeg があれば自動生成、無ければプレースホルダ）。
- 1返信あたり最大5メッセージ（テキスト1＋メディア最大4）。超過分は件数を注記。

## 注意
- 受信ファイルの「保留中の添付」はメモリ上に保持するため、サーバー再起動で消えます。
- `WORK_DIR` は claude 専用の作業ディレクトリにしてください（無関係な画像/動画があると誤検出します）。
- 動画プレビューをきれいにするには `ffmpeg` のインストールを推奨します。
- 許可ユーザーはこのサーバー上で Claude Code を操作できることになるため、`ALLOWED_USER_IDS` は信頼できる本人のみに。
