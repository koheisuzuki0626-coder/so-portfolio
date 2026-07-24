# このリポジトリでの作業ルール

## 最重要：あなたがどこから呼ばれているかで挙動を変えること

- **Discordボットから呼ばれている場合**（`ai_group_chat.py` が `claude -p` で
  あなたを呼んでいる場合。会話ログやDiscordの文脈が渡されるのが特徴）：
  **ターミナルコマンドの実行をユーザーに案内してはいけない。**
  コード反映・再起動が必要なときは「Discordで『再起動して』と送るだけでOK
  （自動で最新コードを取得して再起動される）」と案内すること。
  ユーザーの操作はすべてDiscord内で完結させる。
- **Claude Code のチャットセッションの場合**：以下のルールに従う。

## 必須：コードを書き換えたら「Discordで再起動」を案内する（Claude Codeセッション用）

`discord-groupchat/` のコード（特に `ai_group_chat.py`）を変更してプッシュしたら、
**毎回必ず**、返信の最後にこう案内すること（ユーザーの明確な希望。ターミナルの
コマンドは案内しない）：

> Discordで「**再起動して**」と送ってください（自動で最新コードを取得して再起動されます）

**例外（ボットが動いていない時だけ）**：ボットのプロセスが落ちている・起動して
いない場合はDiscordからの再起動は届かないので、その時に限りMac用の起動コマンドを添える：

```bash
pkill -f ai_group_chat.py; cd ~/so-portfolio && git fetch origin && git reset --hard origin/claude/line-webhook-claude-integration-l3hff3 && cd discord-groupchat && source venv/bin/activate && python ai_group_chat.py
```

※ `git pull`（stash付き含む）ではなく **fetch + reset --hard** を使うこと。
Mac側ではボットの自己改修機能がローカルコミットを作ることがあり、pull だと
divergent branches エラーで止まるトラブルが実際に起きたため。reset 方式なら
Mac側を常にリモートと同一にできる（ボットの自己改修はプッシュされていれば残る。
プッシュ失敗時は自己改修フローが「ローカルコミットのみ」と警告する）。
ボット側の再起動フロー（「再起動して」）も内部で同じ fetch + reset を行う。

理由：Bot は起動時にコードを読み込むため、変更は再起動しないと反映されない。
ユーザーはすべてをDiscord内で完結させたいので、ターミナル操作を求めない。

## discord-groupchat の基本

- Discord Bot 3体（オーケストレーター / Claude / Gemini）を1つの Python プロセスで動かす
- Claude 担当は `claude` CLI（サブスク・API課金なし）、Gemini は API 無料枠
- Gemini はモデルローテーション＋クールダウンで無料枠切れに対応。
  枠切れ時は Claude へフォールバック（テキスト系）またはメタ情報分析（YouTube リサーチ）
- 変更後は `python3 -m py_compile ai_group_chat.py` で構文チェックしてからコミット
