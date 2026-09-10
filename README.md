# so. — Portfolio

映像ポートフォリオサイト(静的HTML / GitHub Pages)。
公開URL: https://koheisuzuki0626-coder.github.io/so-portfolio/

## 公開範囲

いまは **URL を知っている人だけに見せる** 設定にしている。

- `index.html` / `about.html` の `<meta name="robots" content="noindex, nofollow">`
- `robots.txt` から `Sitemap:` の行を外している

検索結果には出ないが、**アクセス制限ではない**。URL を知っていれば誰でも見られるし、
リポジトリが public なのでソースも読める。パスワードをかけたい場合は
GitHub Pages では対応できないため、別のホスティングが必要。

`robots.txt` で `Disallow: /` にしていないのは意図的。クロールを止めると
`noindex` 自体を読んでもらえず、外部リンク経由で URL だけ検索結果に出ることがある。

### 検索に載せたくなったら

1. 両ページの `<meta name="robots">` の行（とその上のコメント）を削除
2. `robots.txt` に `Sitemap: <公開URL>sitemap.xml` を戻す
3. Google Search Console でサイトを登録し、`sitemap.xml` を送信

`sitemap.xml` はそのまま置いてあるので作り直しは不要。

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

## 料金計算機の段差（ファネル計測）

料金表を公開しているので、**高いと感じた人は連絡せずに閉じる**。
そのままでは「誰が価格で諦めたか」が一切残らないため、計算機の通過点に印を打っている。
実装は `index.html` の `track()` まわり。氏名・メールなど個人を特定するものは送らない。

| イベント | 意味 |
|----------|------|
| `plans_view` | 料金セクションが画面に入った（母数） |
| `calc_use` | **自分で条件を変えた** ＝ 具体的に検討した人。1人1回だけ |
| `calc_preset` | 梅／竹／松のプリセットを押した |
| `calc_result` | 選び終えて金額が出た（連打しても最後の1回だけ） |
| `calc_cta` | 相談ボタン／コピーを押した |
| `calc_leave` | 離脱時の最終状態（`used` / `cta` と、そのときの金額） |

**見たいのは `calc_use` と `calc_cta` の人数の差**。これが価格で落ちた人数。
`calc_leave` に離脱時の `total` が入るので、
「松を見た人だけが落ちている」のか「全体的に落ちている」のかまで分かる。
前者なら松だけ下げればよく、料金表全体を下げる必要はない。

### 送信先

`index.html` の `const ANALYTICS_ID = '';` に GA4 の測定ID（`G-` で始まる）を入れると送信が始まる。
**空のままなら外部には一切送らず、Cookie も置かない**（e2e で固定してある）。

記録はどちらの場合も localStorage に残るので、
ブラウザの開発者コンソールで `soFunnel()` と打てば手元で確認できる。

### 入れる前に

外部送信を有効にすると Cookie が置かれる。**先にプライバシーポリシーを用意すること。**
いまサイトには置いていない。

## 会社概要

`about.html` の `#company` に置いてある。ヘッダーとメニューの「会社概要」からここへ飛ぶ。

**いまは個人事業主としての表記**（屋号／代表者／開業）。法人化したら次のように差し替える。

| いまの行 | 法人化後 |
|----------|----------|
| 屋号 `so.` | 商号（登記した正式名称） |
| 代表者 | 代表取締役 |
| 開業 | 設立 |
| —— | 資本金（行を追加） |
| —— | 法人番号（行を追加） |

### まだ載せていない項目

- **開業（年月）** — 未開業のため行ごと出していない。開業したら
  `about.html` の `#company` にあるコメントを戻す（位置は代表者の下）。
  あわせて JSON-LD に `foundingDate` を足す。
- **郵便番号・番地** — 所在地は**市区町村まで**とする方針。自宅の特定を避けるため、
  表示にも JSON-LD にも入れない。
- **インボイス登録番号** — 未取得。取得したら連絡先の下に行を足す。

未記入のプレースホルダ（`◯◯`／`（氏名）`／`000-0000` など）を
そのまま公開してしまわないよう、e2e で落ちるようにしてある。

### 構造化データ

`index.html` の JSON-LD（`Organization`）に、代表者・電話番号・所在地を入れてある。
**画面の会社概要と同じ値であることを e2e で突き合わせている**ので、
片方だけ直すと落ちる。値を変えるときは両方を直すこと。

まだ確定していない `foundingDate` / `postalCode` / `streetAddress` は入れない。
検索結果に誤った所在地が出るのを避けるため。

### 特定商取引法に基づく表記

いまは置いていない。サイトから直接申し込み・決済を受ける形にする場合は必要になるので、
そのタイミングで別ページとして用意する。

## ロゴ

シンボルマーク（五弁の花）＋社名の組み。マークはヘッダー・フッターとも
社名の左に置き、大きさは文字サイズに連動する（`1.62em`）ので画面幅が変わっても比率は崩れない。
社名の「.」は録画ボタンの ● として **CSS の図形**で描いており（`.logo-dot`）、
文字のピリオドに戻すとロゴでなくなる。どちらも e2e で固定してある。

| ファイル | 用途 |
|----------|------|
| `assets/logo-mark.png` | シンボルマーク。サイトが読むのはこれ（256px・減色済み 31KB） |
| `assets/logo-full.png` | 茎まで入った原寸版（401px）。大きく使うとき |
| `assets/logo-lockup.png` | マーク＋社名の組み。明るい地に置く用 |
| `assets/logo-lockup-dark.png` | 同上、暗い地に置く用（社名が白・点が明るい金） |
| `assets/logo-word.svg` / `logo-word-dark.svg` | 社名だけのワードマーク。文字はアウトライン化済み |
| `assets/favicon.png` | ブラウザのタブ（64px・透過） |
| `assets/apple-touch-icon.png` | iOS のホーム画面用（180×180）。透過は黒く潰れるので白地を敷いてある |
| `assets/ogp.png` | SNS 共有時の画像（1200×630） |
| `assets/src/logo-original.jpg` | **元の絵**。ここから全部を作り直せる |

### 作り直すとき

```
npm i sharp
node scripts/build-logo.mjs
```

元は白地の JPEG なので、外周から塗りつぶして「地に繋がっている白」だけを透明にしている。
単純な白キーだと花の内側の淡い色まで穴が開くため。しきい値は暗い地でモヤが出ない値にしてある。

`ogp.png` と `logo-lockup*.png` は `assets/src/` の HTML をブラウザで開いて
書き出したもの（OGP は 1200×630、組みロゴは要素だけを背景透過で切り出し）。

### 小さくしたときの見え方

元の絵は細かい渦と細い輪郭でできているので、**30px 以下ではディテールが溶けて
「色のついた花」までしか読めない**。ヘッダー（約 32px）とファビコン（16〜32px）は
その前提で使っている。単色印刷やスタンプなど、色が使えない場面で必要になったら
別途シルエット版を起こす必要がある。

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
