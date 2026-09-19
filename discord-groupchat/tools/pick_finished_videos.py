#!/usr/bin/env python3
"""解析済みの動画から「完パケ（納品した完成品）」らしいものを選り分ける。

なぜ必要か：`analyze_hdd_all.py` が読み取った動画9,348本には、
オーディションのNGテイク・撮影ラッシュ・Vコン・Premiereのレンダーキャッシュが
大量に混ざっている。そのまま頻度集計すると「三脚に据えたオーディション素材」の
傾向を自分の作風だと誤認する（実際に一度そう報告してしまった・2026-09-02）。

作風のプロファイルは「納品した完成品」だけで取らないと意味がない。

判別は1つの決定的な手がかりが無いので、複数の信号を足して閾値で切る：

  +3  パスに 完パケ/白完/納品/MASTER/本編/OA
  -4  パスに オーディション/素材/ラッシュ/Vコン/Preview/カメラ/テスト/NG
  +2  尺が規格尺（15/30/60/90/120秒 と 6/7秒）の±0.7秒
  +1  解像度が 1920x1080 か 3840x2160
  +1  音声トラックがある
  +1  同じフォルダの動画が5本以下
  -1  同じフォルダの動画が21本以上

閾値は既定4。`--threshold` で動かせる。`--sample` で判定結果を目視できる。

出力：`history/finished_videos.json`（完パケと判定したものの一覧）
      同時に、その集合だけで取り直した作風の頻度集計を標準出力に出す。

使い方：
  python3 tools/pick_finished_videos.py --sample 20   # 判定を目で確かめる
  python3 tools/pick_finished_videos.py               # 確定して書き出す
"""
import argparse
import collections
import json
import re
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LEDGER = BASE / "history" / "hdd_all_analysis.jsonl"
OUT = BASE / "history" / "finished_videos.json"

# 集めたいのは「プロ品質の完成映像」であって「自分の作品」ではない
# （2026-09-02 本人の判断）。他社CM・監督の作品集・役者の過去出演作も、
# すべて完成したプロの仕事なので採用する。実際、自分の納品物と明確に分かる
# ものは十数本しか無く、自作だけに絞ると作風分析の母数にならなかった。
# 手がかりは「ファイル名」と「置き場（親フォルダ）」で分けて見る。
# 案件フォルダ名に "TVCM" や "PV" が入っていることが多く、パス全体で
# 拾うとその案件の全ファイルが該当してしまう（実測：モデルのプライベート
# 動画4.3秒まで完パケ判定された・2026-09-02）。
NAME_RE = re.compile(
    r"完パケ|白完|納品|MASTER|マスター|篇|CM|PV|本編", re.I)
FOLDER_RE = re.compile(
    r"参考CM|演出参考|映像資料|参考資料|作品集|過去CM|過去出演|"
    r"出演動画|完パケ|納品", re.I)
FINISHED_RE = NAME_RE          # 互換のため残す（score 内では使い分ける）
# 素材・中間物だけを落とす。ここに「参考」「資料」を入れてはいけない
# （入れていた時は、拾いたい他社CMを全部捨てていた）。
RAW_RE = re.compile(
    r"撮影データ|確認用|Vコン|Video Previews|pvw|ラッシュ|rush|"
    r"テスト|test|NG\d|没|ボツ|検討|バックアップ|backup|"
    r"検証|小道具|ロケハン|オフライン|プレビュー|素材/|/素材|demo|"
    # 購入前のストック素材（透かし入りプレビュー）。完成映像ではない。
    r"AdobeStock|MotionElements|gettyimages|shutterstock|_Preview", re.I)
# 6〜7秒は偶然その長さのクリップが多く、規格尺の手がかりにならない
# （実測：オーディション素材が大量に引っかかった）。CMの実尺だけに絞る。
STD_DURATIONS = [15, 30, 60, 90, 120]
STD_RES = {(1920, 1080), (3840, 2160)}


def load():
    meta, vis = {}, []
    for line in LEDGER.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if r.get("phase") == "meta" and not r.get("error") and r.get("path"):
            meta[r["path"]] = r.get("data") or {}
        elif (r.get("phase") == "vision" and r.get("vision")
              and r.get("kind") == "video"):
            vis.append(r)
    return meta, vis


