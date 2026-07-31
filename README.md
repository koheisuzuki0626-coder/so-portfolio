# so. — Portfolio

映像スタジオ「so.」のポートフォリオサイト(静的HTML / GitHub Pages)。

## YouTube 新着動画の自動掲載

YouTube に動画をアップすると、Works セクションに自動で並びます。**API キーは不要**です。

```
YouTube にアップ
      ↓
GitHub Actions が6時間おきに RSS を取得   (.github/workflows/sync-youtube.yml)
      ↓
data/videos.json を更新してコミット       (scripts/fetch-youtube.mjs)
      ↓
index.html が読み込んで Works を描画
```

### セットアップ(最初の1回だけ)

1. **`youtube.config.json` にチャンネルを設定する**

   ```json
   {
     "channel": "@your-channel-handle",
     "maxVideos": 6
   }
   ```

   `channel` に指定できる形式:

   | 形式 | 例 |
   |------|-----|
   | ハンドル | `@so-film` |
   | チャンネルID | `UC_x5XG1OV2P6uZZ5FSM9Ttw` |
   | チャンネルURL | `https://www.youtube.com/channel/UC_x5XG1OV2P6uZZ5FSM9Ttw` |

   `maxVideos` は掲載する最大件数(既定 6)。YouTube の RSS は**最新15件まで**しか返さないため、それ以上は指定できません。
   `note` は自由記述のメモで、スクリプトからは無視されます。

   ハンドルはあとから変更される可能性があるため、チャンネルID(`UC...`)で指定しておくのが確実です。

2. **Actions に書き込み権限を与える**

   リポジトリの **Settings → Actions → General → Workflow permissions** で
   **Read and write permissions** を選択。ワークフローが `data/videos.json` をコミットするために必要です。

3. **一度手動で走らせて確認する**

   **Actions → Sync YouTube videos → Run workflow**。
   成功すると `data/videos.json` が更新され、サイトに反映されます。

### 動作の詳細

- **更新頻度**: 6時間ごと(`cron: '17 */6 * * *'`)＋手動実行。すぐ反映したいときは手動実行してください。
- **失敗時**: 取得に失敗するとワークフローは赤くなり、`data/videos.json` は**書き換えません**。サイトは直前の内容を表示し続けます。
- **未設定・データ空のとき**: `index.html` に直接書かれたプレースホルダー(Project 01〜03)がそのまま表示されます。
- **サムネイル**: `maxresdefault.jpg` を試し、無い動画は `hqdefault.jpg` に自動で切り替わります。
- **再生**: カードをクリックするとページ内のライトボックスで再生します(`youtube-nocookie.com` 埋め込み)。JavaScript が無効な環境では YouTube のページに遷移します。

### ローカルでの確認

```bash
node scripts/fetch-youtube.mjs   # data/videos.json を更新
npx http-server -p 8777 .        # http://127.0.0.1:8777 で確認
```

`file://` で直接開くと `data/videos.json` の読み込みが CORS で失敗するため、必ず HTTP サーバー経由で確認してください。

### 手動で作品を差し替えたい場合

自動掲載を使わず手で管理したいときは、ワークフローを無効化(Actions 画面から Disable)して
`index.html` の `#works-grid` 内のカードを直接編集してください。
`data/videos.json` の `videos` が空のままなら、書いた内容がそのまま表示されます。
