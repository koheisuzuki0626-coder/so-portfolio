#!/usr/bin/env python3
"""外付けHDD4台分の制作データを、動画だけでなく全種別まとめて解析する。

なぜ別スクリプトか：`analyze_hdd_videos.py` は
`/Volumes/1/サムシングファン/02_その他/03_事例/元データ` の事例動画173本
決め打ちで、しかも `成果物/事例素材一覧.json` を入力にしている。今回の対象は
4ルート・53,882ファイル・全種別なので、入力の作り方から違う。既存側は
事例動画の解析として動いているので壊さない。

対象ルート（2026-09-02 に本人がDiscordで指定）：
  /Volumes/1/サムシングファン  /Volumes/1/東北新社
  /Volumes/1/c3film_2          /Volumes/2/c3film

2段階で回す（`--phase`）：
  meta   … APIを使わない。ffprobe / prproj / edl / pdf / pptx / docx / xlsx。
           枠を消費しないので一気に終わる。まずこれを全部終わらせる。
  vision … Geminiに画を読ませる。動画の代表フレーム・静止画・psd/ai。
           1日の無料枠で止まるので、台帳に進捗を残して翌日続きから。

中断再開：台帳 `history/hdd_all_analysis.jsonl` に1ファイル1行。キーは
実パス。既に成功している行は飛ばす。`--redo` で無視してやり直す。

AppleDouble（`._` で始まる殻ファイル）は必ず除外する。exFATの外付けでは
実体と同数ちかく作られ、拡張子だけで数えると件数が倍に見える（実際に
09-02 のカウントで 19,683 → 実体 10,046 と半分だった）。

使い方：
  python3 tools/analyze_hdd_all.py --index              # 索引だけ作る
  python3 tools/analyze_hdd_all.py --phase meta         # API不要の層を全部
  python3 tools/analyze_hdd_all.py --phase vision       # 画を読む層（枠まで）
  python3 tools/analyze_hdd_all.py --phase vision --limit 20   # 試走
  python3 tools/analyze_hdd_all.py --stats              # 進捗だけ表示

終了コード： 0=正常 / 2=HDD未マウント / 3=ffmpeg無し
"""
import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # discord-groupchat/
ROOTS = [
    Path("/Volumes/1/サムシングファン"),
    Path("/Volumes/1/東北新社"),
    Path("/Volumes/1/c3film_2"),
    Path("/Volumes/2/c3film"),
]
INDEX = BASE / "history" / "hdd_all_index.jsonl"
LEDGER = BASE / "history" / "hdd_all_analysis.jsonl"
LOCK = BASE / "history" / "hdd_all_analysis.lock"
FRAME_FRACTIONS = [0.08, 0.28, 0.50, 0.72, 0.90]

# 走査から外すディレクトリ（macOS・Windowsが作る管理領域。中身は制作データでない）
SKIP_DIRS = {".Spotlight-V100", ".Trashes", ".fseventsd", ".TemporaryItems",
             "System Volume Information", "$RECYCLE.BIN", ".DocumentRevisions-V100"}

VIDEO_EXT = {"mp4", "mov", "mxf", "mts", "mpeg", "mpg", "avi", "wmv", "m4v",
             "r3d", "m2ts", "mkv"}
IMAGE_EXT = {"jpg", "jpeg", "png", "tif", "tiff", "heic", "gif", "bmp", "webp"}
RAW_EXT = {"arw", "cr2", "nef", "dng", "raf"}
DESIGN_EXT = {"psd", "ai", "eps"}
DOC_EXT = {"pdf", "pptx", "docx", "xlsx", "xls", "doc", "ppt", "txt", "rtf", "key"}
AUDIO_EXT = {"wav", "mp3", "m4a", "aif", "aiff", "flac"}
PROJECT_EXT = {"prproj", "aep", "fcpxml", "edl", "aaf", "avb", "drp", "xml",
               "cdl", "rtn"}

# meta 段（APIを使わない）で扱う種別
META_KINDS = {"video", "audio", "document", "project"}
# vision 段（Geminiに画を読ませる）で扱う種別
VISION_KINDS = {"video", "image", "design"}

VISION_PROMPT_VIDEO = (
    "これは1本の映像作品（CM・企業VP等）から時系列順に抜いた代表フレームです。"
    "映像制作の学習材料にするため、次の項目を簡潔な箇条書きで日本語でまとめてください。"
    "推測が混じる項目は「推定」と付けること。\n"
    "- 被写体（人物/商品/設備/建物/風景/UI など。人物なら寄りか引きか）\n"
    "- 屋内か屋外か\n"
    "- 構図の傾向（日の丸/三分割/対称、余白の取り方）\n"
    "- カメラワークの推定（フィックス/パン/ズーム/手持ち/空撮）\n"
    "- テロップ（有無・位置・分量・キャッチコピーか字幕か）\n"
    "- 色調・ライティング（明るい/硬い/暖色寒色/コントラスト）\n"
    "- 冒頭に置いているもの（ロゴ/人/空撮/テロップ/製品）\n"
    "- ひとこと総評（この映像の作りの型を1〜2文で）"
)
VISION_PROMPT_STILL = (
    "これは映像制作の現場で作られた静止画（ロケハン写真・スチール・"
    "テロップ/サムネのデザイン等）です。学習材料にするため、次を簡潔な"
    "箇条書きで日本語でまとめてください。推測には「推定」と付けること。\n"
    "- 何の画か（ロケハン/物撮り/人物スチール/デザイン版下/UI/資料キャプチャ）\n"
    "- 被写体と構図\n"
    "- 文字要素（あれば内容・書体の印象・配置）\n"
    "- 色調・ライティング\n"
    "- 制作上どう使われたかの推定（ひとこと）"
)