def score(rec, spec, folder_n):
    s = 0
    why = []
    rel = rec.get("rel", "")
    p = Path(rel)
    if NAME_RE.search(p.name):
        s += 2
        why.append("名前が完成物")
    if FOLDER_RE.search(str(p.parent)):
        s += 3
        why.append("完成物の置き場")
    if RAW_RE.search(rel):
        s -= 4
        why.append("素材語")
    d = spec.get("dur_sec")
    if d and any(abs(d - x) <= 0.4 for x in STD_DURATIONS):
        s += 3
        why.append(f"規格尺{d:.0f}s")
    # 10秒未満は完成したCMではない（バンパー6秒はあるが、素材クリップと
    # 見分けが付かないので取らない）。プロ品質の母数を汚す方が損。
    if d and d < 10:
        s -= 3
        why.append("10秒未満")
    if (spec.get("width"), spec.get("height")) in STD_RES:
        s += 1
        why.append("標準解像度")
    if spec.get("acodec"):
        s += 1
        why.append("音声あり")
    if folder_n <= 5:
        s += 1
        why.append("同フォルダ少")
    elif folder_n >= 21:
        s -= 1
        why.append("同フォルダ多")
    return s, why


KW = {
    "テロップ": r"テロップ(?!.{0,10}(無し|なし))", "テロップ無": r"テロップ.{0,10}(無し|なし)",
    "フィックス": r"フィックス", "パン": r"パン", "ズーム": r"ズーム", "手持ち": r"手持ち",
    "空撮": r"空撮|ドローン", "寄り": r"寄り|バストアップ|クローズアップ",
    "引き": r"引き|ロングショット", "屋内": r"屋内", "屋外": r"屋外",
    "暖色": r"暖色", "寒色": r"寒色", "日の丸": r"日の丸", "三分割": r"三分割",
    "対称": r"対称", "人物": r"人物", "製品": r"製品|商品", "ロゴ": r"ロゴ",
    "建物": r"建物", "設備": r"設備", "風景": r"風景",
    "インタビュー": r"インタビュー", "CG": r"CG|モーショングラフィック",
}


def tally(recs, label):
    txt = " ".join(r["vision"] for r in recs)
    print(f"\n=== {label}（{len(recs)}本）===")
    base = len(re.findall(KW["人物"], txt)) or 1
    for k, p in KW.items():
        n = len(re.findall(p, txt))
        print(f"  {k:9s} {n:6d}   （人物を100とした比 {n / base * 100:5.1f}）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--threshold", type=int, default=4)
    ap.add_argument("--sample", type=int, default=0, help="判定を目視する件数")
    args = ap.parse_args()

    meta, vis = load()
    folder_n = collections.Counter(str(Path(r["path"]).parent) for r in vis)
    scored = []
    for r in vis:
        spec = meta.get(r["path"], {})
        s, why = score(r, spec, folder_n[str(Path(r["path"]).parent)])
        scored.append((s, why, r, spec))

    fin = [x for x in scored if x[0] >= args.threshold]
    rest = [x for x in scored if x[0] < args.threshold]
    print(f"全 {len(scored)} 本 → 完パケ判定 {len(fin)} 本 / 除外 {len(rest)} 本"
          f"（閾値 {args.threshold}）")
    dist = collections.Counter(x[0] for x in scored)
    print("スコア分布:", sorted(dist.items(), reverse=True))

    if args.sample:
        print(f"\n--- 完パケ判定の例 {args.sample}件 ---")
        for s, why, r, spec in fin[:args.sample]:
            d = spec.get("dur_sec")
            print(f"  [{s}] {r['rel'][:78]}")
            print(f"       {'/'.join(why)}  尺{d and round(d, 1)}s")
        print(f"\n--- 除外した例 {args.sample}件 ---")
        for s, why, r, spec in rest[:args.sample]:
            print(f"  [{s}] {r['rel'][:78]}  {'/'.join(why) or '手がかり無し'}")
        return

    OUT.write_text(json.dumps(
        [{"path": r["path"], "rel": r["rel"], "root": r["root"], "score": s,
          "why": why, "dur_sec": spec.get("dur_sec"),
          "width": spec.get("width"), "height": spec.get("height"),
          "vision": r["vision"]}
         for s, why, r, spec in fin], ensure_ascii=False, indent=1),
        encoding="utf-8")
    print(f"\n書き出し: {OUT}")
    tally([x[2] for x in fin], "完パケだけの作風")
    tally([x[2] for x in rest], "除外分（素材・ラッシュ）")


if __name__ == "__main__":
    main()
