#!/usr/bin/env python3
"""案件の内容から、似たプロの完成CMを実例として引く。

なぜ作るか：1,274本を平均して「暖色・寄り・フィックス」という作風プロンプトを
作ると、日本のCMほぼ全部に当てはまる記述にしかならず、出てくる映像は凡庸になる
（2026-09-02 本人の指摘：「定型文みたいな動画にならない？」）。
平均ではなく【個別の実例】を毎回引く。実例は具体的なので凡庸を生まない。

母集団は `history/finished_videos.json`（`pick_finished_videos.py` が選んだ
プロ品質の完成映像1,274本）。自作・他社は問わない。プロの仕事であればよい。

似ているかの判定は文字bigramのTF-IDFコサイン類似度。日本語の分かち書きが
要らず、APIも使わないので即答できる（母数が千本規模なら十分な精度）。

使い方：
  python3 tools/find_reference.py "30秒 食品 家族 屋内 あたたかい"
  python3 tools/find_reference.py "工場 職人 手元" --dur 30 -n 5
  python3 tools/find_reference.py "化粧品 女性 寄り" --full   # 読み取り全文
"""
import argparse
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / "history" / "finished_videos.json"
EMB = BASE / "history" / "finished_embeddings.json"
LEDGER = BASE / "history" / "hdd_all_analysis.jsonl"


def _embed_query(q):
    """質問文を埋め込みに変換。索引が無ければ None を返して bigram に落とす。"""
    if not EMB.exists():
        return None, None
    import os
    env = BASE / ".env"
    if env.exists() and not os.getenv("GEMINI_API_KEY"):
        for ln in env.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln.startswith("GEMINI_API_KEY=") and "=" in ln:
                os.environ.setdefault("GEMINI_API_KEY", ln.split("=", 1)[1].strip())
    try:
        from google import genai
        model = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-2")
        # クライアントは変数に保持する。式の中で使い捨てにすると、応答を
        # 読む前に破棄されて "client has been closed" になる。
        cl = genai.Client()
        r = cl.models.embed_content(model=model, contents=[q])
        qv = list(r.embeddings[0].values)
    except Exception as e:  # noqa: BLE001
        print(f"（埋め込み検索が使えないので文字bigramで探します: {str(e)[:80]}）")
        return None, None
    vecs = {r["path"]: r["vec"] for r in
            json.loads(EMB.read_text(encoding="utf-8"))}
    return qv, vecs


def _cos(a, b):
    s = na = nb = 0.0
    for x, y in zip(a, b):
        s += x * y
        na += x * x
        nb += y * y
    return s / (math.sqrt(na) * math.sqrt(nb) or 1.0)


def bigrams(text):
    """日本語は分かち書きせず文字bigramで見る（MeCab等の依存を持たないため）。"""
    t = re.sub(r"\s+", "", text)
    return [t[i:i + 2] for i in range(len(t) - 1)]


def build(docs):
    df = Counter()
    tfs = []
    for d in docs:
        tf = Counter(bigrams(d["_text"]))
        tfs.append(tf)
        df.update(tf.keys())
    n = len(docs)
    idf = {g: math.log(n / (1 + c)) + 1 for g, c in df.items()}
    vecs = []
    for tf in tfs:
        v = {g: (1 + math.log(c)) * idf.get(g, 0) for g, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append({g: x / norm for g, x in v.items()})
    return idf, vecs


def query_vec(q, idf):
    tf = Counter(bigrams(q))
    v = {g: (1 + math.log(c)) * idf.get(g, 0) for g, c in tf.items() if g in idf}
    norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
    return {g: x / norm for g, x in v.items()}


# カット数（編集のリズム）はここでは出さない。
# EDLは3案件（メナード144本・猪木モンスト68本・不二家2本）にしか無く、
# しかも `event_count=0` にパースされているものが多い＝パーサが実際の
# EDL形式に合っていない。案件全体の中央値を個々の動画の値のように
# 見せていたので外した（2026-09-02）。直すならEDLパーサから。
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("query", help="案件の内容（例: 30秒 食品 家族 屋内）")
    ap.add_argument("-n", type=int, default=5, help="出す件数")
    ap.add_argument("--dur", type=float, default=0, help="尺で絞る（±2秒）")
    ap.add_argument("--root", default="", help="ルート名で絞る")
    ap.add_argument("--full", action="store_true", help="読み取りを全文表示")
    ap.add_argument("--bigram", action="store_true",
                    help="埋め込みを使わず文字bigramで探す（APIを叩かない）")
    args = ap.parse_args()

    if not SRC.exists():
        print(f"母集団がありません: {SRC}\n"
              f"先に tools/pick_finished_videos.py を実行してください。")
        return
    docs = json.loads(SRC.read_text(encoding="utf-8"))
    for d in docs:
        d["_text"] = (d.get("vision") or "") + " " + (d.get("rel") or "")
    if args.dur:
        docs = [d for d in docs if d.get("dur_sec")
                and abs(d["dur_sec"] - args.dur) <= 2]
    if args.root:
        docs = [d for d in docs if args.root in d.get("root", "")]
    if not docs:
        print("条件に合う実例がありません。--dur / --root を緩めてください。")
        return

    # 埋め込みがあれば意味で探す（語彙が違っても拾える）。無ければ文字bigram。
    qvec, evecs = (None, None) if args.bigram else _embed_query(args.query)
    if qvec and evecs:
        how = "意味で検索（埋め込み）"
        scored = [(_cos(qvec, evecs[d["path"]]), d)
                  for d in docs if d["path"] in evecs]
        if not scored:                      # 索引が母集団とずれている
            qvec = None
    if not qvec:
        how = "語の一致で検索（文字bigram）"
        idf, vecs = build(docs)
        qv = query_vec(args.query, idf)
        scored = [(sum(x * v.get(g, 0) for g, x in qv.items()), d)
                  for d, v in zip(docs, vecs)]
    scored.sort(key=lambda x: x[0], reverse=True)

    print(f"「{args.query}」に近い実例 上位{args.n}件"
          f"（母集団 {len(docs)} 本 / {how}）\n")
    for i, (s, d) in enumerate(scored[:args.n], 1):
        dur = d.get("dur_sec")
        wh = f"{d.get('width')}x{d.get('height')}" if d.get("width") else "?"
        print(f"── {i}. 類似度 {s:.3f} ─────────────────────────────")
        print(f"   {d['root']} / {d['rel']}")
        print(f"   尺 {dur and round(dur, 1)}秒 / {wh}")
        v = d.get("vision") or ""
        print("   " + ("\n   ".join(v.splitlines()) if args.full
                       else "\n   ".join(v.splitlines()[:8])))
        print()


if __name__ == "__main__":
    main()
