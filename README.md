# so. — Portfolio

映像ポートフォリオサイト(静的HTML / GitHub Pages)。
公開URL: https://koheisuzuki0626-coder.github.io/so-portfolio/

## 公開URLを変えるとき

canonical / og:url / og:image / 構造化データ / sitemap.xml / robots.txt には
絶対URLが必要で、いま17か所に散っている。1か所でも直し忘れると検索エンジンに
古いURLを伝え続けることになるため、まとめて書き換えるスクリプトを用意している。

```bash
node scripts/set-site-url.mjs --show                    # いまの設定を表示
node scripts/set-site-url.mjs https://example.jp/       # 独自ドメインへ(CNAME も生成)
node scripts/set-site-url.mjs https://xxx.github.io/    # github.io へ(CNAME は削除)
```

独自ドメインを指定した場合は、このあと GitHub 側で
**Settings → Pages → Custom domain** の設定と **Enforce HTTPS** が必要。

## ページ構成

| ファイル | 内容 |
|----------|------|
| `index.html` | トップ。サービス〜料金〜実績〜お問い合わせ |
| `about.html` | 会社紹介(so. について)。トップのフッター「About — so. について」から辿れる |
| `assets/site.css` | 2ページ共通のスタイル。**両方に効く**ので変更時は両ページを確認すること |

`about.html` はトップのようなスクロール演出やライトボックスを持たず、
ヘッダーの導線もトップへ戻すだけの簡易版にしている(ドロップダウンなし＝ハンバーガーも不要)。
フッターのジャンル一覧はトップの `.genre` カードから生成できないため静的に書いており、
ズレたら e2e で落ちるようにしてある。

## YouTube 新着動画の自動掲載

YouTube に動画をアップすると、Works セクションに自動で並びます。**APIキーは不要**です。

```
YouTube にアップ
      ↓
GitHub Actions が6時間おきにチャンネルページを取得   (.github/workflows/sync-youtube.yml)
      ↓
data/videos.json を更新してコミット                  (scripts/fetch-youtube.mjs)
      ↓
index.html が読み込んで Works を描画
```

対象チャンネルは `youtube.config.json` で設定します。

```json
{
  "note": "@hzrinrng / ふぞろいの林檎たち",
  "channel": "UCK-RAb4PrWVN4EXXPnbJ67w",
  "maxVideos": 50
}
```

| キー | 説明 |
|------|------|
| `channel` | `UC...`(チャンネルID)/ `@ハンドル` / チャンネルURL のいずれか。ハンドルは変更されうるのでチャンネルID推奨 |
| `maxVideos` | 保存する最大件数(既定 6)。チャンネルページの1画面分=約30件が取得上限なので、50 を指定すれば実質「取れるだけ全部」 |
| `note` | 自由記述のメモ。スクリプトからは無視されます |

### 更新のタイミング

- 6時間おき(`cron: '17 */6 * * *'`)＋ **Actions → Sync YouTube videos → Run workflow** で手動実行
- すぐ反映したいときは手動実行してください

## 取得方式について(重要)

当初は YouTube の RSS フィード(`/feeds/videos.xml`)を使う実装でしたが、
**GitHub Actions のランナーからは UA の有無・リトライに関わらず常に 404 が返る**ことを実測で確認したため、
チャンネルの videos タブに埋め込まれた `ytInitialData` を読む方式に変更しています。

実測結果(GitHub Actions ubuntu-latest、2026-07):

| 対象 | 結果 |
|------|------|
| `youtube.com/feeds/videos.xml`(RSS) | 404(3回リトライ・UA有無いずれも) |
| `youtube.com/channel/{id}/videos` | 200、動画29件を抽出可能 |
| `googleapis.com/youtube/v3/...` | 403(= キーがあれば到達可能) |

### この方式の制約

- **壊れやすい。** YouTube のフロントエンド実装に依存します。実際に旧構造(`videoRenderer`)から
  現構造(`lockupViewModel`)への変更が起きており、今後も変わる可能性があります。
  そのため **1件も抽出できなければスクリプトは異常終了し、`data/videos.json` を書き換えません。**
  ワークフローが赤くなるだけで、サイトは直前の内容を表示し続けます(静かに空データを公開することはありません)。
- **説明文は取得できません。** カードにはタイトルと年のみ表示されます。
- **投稿日は概算です。** チャンネルページは「2 日前」のような相対表記しか持たないため、
  取得時刻から逆算しています(`publishedIsApproximate: true`)。年の表示が境界で1年ずれる可能性があります。

### より安定させたい場合(YouTube Data API v3)

APIキーを使えば、公式APIで正確な投稿日・説明文が取得でき、実装が壊れる心配もなくなります。

1. Google Cloud Console でプロジェクトを作り YouTube Data API v3 を有効化、APIキーを発行
2. リポジトリの **Settings → Secrets and variables → Actions** に `YOUTUBE_API_KEY` として登録
3. スクリプトをAPI方式に差し替え(未実装)

消費クォータは1回の実行あたり約2ユニット、無料枠は1日10,000ユニットなので余裕があります。

## 動作の詳細

