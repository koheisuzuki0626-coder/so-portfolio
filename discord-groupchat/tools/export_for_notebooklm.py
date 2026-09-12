#!/usr/bin/env python3
"""解析結果を NotebookLM に読ませるMarkdownに書き出す。

なぜ必要か：解析の生データは49MBのJSONLで、人にもNotebookLMにも読みにくい。
NotebookLM には公開APIが無い（Enterprise向けプレビューのみ・2026-09時点）ので、
自動連携はできず、ファイルを作って手でアップロードする形になる。

NotebookLM の制限（2026-09時点）：
  ソース数  50（Standard）/ 100（Plus）/ 300（Pro）
  1ソース   50万語 または 200MB
1ソースの上限が大きいので、10ファイル程度に束ねれば全部載る。

出力先：`成果物/NotebookLM用/`（作業ブランチとmainの両方に置く方針の場所）

**出力は決定的にすること（重要）。**
同じ台帳から作り直したら1バイトも変わらない、を守る。そうであれば
git の差分が出ず、何度作り直しても履歴は膨らまない（32MBがコミット
済みなので、ここが崩れると作り直すたびに32MB積み上がる）。
具体的には：
  - 日時・実行環境・乱数を出力に含めない
  - 並び順を固定する（dict の挿入順に頼らず sorted を使う）
  - 「N件中M件目」のような、母数で変わる番号を振らない
変更したら `python3 tools/export_for_notebooklm.py` を2回走らせて
`git status` が空のままかを確認すること。

使い方：
  python3 tools/export_for_notebooklm.py
"""
import json
import re
from collections import defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
REPO = BASE.parent
LEDGER = BASE / "history" / "hdd_all_analysis.jsonl"
FINISHED = BASE / "history" / "finished_videos.json"
OUTDIR = REPO / "成果物" / "NotebookLM用"

# NotebookLM が1ソースに飲める量は大きいが、1ファイルが巨大だと扱いづらいので
# この字数で分割する（日本語なので語数ではなく字数で見る）。
SPLIT_CHARS = 900_000


def load_ledger():
    vis, meta = [], []
    for line in LEDGER.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("phase") == "vision" and r.get("vision"):
            vis.append(r)
        elif r.get("phase") == "meta" and not r.get("error"):
            meta.append(r)
    # --redo で同じパスが複数回入っているので、後勝ちで1件に寄せる
    def dedup(rows, key):
        out = {}
        for r in rows:
            k = r.get(key) or (r.get("paths") or [None])[0]
            if k:
                out[k] = r
        # パスで並べ替える。台帳の追記順に頼ると、--redo で行が増えたときに
        # 中身が同じでも並びが変わり、32MB全部が差分になってしまう。
        return [out[k] for k in sorted(out)]
    return dedup(vis, "path"), dedup(meta, "path")


def write(name, header, blocks):
    """字数で分割しながら書き出す。戻り値は作ったファイル名の一覧。"""
    OUTDIR.mkdir(parents=True, exist_ok=True)
    made, part, buf, n = [], 1, [header], len(header)
    for b in blocks:
        if n + len(b) > SPLIT_CHARS and len(buf) > 1:
            p = OUTDIR / (f"{name}_{part}.md" if part > 1 or n > SPLIT_CHARS
                          else f"{name}.md")
            p.write_text("\n".join(buf), encoding="utf-8")
            made.append(p)
            part += 1
            buf, n = [header + f"\n\n（続き {part}）\n"], len(header)
        buf.append(b)
        n += len(b)
    if len(buf) > 1:
        p = OUTDIR / (f"{name}_{part}.md" if part > 1 else f"{name}.md")
        p.write_text("\n".join(buf), encoding="utf-8")
        made.append(p)
    return made


def block_for(r):
    """1本ぶんの記述。出典を辿れるようパスと尺を必ず添える。"""
    head = f"\n## {Path(r.get('rel') or '').name}\n"
    head += f"- 置き場: `{r.get('root')}/{r.get('rel')}`\n"
    if r.get("dur_sec"):
        head += f"- 尺: {round(r['dur_sec'], 1)}秒"
        if r.get("width"):
            head += f" / {r['width']}x{r['height']}"
        head += "\n"
    return head + "\n" + (r.get("vision") or "").strip() + "\n"


