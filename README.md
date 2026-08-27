# so. — Portfolio

映像ポートフォリオサイト(静的HTML / GitHub Pages)。
公開URL: https://koheisuzuki0626-coder.github.io/so-portfolio/

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

このサイトは**スマホだけで編集・公開まで完結**します。PC は開いたときに追いつかせるだけで構いません。

```
スマホの Claude アプリ → Code タブ → セッションを開いて指示
      ↓
クラウド上で編集し、GitHub にコミット＆プッシュ
      ↓
main に入った時点で GitHub Pages が公開          (数十秒〜数分)
      ↓
PC を開いたときに git pull  ← ここで初めて PC が追いつく
```

**PC とスマホを繋いでいるのは GitHub です。** リアルタイムに繋がっているわけではないので、
PC 側は開いたときに一度だけ次を実行してください。

```bash
cd ~/so-portfolio      # リポジトリを置いた場所
git pull origin main
```

`Already up to date.` と出れば、PC はスマホでやった内容に追いついています。

### 気をつける点

- **作業ブランチのままだとサイトには出ません。** 公開されるのは `main` の内容だけです。
  スマホから作業したときは、最後に main へ入れるところまで済ませてください
- PC 側でも編集した場合は、先に `git push` してからスマホで作業を始めてください。
  両方で別々に編集すると衝突します(スマホ側に寄せるのが楽です)
- Claude Code の Remote Control(PC のセッションをスマホから操作する機能)は、
  PC で `claude` を起動している間しか使えません。スマホ中心の使い方には不要です