- **表示件数**: Works は最初の6件を表示し、`More (残数)` ボタンで6件ずつ追加表示します。
  全件出しきるとボタンは消えます。6件以下のときはボタン自体が出ません
- **未設定・データ空・取得失敗のとき**: `index.html` に直接書かれたプレースホルダー(Project 01〜03)が表示されます
- **サムネイル**: `maxresdefault.jpg` を試し、無い動画は `hqdefault.jpg` に自動で切り替わります
- **再生**: カードをクリックするとページ内のライトボックスで再生(`youtube-nocookie.com` 埋め込み)。
  Esc または背景クリックで閉じ、iframe を破棄して再生を止めます。
  JavaScript が無効な環境では YouTube のページに遷移します

## ローカルでの確認

```bash
node scripts/fetch-youtube.mjs   # data/videos.json を更新
npx http-server -p 8777 .        # http://127.0.0.1:8777 で確認
```

`file://` で直接開くと `data/videos.json` の読み込みが CORS で失敗するため、必ず HTTP サーバー経由で確認してください。

## 手動で作品を差し替えたい場合

自動掲載を使わず手で管理したいときは、ワークフローを無効化(Actions 画面から Disable)して
`index.html` の `#works-grid` 内のカードを直接編集してください。
`data/videos.json` の `videos` が空なら、書いた内容がそのまま表示されます。

## スマホ中心で編集する

このサイトは**スマホだけで編集・公開まで完結**します。PC は常時起動しているので、
Remote Control で繋いでおけば、スマホから出した指示が PC の実ファイルを直接編集します。

```
スマホの Claude アプリ → Code タブ → PC のセッションを開いて指示
      ↓
PC 上で編集される(＝この時点で PC は同期済み。git pull は不要)
      ↓
GitHub の main にプッシュ
      ↓
GitHub Pages が公開                              (数十秒〜数分)
```

### 1. PC 側の設定(最初に1回だけ)

```bash
node scripts/setup-remote-control.mjs
```

`~/.claude/settings.json` に次の3つを書き込みます(既存の設定は保持し、`.bak` を残します)。

| キー | 効果 | |
|------|------|---|
| `inputNeededNotifEnabled` | 許可を求められたとき・質問されたときスマホに通知 | **最重要** |
| `agentPushNotifEnabled` | 長い処理が終わったらスマホに通知 | 重要 |
| `remoteControlAtStartup` | `claude` と打つだけで毎回スマホに繋がる | 接続の土台 |

通知のうち `inputNeededNotifEnabled` がいちばん効きます。Claude Code は許可を求める場面で
**返事があるまで完全に止まる**ため、これが無いと「スマホを見たら1時間前から止まっていた」が起きます。

| オプション | 動作 |
|-----------|------|
| `--dry-run` | 何が変わるか表示するだけ。書き込まない |
| `--check` | 環境の確認だけ。繋がらないときの原因調べに使う |
| `--off` | 3つを `false` に戻す |

### 2. PC 側でセッションを起動しておく

常時起動なので、`tmux` の中で起動します。こうしないとターミナルを閉じた時点で切れます。

```bash
brew install tmux                 # 未インストールなら
cd ~/so-portfolio
tmux new -s claude                # claude という名前の常駐枠を作る
claude                            # その中で起動(自動でスマホに繋がる)
```

`Ctrl+b` を押してから `d` で、**起動したまま**ターミナルから離れられます。
戻りたいときは `tmux attach -t claude`。

クラムシェル運用では**スリープした時点で接続が切れます**。これが実運用でいちばん詰まるところです。
確実なのは `caffeinate` で寝かせないことです。tmux ごと包んでしまうのが楽です。

```bash
caffeinate -is tmux new -s claude
```

`-i` はアイドルスリープ、`-s` はシステムスリープを抑止します。この tmux を抜けるまで PC は寝ません。
(システム設定 → バッテリー / ロック画面 側でスリープを切っておく方法でもかまいませんが、
項目名が macOS のバージョンで変わるため `caffeinate` のほうが確実です)

### 3. スマホから繋ぐ

Claude アプリ(iOS / Android)の下部 **Code** タブにセッションが並びます
(PCアイコン＋緑の点＝オンライン)。アプリが未インストールなら Claude Code 内で `/mobile` と打つと QR が出ます。

### 繋がらないとき

```bash
node scripts/setup-remote-control.mjs --check
```

`ANTHROPIC_API_KEY` / `DISABLE_TELEMETRY` / `api.anthropic.com` 以外を指す `ANTHROPIC_BASE_URL` などが
1つでも立っていると Remote Control は起動しません。先にそれを解除してください。
認証は claude.ai アカウント(`claude auth login`)が必要で、APIキー認証では使えません。

PC が落ちている・繋がらないときは、Claude アプリからクラウド上の新しいセッションを始めれば
そちらでも作業できます。ただしそれは PC のファイルを触らないので、
あとで PC 側に `git pull origin main` が必要です。

### 気をつける点

- **公開されるのは `main` の内容だけです。** 作業ブランチのままだとサイトには出ません
- PC で直接編集した場合は、先にコミットしてからスマホで作業を始めてください