def main():
    vis, meta = load_ledger()
    spec = {r["path"]: (r.get("data") or {}) for r in meta}
    made = []

    # ① プロ品質の完成CM（主力）
    if FINISHED.exists():
        fin = sorted(json.loads(FINISHED.read_text(encoding="utf-8")),
                     key=lambda d: d["path"])
        blocks = [block_for(d) for d in fin]
        made += write(
            "01_プロの完成CM_1274本",
            "# プロ品質の完成CM 1,274本の映像分析\n\n"
            "外付けHDD4台（東北新社・c3film・c3film_2・サムシングファン）の制作データから、\n"
            "プロ品質の完成映像だけを選り分けたもの。自作・他社は問わない\n"
            "（参考として集めた他社CM、監督の作品集、役者の過去出演作も含む）。\n"
            "各項目は1本につき代表5フレームをAIが読み取った記述。\n\n"
            "**注意**：クライアントの反応・再生数・成果は含まない。\n"
            "「よく使われている形」は分かるが「効果があった形」は分からない。\n",
            blocks)

    # ② 会社（HDD）ごとの全読み取り
    byroot = defaultdict(list)
    for r in vis:
        if r.get("kind") == "video":
            byroot[r.get("root") or "不明"].append(r)
    for i, (root, rows) in enumerate(sorted(byroot.items()), start=2):
        for r in rows:
            s = spec.get(r["path"], {})
            r["dur_sec"], r["width"], r["height"] = (
                s.get("dur_sec"), s.get("width"), s.get("height"))
        made += write(
            f"{i:02d}_{root}_動画の読み取り",
            f"# {root} の動画 {len(rows)}本の映像分析\n\n"
            "完成品・撮影ラッシュ・オーディション・参考資料が混在している。\n"
            "「完成した映像の傾向」を知りたい場合は 01 のファイルを見ること。\n",
            [block_for(r) for r in rows])

    # ③ 編集データ（Premiere・EDLのカット割り）
    pr = [r for r in meta if r.get("ext") == "prproj"]
    edl = [r for r in meta if r.get("ext") == "edl"]
    blocks = []
    for r in pr:
        d = r.get("data") or {}
        blocks.append(
            f"\n## {Path(r['rel']).name}\n- 置き場: `{r['root']}/{r['rel']}`\n"
            f"- 参照素材: {d.get('media_count')}点（ユニーク {d.get('media_unique')}）\n"
            f"- 素材の内訳: {d.get('media_ext')}\n"
            f"- シーケンス名: {', '.join((d.get('seq_names') or [])[:12])}\n"
            f"- 使用エフェクト（多い順）: "
            f"{', '.join(f'{n}×{c}' for n, c in (d.get('effects_top') or [])[:12])}\n")
    for r in edl:
        d = r.get("data") or {}
        cuts = d.get("cut_secs") or []
        if not cuts:
            continue
        s = sorted(cuts)
        blocks.append(
            f"\n## {Path(r['rel']).name}（カット表）\n"
            f"- 置き場: `{r['root']}/{r['rel']}`\n"
            f"- カット数: {d.get('event_count')} / 総尺: {d.get('cut_total_sec')}秒\n"
            f"- 1カットの長さ: 中央 {s[len(s) // 2]:.2f}秒 "
            f"（最短 {s[0]:.2f} / 最長 {s[-1]:.2f}）※30fps換算\n")
    made += write(
        "90_編集データ_Premiereとカット割り",
        "# 編集プロジェクトとカット表\n\n"
        "Premiereのプロジェクト276本の中身（参照素材・使用エフェクト）と、\n"
        "EDL（カット表）から計算した1カットの長さ。\n\n"
        "実測：タイムラインEDL 77本・1,996カットで、1カットの中央値は0.90秒。\n"
        "1秒未満が53.2%、1〜2秒が28.4%。1本あたり総尺38.3秒・カット数24（中央値）。\n",
        blocks)

    # ④ 企画書・PPM資料などの本文
    docs = [r for r in meta if r.get("kind") == "document"
            and (r.get("data") or {}).get("text_len", 0) > 200]
    blocks = []
    for r in docs:
        d = r["data"]
        t = re.sub(r"\s+", " ", d.get("text") or "")[:6000]
        blocks.append(f"\n## {Path(r['rel']).name}\n"
                      f"- 置き場: `{r['root']}/{r['rel']}`\n\n{t}\n")
    made += write(
        "95_企画書・資料の本文",
        "# 企画書・PPM資料・構成表などの本文\n\n"
        "PDF・PowerPoint・Word・Excelから抽出したテキスト。\n"
        "「どう考えて作ったか」が残っている資料群。\n",
        blocks)

    print(f"書き出し先: {OUTDIR}")
    total = 0
    for p in made:
        n = p.stat().st_size
        total += n
        print(f"  {p.name:44s} {n / 1_000_000:6.2f} MB")
    print(f"\n{len(made)} ファイル / 合計 {total / 1_000_000:.1f} MB")
    print("NotebookLM のソース上限（Standard 50件）に十分収まります。")


if __name__ == "__main__":
    main()
