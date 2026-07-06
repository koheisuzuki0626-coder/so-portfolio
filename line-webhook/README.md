# LINE × Claude CLI Webhook

LINE Messaging API の Webhook サーバーです。許可したユーザーから届いたテキストを
Claude Code CLI (`claude "<text>"`) に渡し、その標準出力を LINE に返信します。

## 特徴

- **署名検証**: `X-Line-Signature` をチャネルシークレットで検証し、LINE からの正規リクエストのみ処理します（`@line/bot-sdk` のミドルウェア）。
- **ユーザー制限**: `ALLOWED_USER_IDS` に登録した LINE ユーザーIDのみ反応します。それ以外は無視します。
- **コマンドインジェクション対策**: 受信テキストはシェル文字列に埋め込まず、`execFile('claude', [text])` で **単一の引数** として渡すため、`"` `$()` `;` `|` などのメタ文字が悪用されることはありません。
- **タイムアウト / 出力サイズ制限**: 長時間実行や巨大出力を抑制し、LINE の5000文字上限に合わせて返信を切り詰めます。

## セットアップ

```bash
cd line-webhook
npm install
cp .env.example .env   # 値を編集
npm start
```

### 必要な環境変数

| 変数 | 説明 |
| --- | --- |
| `LINE_CHANNEL_ACCESS_TOKEN` | Messaging API のチャネルアクセストークン |
| `LINE_CHANNEL_SECRET` | チャネルシークレット（署名検証に使用） |
| `ALLOWED_USER_IDS` | 反応を許可する LINE ユーザーID（カンマ区切り） |
| `PORT` | 待ち受けポート（既定: 3000） |
| `CLAUDE_BIN` | `claude` CLI のパス（PATH にない場合） |
| `COMMAND_TIMEOUT_MS` | コマンドのタイムアウト（既定: 120000ms） |

## LINE 側の設定

1. [LINE Developers](https://developers.line.biz/) で Messaging API チャネルを作成。
2. Webhook URL に `https://<あなたのドメイン>/webhook` を設定し、Webhook を有効化。
3. 応答メッセージ（自動応答）はオフにしておくと二重返信になりません。

## 自分のユーザーIDの調べ方

一度ボットにメッセージを送ると、サーバーログに `source.userId`（`Uxxxx...`）が
出力されます。その値を `ALLOWED_USER_IDS` に設定してください。

## 動作の流れ

```
LINE → POST /webhook → 署名検証 → ユーザーID照合 → claude "<text>" 実行 → stdout を返信
```

## 注意

`claude` CLI は受信テキストをそのままプロンプトとして実行します。許可ユーザーには
このサーバーが動く環境上で Claude Code を操作できる権限を与えることになるため、
`ALLOWED_USER_IDS` は信頼できる本人のみに限定してください。