# 静止画は1枚ずつ投げると枠を食い潰す（実測：無料枠は1日54件で尽きた・
# 2026-09-02）。同じフォルダの写真はまとめて1リクエストで読ませる。
# 同一ロケ・同一案件の写真は「その撮影の傾向」として束で読む方が、
# 学習材料としてもむしろ適切。
BATCH_STILLS = 10

VISION_PROMPT_STILL_BATCH = (
    "これは映像制作の現場で作られた静止画を、同じフォルダから{n}枚まとめて"
    "並べたものです（ロケハン写真・スチール・デザイン版下・資料など）。"
    "学習材料にするため、次の形で日本語でまとめてください。"
    "推測には「推定」と付けること。\n\n"
    "【全体】この束に共通する傾向を3〜5行で。\n"
    "- 何の画の集まりか（ロケハン/物撮り/人物スチール/デザイン版下/資料）\n"
    "- 被写体と構図の傾向、色調・ライティングの傾向\n"
    "- 制作上どう使われたかの推定\n\n"
    "【各画】1枚ずつ「1. 〜」の形で、1枚あたり1〜2行。"
    "被写体・構図・文字要素の有無だけ簡潔に。"
)


def _log(msg):
    print(msg, flush=True)


def _ext(p):
    n = p.name
    return n.rsplit(".", 1)[1].lower() if "." in n else ""


def _kind(ext):
    if ext in VIDEO_EXT:
        return "video"
    if ext in IMAGE_EXT:
        return "image"
    if ext in RAW_EXT:
        return "raw"
    if ext in DESIGN_EXT:
        return "design"
    if ext in DOC_EXT:
        return "document"
    if ext in AUDIO_EXT:
        return "audio"
    if ext in PROJECT_EXT:
        return "project"
    return "other"


# ---------- 索引づくり ----------

def build_index(force=False):
    """4ルートを歩いて1ファイル1行の索引を作る。AppleDoubleと管理領域は除く。"""
    if INDEX.exists() and not force:
        n = sum(1 for _ in INDEX.open(encoding="utf-8"))
        _log(f"索引は既にあります（{n} 件）。作り直すなら --index --force")
        return
    INDEX.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    skipped_ad = 0
    with INDEX.open("w", encoding="utf-8") as out:
        for root in ROOTS:
            if not root.is_dir():
                _log(f"（未マウント・飛ばす: {root}）")
                continue
            _log(f"走査中: {root}")
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
                for fn in filenames:
                    # AppleDouble の殻。実体と紛らわしいので必ず落とす。
                    if fn.startswith("._"):
                        skipped_ad += 1
                        continue
                    if fn in (".DS_Store", "Icon\r"):
                        continue
                    p = Path(dirpath) / fn
                    try:
                        st = p.stat()
                    except OSError:
                        continue
                    ext = _ext(p)
                    out.write(json.dumps({
                        "path": str(p),
                        "root": root.name,
                        "rel": str(p.relative_to(root)),
                        "ext": ext,
                        "kind": _kind(ext),
                        "size": st.st_size,
                        "mtime": int(st.st_mtime),
                    }, ensure_ascii=False) + "\n")
                    n += 1
    _log(f"索引 {n} 件（AppleDouble {skipped_ad} 件を除外）→ {INDEX}")


def load_index():
    if not INDEX.exists():
        _log("索引がありません。先に --index を実行してください。")
        sys.exit(1)
    rows = []
    for line in INDEX.open(encoding="utf-8"):
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


# ---------- 台帳 ----------

def load_done(phase):
    """その段で既に成功しているパスの集合。"""
    done = set()
    if not LEDGER.exists():
        return done
    for line in LEDGER.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("phase") == phase and not rec.get("error"):
            # 1レコードが複数ファイルを束ねることがある（静止画のまとめ読み）。
            # その場合 paths に全員が入っているので、全員を済み扱いにする。
            if rec.get("paths"):
                done.update(rec["paths"])
            elif rec.get("path"):
                done.add(rec["path"])
    return done


_LEDGER_LOCK = threading.Lock()


def append_ledger(rec):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    # 並列で回すので、1行が混ざらないよう直列化する。
    with _LEDGER_LOCK:
        with LEDGER.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# ---------- meta 段：APIを使わない抽出 ----------

