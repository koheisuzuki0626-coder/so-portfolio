#!/usr/bin/env python3
"""実例検索用の埋め込みベクトルを作る（1回だけ実行すればよい）。

なぜ必要か：`find_reference.py` の文字bigram検索は語彙が一致しないと拾えない
（「食品」で引くと「グルメ」「飲料」の案件が出てこない）。埋め込みなら
意味が近ければ拾えるので、案件の説明をそのまま投げられる。

母集団は `history/finished_videos.json`（プロ品質の完成映像1,274本）。
1,274本ぶんの埋め込みは数セント程度。生成に比べて桁違いに安い。

出力：`history/finished_embeddings.json`（パスとベクトルの対）

使い方：
  python3 tools/build_reference_index.py            # 未作成のぶんだけ
  python3 tools/build_reference_index.py --redo     # 全部作り直す
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
SRC = BASE / "history" / "finished_videos.json"
OUT = BASE / "history" / "finished_embeddings.json"
MODEL = os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-2")
BATCH = 20


def client():
    env = BASE / ".env"
    if env.exists() and not os.getenv("GEMINI_API_KEY"):
        for ln in env.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln.startswith("GEMINI_API_KEY=") and "=" in ln:
                os.environ.setdefault("GEMINI_API_KEY", ln.split("=", 1)[1].strip())
    from google import genai
    return genai.Client()


def doc_text(d):
    """埋め込みに渡す文。読み取り本文＋パス（案件名に情報があるため）。"""
    return ((d.get("vision") or "")[:4000] + "\n" + (d.get("rel") or ""))[:4500]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--redo", action="store_true")
    args = ap.parse_args()

    if not SRC.exists():
        print(f"母集団がありません: {SRC}")
        sys.exit(1)
    docs = json.loads(SRC.read_text(encoding="utf-8"))
    have = {}
    if OUT.exists() and not args.redo:
        have = {r["path"]: r["vec"] for r in json.loads(
            OUT.read_text(encoding="utf-8"))}
    todo = [d for d in docs if d["path"] not in have]
    print(f"母集団 {len(docs)} 本 / 既存 {len(have)} / 作成 {len(todo)}"
          f"（モデル {MODEL}）")
    if not todo:
        print("作るものがありません。")
        return

    c = client()
    made = 0
    t0 = time.time()
    for i in range(0, len(todo), BATCH):
        chunk = todo[i:i + BATCH]
        texts = [doc_text(d) for d in chunk]
        try:
            resp = c.models.embed_content(model=MODEL, contents=texts)
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {i}〜: {str(e)[:140]}")
            time.sleep(3)
            continue
        for d, emb in zip(chunk, resp.embeddings):
            have[d["path"]] = list(emb.values)
            made += 1
        if (i // BATCH) % 5 == 0:
            el = time.time() - t0
            print(f"  {made}/{len(todo)} 済み（経過 {el:.0f}s）")

    OUT.write_text(json.dumps(
        [{"path": p, "vec": v} for p, v in have.items()],
        ensure_ascii=False), encoding="utf-8")
    print(f"\n完了。{made} 本を作成 / 合計 {len(have)} 本 → {OUT}")


if __name__ == "__main__":
    main()
