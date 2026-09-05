#!/usr/bin/env python3
"""元データHDDの動画を1本ずつ「見て」、画の傾向をメモに溜める単独スクリプト。

なぜ単独か：ボットのエージェント実行（claude -p にプランを立てさせて承認して
回す方式）が、この作業（100本超・フレーム抽出・画の読み取り）で毎回ハングして
Discordごと無反応になった（2026-08-29 〜 08-30、6回以上）。決め打ちの手順を
1つのスクリプトにして、落ちても `--resume` で続きから回せる形にする。

やること（1本ごと）：
  1. 既存の一覧 `成果物/事例素材一覧/事例素材一覧.json` からパスを引く
  2. ffmpeg で代表フレームを数枚抜く（HDDには一切書かない・tmpだけ）
  3. Gemini に画を読ませて、被写体・構図・カメラワーク・テロップ等を短くもらう
  4. `discord-groupchat/history/hdd_video_vision.jsonl` に1行追記

まとめ物（エクセル等）は作らない。読んだ内容は学習の材料としてこのjsonlに残す。

使い方：
  python3 tools/analyze_hdd_videos.py            # 続きから全部
  python3 tools/analyze_hdd_videos.py --limit 5  # 5本だけ（試走）
  python3 tools/analyze_hdd_videos.py --only なんつね   # 表題/フォルダ部分一致
  python3 tools/analyze_hdd_videos.py --redo     # 台帳を無視して全部やり直す

終了コード： 0=正常 / 2=HDD未マウント / 3=ffmpeg無し / 4=一覧JSON無し
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent          # discord-groupchat/
REPO = BASE.parent                                      # so-portfolio/
HDD_ROOT = Path("/Volumes/1/サムシングファン/02_その他/03_事例/元データ")
LIST_JSON = REPO / "成果物" / "事例素材一覧" / "事例素材一覧.json"
LEDGER = BASE / "history" / "hdd_video_vision.jsonl"
FRAME_FRACTIONS = [0.08, 0.28, 0.50, 0.72, 0.90]

VISION_PROMPT = (
    "これは1本の企業VP・事例動画から時系列順に抜いた代表フレームです。"
    "映像制作の学習材料にするため、次の項目を簡潔な箇条書きで日本語でまとめてください。"
    "推測が混じる項目は「推定」と付けること。\n"
    "- 被写体（人物/商品/設備/建物/風景/UI など。人物なら寄りか引きか）\n"
    "- 屋内か屋外か\n"
    "- 構図の傾向（日の丸/三分割/対称、余白の取り方、画面比の印象）\n"
    "- カメラワークの推定（フィックス/パン/ズーム/手持ち/空撮。連続フレームの変化から）\n"
    "- テロップ（有無・位置・分量・キャッチコピーか字幕か）\n"
    "- 色調・ライティング（明るい/硬い/暖色寒色/コントラスト）\n"
    "- 冒頭に置いているもの（ロゴ/人/空撮/テロップ/製品）\n"
    "- ひとこと総評（この動画の作りの型を1〜2文で）"
)


def _log(msg):
    print(msg, flush=True)


def _ffmpeg_ok():
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def _load_list():
    if not LIST_JSON.exists():
        _log(f"一覧JSONが見つかりません: {LIST_JSON}")
        sys.exit(4)
    return json.loads(LIST_JSON.read_text(encoding="utf-8"))


def _resolve_path(row):
    """その作品の実ファイルパス。軽量版があればそれを優先（軽くて速い・画は同じ）。"""
    folder = "" if row.get("フォルダ") in ("（直下）", "", None) else row["フォルダ"]
    for key in ("軽量版パス", "相対パス", "ファイル名"):
        rel = row.get(key)
        if not rel:
            continue
        for cand in ((HDD_ROOT / folder / rel), (HDD_ROOT / rel)):
            if cand.is_file():
                return cand
    return None


def _load_done():
    """台帳に既にある（vision取得済みの）相対パスの集合。"""
    done = set()
    if LEDGER.exists():
        for line in LEDGER.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("rel") and rec.get("vision"):
                done.add(rec["rel"])
    return done


def _extract_frames(src, dur_sec, out_dir, n_frames):
    """代表フレームを抜く。1枚が遅い/失敗しても run 全体を落とさない。
    外付けHDD（exFAT・USB）は1フレームの取り出しに時間がかかることがあり、
    以前は ffmpeg のタイムアウトが未捕捉で run ごと死んでいた（08-30）。"""
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
        except subprocess.TimeoutExpired:
            _log(f"      （フレーム{i} が90秒で取れず。スキップ）")
            continue
        except Exception as e:  # noqa: BLE001
            _log(f"      （フレーム{i} 抽出エラー: {str(e)[:80]}）")
            continue
        if out.is_file() and out.stat().st_size > 0:
            frames.append(out)
    return frames


_genai = {"client": None, "types": None, "models": None}


def _gemini_setup():
    if _genai["client"]:
        return
    # .env を読む（このスクリプトは単独起動なので環境に無いことがある）
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
    _genai["models"] = [m.strip() for m in os.getenv(
        "GEMINI_MODELS",
        "gemini-2.5-flash,gemini-2.5-flash-lite").split(",") if m.strip()]


def _read_frames_with_gemini(frames):
    """(vision_text, model_used) を返す。全モデルだめなら (None, 理由)。"""
    _gemini_setup()
    types = _genai["types"]

    def _part(data):
        if hasattr(types.Part, "from_bytes"):
            return types.Part.from_bytes(data=data, mime_type="image/jpeg")
        return types.Part(inline_data=types.Blob(
            data=data, mime_type="image/jpeg"))

    parts = [_part(fp.read_bytes()) for fp in frames]
    parts.append(VISION_PROMPT)
    errs = []
    for model in _genai["models"]:
        for attempt in range(2):
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
                if "429" in msg or "RESOURCE_EXHAUSTED" in msg or "quota" in msg.lower():
                    time.sleep(2)
                    continue
                break
    return None, " / ".join(errs)[:300]


def _read_frames_with_claude(frames):
    """Gemini が枠切れの時の代役。claude CLI（サブスク・画像読み取り無料）で
    フレームを読む。(vision_text, "claude-cli") か (None, 理由)。"""
    claude = shutil.which("claude")
    if not claude:
        return None, "claude CLI が見つからない"
    tmpdir = frames[0].parent
    listing = "\n".join(f"- {f}" for f in frames)
    prompt = (f"次の画像ファイル（1本の企業VP・事例動画から時系列順に抜いた"
              f"代表フレーム {len(frames)} 枚）を Read して読んでください:\n{listing}\n\n"
              + VISION_PROMPT + "\n\n読み取り結果の本文だけを返してください。")
    try:
        r = subprocess.run(
            [claude, "-p", "--dangerously-skip-permissions",
             "--add-dir", str(tmpdir)],
            input=prompt.encode(), capture_output=True, timeout=240)
    except subprocess.TimeoutExpired:
        return None, "claude CLI タイムアウト"
    out = (r.stdout.decode(errors="replace") or "").strip()
    err = (r.stderr.decode(errors="replace") or "").strip()
    if r.returncode != 0 or not out:
        low = (err or out).lower()
        if "limit" in low or "quota" in low:
            return None, f"claude 上限: {(err or out)[:120]}"
        return None, f"claude CLI 失敗: {(err or out)[:160]}"
    return out, "claude-cli"


def _read_frames(frames):
    """まず Gemini（速い・枠がある間）、だめなら claude CLI（サブスク）。"""
    txt, used = _read_frames_with_gemini(frames)
    if txt:
        return txt, used
    g_err = used
    txt, used = _read_frames_with_claude(frames)
    if txt:
        return txt, used
    return None, f"gemini→{g_err[:120]} / claude→{used[:120]}"


def _append_ledger(rec):
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


# 演出のヒント・映像の分析結果は、どんなものでも1か所に集める（本人の指示・
# 2026-08-31）。走らせた事実がどこにも残らないと、また探し回ることになる。
KNOWLEDGE = BASE / "fixtures" / "youtube_insights.md"


def _note_in_knowledge(ok, novis, miss):
    """解析を走らせたら、その事実を『映像制作の知見』に1件残す。"""
    try:
        total = 0
        try:
            total = len({json.loads(l)["rel"] for l in
                         LEDGER.read_text(encoding="utf-8").splitlines()
                         if l.strip() and json.loads(l).get("vision")})
        except (OSError, ValueError, KeyError):
            pass
        stamp = time.strftime("%Y-%m-%d %H:%M")
        entry = (f"\n## {stamp}\n"
                 f"【自動・元データ動画の映像解析】今回 {ok} 本を読み取り"
                 f"（保留 {novis} / 取得不可 {miss}）。累計 **{total} 本**。\n"
                 f"1本ごとの読み取り（被写体・構図・カメラワーク・テロップ・色調）は "
                 f"`{LEDGER.name}`。傾向と演出のヒントは、このファイル上部の"
                 f"【A. プロの事例動画の演出】に集約してある。\n")
        with KNOWLEDGE.open("a", encoding="utf-8") as f:
            f.write(entry)
        _log(f"知見に追記: {KNOWLEDGE}")
    except OSError as e:
        _log(f"（知見への追記に失敗: {str(e)[:80]}）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="処理する本数の上限（0=全部）")
    ap.add_argument("--only", default="", help="表題/フォルダの部分一致で絞る")
    ap.add_argument("--redo", action="store_true", help="台帳を無視して全部やり直す")
    ap.add_argument("--frames", type=int, default=5, help="1本あたりのフレーム数")
    args = ap.parse_args()

    if not HDD_ROOT.is_dir():
        _log(f"HDDが見つかりません（未マウント）: {HDD_ROOT}")
        sys.exit(2)
    try:
        next(os.scandir(HDD_ROOT))
    except PermissionError:
        _log("HDDは在るが読み取り拒否（macOSのフルディスクアクセス未許可）。"
             "設定でこのプロセスを許可してから再実行してください。")
        sys.exit(2)
    except (StopIteration, OSError):
        pass
    if not _ffmpeg_ok():
        _log("ffmpeg / ffprobe が見つかりません（brew install ffmpeg）。")
        sys.exit(3)

    # 二重起動の防止（ボット再起動・!analyze と自然文の両方から起動されうる）。
    lock = LEDGER.parent / "hdd_video_vision.lock"
    if lock.exists():
        try:
            pid = int(lock.read_text().strip() or "0")
        except ValueError:
            pid = 0
        alive = False
        if pid > 0:
            try:
                os.kill(pid, 0)
                alive = True
            except OSError:
                alive = False
        if alive:
            _log(f"すでに解析が動いています（PID {pid}）。二重には起動しません。")
            sys.exit(0)
        lock.unlink(missing_ok=True)      # 前回の残骸
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()))

    rows = _load_list()
    done = set() if args.redo else _load_done()
    if args.only:
        rows = [r for r in rows
                if args.only in (r.get("表題", "") + r.get("フォルダ", ""))]
    todo = [r for r in rows if r.get("相対パス") not in done]
    if args.limit:
        todo = todo[:args.limit]

    _log(f"対象 {len(todo)} 本（一覧 {len(rows)} 本／台帳済み {len(done)} 本）"
         f"／台帳: {LEDGER}")
    ok = miss = novis = 0
    t0 = time.time()
    for idx, row in enumerate(todo, 1):
        rel = row.get("相対パス", "?")
        title = row.get("表題", rel)
        folder = row.get("フォルダ", "")
        src = _resolve_path(row)
        head = f"[{idx}/{len(todo)}] （{folder}）{title}"
        if not src:
            _log(f"{head} … ✗ ファイルが見つからない")
            _append_ledger({"rel": rel, "title": title, "folder": folder,
                            "vision": None, "error": "file-not-found",
                            "t": time.time()})
            miss += 1
            continue
        tmp = Path(tempfile.mkdtemp(prefix="hddvid_"))
        try:
            frames = _extract_frames(src, float(row.get("尺(秒)") or 0),
                                     tmp, args.frames)
            if not frames:
                _log(f"{head} … ✗ フレーム抽出に失敗")
                _append_ledger({"rel": rel, "title": title, "folder": folder,
                                "vision": None, "error": "no-frames",
                                "src": str(src), "t": time.time()})
                miss += 1
                continue
            vis, used = _read_frames(frames)
            rec = {"rel": rel, "title": title, "folder": folder,
                   "src": str(src), "dur_sec": row.get("尺(秒)"),
                   "resolution": row.get("解像度"), "orient": row.get("向き"),
                   "frames": len(frames), "vision": vis,
                   "model": used if vis else None,
                   "error": None if vis else f"vision-failed: {used}",
                   "t": time.time()}
            _append_ledger(rec)
            if vis:
                _log(f"{head} … ✓ {used}（{len(frames)}枚）")
                ok += 1
            else:
                _log(f"{head} … △ 画の読み取り保留（{used[:80]}）")
                novis += 1
        except Exception as e:  # noqa: BLE001
            # 1本の想定外エラーで run 全体を止めない。次回 --resume で拾い直す。
            _log(f"{head} … ✗ 想定外のエラーでスキップ: {str(e)[:120]}")
            _append_ledger({"rel": rel, "title": title, "folder": folder,
                            "vision": None, "error": f"exception: {str(e)[:200]}",
                            "src": str(src), "t": time.time()})
            miss += 1
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if idx <= 5 or idx % 10 == 0:
            el = time.time() - t0
            _log(f"    — 経過 {el:.0f}s ／ 1本あたり平均 {el / idx:.0f}s")

    el = time.time() - t0
    _log(f"\n完了。読めた {ok} 本 ／ 画の保留 {novis} 本 ／ 取得不可 {miss} 本"
         f"／ 所要 {el:.0f}s（平均 {el / max(len(todo), 1):.0f}s/本）")
    if novis:
        _log("※ 保留分は Gemini/Claude の枠が戻ってから同じコマンドで拾い直せます。")
    _log(f"台帳: {LEDGER}")
    if ok:
        _note_in_knowledge(ok, novis, miss)


if __name__ == "__main__":
    _lock = LEDGER.parent / "hdd_video_vision.lock"
    try:
        main()
    finally:
        try:
            if _lock.exists() and _lock.read_text().strip() == str(os.getpid()):
                _lock.unlink()
        except OSError:
            pass