def probe_media(path):
    """ffprobe で尺・解像度・コーデック・fps を取る。"""
    cmd = ["ffprobe", "-v", "error", "-print_format", "json",
           "-show_format", "-show_streams", str(path)]
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=120)
    except subprocess.TimeoutExpired:
        return None, "ffprobe timeout"
    if r.returncode != 0:
        return None, f"ffprobe rc={r.returncode}: {r.stderr.decode(errors='replace')[:120]}"
    try:
        d = json.loads(r.stdout.decode(errors="replace"))
    except ValueError:
        return None, "ffprobe の出力が読めない"
    fmt = d.get("format", {})
    out = {"dur_sec": float(fmt.get("duration") or 0) or None,
           "bitrate": fmt.get("bit_rate")}
    for s in d.get("streams", []):
        if s.get("codec_type") == "video" and "width" not in out:
            out["width"] = s.get("width")
            out["height"] = s.get("height")
            out["vcodec"] = s.get("codec_name")
            fr = s.get("r_frame_rate") or "0/1"
            try:
                a, b = fr.split("/")
                out["fps"] = round(float(a) / float(b), 3) if float(b) else None
            except (ValueError, ZeroDivisionError):
                out["fps"] = None
        elif s.get("codec_type") == "audio" and "acodec" not in out:
            out["acodec"] = s.get("codec_name")
            out["channels"] = s.get("channels")
            out["sample_rate"] = s.get("sample_rate")
    if out.get("width") and out.get("height"):
        w, h = out["width"], out["height"]
        out["orient"] = "縦" if h > w else ("正方" if h == w else "横")
    return out, None


_XML_TAG = re.compile(rb"<([A-Za-z][\w:.-]*)")


def parse_prproj(path):
    """Premiere のプロジェクト（gzip圧縮XML）から編集の骨格を拾う。

    完全なパースはしない（スキーマが巨大で版によって変わる）。狙いは
    「どんな素材を・いくつ・どんなエフェクトで組んだか」の傾向値。
    """
    try:
        with gzip.open(path, "rb") as f:
            raw = f.read(60 * 1024 * 1024)      # 上限60MB（巨大プロジェクト対策）
    except OSError as e:
        return None, f"prproj を開けない: {str(e)[:100]}"
    txt = raw.decode("utf-8", "replace")
    # 参照している素材ファイル名
    media = re.findall(r"<ActualMediaFilePath>(.*?)</ActualMediaFilePath>", txt)
    names = []
    for m in media:
        m = m.split("/")[-1].split("\\")[-1]
        if m:
            names.append(m)
    seq = re.findall(r"<Name>(.*?)</Name>", txt)
    # 使っているエフェクト（MatchName に Premiere の内部名が入る）
    effects = re.findall(r"<MatchName>(.*?)</MatchName>", txt)
    eff_count = {}
    for e in effects:
        eff_count[e] = eff_count.get(e, 0) + 1
    top_eff = sorted(eff_count.items(), key=lambda x: -x[1])[:25]
    ext_count = {}
    for n in names:
        e = n.rsplit(".", 1)[1].lower() if "." in n else "(なし)"
        ext_count[e] = ext_count.get(e, 0) + 1
    return {
        "media_count": len(names),
        "media_unique": len(set(names)),
        "media_ext": ext_count,
        "media_sample": sorted(set(names))[:40],
        "seq_names": [s for s in dict.fromkeys(seq) if s][:40],
        "effects_top": top_eff,
        "effect_kinds": len(eff_count),
        "xml_bytes": len(raw),
    }, None


_TC_RE = re.compile(r"(\d\d):(\d\d):(\d\d):(\d\d)")


def _tc_sec(tc, fps):
    h, m, s, fr = (int(x) for x in tc)
    return h * 3600 + m * 60 + s + fr / fps


