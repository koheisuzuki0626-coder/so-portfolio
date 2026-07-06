# オーケストレーター（Claude × Gemini）

Claude を「オーケストレーター（司令塔）」として動かし、必要に応じて Gemini CLI に
下書き・別視点・要約などを依頼させ、Claude が統合して最終回答を出すための設定です。

## 仕組み
- `CLAUDE.md` にオーケストレーターの振る舞いを定義しています。
- Claude Code はこのフォルダで起動されると、この `CLAUDE.md` を読み込みます。
- LINE Webhook（`../line-webhook`）の `WORK_DIR` をこのフォルダに向けることで、
  LINE 経由の会話がこの座組で動きます。

## 前提
- `gemini` CLI がインストール済みで、`GEMINI_API_KEY` が使えること
  （`gemini -p "..."` が返事すればOK）。

## LINE Webhook との連携
`../line-webhook/.env` に次を設定して Webhook を再起動します（`you` は自分のユーザー名）:

```
WORK_DIR=/Users/you/so-portfolio/orchestrator
CLAUDE_ARGS=-p --dangerously-skip-permissions
GEMINI_API_KEY=（Google AI Studio のAPIキー）
```

`--dangerously-skip-permissions` は、Claude が LINE の指示に応じて `gemini` などの
コマンドを確認なしで実行できるようにするためです。反応するのは許可済みユーザーID
だけなので、その前提での利用に留めてください。

## 調整
座組の役割分担や口調を変えたいときは `CLAUDE.md` を編集してください。
編集後は Webhook を再起動すれば反映されます。
