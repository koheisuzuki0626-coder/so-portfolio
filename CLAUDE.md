# このリポジトリでの作業ルール

## 必須：コードを書き換えたら再起動コマンドを送る

`discord-groupchat/` のコード（特に `ai_group_chat.py`）を変更してプッシュしたら、
**毎回必ず**、返信の最後にユーザーがMacで実行する再起動コマンドを添えること：

```bash
pkill -f ai_group_chat.py; cd ~/so-portfolio && git pull && cd discord-groupchat && source venv/bin/activate && python ai_group_chat.py
```

理由：Bot は起動時にコードを読み込むため、変更は再起動しないと反映されない。
ユーザーは毎回このコマンドを必要とするので、聞かれる前に送る。

## discord-groupchat の基本

- Discord Bot 3体（オーケストレーター / Claude / Gemini）を1つの Python プロセスで動かす
- Claude 担当は `claude` CLI（サブスク・API課金なし）、Gemini は API 無料枠
- Gemini はモデルローテーション＋クールダウンで無料枠切れに対応。
  枠切れ時は Claude へフォールバック（テキスト系）またはメタ情報分析（YouTube リサーチ）
- 変更後は `python3 -m py_compile ai_group_chat.py` で構文チェックしてからコミット