def parse_edl(path):
    """EDL（カット表）。カット数・使用テープ名に加えて【1カットの長さ】を出す。

    標準CMX3600の1行はこの形：
      000001  A002C038_190521_R5F8  V  C  <src_in> <src_out> <rec_in> <rec_out>
    後ろ2つがタイムライン上の位置なので、その差が1カットの尺になる。

    なぜ尺まで取るか：AI動画生成が「作り物」に見える一番の原因が長回しで、
    実際のプロがどのくらいの速さで切っているかを裏付けたいから
    （2026-09-02。台帳の先頭25行だけで試算したら中央値1.17秒で、
    それまで一般論として言っていた「2〜3秒」より遥かに速かった）。

    fps は EDL に書かれていないので30固定で計算し、その旨を添える。
    23.98素材なら実尺は約1.25倍になる。
    """
    try:
        txt = path.read_text(encoding="utf-8", errors="replace")[:2 * 1024 * 1024]
    except OSError as e:
        return None, f"edl を開けない: {str(e)[:100]}"
    lines = [l.rstrip() for l in txt.splitlines()]
    events = [l for l in lines if re.match(r"^\d{3,6}\s+\S+", l)]
    reels = sorted({m.group(1) for l in events
                    if (m := re.match(r"^\d{3,6}\s+(\S+)", l))})
    cuts = []
    for l in events:
        tcs = _TC_RE.findall(l)
        if len(tcs) >= 4:
            d = _tc_sec(tcs[3], 30) - _tc_sec(tcs[2], 30)
            if 0.03 < d < 120:            # 明らかな異常値だけ捨てる
                cuts.append(round(d, 3))
    out = {"title": next((l.split(":", 1)[1].strip() for l in lines[:5]
                          if l.upper().startswith("TITLE:")), None),
           "event_count": len(events),
           "reels": reels[:60],
           "reel_count": len(reels),
           "cut_secs": cuts[:2000],
           "cut_fps_assumed": 30,
           "head": "\n".join(lines[:25])}
    if cuts:
        s = sorted(cuts)
        out["cut_median"] = s[len(s) // 2]
        out["cut_total_sec"] = round(sum(cuts), 2)
    return out, None


def _zip_text(path, member_prefix, tag):
    """OOXML（pptx/docx）はzip内XML。ライブラリ無しでテキストだけ抜く。"""
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as e:
        return None, f"zipとして開けない: {str(e)[:100]}"
    parts = [n for n in z.namelist() if n.startswith(member_prefix)]
    chunks = []
    for n in sorted(parts)[:400]:
        try:
            x = z.read(n).decode("utf-8", "replace")
        except (KeyError, OSError):
            continue
        chunks += re.findall(tag, x, re.S)
    z.close()
    text = " ".join(c.strip() for c in chunks if c.strip())
    text = re.sub(r"\s+", " ", text)
    return {"unit_count": len(parts), "text": text[:20000],
            "text_len": len(text)}, None


def parse_document(path, ext):
    if ext == "pdf":
        try:
            import pypdf
        except ImportError:
            return None, "pypdf が入っていない"
        try:
            r = pypdf.PdfReader(str(path))
            pages = len(r.pages)
            buf = []
            for pg in r.pages[:60]:            # 冒頭60ページで十分
                try:
                    buf.append(pg.extract_text() or "")
                except Exception:  # noqa: BLE001
                    continue
            text = re.sub(r"\s+", " ", " ".join(buf))
            return {"pages": pages, "text": text[:20000],
                    "text_len": len(text)}, None
        except Exception as e:  # noqa: BLE001
            return None, f"pdf 解析失敗: {str(e)[:120]}"
    if ext == "pptx":
        return _zip_text(path, "ppt/slides/slide", r"<a:t>(.*?)</a:t>")
    if ext == "docx":
        return _zip_text(path, "word/document", r"<w:t[^>]*>(.*?)</w:t>")
    if ext == "xlsx":
        try:
            import openpyxl
            wb = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
            sheets, buf = [], []
            for ws in wb.worksheets[:12]:
                sheets.append(ws.title)
                for i, row in enumerate(ws.iter_rows(values_only=True)):
                    if i > 200:
                        break
                    buf.append(" ".join(str(c) for c in row if c is not None))
            wb.close()
            text = re.sub(r"\s+", " ", " ".join(buf))
            return {"sheets": sheets, "text": text[:20000],
                    "text_len": len(text)}, None
        except Exception as e:  # noqa: BLE001
            return None, f"xlsx 解析失敗: {str(e)[:120]}"
    # TODO(2026-09-02 本人と合意・後回し): .xls / .doc / .ppt の旧バイナリ形式が
    # 未対応（openpyxl は xlsx 専用）。対象は27件程度と少ないので後で足す。
    # やるなら xlrd（xls）と antiword/textract 相当が要る。
    # .key（Keynote・30件）も同様に未対応。新しい版は zip なので拾える見込み。
    if ext in ("txt", "rtf"):
        try:
            t = path.read_text(encoding="utf-8", errors="replace")[:20000]
            return {"text": t, "text_len": len(t)}, None
        except OSError as e:
            return None, str(e)[:120]
    return None, f"未対応の書類形式: {ext}"


def run_meta(row):
    """1ファイルぶんの meta 抽出。(data, error) を返す。"""
    p = Path(row["path"])
    kind, ext = row["kind"], row["ext"]
    if not p.is_file():
        return None, "file-not-found"
    if kind in ("video", "audio"):
        return probe_media(p)
    if kind == "project":
        if ext == "prproj":
            return parse_prproj(p)
        if ext == "edl":
            return parse_edl(p)
        if ext in ("fcpxml", "xml", "cdl"):
            try:
                t = p.read_text(encoding="utf-8", errors="replace")[:200000]
            except OSError as e:
                return None, str(e)[:120]
            tags = {}
            for m in _XML_TAG.finditer(t.encode("utf-8", "replace")):
                k = m.group(1).decode()
                tags[k] = tags.get(k, 0) + 1
            return {"xml_tags_top": sorted(tags.items(), key=lambda x: -x[1])[:25],
                    "head": t[:3000]}, None
        # aaf / avb / drp / rtn はバイナリ。中身は開かず在ることだけ記録する。
        return {"binary": True, "note": f"{ext} はバイナリ形式のため書誌のみ"}, None
    if kind == "document":
        return parse_document(p, ext)
    return None, f"meta 対象外: {kind}"


# ---------- vision 段：画を読ませる ----------

_genai = {"client": None, "types": None, "models": None}


def _gemini_setup():
    if _genai["client"]:
        return
    env = BASE / ".env"
    if env.exists() and not os.getenv("GEMINI_API_KEY"):
        for ln in env.read_text(encoding="utf-8").splitlines():
            ln = ln.strip()
            if ln.startswith("GEMINI_API_KEY=") and "=" in ln:
                os.environ.setdefault("GEMINI_API_KEY", ln.split("=", 1)[1].strip())
    from google import genai
    from google.genai import types
    _genai["client"] = genai.Client()
    _genai["types"] = types
    # 最適解として flash を主にする（2026-09-02 本人と合意）。lite は $15 対 $54 と
    # 安いが読み取りの質が落ちる。画は縮小しない——384pxに落とすと $54→$24 になる
    # 代わりにテロップの文字とカメラの寄り引きが読めなくなり、目的に反する。
    _genai["models"] = [m.strip() for m in os.getenv(
        "GEMINI_MODELS",
        "gemini-2.5-flash,gemini-2.5-flash-lite").split(",") if m.strip()]


class QuotaExhausted(Exception):
    """その日の無料枠を使い切った。run を止めて翌日続きから。"""


def read_images_with_gemini(jpgs, prompt):
    _gemini_setup()
    types = _genai["types"]

    def _part(data):
        if hasattr(types.Part, "from_bytes"):
            return types.Part.from_bytes(data=data, mime_type="image/jpeg")
        return types.Part(inline_data=types.Blob(data=data, mime_type="image/jpeg"))

    parts = [_part(f.read_bytes()) for f in jpgs]
    parts.append(prompt)
    errs = []
    quota_hits = 0
    for model in _genai["models"]:
        for _ in range(2):
            try:
                resp = _genai["client"].models.generate_content(
                    model=model, contents=parts)
                txt = (getattr(resp, "text", "") or "").strip()
                if txt:
                    return txt, model
                errs.append(f"{model}: 空の応答")
                break
            except Exception as e:  # noqa: BLE001
                msg = str(e)[:160]
                errs.append(f"{model}: {msg}")
                low = msg.lower()
                if "429" in msg or "resource_exhausted" in low or "quota" in low:
                    quota_hits += 1
                    time.sleep(3)
                    continue
                break
    # 全モデルが枠切れなら、その日はもう回らない。呼び出し側で止める。
    if quota_hits >= len(_genai["models"]):
        raise QuotaExhausted(" / ".join(errs)[:300])
    return None, " / ".join(errs)[:300]


def read_images_with_claude(jpgs, prompt):
    """Gemini が枠切れの時の代役。claude CLI（サブスク・API課金なし）に
    画像を Read させる。25,611件を Gemini の無料枠だけで回すと数週間かかるので、
    枠が尽きた日はこちらで回し続ける。(text, "claude-cli") か (None, 理由)。"""
    claude = shutil.which("claude")
    if not claude:
        return None, "claude CLI が見つからない"
    tmpdir = jpgs[0].parent
    listing = "\n".join(f"- {f}" for f in jpgs)
    body = (f"次の画像ファイル {len(jpgs)} 枚を Read して読んでください:\n"
            f"{listing}\n\n" + prompt + "\n\n読み取り結果の本文だけを返してください。")
    try:
        r = subprocess.run(
            [claude, "-p", "--dangerously-skip-permissions",
             "--add-dir", str(tmpdir)],
            input=body.encode(), capture_output=True, timeout=240)
    except (subprocess.TimeoutExpired, OSError) as e:
        return None, f"claude CLI 失敗: {str(e)[:100]}"
    out = (r.stdout.decode(errors="replace") or "").strip()
    err = (r.stderr.decode(errors="replace") or "").strip()
    if r.returncode != 0 or not out:
        low = (err or out).lower()
        if "limit" in low or "quota" in low:
            raise QuotaExhausted(f"claude も上限: {(err or out)[:120]}")
        return None, f"claude CLI 失敗: {(err or out)[:160]}"
    return out, "claude-cli"


def read_images(jpgs, prompt):
    """まず Gemini（速い・1.3秒）、枠切れなら claude CLI（6秒・サブスク）。

    Gemini が枠切れでも run を止めない。両方だめになって初めて止める。
    """
    try:
        txt, used = read_images_with_gemini(jpgs, prompt)
        if txt:
            return txt, used
        g_err = used
    except QuotaExhausted as e:
        g_err = f"gemini枠切れ({str(e)[:60]})"
    txt, used = read_images_with_claude(jpgs, prompt)   # 上限なら QuotaExhausted
    if txt:
        return txt, used
    return None, f"gemini→{g_err[:100]} / claude→{used[:100]}"


def extract_frames(src, dur_sec, out_dir, n_frames=5):
    fracs = FRAME_FRACTIONS if n_frames >= 5 else FRAME_FRACTIONS[:n_frames]
    frames = []
    for i, fr in enumerate(fracs):
        t = max(0.0, min(dur_sec * fr, max(dur_sec - 0.5, 0.0))) if dur_sec else i
        out = out_dir / f"f{i:02d}.jpg"
        cmd = ["ffmpeg", "-nostdin", "-y", "-noaccurate_seek",
               "-ss", f"{t:.2f}", "-i", str(src), "-map", "0:v:0",
               "-frames:v", "1", "-q:v", "3", "-vf", "scale=640:-1", str(out)]
        try:
            subprocess.run(cmd, capture_output=True, timeout=90)
        except (subprocess.TimeoutExpired, OSError):
            continue
        if out.is_file() and out.stat().st_size > 0:
            frames.append(out)
    return frames


def _ok_jpeg(p):
    return p.is_file() and p.stat().st_size > 0


def to_jpeg(src, out_path, max_px=900):
    """静止画・psd/ai を jpeg に落とす。

    まず sips（macOS標準・psd や普通の画像はこれで足りる）。
    `.ai` の多くは PDF ではなく PostScript(EPS) なので sips が失敗する
    （実測：621件の .ai が全滅した・2026-09-02）。その場合だけ
    Ghostscript に回す。gs が無い環境では従来どおり失敗扱い。
    """
    try:
        r = subprocess.run(
            ["sips", "-s", "format", "jpeg", "-Z", str(max_px),
             str(src), "--out", str(out_path)],
            capture_output=True, timeout=120)
        if r.returncode == 0 and _ok_jpeg(out_path):
            return True
    except (subprocess.TimeoutExpired, OSError):
        pass
    if not shutil.which("gs"):
        return False
    try:
        subprocess.run(
            ["gs", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=jpeg",
             "-r72", "-dFirstPage=1", "-dLastPage=1", "-dJPEGQ=85",
             f"-sOutputFile={out_path}", str(src)],
            capture_output=True, timeout=180)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return _ok_jpeg(out_path)


def _dur_from_ledger(path):
    """meta 段で取った尺を使い回す（ffprobe の二度手間を避ける）。"""
    return _META_DUR.get(path)


_META_DUR = {}


def _preload_meta_durations():
    if not LEDGER.exists():
        return
    for line in LEDGER.open(encoding="utf-8"):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("phase") == "meta" and not rec.get("error"):
            d = (rec.get("data") or {}).get("dur_sec")
            if d:
                _META_DUR[rec.get("path")] = d


def run_vision(row, tmp):
    """1ファイルぶんの vision 抽出。(vision_text, model, error, timing) を返す。

    timing は {"prep": 画の用意にかかった秒, "model": モデル呼び出しの秒}。
    どちらが律速かを実測で分けるために残す（HDDのシークが支配的なら
    プランを上げても速くならない・2026-09-02）。
    """
    p = Path(row["path"])
    if not p.is_file():
        return None, None, "file-not-found", {}
    kind = row["kind"]
    t0 = time.time()
    if kind == "video":
        dur = _dur_from_ledger(row["path"]) or 0
        imgs = extract_frames(p, float(dur or 0), tmp)
        prompt = VISION_PROMPT_VIDEO
        if not imgs:
            return None, None, "no-frames", {"prep": round(time.time() - t0, 2)}
    elif kind in ("image", "design"):
        out = tmp / "s.jpg"
        prompt = VISION_PROMPT_STILL
        if not to_jpeg(p, out):
            return None, None, "jpeg変換に失敗", {"prep": round(time.time() - t0, 2)}
        imgs = [out]
    else:
        return None, None, f"vision 対象外: {kind}", {}
    t1 = time.time()
    txt, used = read_images(imgs, prompt)
    timing = {"prep": round(t1 - t0, 2), "model": round(time.time() - t1, 2)}
    return (txt, used if txt else None,
            None if txt else f"vision-failed: {used}", timing)


# ---------- 静止画の間引き ----------

_SEQ_RE = re.compile(r"^(.*?)(\d{2,6})$")


def run_vision_batch(rows, tmp):
    """静止画をまとめて1リクエストで読む。(text, model, error, timing) を返す。"""
    t0 = time.time()
    imgs, used_rows = [], []
    for i, r in enumerate(rows):
        src = Path(r["path"])
        if not src.is_file():
            continue
        out = tmp / f"s{i:02d}.jpg"
        if to_jpeg(src, out):
            imgs.append(out)
            used_rows.append(r)
    if not imgs:
        return None, None, "jpeg変換に全部失敗", {"prep": round(time.time() - t0, 2)}, []
    t1 = time.time()
    prompt = VISION_PROMPT_STILL_BATCH.format(n=len(imgs))
    txt, used = read_images(imgs, prompt)
    timing = {"prep": round(t1 - t0, 2), "model": round(time.time() - t1, 2)}
    return (txt, used if txt else None,
            None if txt else f"vision-failed: {used}", timing, used_rows)


def build_vision_units(rows):
    """vision 段の作業単位を作る。

    動画・デザインは1件1リクエスト（フレームが5枚あるので束ねられない）。
    静止画は同じフォルダごとに BATCH_STILLS 枚ずつ束ねる。
    """
    units = []
    stills = [r for r in rows if r["kind"] == "image"]
    for r in rows:
        if r["kind"] != "image":
            units.append({"type": "single", "rows": [r]})
    byfolder = {}
    for r in stills:
        byfolder.setdefault(str(Path(r["path"]).parent), []).append(r)
    for _, g in sorted(byfolder.items()):
        g.sort(key=lambda r: r["path"])
        for i in range(0, len(g), BATCH_STILLS):
            units.append({"type": "batch", "rows": g[i:i + BATCH_STILLS]})
    return units


def thin_images(rows, keep_per_group=3):
    """同一フォルダの連番（撮影の連射・書き出し連番）を代表数枚に絞る。

    jpgが22,495枚あり、全部個別に読ませると枠を食い潰す割に得るものが薄い
    （同じ被写体の連射が大半）。フォルダ＋数字を除いた語幹でまとめ、
    先頭・中間・末尾の3枚だけ残す。
    """
    groups = {}
    for r in rows:
        p = Path(r["path"])
        stem = p.stem
        m = _SEQ_RE.match(stem)
        base = m.group(1) if m else stem
        groups.setdefault((str(p.parent), base, r["ext"]), []).append(r)
    kept = []
    for _, g in groups.items():
        g.sort(key=lambda r: r["path"])
        if len(g) <= keep_per_group:
            kept += g
        else:
            idxs = {0, len(g) // 2, len(g) - 1}
            kept += [g[i] for i in sorted(idxs)]
    return kept


# ---------- 実行 ----------

def acquire_lock():
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text().strip() or "0")
        except ValueError:
            pid = 0
        if pid > 0:
            try:
                os.kill(pid, 0)
                _log(f"すでに解析が動いています（PID {pid}）。二重には起動しません。")
                sys.exit(0)
            except OSError:
                pass
        LOCK.unlink(missing_ok=True)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    LOCK.write_text(str(os.getpid()))


def release_lock():
    try:
        if LOCK.exists() and LOCK.read_text().strip() == str(os.getpid()):
            LOCK.unlink()
    except OSError:
        pass


_STOP = threading.Event()


def _do_unit(unit):
    """1リクエストぶんを処理して台帳レコードを返す。ワーカースレッドから呼ぶ。"""
    rows = unit["rows"]
    first = rows[0]
    tmp = Path(tempfile.mkdtemp(prefix="hddall_"))
    members = rows
    try:
        if unit["type"] == "batch":
            vis, used, e, timing, members = run_vision_batch(rows, tmp)
        else:
            vis, used, e, timing = run_vision(first, tmp)
    except QuotaExhausted:
        _STOP.set()               # 他のワーカーにも止まってもらう
        raise
    except Exception as ex:  # noqa: BLE001
        vis, used, e, timing = None, None, f"exception: {str(ex)[:200]}", {}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    rec = {"phase": "vision", "unit": unit["type"], "root": first["root"],
           "kind": first["kind"], "vision": vis, "model": used,
           "error": e, "timing": timing, "t": time.time()}
    if unit["type"] == "batch":
        rec["paths"] = [r["path"] for r in members]
        rec["rels"] = [r["rel"] for r in members]
        rec["folder"] = str(Path(first["path"]).parent)
    else:
        rec["path"] = first["path"]
        rec["rel"] = first["rel"]
        rec["ext"] = first["ext"]
        rec["size"] = first["size"]
    return rec, len(members)


def run_vision_phase(units, done, target, workers=1):
    """vision 段の本体。作業単位（1件 or 静止画の束）ごとに回す。

    待ち時間のほぼ全部がAPI応答なので（実測：画の用意0.07秒 / モデル10.5秒）、
    スレッドで並列にすると素直に台数ぶん速くなる。
    """
    _log(f"[vision] 対象 {len(units)} リクエスト"
         f"（全体 {len(target)} ファイル / 済み {len(done)}）"
         f"／並列 {workers}／台帳: {LEDGER}")
    _STOP.clear()
    ok = err = files_ok = 0
    done_n = 0
    t0 = time.time()
    quota_msg = None
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_do_unit, u): u for u in units}
        try:
            for fut in as_completed(futs):
                try:
                    rec, n = fut.result()
                except QuotaExhausted as qe:
                    quota_msg = str(qe)[:120]
                    break
                except Exception as ex:  # noqa: BLE001
                    err += 1
                    _log(f"  ✗ 想定外: {str(ex)[:90]}")
                    continue
                append_ledger(rec)
                done_n += 1
                if rec.get("vision"):
                    ok += 1
                    files_ok += n
                else:
                    err += 1
                    _log(f"  ✗ {(rec.get('error') or '')[:70]}")
                if done_n % 50 == 0:
                    el = time.time() - t0
                    rate = done_n / el
                    _log(f"    — {done_n}/{len(units)}req / {files_ok}ファイル "
                         f"/ 経過 {el / 60:.1f}分 / {rate * 3600:.0f}req/時 "
                         f"/ 残り推定 {(len(units) - done_n) / rate / 3600:.1f}時間")
        finally:
            _STOP.set()
            for f in futs:
                f.cancel()
    el = time.time() - t0
    if quota_msg:
        _log(f"\n⏸ 本日ぶんの枠を使い切りました（{quota_msg}）。"
             f"\n   同じコマンドで翌日続きから回せます。")
    _log(f"\n完了。成功 {ok} リクエスト（{files_ok} ファイル）/ 失敗 {err} "
         f"/ 所要 {el / 60:.1f}分")
    _log(f"台帳: {LEDGER}")


def show_stats():
    rows = load_index()
    by_kind = {}
    for r in rows:
        by_kind[r["kind"]] = by_kind.get(r["kind"], 0) + 1
    _log(f"索引: {len(rows)} 件")
    for k, n in sorted(by_kind.items(), key=lambda x: -x[1]):
        _log(f"  {k:9s} {n:7d}")
    for phase, kinds in (("meta", META_KINDS), ("vision", VISION_KINDS)):
        done = load_done(phase)
        target = [r for r in rows if r["kind"] in kinds]
        if phase == "vision":
            imgs = [r for r in target if r["kind"] == "image"]
            others = [r for r in target if r["kind"] != "image"]
            target = others + thin_images(imgs)
        rest = [r for r in target if r["path"] not in done]
        _log(f"\n[{phase}] 対象 {len(target)} / 済み {len(done)} "
             f"/ 残り {len(rest)} ファイル")
        if phase == "vision":
            _log(f"          → まとめ読みで {len(build_vision_units(rest))} リクエスト")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--index", action="store_true", help="索引を作る")
    ap.add_argument("--force", action="store_true", help="索引を作り直す")
    ap.add_argument("--phase", choices=["meta", "vision"], help="実行する段")
    ap.add_argument("--limit", type=int, default=0, help="件数上限（0=全部）")
    ap.add_argument("--kind", default="", help="種別で絞る（video/image/document…）")
    ap.add_argument("--root", default="", help="ルート名で絞る（部分一致）")
    ap.add_argument("--redo", action="store_true", help="台帳を無視してやり直す")
    ap.add_argument("--stats", action="store_true", help="進捗だけ表示")
    ap.add_argument("--workers", type=int, default=8,
                    help="vision段の並列数（既定8。待ちはほぼAPI応答なので効く）")
    args = ap.parse_args()

    mounted = [r for r in ROOTS if r.is_dir()]
    if not mounted:
        _log("HDDが1台もマウントされていません。ドライブを挿してから再実行してください。")
        sys.exit(2)
    if len(mounted) < len(ROOTS):
        for r in ROOTS:
            if not r.is_dir():
                _log(f"⚠ 未マウント: {r}（このぶんは飛ばします）")

    if args.index:
        build_index(force=args.force)
        return
    if args.stats:
        show_stats()
        return
    if not args.phase:
        _log("--phase meta か --phase vision を指定してください（--stats で進捗）。")
        return
    if args.phase in ("meta", "vision") and not (shutil.which("ffprobe")
                                                 and shutil.which("ffmpeg")):
        _log("ffmpeg / ffprobe が見つかりません（brew install ffmpeg）。")
        sys.exit(3)

    acquire_lock()
    try:
        rows = load_index()
        kinds = META_KINDS if args.phase == "meta" else VISION_KINDS
        target = [r for r in rows if r["kind"] in kinds]
        if args.phase == "vision":
            imgs = [r for r in target if r["kind"] == "image"]
            others = [r for r in target if r["kind"] != "image"]
            n_before = len(imgs)
            imgs = thin_images(imgs)
            _log(f"静止画の間引き: {n_before} → {len(imgs)} 枚（連番を代表3枚に）")
            target = others + imgs
            _preload_meta_durations()
        if args.kind:
            target = [r for r in target if r["kind"] == args.kind]
        if args.root:
            target = [r for r in target if args.root in r["root"]]
        done = set() if args.redo else load_done(args.phase)
        todo = [r for r in target if r["path"] not in done]
        # 軽いものから先に終わらせる（途中で止まっても成果が残るように）
        todo.sort(key=lambda r: (r["kind"] != "project", r["kind"] != "document",
                                 r["size"]))
        if args.phase == "vision":
            units = build_vision_units(todo)
            _log(f"作業単位: {len(todo)} ファイル → {len(units)} リクエスト"
                 f"（静止画は同フォルダ{BATCH_STILLS}枚ずつまとめ読み）")
            if args.limit:
                units = units[:args.limit]
            run_vision_phase(units, done, target, workers=max(1, args.workers))
            return
        if args.limit:
            todo = todo[:args.limit]

        _log(f"[{args.phase}] 対象 {len(todo)} 件"
             f"（全体 {len(target)} / 済み {len(done)}）／台帳: {LEDGER}")
        ok = err = 0
        t0 = time.time()
        for i, row in enumerate(todo, 1):
            name = Path(row["path"]).name
            head = f"[{i}/{len(todo)}] {row['root']}/{row['kind']}/{name[:60]}"
            rec = {"phase": args.phase, "path": row["path"], "root": row["root"],
                   "rel": row["rel"], "kind": row["kind"], "ext": row["ext"],
                   "size": row["size"], "t": time.time()}
            if args.phase == "meta":
                try:
                    data, e = run_meta(row)
                except Exception as ex:  # noqa: BLE001
                    data, e = None, f"exception: {str(ex)[:200]}"
                rec["data"], rec["error"] = data, e
                append_ledger(rec)
                if e:
                    err += 1
                    _log(f"{head} … ✗ {e[:70]}")
                else:
                    ok += 1
                    if i <= 5 or i % 50 == 0:
                        _log(f"{head} … ✓")
            else:
                tmp = Path(tempfile.mkdtemp(prefix="hddall_"))
                timing = {}
                try:
                    vis, used, e, timing = run_vision(row, tmp)
                except QuotaExhausted as qe:
                    _log(f"\n⏸ 本日ぶんの枠を使い切りました（{str(qe)[:100]}）。"
                         f"\n   ここまで {ok} 件。同じコマンドで翌日続きから回せます。")
                    break
                except Exception as ex:  # noqa: BLE001
                    vis, used, e = None, None, f"exception: {str(ex)[:200]}"
                finally:
                    shutil.rmtree(tmp, ignore_errors=True)
                rec["vision"], rec["model"], rec["error"] = vis, used, e
                rec["timing"] = timing
                append_ledger(rec)
                if vis:
                    ok += 1
                    if i <= 5 or i % 20 == 0:
                        _log(f"{head} … ✓ {used}")
                else:
                    err += 1
                    _log(f"{head} … ✗ {(e or '')[:70]}")
            if i % 100 == 0:
                el = time.time() - t0
                _log(f"    — {i}件 / 経過 {el:.0f}s / 平均 {el / i:.2f}s per 件 "
                     f"/ 残り推定 {(len(todo) - i) * el / i / 60:.0f}分")
        el = time.time() - t0
        _log(f"\n完了。成功 {ok} / 失敗 {err} / 所要 {el:.0f}s"
             f"（平均 {el / max(ok + err, 1):.2f}s per 件）")
        _log(f"台帳: {LEDGER}")
    finally:
        release_lock()


if __name__ == "__main__":
    try:
        main()
    finally:
        release_lock()
