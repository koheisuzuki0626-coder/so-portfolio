"""
Discord グループチャット（人間 × オーケストレーター × Claude × Gemini）
====================================================================
Discord Bot 3体を1プロセスで動かす。

  ・普通に発言 / @オーケストレーター → オーケストレーターが Claude と Gemini を使い、
                                        1つにまとめた回答を返す（司令塔）
  ・「クロード ○○」/ @Claude         → Claude だけが返す
  ・「gemini ○○」/ @Gemini          → Gemini だけが返す
  ・@Claude と @Gemini 両方指名       → 二人が個別に返す
  ・!talk お題                        → Claude と Gemini だけで自動トーク
  ・!stop                             → 自動トークを停止

Claude担当は `claude` CLI（サブスク）。Gemini担当は Gemini API 無料枠キー。
メッセージを読むのはオーケストレーターBotだけ（他は送信専用）なので、
MESSAGE CONTENT INTENT はオーケストレーター用にだけONにすればよい。
"""

import asyncio
import contextvars
import io
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

import aiohttp
import discord
from dotenv import load_dotenv
from google import genai

load_dotenv()

# Higgsfield（映像パイプライン用）。未設定でもチャット機能は動くように保護。
try:
    import hf_wrapper
    HF_AVAILABLE = True
    HF_IMPORT_ERROR = None
except Exception as _e:  # noqa: BLE001
    HF_AVAILABLE = False
    HF_IMPORT_ERROR = _e

# ---------- 設定 ----------
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")
# Geminiモデルを順に試す（各モデルは別々の日次無料枠）。上のモデルの枠が切れたら次へ。
GEMINI_MODELS = [
    m.strip()
    for m in os.getenv(
        "GEMINI_MODELS",
        "gemini-2.0-flash,gemini-2.5-flash,gemini-2.5-flash-lite,gemini-2.0-flash-lite",
    ).split(",")
    if m.strip()
]
GEMINI_MODEL = GEMINI_MODELS[0]  # 検索グラウンディング等で使う既定モデル
MAX_TURNS = int(os.getenv("MAX_TURNS", "6"))
REPLY_CHARS = 400
SEND_DELAY = 2
HISTORY_LIMIT = int(os.getenv("HISTORY_LIMIT", "40"))  # プロンプトに入れる直近発言数
CLAUDE_TIMEOUT = int(os.getenv("CLAUDE_TIMEOUT", "300"))
# 作業（生成・修正・リサーチ・学習など）の前に、理解した内容とやることを提示して
# 同意を得る＝反復確認。0 にすると従来どおり即実行する。
CONFIRM_BEFORE_WORK = os.getenv("CONFIRM_BEFORE_WORK", "1") not in ("0", "false", "False")
# claude CLI の同時実行数。裏方の処理（プロファイル学習・要約・検品・企画など）と
# 会話の返事が同じ枠を奪い合うと、返事が順番待ちで遅くなる。
# 裏方は BG_CONCURRENCY 枠までに抑え、残りは必ず会話用に空けておく。
CLAUDE_CONCURRENCY = int(os.getenv("CLAUDE_CONCURRENCY", "3"))
BG_CONCURRENCY = int(os.getenv("BG_CONCURRENCY", "1"))
# フル権限の実行タスク（調査・自己改修・MCP投入）は特に重いので1本ずつ
EXEC_CONCURRENCY = int(os.getenv("EXEC_CONCURRENCY", "1"))
_sems = {}          # (名前, ループ) -> Semaphore


def _sem(name, size):
    """セマフォを【実行中のイベントループ上で】作って返す。
    モジュール読み込み時に asyncio.Semaphore() を作ると、Python 3.9 では
    その時点の（Discordが後で使うのとは別の）ループに紐づいてしまい、
    順番待ちが発生した瞬間だけ
    『got Future attached to a different loop』で応答が落ちる。
    実際にMac(python3.9)で発生したため、必ずこの関数経由で取得すること。"""
    loop = asyncio.get_running_loop()
    key = (name, id(loop))
    if key not in _sems:
        _sems.clear()          # ループが変わったら作り直す（古い分は捨てる）
        _sems[key] = asyncio.Semaphore(size)
    return _sems[key]


def _get_claude_sem():
    return _sem("claude", CLAUDE_CONCURRENCY)


def _get_bg_sem():
    """裏方処理用の追加の関門。会話の返事はここを通らないので、
    裏方が何本走っていても返事用の枠が必ず残る。"""
    return _sem("bg", BG_CONCURRENCY)

gemini_client = genai.Client()  # 環境変数 GEMINI_API_KEY を自動参照

orch_intents = discord.Intents.default()
orch_intents.message_content = True
orch = discord.Client(intents=orch_intents)
claude_bot = discord.Client(intents=discord.Intents.default())
gemini_bot = discord.Client(intents=discord.Intents.default())

state = {"running": False, "stop": False}

# ---------- 会話履歴（永続化＋長期記憶） ----------
# 全発言は history/{channel_id}.jsonl に追記保存（再起動しても消えない）。
# プロンプトには「長期記憶の要約 + 直近 HISTORY_LIMIT 件」を渡す。
# 直近枠から溢れた古い発言は、自動で要約に畳み込まれる（圧縮された記憶）。
_BASE = os.path.dirname(os.path.abspath(__file__))
HISTORY_DIR = Path(os.getenv("HISTORY_DIR", os.path.join(_BASE, "history")))
HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def _read_json(path, default=None):
    """JSONファイルを読む（無い/壊れていれば default）。記録系の共通入口。"""
    try:
        if Path(path).exists():
            return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        print(f"[json] 読込失敗 {Path(path).name}: {str(e)[:120]}")
    return {} if default is None else default


def _write_json(path, data, tag="json"):
    """JSONファイルを書く（失敗しても落とさない）。記録系の共通出口。"""
    try:
        Path(path).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        return True
    except Exception as e:  # noqa: BLE001
        print(f"[{tag}] 保存失敗: {str(e)[:120]}")
        return False


SUMMARY_SPEAKER = "📝これまでの経緯"  # 履歴リスト先頭に置く長期記憶エントリの名前
SUMMARIZE_BATCH = int(os.getenv("SUMMARIZE_BATCH", "12"))  # この件数たまったら要約
SUMMARY_MAX_CHARS = 4000

histories = {}   # channel_id -> list[(speaker, text)]（先頭に要約エントリを持つことがある）
_pending_summary = {}  # channel_id -> 要約待ちの古い発言リスト
_summarizing = set()   # 要約処理中の channel_id


def _hist_path(cid):
    return HISTORY_DIR / f"{cid}.jsonl"


def _summary_path(cid):
    return HISTORY_DIR / f"{cid}_summary.txt"


def _append_jsonl(cid, speaker, text):
    try:
        with open(_hist_path(cid), "a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"t": time.time(), "speaker": speaker, "text": text},
                ensure_ascii=False,
            ) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"[history] 保存失敗: {e}")


def _load_history(cid):
    """ディスクから長期記憶（要約）と直近の発言を復元する。"""
    h = []
    try:
        sp = _summary_path(cid)
        if sp.exists():
            summary = sp.read_text(encoding="utf-8").strip()
            if summary:
                h.append((SUMMARY_SPEAKER, summary))
    except Exception as e:  # noqa: BLE001
        print(f"[history] 要約読込失敗: {e}")
    try:
        fp = _hist_path(cid)
        if fp.exists():
            for ln in fp.read_text(encoding="utf-8").splitlines()[-HISTORY_LIMIT:]:
                try:
                    d = json.loads(ln)
                    h.append((d["speaker"], d["text"]))
                except Exception:  # noqa: BLE001
                    continue
    except Exception as e:  # noqa: BLE001
        print(f"[history] 履歴読込失敗: {e}")
    return h


def get_history(cid):
    if cid not in histories:
        histories[cid] = _load_history(cid)
    return histories[cid]


def _has_summary(h):
    return bool(h) and h[0][0] == SUMMARY_SPEAKER


def add_history(cid, speaker, text):
    h = get_history(cid)
    h.append((speaker, text))
    _append_jsonl(cid, speaker, text)
    # 直近枠から溢れた古い発言は「要約待ち」に回す（要約エントリは先頭に保持）
    head = 1 if _has_summary(h) else 0
    excess = (len(h) - head) - HISTORY_LIMIT
    if excess > 0:
        _pending_summary.setdefault(cid, []).extend(h[head:head + excess])
        del h[head:head + excess]
        _schedule_summarize(cid)


def _schedule_summarize(cid):
    """要約待ちが一定量たまったら、バックグラウンドで長期記憶に畳み込む。"""
    if len(_pending_summary.get(cid, [])) < SUMMARIZE_BATCH or cid in _summarizing:
        return
    try:
        asyncio.get_running_loop().create_task(_summarize_pending(cid))
    except RuntimeError:
        pass  # イベントループ外（起動前など）は次の機会に


async def _summarize_pending(cid):
    """古い発言を既存の要約に統合し、長期記憶を更新する。"""
    if cid in _summarizing:
        return
    _summarizing.add(cid)
    batch = []
    try:
        batch = _pending_summary.get(cid) or []
        if not batch:
            return
        _pending_summary[cid] = []
        h = get_history(cid)
        old_summary = h[0][1] if _has_summary(h) else ""
        transcript = "\n".join(f"{s}: {t}" for s, t in batch)
        prompt = (
            "あなたはDiscordグループチャットの記憶係。既存の要約に新しい会話を統合し、"
            "更新版の要約だけを日本語で出力する。\n"
            "残すもの: 決定事項・約束・進行中の作業や依頼・人物の好みや事情・重要な事実。\n"
            "捨てるもの: 挨拶・相づち・重複。\n"
            f"{SUMMARY_MAX_CHARS}字以内。箇条書き中心で簡潔に。\n\n"
            f"【既存の要約】\n{old_summary or '(まだ無し)'}\n\n"
            f"【新しく統合する会話】\n{transcript}\n\n更新後の要約:"
        )
        # 速度不問のバックグラウンド処理なので Claude 優先（Gemini無料枠を温存）
        new_summary = await _ai_text_bg(prompt, "summarize")
        new_summary = (new_summary or "").strip()[:SUMMARY_MAX_CHARS]
        if not new_summary:
            raise RuntimeError("要約が空でした")
        if _has_summary(h):
            h[0] = (SUMMARY_SPEAKER, new_summary)
        else:
            h.insert(0, (SUMMARY_SPEAKER, new_summary))
        _summary_path(cid).write_text(new_summary, encoding="utf-8")
        print(f"[history] 長期記憶を更新: channel={cid}, {len(batch)}件を統合")
    except Exception as e:  # noqa: BLE001
        # 失敗したら次回に持ち越し（発言は捨てない）
        _pending_summary.setdefault(cid, [])[:0] = batch
        print(f"[history] 要約失敗（次回再試行）: {str(e)[:200]}")
    finally:
        _summarizing.discard(cid)


def transcript_block(history):
    """会話ログを、指示文と混ざらないように区切って渡す。

    以前はプロンプトの末尾を「あなたの回答:」のような穴埋め形式にしていたが、
    claude CLI は補完モデルではなくエージェントなので、この末尾を
    「ユーザーが貼った文章」と読んでしまうことがあった。実際に
    「『あなたの回答:』の後が空っぽ。何を貼ろうとしてた？」と返す事故が起きた
    （会話の中身が薄い＝リンクだけ、のときに起きやすい）。
    区切り線で囲み、最後は穴埋めではなく明確な指示文で終える。

    あわせて「どれが最新か」「前後関係」を明示する。名前と本文を並べただけだと
    AIが各発言を独立した質問として読み、直前のやり取りを踏まえない返事
    （リンクを貼った直後の「要約して」が何を指すか分からない等）になるため。"""
    body = build_transcript(history)
    latest = (_latest_user_msg(history) or "").strip()
    head = (
        "会話ログを古い順に並べます。上ほど古く、一番下が最新です。"
        "前の発言を受けた省略（『それ』『さっきの』『要約して』など）は、"
        "直前の流れから何を指しているかを判断してください。\n"
    )
    tail = f"\n\n【いま答えるべき発言】{latest[:200]}" if latest else ""
    return (f"{head}--- 会話ログ ここから ---\n{body}\n"
            f"--- 会話ログ ここまで ---{tail}")


def build_transcript(history):
    lines = []
    for name, text in history:
        if name == SUMMARY_SPEAKER:
            lines.append(f"【これまでの経緯（長期記憶の要約）】\n{text}\n【ここから直近の会話】")
        else:
            lines.append(f"{name}: {text}")
    return "\n".join(lines) or "(まだ会話なし)"


def _cid_of_history(history):
    """history リストから channel_id を逆引き（同一オブジェクト参照で照合）。"""
    for k, v in histories.items():
        if v is history:
            return k
    return None


RECALL_MAX_CHARS = int(os.getenv("RECALL_MAX_CHARS", "150000"))


def _read_full_log(cid, max_chars=RECALL_MAX_CHARS):
    """チャンネルの全ログを日時付きで読み出す（新しい方から max_chars 分）。"""
    fp = _hist_path(cid)
    if not fp.exists():
        return ""
    out, total = [], 0
    for ln in reversed(fp.read_text(encoding="utf-8").splitlines()):
        try:
            d = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(d.get("t", 0)))
        s = f"[{ts}] {d.get('speaker', '?')}: {d.get('text', '')}"
        total += len(s) + 1
        if total > max_chars:
            break
        out.append(s)
    return "\n".join(reversed(out))


async def _recall_context(cid, question):
    """過去ログ全文をGemini（大容量コンテキスト・無料枠）に読ませ、
    質問に関係する部分を日時付きで抽出して返す。"""
    log = _read_full_log(cid)
    if not log:
        return ""
    prompt = (
        "以下はDiscordチャンネルの過去ログ。質問に関係する出来事・決定事項・発言を、"
        "日時付きで抜き出して簡潔にまとめる。無関係な話は省く。見つからなければ"
        "『関連する記録なし』とだけ返す。\n\n"
        f"質問: {question}\n\n過去ログ:\n{log}\n\n関連情報:"
    )
    try:
        ans = await _gemini_call(prompt)
    except Exception:  # noqa: BLE001
        try:
            # Claudeにフォールバック（コンテキストが小さいのでログを短縮）
            ans = await run_claude_cli(prompt[-100000:])
        except Exception:  # noqa: BLE001
            return ""
    ans = (ans or "").strip()
    return f"【過去ログからの関連情報】\n{ans}" if ans else ""


# ---------- 自己修復：エラーログ＋自己診断 ----------
# 「失敗が見えない（毎回スクショ待ち）」を解消する。全例外をここに記録し、
# Discordから「エラー教えて」で取り出せる。「システムチェック」で各機能を能動診断。
ERROR_LOG = HISTORY_DIR / "errors.log"


def _log_error(context, exc):
    """例外を errors.log に追記し、短い要約文字列を返す（ユーザー通知用）。"""
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    ts = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    entry = f"\n===== {ts} | {context} =====\n{tb}"
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(entry)
        # 肥大化防止：末尾200KBに丸める
        if ERROR_LOG.stat().st_size > 200_000:
            data = ERROR_LOG.read_text(encoding="utf-8")[-150_000:]
            ERROR_LOG.write_text(data, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[error_log] 記録失敗: {e}")
    print(f"[ERROR] {context}: {exc}")
    return f"{type(exc).__name__}: {str(exc)[:200]}"


_bg_tasks = set()   # 実行中の背景タスクの強参照（GCでの消滅を防ぐ）


def _track(task):
    """背景タスクへの参照を保持する。
    asyncio は task への参照が無くなると実行途中でも回収し得るため、
    保持しないと生成の完了監視などが【何も言わずに】消えることがある。"""
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)
    return task


# 実行中の背景作業（cid -> {作業名: 開始時刻}）。
# 「まだ？」と聞かれたとき、直近の生成ではなく【今やっていること】を答えるために使う。
_running = {}
# 長い作業の途中経過を何秒おきに流すか（0で無効）。黙り込みを防ぐため。
HEARTBEAT_SEC = int(os.getenv("HEARTBEAT_SEC", "90"))


# どの機能が発火したかの記録（cid -> 直近の [時刻, 発言, 機能名]）。
# 「変な挙動」の調査のたびに、発言から発火先を推測していた。実際に
# 「セルラーモデル」で生成モデル設定が出た件は、これがあれば一目で分かった。
_fired_log = {}
_fired_seq = {}
FIRED_KEEP = 15


# 記録はファイルにも残す。メモリだけだと再起動で消え、実際に
# 「いきなり何か出てきた」の調査時に2回続けて（記録なし）になった。
TRACE_FILE = HISTORY_DIR / "trace.jsonl"
TRACE_KEEP = 500
_trace_writes = 0


def _trace(cid, kind, text):
    """起きたことを1行ずつ残す（再起動しても消えない）。
    kind: "route"=発言の行き先 / "out"=ボットが実際に送った内容。"""
    global _trace_writes
    try:
        with TRACE_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(
                {"t": time.time(), "cid": cid, "kind": kind,
                 "text": (text or "")[:400]}, ensure_ascii=False) + "\n")
        _trace_writes += 1
        if _trace_writes % 50 == 0:          # たまに間引く（毎回読み書きしない）
            lines = TRACE_FILE.read_text(encoding="utf-8").splitlines()
            if len(lines) > TRACE_KEEP:
                TRACE_FILE.write_text(
                    "\n".join(lines[-TRACE_KEEP:]) + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass                                  # 記録の失敗で本体を止めない


def _trace_read(cid, kind, n):
    """記録を新しい順に n 件読んで、古い順に戻す。"""
    try:
        lines = TRACE_FILE.read_text(encoding="utf-8").splitlines()
    except Exception:  # noqa: BLE001
        return []
    out = []
    for ln in reversed(lines):
        try:
            d = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        if d.get("kind") == kind and (cid is None or d.get("cid") == cid):
            out.append(d)
            if len(out) >= n:
                break
    return list(reversed(out))


def _fired(cid, name, said=""):
    """この発言がどの機能に流れたかを残す。判定を変えた時の検証にも使う。"""
    log = _fired_log.setdefault(cid, [])
    log.append((time.time(), (said or "")[:60], name))
    del log[:-FIRED_KEEP]
    # 件数は上限で頭打ちになるので、通し番号を別に持つ
    # （長さで「新しく発火したか」を見ると、埋まった後は必ず素通りになる）
    _fired_seq[cid] = _fired_seq.get(cid, 0) + 1
    _trace(cid, "route", f"「{(said or '')[:60]}」 → {name}")
    return name


def _stamp(t):
    return datetime.fromtimestamp(t, JST).strftime("%m-%d %H:%M:%S")


def _fired_recent(cid, n=12):
    """直近の『発言 → 発火した機能』（再起動をまたいでも残る）。"""
    rows = [f"{_stamp(d['t'])}  {d['text']}" for d in _trace_read(cid, "route", n)]
    return "\n".join(rows) or "（記録なし）"


def _sent_recent(cid, n=12):
    """ボットが実際に送った内容。会話履歴に残らない投稿も含めて追える。
    『いきなり〇〇を出してきた』の調査は、これが無いと何も分からなかった。"""
    rows = []
    for d in _trace_read(cid, "out", n):
        body = " ".join((d.get("text") or "").split())[:160]
        rows.append(f"{_stamp(d['t'])}  {body}")
    return "\n".join(rows) or "（記録なし）"


def _busy_tasks(cid):
    """いま実行中の「ユーザーが待っている作業」（完了監視は除く）。"""
    return [(n, sec) for n, sec in _running_for(cid) if "監視" not in n]


# ---- 所要時間は「推測」ではなく「実測」で答える -------------------------
# 以前はここに手書きの目安表（デザイン3分、動画5分…）を置いていたが、
# あれは根拠のない当てずっぱうで、実際より短く出て「まだ終わらない」と
# 何度も待たせた。作業が終わるたびに実時間を記録し、その記録だけを根拠に
# 答える。記録が無い作業は「分からない」と正直に言う（短めに盛らない）。
TASK_TIMES_FILE = HISTORY_DIR / "task_times.json"
TASK_TIMES_KEEP = 30        # 作業ごとに残す実測の件数（直近ほど今の実力に近い）
_task_times = None          # {作業名: [秒, ...]} 遅延読み込み


def _load_task_times():
    global _task_times
    if _task_times is None:
        try:
            data = json.loads(TASK_TIMES_FILE.read_text(encoding="utf-8"))
            _task_times = {
                str(k): [float(x) for x in v if isinstance(x, (int, float))]
                for k, v in (data or {}).items() if isinstance(v, list)
            }
        except Exception:  # noqa: BLE001
            _task_times = {}
    return _task_times


def _record_task_time(name, sec):
    """作業1回分の実時間を残す。次からの見積もりの唯一の根拠になる。"""
    if not name or sec is None or sec < 1 or sec > 24 * 3600:
        return
    name = TASK_ALIASES.get(name, name)   # 表示名がぶれても実測は1か所に貯める
    times = _load_task_times()
    times.setdefault(name, []).append(round(float(sec), 1))
    times[name] = times[name][-TASK_TIMES_KEEP:]
    try:
        TASK_TIMES_FILE.write_text(
            json.dumps(times, ensure_ascii=False), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass        # 記録に失敗しても本体は止めない


# 表示名と実測を記録する名前の名寄せ。同じ作業を別名で呼んでいる箇所があり、
# そのままだと実測があるのに「不明」と答えてしまう。
TASK_ALIASES = {"動画/画像生成": "動画生成"}


def _task_stats(name):
    """(件数, 中央値秒, 最長秒)。実測が無ければ None（＝分からない）。"""
    times = _load_task_times()
    vals = sorted(times.get(name) or times.get(TASK_ALIASES.get(name, ""), []) or [])
    if not vals:
        return None
    mid = len(vals) // 2
    med = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
    return len(vals), med, vals[-1]


def _fmt_dur(sec):
    """秒を人が読む形に。端数を丸めて短く見せない（切り上げ寄り）。"""
    sec = int(math.ceil(float(sec)))
    if sec < 90:
        return f"{sec}秒"
    m, s = divmod(sec, 60)
    return f"{m}分" if s < 10 else f"{m}分{s}秒"


def _eta_hint(name):
    """作業を始めるときに出す所要時間の一言。実測が無ければそう言う。"""
    st = _task_stats(name)
    if not st:
        return "所要時間はまだ実測がないので不明です（今回の時間を記録します）"
    n, med, mx = st
    if med >= mx:
        return f"過去{n}回の実測では{_fmt_dur(mx)}ほど"
    return f"過去{n}回の実測では{_fmt_dur(med)}〜{_fmt_dur(mx)}"


def _eta_text(name, sec):
    """『あとどのくらい？』への答え。実測が無いときは推測で埋めない。"""
    st = _task_stats(name)
    if not st:
        return f"{sec}秒経過／残り時間は実測がないため不明"
    n, med, mx = st
    base = f"{sec}秒経過／実測{n}回では{_fmt_dur(med)}〜{_fmt_dur(mx)}"
    if sec >= mx:
        return (f"{base}。過去最長を超えています。"
                "もう少し待つか「やめて」で中止できます")
    if sec < med:
        return f"{base}（残りおよそ{_fmt_dur(med - sec)}〜{_fmt_dur(mx - sec)}）"
    return f"{base}（長めの回に入っています。残り最大{_fmt_dur(mx - sec)}）"


def _wants_heartbeat(name):
    """途中経過を流すか。実測で『最長でも短い』と分かっている作業だけ省く。
    未計測なら流す（黙り込ませるより、うるさい方がまし）。"""
    if not HEARTBEAT_SEC:
        return False
    st = _task_stats(name)
    return not (st and st[2] < HEARTBEAT_SEC)


def _running_note(cid):
    """実行中の作業をAIへ伝える一文。これを渡さないと、AIは裏で動いている
    ことを知らないまま「その機能は無い」と作り話をする（実際に起きた）。"""
    busy = _busy_tasks(cid) if cid is not None else []
    if not busy:
        # 何も動いていないことも明示する。空にしておくとAIが自由に
        # 「いま作業中」と作り話をした（実際に「性格分析表はもう少しかかる」
        # と言い続け、最後に「着手してなかった」と認める事故が起きた）。
        return (
            "【いま裏で実行中の作業】無し（何も動いていない）\n"
            "『作業中』『もう少しかかる』『できたら共有する』のように、"
            "動いていない作業を進行中だと言ってはいけない。"
            "頼まれた覚えがあるのに動いていないなら、"
            "『まだ手をつけていない。今からやる』と正直に言うこと。\n\n"
        )
    detail = "／".join(f"「{n}」({_eta_text(n, sec)})" for n, sec in busy[:3])
    return (
        f"【いま裏で実行中の作業】{detail}\n"
        "この作業は実際に動いていて、終わればこのチャンネルに結果が出る。"
        "進捗を聞かれたら『実行中』と答えること。"
        "『その機能は無い』『実装されていない』のように、"
        "動いている作業を否定することは絶対にしない。\n\n"
    )


class _Clock:
    """作業の開始時刻を持つ札。承認待ちを挟む作業では、承認が下りた時点で
    打ち直す（人が返事をするまでの時間を作業時間に混ぜると実測が狂うため）。"""
    __slots__ = ("t", "measurable")

    def __init__(self, t, measurable=True):
        self.t = t
        self.measurable = measurable    # 実測として記録してよいか


def _clock_start(v):
    """_running の値から開始時刻を取り出す（テストが素の数値を入れても動く）。"""
    return v.t if isinstance(v, _Clock) else v


# いま実行中の作業の札。作業の途中から「この時間は実測に入れない」と
# 言えるようにするため、呼び出しの深さに関係なく届く形で持つ。
_current_clock = contextvars.ContextVar("current_clock", default=None)


def _defer_measure():
    """『ここで終わりではなく、別の見張りが続きを引き継ぐ』という合図。
    生成は投入(数十秒)と完成待ち(数分〜十数分)に分かれていて、投入までを
    所要時間として記録すると実態より何倍も短い見積もりになる。"""
    c = _current_clock.get()
    if isinstance(c, _Clock):
        c.measurable = False


def _gen_task_name(job):
    """生成ジョブを、実測を貯める名前に振り分ける（種類ごとに時間が違う）。"""
    if "motion" in (job.get("model") or ""):
        return "モーション生成"
    return "動画生成" if job.get("media_type") == "video" else "画像生成"


def _start_work(cid, context):
    """承認待ちが終わり、実作業が始まった合図。ここから計り直す。"""
    v = (_running.get(cid) or {}).get(context)
    if isinstance(v, _Clock):
        v.t = time.time()
        v.measurable = True


def _running_for(cid):
    """このチャンネルで実行中の作業を [(名前, 経過秒)] で返す。"""
    now = time.time()
    return [(name, int(now - _clock_start(v)))
            for name, v in (_running.get(cid) or {}).items()]


async def _heartbeat(cid, context, every=90):
    """長い作業のあいだ、黙り込まないように定期的に一言入れる。
    聞かれる前に状況が出るので『反応が無い＝壊れた？』と思わせない。"""
    try:
        while True:
            await asyncio.sleep(every)
            busy = dict(_busy_tasks(cid))
            if context not in busy:
                return
            try:
                await send_as(orch, cid,
                              f"⏳ 「{context}」続行中（{_eta_text(context, busy[context])}）")
            except Exception:  # noqa: BLE001
                return
    except asyncio.CancelledError:
        return


def _spawn(coro, cid, context, gated=False):
    """背景タスクを、例外が沈黙しないラッパーで起動する。
    on_message のガードは create_task した処理には効かないため、ここで捕捉して
    errors.log 記録＋チャンネル通知する（裏側の静かな失敗を防ぐ）。
    実行中は _running に登録し、進捗を聞かれたら答えられるようにする。
    gated=True は「先に確認を取る作業」。承認が下りるまでは時間を測らない
    （人が返事をするまでの数分が『所要時間』に化けるのを防ぐ）。"""
    # 登録は「起動した瞬間」に行う。タスクが動き出すのを待つと、
    # 直後に「まだ？」と聞かれたときに実行中だと分からない。
    token = _Clock(time.time(), measurable=not gated)
    _running.setdefault(cid, {})[context] = token

    async def _wrapped():
        _current_clock.set(token)   # 途中で「まだ終わりではない」と言えるように
        # 実測で「速い」と分かっている作業以外は途中経過を流す
        hb = None
        if _wants_heartbeat(context):
            hb = asyncio.create_task(_heartbeat(cid, context, HEARTBEAT_SEC))
        ok = False
        try:
            await coro
            ok = True
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            summary = _log_error(f"bg:{context}", e)
            try:
                await send_as(orch, cid, f"⚠️ {context} でエラー（記録済み）: {summary}")
            except Exception:  # noqa: BLE001
                pass
            # 聞かれる前に状況を共有しておく（スクショ待ちをなくす）
            _track(asyncio.create_task(_autoshare_log(cid, f"bg:{context}")))
        finally:
            # 最後まで終わった回だけを実測に足す（失敗や中止の時間は混ぜない）
            if ok and token.measurable:
                _record_task_time(context, time.time() - token.t)
            d = _running.get(cid) or {}
            if d.get(context) is token:   # 同名の新しい作業を消さない
                d.pop(context, None)
            if hb:
                hb.cancel()
    return _track(asyncio.create_task(_wrapped()))


def _recent_errors(n=3):
    """直近n件のエラーを返す（Discord表示用）。"""
    try:
        if not ERROR_LOG.exists():
            return "記録されたエラーはありません（正常）。"
        blocks = ERROR_LOG.read_text(encoding="utf-8").split("\n===== ")
        blocks = [b for b in blocks if b.strip()]
        if not blocks:
            return "記録されたエラーはありません（正常）。"
        out = []
        for b in blocks[-n:]:
            lines = ("===== " + b).strip().splitlines()
            head = lines[0]
            # 例外の最終行（実際のエラー）を拾う
            err = next((ln for ln in reversed(lines)
                        if ln.strip() and not ln.startswith(" ")
                        and "Traceback" not in ln), lines[-1])
            out.append(f"🔴 {head}\n   {err.strip()[:300]}")
        return "\n".join(out)
    except Exception as e:  # noqa: BLE001
        return f"エラーログの読込に失敗: {e}"


# ---------- デバッグログの共有（Claude Codeのチャットから読めるようにする） ----------
# history/ はgit管理外なので、共有用に debug/ へ書き出してプッシュする。
# 目的：不具合のたびにユーザーがスクショを撮る手間をなくす。
DEBUG_DIR = Path(_BASE) / "debug"
DEBUG_LOG = DEBUG_DIR / "discord_log.md"


def _recent_messages(cid, limit=80):
    """会話ログの直近n件を [(時刻, 話者, 本文)] で返す。"""
    rows = []
    try:
        path = _hist_path(cid)
        if not path.exists():
            return rows
        for line in path.read_text(encoding="utf-8").splitlines()[-limit:]:
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            ts = datetime.fromtimestamp(d.get("t", 0), JST).strftime("%m/%d %H:%M")
            rows.append((ts, d.get("speaker", "?"), d.get("text", "")))
    except Exception as e:  # noqa: BLE001
        print(f"[debuglog] 読込失敗: {e}")
    return rows


async def _push_paths(paths, msg, tries=3):
    """指定ファイルをコミットして、リモートに追いついてからプッシュする。
    返り値: (成功したか, 失敗理由)。

    以前は pull せずに `git push` するだけだったため、Claude Code 側の新しい
    コミットがリモートにあると毎回拒否されていた。実際そのせいで
    「ログ送って」が一度も届かず、スクショに頼るしかない状態が続いた。
    拒否されたら fetch + rebase で追いついてから再試行する。"""
    paths = list(paths)
    rc, out = await _git_self(["add", "--"] + paths)
    if rc != 0:
        return False, f"git add に失敗: {out[:200]}"
    rc, _ = await _git_self(["diff", "--cached", "--quiet", "--"] + paths)
    if rc != 0:   # 差分あり（0なら前回と同内容なのでコミット不要）
        rc, out = await _git_self(["commit", "-m", msg, "--"] + paths)
        if rc != 0:
            return False, f"コミットに失敗: {out[:200]}"
    rc, branch = await _git_self(["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch.strip()
    last = ""
    for _ in range(tries):
        # 通信が遅いだけの場合に備えて、pushは長めに待つ（認証待ちで固まる問題は
        # _git_env() の非対話設定で数秒で失敗するようにしてある）
        rc, out = await _git_self(["push", "origin", "HEAD"], timeout=180)
        if rc == 0:
            return True, ""
        last = out
        if not branch or branch == "HEAD":
            break
        # リモートが先に進んでいるだけなら、追いついてから押し直す
        rc_f, _ = await _git_self(["fetch", "origin", branch])
        if rc_f != 0:
            return False, "GitHubに接続できませんでした（オフライン？）"
        rc_r, out_r = await _git_self(["rebase", f"origin/{branch}"])
        if rc_r != 0:
            await _git_self(["rebase", "--abort"])
            return False, f"リモートの変更に追いつけませんでした: {out_r[:200]}"
    hint = _git_fail_hint(last)
    # 何が起きているか分かるように、繋がるかどうかも一度だけ確かめる
    rc_ls, out_ls = await _git_self(["ls-remote", "--heads", "origin"], timeout=30)
    if rc_ls != 0 and not hint:
        hint = _git_fail_hint(out_ls) or f"GitHubに接続できません: {out_ls[:120]}"
    return False, f"プッシュに失敗: {last[:200]}" + (f"\n→ {hint}" if hint else "")


# 不具合を訴えている発言。これを見たら、頼まれる前にログを共有しておく
# （スクショを撮って送ってもらう手間をなくすため）。
# ボットの不具合を訴えているかの判定。
# 事故：「勝手に制作して上げてる動画なんだよね」（自分のYouTube動画の話）に
# 反応して、会話の途中で「🗂 状況を自動で共有しました」と割り込んだ。
# 「勝手に」「おかしい」は普通の日本語で日常的に使う語で、それ単体では
# ボットの話かどうか分からない。2段構えにして、単体で成立する言い方と、
# 何がおかしいのかを示す語とセットで初めて成立する言い方を分ける。
_TROUBLE_STRONG_RE = re.compile(
    "誤動作|変な挙動|変な動き|挙動が?おかしい|バグって|不具合|フリーズ|クラッシュ|"
    "反応しない|反応がない|返事がない|返事しない|返信がない|応答がない|"
    "エラーが?出|エラーになる|落ちてる|落ちてた|ちゃんと動いてない|読み込めない"
)
_TROUBLE_WEAK_RE = re.compile(
    "おかしい|動かない|動作しない|うまくいかない|うまく行かない|失敗してる|"
    "直ってない|直って無い|なおってない|治ってない|止まってる|固まってる|"
    "意味不明|変になる|変になった"
)
# 「ボットのこと」を言っていると分かる語。弱い言い方はこれとセットの時だけ拾う。
_TROUBLE_SUBJ_RE = re.compile(
    "ボット|ぼっと|bot|クロード|claude|ジェミニ|gemini|再起動|コード|プログラム|"
    "アプリ|機能|挙動|動作|エラー|ログ|生成|デザイン|サムネ|バナー|相関図|"
    "プロンプト|モデル|返事|返信|応答|投入|アップロード",
    re.I,
)
# 「勝手に○○した」は、ボットが頼んでいない動作をした時だけ訴えになる。
# 「勝手に制作して上げてる（自分が作った）」と区別するため、直後に
# ボット側の動作を表す語が来ている場合に限る。
_TROUBLE_AUTO_RE = re.compile(
    "勝手に[^。、\n]{0,15}(生成|作り直|投稿|始ま|動い|切り替わ|変わって|消え|送信|実行|返事)"
)


def _looks_trouble(content):
    """『ボットの調子がおかしい』という訴えか。普通の会話では鳴らさない。"""
    if not content:
        return False
    if _USER_REPORT_RE.search(content):     # お礼・成功報告はどう見ても不具合ではない
        return False
    if _TROUBLE_STRONG_RE.search(content) or _TROUBLE_AUTO_RE.search(content):
        return True
    return bool(_TROUBLE_WEAK_RE.search(content) and _TROUBLE_SUBJ_RE.search(content))
AUTOLOG_MIN_GAP = int(os.getenv("AUTOLOG_MIN_GAP", "600"))  # 連投を防ぐ間隔（秒）
_last_autolog = {}


async def _autoshare_log(cid, why=""):
    """不具合の訴えやエラーを検知したら、聞かれる前にログを共有する。
    開発側（Claude Codeのチャット）が中身を直接読めるようになるので、
    スクリーンショットのやり取りが要らなくなる。"""
    now = time.time()
    if now - _last_autolog.get(cid, 0) < AUTOLOG_MIN_GAP:
        return                       # 短時間の連投では送り直さない
    _last_autolog[cid] = now
    try:
        res = await _share_debug_log(cid)
    except Exception as e:  # noqa: BLE001
        print(f"[autolog] 共有に失敗: {str(e)[:150]}")
        return
    try:
        await send_as(orch, cid, (
            "🗂 状況を自動で共有しました（スクショなしで開発側から直接見られます）"
            if res.startswith("✅") else res
        ))
    except Exception:  # noqa: BLE001
        pass


async def _freshness_note():
    """走っているコードが最新かどうかの一言。
    「直したのに直っていない」の多くは、古い版のまま試していたことが原因。
    ログの一行目で分かるようにしておく。"""
    try:
        has_new, n = await _remote_has_new_code()
    except Exception:  # noqa: BLE001
        return "（最新かどうか確認できず）"
    if not has_new:
        return "（最新）"
    return (f"（⚠️ **{n}件古い**。未反映の修正があります"
            f"{'／自動更新はオン' if _auto_update_on() else '／自動更新はオフ'}）")


async def _share_debug_log(cid, limit=80):
    """直近の会話・エラー・生成状態をリポジトリに書き出してプッシュする。
    プッシュ後は Claude Code のチャット側から中身を直接読める。"""
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
    _, head = await _git_self(["rev-parse", "--short", "HEAD"])
    lines = [
        "# Discord デバッグログ（自動共有）",
        f"- 書き出し: {now}",
        f"- 実行中のコード: {head.strip()[:12]}{await _freshness_note()}",
        f"- チャンネル: {cid}",
        "",
        "## 生成の状態",
        f"- 直前の生成: {json.dumps(_load_last_gen(cid) or {}, ensure_ascii=False)[:500]}",
        f"- 進行中ジョブ: {json.dumps(_load_motion_job() or {}, ensure_ascii=False)[:300]}",
        f"- 実際に投入されたプロンプト: {(_last_submitted.get('prompt') or '(記録なし)')[:400]}",
        f"- モデル設定: {json.dumps(gen_settings, ensure_ascii=False)[:300]}",
        f"- Geminiクールダウン中: {[m for m, t in _gemini_cooldown.items() if t > time.time()]}",
        "",
        "## 発言がどの機能に流れたか（新しいものほど下）",
        "```",
        _fired_recent(cid, 12),
        "```",
        "",
        "## ボットが実際に送った内容（会話履歴に残らないものも含む）",
        "```",
        _sent_recent(cid, 12),
        "```",
        "",
        "## 直近のエラー",
        "```",
        _recent_errors(3)[:4000],
        "```",
        "",
        f"## 直近の会話（{limit}件まで）",
    ]
    for ts, who, text in _recent_messages(cid, limit):
        lines.append(f"- **{ts} {who}**: {text[:600]}")
    DEBUG_LOG.write_text("\n".join(lines), encoding="utf-8")

    # _git_self は discord-groupchat/ で動くので、パスもそこからの相対にする
    # （リポジトリroot基準にすると git add がファイルを見つけられない）
    rel = str(DEBUG_LOG.relative_to(Path(_BASE)))
    ok, why = await _push_paths([rel], f"Discordログを共有（{now}）")
    if not ok:
        return (f"⚠️ ログを共有できませんでした: {why}\n"
                "（書き出し自体は完了しているので、次回の共有で一緒に届きます）")
    return (f"✅ 直近の会話・エラー・生成状態を共有しました（{rel}）。\n"
            "Claude Codeのチャットで「**ログ見て**」と言えば、そのまま読めます。\n"
            "※会話の内容がGitHubのプライベートリポジトリに保存されます。")


async def _self_diagnose():
    """各サブシステムを能動チェックして健全性レポートを返す（Discord内で完結）。"""
    lines = ["🩺 **システム自己診断**"]

    # ① ルーティング＋E2Eの回帰テスト（別プロセスで隔離実行）
    for tf, name in (("test_routing.py", "ルーティング"),
                     ("test_phrasing.py", "言い回し"),
                     ("simulate.py", "E2Eシミュレーション")):
        try:
            r = await asyncio.to_thread(
                subprocess.run, [sys.executable, tf],
                capture_output=True, text=True, timeout=180, cwd=BASE_DIR,
            )
            tail = (r.stdout or "").strip().splitlines()[-1] if r.stdout else ""
            lines.append(("✅ " if r.returncode == 0 else "❌ ") + f"{name}テスト: {tail}")
        except Exception as e:  # noqa: BLE001
            lines.append(f"⚠️ {name}テスト実行不可: {str(e)[:120]}")

    # ② Gemini（軽い呼び出し）
    try:
        await asyncio.wait_for(_gemini_call("ping。'ok'とだけ返して"), timeout=40)
        lines.append("✅ Gemini API: 応答あり")
    except Exception as e:  # noqa: BLE001
        lines.append(("⚠️ " if _is_quota_error(e) else "❌ ")
                     + f"Gemini API: {str(e)[:100]}")

    # ③ Claude CLI
    try:
        out = await run_claude_cli("'ok'とだけ返して")
        lines.append("✅ Claude CLI: 応答あり" if out else "⚠️ Claude CLI: 空応答")
    except Exception as e:  # noqa: BLE001
        lines.append(f"❌ Claude CLI: {str(e)[:120]}")

    # ④ 設定・キー
    lines.append(("✅ " if YOUTUBE_API_KEY else "⚠️ ")
                 + f"YouTube APIキー: {'あり' if YOUTUBE_API_KEY else '未設定（!trend不可）'}")
    lines.append(("✅ " if os.getenv("HIGGSFIELD_API_KEY") else "⚠️ ")
                 + f"Higgsfield キー: {'あり' if os.getenv('HIGGSFIELD_API_KEY') else '未設定'}")

    # ⑤ 直近エラー
    lines.append("\n【直近のエラー】\n" + _recent_errors(2))
    return "\n".join(lines)


# ---------- 人間のプロファイル（パーソナライズ） ----------
# 発言が PROFILE_UPDATE_EVERY 件たまるごとに、その人の発言サンプルから
# 人物像（性格・口調・好み・興味・価値観）を自動学習して保存。
# 保存先: history/profile_{名前}.md。全AIの応答プロンプトに常時注入される。
PROFILE_UPDATE_EVERY = int(os.getenv("PROFILE_UPDATE_EVERY", "20"))
PROFILE_MAX_CHARS = int(os.getenv("PROFILE_MAX_CHARS", "1500"))
_profile_counts = {}   # speaker -> 前回更新からの発言数
_profiling = set()     # 更新処理中の speaker


def _profile_path(speaker):
    safe = re.sub(r"[^\w぀-ヿ一-鿿]+", "_", speaker)[:40]
    return HISTORY_DIR / f"profile_{safe}.md"


_profiles_cache = {"key": None, "text": ""}


def _load_profiles():
    """保存済みの全員分のプロファイルをまとめて返す（プロンプト注入用）。
    毎メッセージで呼ばれるため、ファイル更新時刻ベースでキャッシュする。"""
    try:
        files = sorted(HISTORY_DIR.glob("profile_*.md"))
        key = tuple((f.name, f.stat().st_mtime) for f in files)
        if key == _profiles_cache["key"]:
            return _profiles_cache["text"]
        parts = [f.read_text(encoding="utf-8").strip() for f in files]
        text = "\n\n".join(p for p in parts if p)[:PROFILE_MAX_CHARS * 2]
        _profiles_cache["key"], _profiles_cache["text"] = key, text
        return text
    except Exception as e:  # noqa: BLE001
        print(f"[profile] 読込失敗: {e}")
        return _profiles_cache["text"]


def _profiles_context():
    p = _load_profiles()
    if not p:
        return ""
    return (
        "【参加者のプロファイル（これまでの会話から学習した人物像。"
        "口調・好み・関心に合わせて応答すること。"
        "本人から『俺のことどう思ってる？』『私ってどんな人？』のように"
        "自分の印象・人物像を聞かれたら、このプロファイルを踏まえて"
        "率直かつ具体的に、自分の言葉で答えること）】\n" + p + "\n\n"
    )


def _speaker_recent_msgs(cid, speaker, n=60):
    """チャンネルの全ログから、その人の直近の発言を集める。"""
    fp = _hist_path(cid)
    if not fp.exists():
        return []
    out = []
    for ln in reversed(fp.read_text(encoding="utf-8").splitlines()):
        try:
            d = json.loads(ln)
        except Exception:  # noqa: BLE001
            continue
        if d.get("speaker") == speaker:
            out.append(d.get("text", "")[:300])
            if len(out) >= n:
                break
    return list(reversed(out))


async def _update_profile(cid, speaker):
    """発言サンプルから人物プロファイルを更新（バックグラウンド・Claude優先）。"""
    if speaker in _profiling:
        return
    _profiling.add(speaker)
    try:
        msgs = _speaker_recent_msgs(cid, speaker)
        if len(msgs) < 5:
            return  # 材料不足
        old = ""
        try:
            fp = _profile_path(speaker)
            if fp.exists():
                old = fp.read_text(encoding="utf-8").strip()
        except Exception:  # noqa: BLE001
            pass
        prompt = (
            f"あなたは観察眼の鋭い記憶係。Discordでの {speaker} の発言サンプルをもとに、"
            "本人の人物像プロファイルを更新する。\n"
            "含める: 性格、話し方・口調の特徴、好き/嫌い、興味関心、よく話す話題、"
            "仕事や活動、目標・やりたいこと、大事にしている価値観、接し方のコツ。\n"
            "発言に根拠のある特徴だけを書き、決めつけや過度な推測はしない。"
            "古い情報と矛盾したら新しい発言を優先。\n"
            f"{PROFILE_MAX_CHARS}字以内、箇条書き中心。冒頭は『## {speaker} のプロファイル』。\n\n"
            f"【既存プロファイル】\n{old or '(まだ無し)'}\n\n"
            f"【{speaker} の最近の発言】\n" + "\n".join(f"- {m}" for m in msgs) +
            "\n\n更新後のプロファイル:"
        )
        new = (await _ai_text_bg(prompt, "profile") or "").strip()[:PROFILE_MAX_CHARS]
        if new:
            _profile_path(speaker).write_text(new, encoding="utf-8")
            print(f"[profile] {speaker} のプロファイルを更新（発言{len(msgs)}件から）")
    except Exception as e:  # noqa: BLE001
        print(f"[profile] 更新失敗: {str(e)[:200]}")
    finally:
        _profiling.discard(speaker)


# ---------- 導入前の過去ログをDiscordから一度だけ取り込む ----------
IMPORT_LIMIT = int(os.getenv("IMPORT_LIMIT", "2000"))  # 1チャンネルあたり最大取込件数
_import_started = set()


async def _backfill_channel_history(channel):
    """ボット導入前・永続化導入前の古いメッセージをDiscord APIから遡って取り込む。
    チャンネルごとに一度だけ実行（history/{cid}.imported が目印）。"""
    cid = channel.id
    marker = HISTORY_DIR / f"{cid}.imported"
    if cid in _import_started or marker.exists():
        return
    _import_started.add(cid)
    try:
        from datetime import datetime, timezone

        fp = _hist_path(cid)
        existing = fp.read_text(encoding="utf-8") if fp.exists() else ""
        before = None
        for ln in existing.splitlines():
            try:
                before = datetime.fromtimestamp(json.loads(ln)["t"], tz=timezone.utc)
                break
            except Exception:  # noqa: BLE001
                continue

        rows = []
        async for m in channel.history(limit=IMPORT_LIMIT, before=before):
            text = (m.content or "").strip()
            if not text:
                continue
            rows.append({
                "t": m.created_at.timestamp(),
                "speaker": m.author.display_name,
                "text": text,
            })
        rows.reverse()  # 古い順に直す
        if rows:
            with open(fp, "w", encoding="utf-8") as f:
                for r in rows:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                f.write(existing)
            histories.pop(cid, None)  # 次アクセス時に再読込
        marker.write_text("done", encoding="utf-8")
        print(f"[history] 過去ログ取込完了: channel={cid}, {len(rows)}件")
    except Exception as e:  # noqa: BLE001
        print(f"[history] 過去ログ取込失敗（権限「メッセージ履歴を読む」を確認）: {str(e)[:200]}")


# ---------- 各AIへの問い合わせ ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))  # .claude/settings.json のある場所


async def _reap(proc, timeout=5):
    """kill した子プロセスの終了を待って回収する（ゾンビの蓄積を防ぐ）。"""
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except Exception:  # noqa: BLE001
        pass


# 直近の返事をどのエンジンが書いたか。AIに自分で名乗らせると
# 「俺はGeminiじゃなくてクロード」のような混乱が起きるので、コード側で把握して
# ラベルを付ける。ユーザーが「誰が答えたか」を見分けられるようにするため。
_last_engine = {"name": ""}
# 今の返事を実際に書いたのは誰か（_last_engine は裏方の処理に汚されるので別に持つ）
_wrote = {"name": "", "why": ""}
GEMINI_STANDIN = "Gemini（クロードの代打）"


def _model_args():
    """claude CLI に渡すモデル指定。未設定ならCLIの既定にまかせる。
    gen_settings は起動後も書き換わるので、呼ぶたびに読み直す
    （＝Discordで切り替えたら次の発言から即反映される）。"""
    m = (gen_settings.get("claude_model") or "").strip()
    return ["--model", m] if m else []


async def run_claude_cli(prompt, background=False):
    """Claude Code CLI をヘッドレスで呼ぶ（サブスク利用・API課金なし）。
    プロンプトは stdin で渡す（長文でOSの引数上限を超えないように）。
    同時実行はセマフォで制限し、渋滞によるタイムアウトを防ぐ。
    background=True の裏方処理は追加の関門を通り、会話用の枠を空けたままにする。"""
    if background:
        async with _get_bg_sem():
            return await _claude_cli_run(prompt)
    return await _claude_cli_run(prompt)


async def _claude_cli_run(prompt):
    # cwd を固定 → discord-groupchat/.claude/settings.json（WebSearch許可）が読まれる。
    # ※ワークスペースを一度「信頼(trust)」しておかないと settings.json は無視される。
    async with _get_claude_sem():
        proc = await asyncio.create_subprocess_exec(
            CLAUDE_BIN, "-p", *_model_args(),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=BASE_DIR,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(input=prompt.encode()), timeout=CLAUDE_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            await _reap(proc)   # 待たないとゾンビが残り続ける（常駐プロセスのため）
            raise RuntimeError(f"claude CLI がタイムアウトしました（{CLAUDE_TIMEOUT}秒）")
    _last_engine["name"] = CLAUDE2_NAME
    out_s = out.decode(errors="replace").strip()
    err_s = err.decode(errors="replace").strip()
    if proc.returncode != 0:
        # CLIはエラーを stdout に出すことがあるため両方見る。全文はターミナルへ。
        detail = err_s or out_s or f"claude CLI error (exit={proc.returncode}, 出力なし)"
        print(
            f"[claude_cli] 失敗 exit={proc.returncode}\n"
            f"  stderr: {err_s[:1000] or '(空)'}\n"
            f"  stdout: {out_s[:1000] or '(空)'}"
        )
        low = detail.lower()
        if "usage limit" in low or "rate limit" in low:
            raise RuntimeError(
                "Claude Code サブスクの利用上限に達しています。"
                "時間をおいて再度お試しください。 / " + detail[:200]
            )
        if "log in" in low or "login" in low or "authent" in low or "api key" in low:
            raise RuntimeError(
                "claude CLI が未ログインの可能性があります。"
                "Macのターミナルで `claude` を一度実行してログインしてください。 / " + detail[:200]
            )
        raise RuntimeError(detail[:500])
    return out_s


# --- 応答ルールは「いつでも安全なもの」と「ボットの話の時だけのもの」に分ける ---
#
# なぜ分けるのか（何度も同じ事故を起こしたので必ず読むこと）:
#   運用ルールを全部の返事に混ぜると、AIはその文言自体を返事として出してしまう。
#   実例 ①「動画できた？と送れば確認できます」が無関係な雑談に付いた
#        ②「再起動してください」が毎回の返事の末尾に付いた
#        ③ 体のツボの痛みの相談に「原因が分かっていないので『ログ送って』で
#           状況を共有してください」と答えた
#   どれも原因は同じ＝「〜の時だけ」という条件をAI任せにしたこと。
#   条件はプロンプトの文章ではなく、コード側で渡す/渡さないを決めて担保する。

# 話題に関係なく常に守らせる「話し方」のルール。
# ここには“具体的な案内文”を書かない（書くとそれが返事として漏れる）。
TALK_RULES = (
    "話し方のルール: 返事の先頭に「クロード:」のような話者名を付けない（本文だけを書く）。"
    "前置き・名乗り・定型の案内文を毎回付けない。"
    "自分がClaudeかGeminiか、他にどの役がいるかといった内輪の説明はしない"
    "（ユーザーが聞いているのはその話ではない）。"
    "過去の自分の発言で正体やAIの構成について説明していても蒸し返さない"
    "（訂正・補足・お詫びも含めて一切触れない）。"
    "ユーザーの最後の発言の中身そのものに答えること。"
    "答えが分からない話題では、知らないことは知らないと普通の言葉で言い、"
    "ボットの操作案内にすり替えない。"
)

# ボットの運用・制作物の話をしている時【だけ】渡すルール。
# 雑談や一般知識の質問には渡さない（渡すと上の事故が再発する）。
OPS_RULES = (
    "運用ルール: ユーザーの操作は常にDiscord内で完結させ、ターミナルコマンドは案内しない。"
    "【厳禁】『Claude Codeのセッションで相談して』『開発側に聞いて』"
    "『別のツールで』のように、ユーザーをDiscordの外へ回してはいけない"
    "（実際にそう答えて話が止まった）。自分の構成や仕様の話も、"
    "分かる範囲でその場で普通に答える。仕様変更が要るなら"
    "『それは作り込みが要るから、直してほしければそう言って』と伝えるだけでよい。"
    "『再起動してください』という案内は、ユーザーが"
    "【コードの修正をボットに反映すること】について聞いてきた時だけ使う。"
    "作業結果の報告や普通の質問の末尾に再起動の案内を付けるのは禁止。"
    "『「動画できた？」と送れば自動確認される』という案内は、ユーザーが生成の"
    "進捗・完成について聞いてきた時だけ使う。同じ案内を1つの返事で繰り返すのも禁止。"
    "ユーザーが動画やYouTubeリンクを共有しただけの時は、その内容への感想や会話で"
    "普通に返す（機能案内をしない）。"
    "『モーション動画できた？』という古い言い回しは、過去の会話ログに"
    "残っていても絶対に真似しない。"
    "生成の完成/未完成を自分で推測して断言しない（確認は自動チェックに任せる）。"
    "ボットの不具合について、実際のエラーを確認していない原因を作り話しない"
    "（『ツールの権限が下りていない』『APIの制限で』等と推測で言わない）。"
    "ボットの動きが分からないときだけ『「ログ送って」で状況を共有してほしい』と頼む"
    "——それ以外の話題でこの案内を出すのは禁止。"
    "【重要】内部の状態を具体的な数字・時刻で語らない。"
    "『12:50に上限がリセットされた』『あと3回で枠切れ』など、"
    "実際に確認していない枠の残量・リセット時刻・完了時刻を作ってはいけない。"
    "進捗は上に書かれた実行中の情報だけを使い、書かれていなければ"
    "『分からない』と言う。"
    "所要時間も同じで、上に書かれた実測の数字だけを使う。"
    "『3分ほどで終わります』『すぐできます』のように自分で時間を作らない"
    "（実際より短く言われて待たされるのが一番困る、と本人から指摘されている）。"
    "実測が書かれていなければ『まだ実測がないので分からない』と正直に言う。"
    "生成物が意図と違ったとき、ユーザーの指示の書き方のせいにするのは禁止"
    "（『具体的すぎる』『曖昧すぎる』『うまく汲み取れなかった』等と言わない）。"
    "どんな自然な言い方でも適切なプロンプトに翻訳するのはこちらの責任。"
    "違っていたら素直に謝り、『どこが違いましたか？』と1点だけ聞いて、"
    "『〇〇を直して作り直して』で直せると案内する。"
    "進捗確認のために再起動を勧めるのは禁止"
    "（再起動すると進行中の完了監視が止まってしまうため）。"
)

# 「ボット自身・制作の話をしているか」の判定語。ここに無い話題（体調・雑談・
# 一般知識など）では運用ルールを一切渡さないので、案内文が漏れようがない。
_OPS_TOPIC_RE = re.compile(
    # 事故：「オーケストレーターの運用について、いい方法ある？」に対して
    # 自分の構成の話だと気づかず、koheiの生活サポートの話として答えた。
    # 自分自身の呼び名・役割の名前が入っていなかったため。
    r"(オーケストレーター|オケストレーター|orchestrator|"
    r"クロード[123]|リサーチャー|アドバイザー|話者|校閲|精査|連携|役割分担|"
    r"ボット|ぼっと|bot|再起動|起動|コード|プログラム|バグ|エラー|不具合|"
    # 「作っ」「できた」「完成」を素で入れていたため、『アップルパイ作った』
    # 『料理完成した』のような日常会話にも運用ルールが混ざっていた。
    # 制作物の名前（動画・画像・サムネ…）が下に並んでいるので、それで足りる。
    r"直し|修正|反映|デプロイ|ログ|進捗|生成|"
    # 進捗の聞き方だけを拾う（「アップルパイ作った」のような報告は拾わない）
    r"できた[？?]|できてる|まだできて|完成した[？?]|"
    # 「アップ」だけだと『アップルウォッチ』『アップグレード』にも当たり、
    # 物の売買の相談にボットの運用ルールを混ぜてしまった。動画を上げる話に限る。
    r"動画|映像|画像|イラスト|サムネ|バナー|字幕|編集|プロンプト|投稿|"
    r"相関図|関係図|家系図|組織図|年表|デザイン|フローチャート|マインドマップ|"
    r"アップ(ロード|デート|し|する|した|して)|"
    r"チャンネル|discord|ディスコ|claude|クロード|gemini|ジェミニ|"
    r"api|mcp|higgs|ヒッグス|クレジット|課金|youtube|ユーチューブ|ショート)",
    re.I,
)


def _recent_text(history, n=4):
    """直近の発言をつないだ文字列（話題判定用）。"""
    return "\n".join(t for _, t in (history or [])[-n:] if t)


# このボットが実際にできること。AIは自分の機能一覧を知らないので、
# 渡さないと「その機能は無い」「まだ実装できていない」と作り話をする。
# 実際に、動いている最中のデザイン制作を「機能自体が無い」と否定し、
# 再起動後もその発言を履歴から読んで繰り返す事故が起きた。
BOT_CAPABILITIES = (
    "・動画生成（モデル指定も自動選定も可）／動画の編集（字幕・尺・縦型化・連結）\n"
    "・画像生成（Geminiの無料枠）\n"
    "・デザイン制作＝HTMLで組んでPNGに書き出す。サムネ・バナー・チラシ・"
    "スライド・価格表に加えて、相関図・家系図・年表・組織図・フローチャートも作れる\n"
    "・モーション転写／作り直し・修正\n"
    "・YouTubeリサーチ／自分のチャンネルの実績分析（週次レポート）／バズ度予測\n"
    "・参考動画からのスタイル学習／料金・残クレジットの照会\n"
    "・長い動画（1GBまで）からショートの切り抜き。素材はYouTubeのリンク・"
    "動画の添付・直リンク・Macのファイルのパスのいずれでもよい。"
    "字幕を読んで見どころを選び、"
    "Mac上のffmpegで縦型9:16に切り出して日本語字幕を焼き付ける。"
    "字幕が付いていない動画は、音声から文字起こしして字幕を自分で作る。"
    "生成モデルを使わないのでクレジットは消費しない\n"
    "・自己改修・再起動・ログ共有／会話モデルの切替\n"
    "【どれで作るかの既定】\n"
    "・図・表・相関図・年表・サムネ・バナーなど【文字が主役】のもの＝"
    "クロードがHTMLで組んでPNGに書き出す（無料・文字が崩れない）\n"
    "・絵そのもの＝まずGeminiの無料枠\n"
    "・Higgsfield（クレジット消費）は【名指しで頼まれた時】と【動画】だけ。"
    "うまくいかない時に黙ってHiggsfieldへ切り替えてはいけない\n"
    "【返事の作られ方（聞かれたら答えてよい）】\n"
    "・オーケストレーターが受け取り、内容で担当を決める\n"
    "・返事はクロードが書く。長い返事はGeminiが校閲し、指摘があれば"
    "クロードが直す（Geminiは本文を書き直さない＝文体を保つため）\n"
    "・クロード1=リサーチャー / クロード2=PM（返事担当） / クロード3=アドバイザー。"
    "『クロード1に聞いて』『多角的に見て』で複数の視点を出せる\n"
    "・この仕組みは【すでに動いている】。『できない』『実装が必要』と答えないこと\n"
)
# 道具の名前の意味。機能一覧だけ渡しても、名前の意味を知らないと
# 「ヒッグスフィールドって何ですか？」とユーザーに聞き返してしまう
# （実際に起きた。AIの学習データに無い固有名詞なので、こちらで教える）。
BOT_GLOSSARY = (
    "【この環境の用語】\n"
    "・Higgsfield（ヒッグスフィールド）＝動画・画像を生成するプラットフォーム。"
    "このボットはMCP経由で使う。生成するとクレジットを消費する。"
    "ffmpegやPlaywrightが入ったクラウドLinux（sandbox_exec）も使えて、"
    "動画の編集やデザインの画像化はそこで行う\n"
    "・クレジット＝Higgsfieldの利用単位。動画1本で10〜25程度\n"
    "・Veo / Kling / Seedance / Sora＝Higgsfieldで使える動画モデル\n"
    "・Nano Banana / Soul / Seedream＝画像モデル\n"
    "・Gemini＝Googleの無料枠のAI。判定・画像や動画の読み取り・要約・"
    "回答の精査など裏方を担当する\n"
    "・クロード1/2/3＝リサーチャー / PM（返事担当）/ アドバイザー\n"
    "・MCP＝外部の道具をAIから使うための接続方式\n"
    "これらはこの環境で日常的に使っている道具なので、"
    "ユーザーに「〜とは何ですか」と聞き返してはいけない。\n"
)
CAPABILITY_RULES = (
    BOT_GLOSSARY
    + "【このボットができること】\n" + BOT_CAPABILITIES
    + "上にある機能を『無い』『実装されていない』『今この場では作れない』と"
    "言ってはいけない。過去の自分の発言でそう言っていても、それは誤りなので"
    "繰り返さない。作業が終わっていないだけなら『まだ実行中』と答えること。"
    "実際にエラーが出た場合だけ、そのエラーの内容を伝える。\n"
)


def ops_guide(context_text=""):
    """その場の話題に合わせた応答ルールを返す。
    ボット・制作の話でなければ運用ルールは渡さない（案内文の誤爆防止）。"""
    text = context_text if isinstance(context_text, str) else _recent_text(context_text)
    if not _OPS_TOPIC_RE.search(text or ""):
        return TALK_RULES
    return TALK_RULES + OPS_RULES + CAPABILITY_RULES


# 「いまの実際の値」を聞かれている話題。記憶で答えると必ず古い/嘘になる。
# 実例:「アップルウォッチSeries7セルラーを売りたいので本文と金額教えて」に
# ちゃんと答えられなかった（相場を調べず、シリアルからも何も分からないまま）。
_FACT_TOPIC_RE = re.compile(
    "相場|買取|下取り|出品|売りたい|売れる|売値|いくらで売|"
    "メルカリ|ラクマ|ヤフオク|フリマ|中古|定価|実売|時価|"
    "今の(値段|価格|相場)|現在の(値段|価格)|在庫|発売日|最新(価格|情報|版)"
)
_SELL_TOPIC_RE = re.compile("出品|売りたい|売る|売却|手放|買取|下取り|フリマ")
FACT_RULES = (
    "【この話題で必ず守ること】いまの実際の値を聞かれている。"
    "自分の記憶で答えると必ず古くなるので、WebSearch / WebFetch で実際に"
    "調べてから答えること（このワークスペースでは許可されている）。"
    "調べた結果が得られなかった時は、金額を推測で断定せず"
    "『調べられなかった』と正直に言う。それらしい数字を作らない。\n"
    "型番・シリアル番号から仕様（サイズ・色・容量・世代）を断定しない。"
    "メーカーの確認ページで調べるか、分からなければ本人に聞くこと"
    "（近年のシリアルは規則性が無く、見ただけでは分からない）。\n"
    "金額を答えるときは、①調べた売れ筋の価格帯 ②その根拠（どこの相場か）"
    "③価格を左右する条件（状態・付属品・サイズ・バッテリー最大容量など）"
    "を分けて書く。足りない情報は最後に1〜2点だけ聞く（質問攻めにしない）。\n"
)
SELL_RULES = (
    "売却・出品の相談なので、聞かれていれば出品用の本文もそのまま貼れる形で書く："
    "タイトル（商品名・型番・サイズ・色）／状態／付属品の有無／"
    "使用期間・バッテリー状態／発送方法／注意書き（すり替え防止・返品可否）。"
    "分からない項目は勝手に埋めず「【要確認】」と書いて本人に確認させる。\n"
)


def fact_guide(context_text=""):
    """相場・最新の実データを聞かれている時だけ、調べさせるルールを渡す。
    それ以外の話題には渡さない（無関係な会話に指示文が漏れるのを防ぐ）。"""
    text = context_text if isinstance(context_text, str) else _recent_text(context_text)
    text = text or ""
    if not _FACT_TOPIC_RE.search(text):
        return ""
    return FACT_RULES + (SELL_RULES if _SELL_TOPIC_RE.search(text) else "")


def topic_guide(context_text=""):
    """その場の話題に応じたルール一式（運用＋実データ）。"""
    return ops_guide(context_text) + fact_guide(context_text)


# 後方互換（外部から参照された場合は全部入りを返す）
BOT_OPS_GUIDE = TALK_RULES + OPS_RULES


def peer_persona(me, partner, history=None):
    return (
        f"あなたは{me}。人間たちと{partner}が参加するDiscordのグループチャットにいる。"
        f"{partner}も人間も対等な仲間。日本語で{REPLY_CHARS}字以内、1発言だけで自然に参加する。"
        "前置きや名乗りは不要。直前の流れを踏まえ、自分の視点も述べること。"
        "自分や相手の過去の発言と同じ内容の繰り返しは厳禁。毎回、新しい内容を足すこと。"
        + topic_guide(history)
    )


def peer_prompt(me, partner, history):
    return (
        peer_persona(me, partner, history) + "\n\n" + _profiles_context()
        + transcript_block(history)
        + f"\n\n上の会話ログの流れに続けて、{me} としての発言を1つだけ書いてください。"
        "ログや指示文をそのまま繰り返さず、発言の本文だけを書くこと。"
    )


async def ask_claude(history):
    return await run_claude_cli(peer_prompt("Claude", "Gemini", history))


# ---------- Claudeの役割ペルソナ（同じサブスクの中で視点を分ける） ----------
# Discordのアカウントは増やさず、同じClaudeを別の役割で呼び分ける。
# アカウントを増やすとトークン発行など手作業が要るうえ、
# 実体は同じClaudeなので、役割（プロンプト）を変えるだけで目的は達せられる。
# 表に出る話者の名前。1=調べる人、2=進める人、3=別の見方を出す人。
CLAUDE1_NAME = "クロード1（リサーチャー）"
CLAUDE2_NAME = "クロード2（PM）"
CLAUDE3_NAME = "クロード3（アドバイザー）"

CLAUDE_PERSONAS = {
    "claude1": (
        CLAUDE1_NAME,
        "あなたはリサーチャー。事実・数字・出典・前提条件を集めて整理することに徹する。"
        "推測と事実を必ず区別し、分からないことは『不明』と書く。"
        "意見や評価は述べず、判断材料だけを箇条書きで簡潔に並べる。",
    ),
    "claude3": (
        CLAUDE3_NAME,
        "あなたはアドバイザー。ひとつの結論に飛びつかず、"
        "賛成・反対・第三の見方を並べ、見落とされがちな観点やリスクを指摘する。"
        "『こう見ることもできる』という角度を最低3つ挙げ、最後に一番妥当だと思う見方を1行で示す。"
        "広告・企画・打ち出し方の相談では、一流広告代理店の"
        "クリエイティブディレクターとして振る舞う。"
        "誰に何を言うかを定め、切り口を複数出し、"
        "外した時のリスクまで示したうえで推しを1つ選ぶ。",
    ),
}


async def _ask_claude_persona(role, history):
    """役割つきでClaudeに答えさせる（同じCLIを別の人格で呼ぶ）。"""
    name, persona = CLAUDE_PERSONAS[role]
    prompt = (
        f"あなたは{name}。{persona}\n"
        f"日本語で{REPLY_CHARS}字以内、前置きや名乗りは不要、回答本体のみ。"
        + topic_guide(history) + "\n\n"
        + transcript_block(history)
        + "\n\n上の会話ログの最後の発言に、あなたの立場で答えてください。"
        "ログや指示文をそのまま繰り返さず、回答の本文だけを書くこと。"
    )
    return await run_claude_cli(prompt)


GEMINI_VIEW_PERSONA = (
    "あなたは別のモデル（Gemini）としての視点担当。"
    "クロードとは違う切り口を出すことに徹する。"
    "特に、最新の動向・数字・具体例・視覚的/体験的な観点・"
    "見落とされている前提を挙げる。"
    "確実でないことは『不明』と書き、推測を事実のように書かない。"
    f"日本語{REPLY_CHARS}字以内、箇条書きで簡潔に。前置き不要。"
)


async def _ask_gemini_view(history):
    """Geminiに『別の視点』だけを出させる（返事そのものは書かせない）。"""
    prompt = (
        GEMINI_VIEW_PERSONA + "\n\n" + transcript_block(history)
        + "\n\n上の会話の最後の論点について、あなたの視点を出してください。"
    )
    return await _gemini_call(prompt, "gemini_view")


async def _run_multi_view(message, content, roles=None):
    """複数の視点で検討して統合する。クロードの2役に加えてGeminiにも
    別の切り口を出させ、最後はクロードが1つにまとめて答える
    （＝声はひとつ、頭は複数）。"""
    cid = message.channel.id
    roles = roles or ["claude1", "claude3"]
    history = get_history(cid)
    await send_as(orch, cid, "🧠 複数の視点で検討します（少し時間がかかります）…")
    tasks = [_ask_claude_persona(r, history) for r in roles]
    use_gemini = not _gemini_all_cooling()
    if use_gemini:
        tasks.append(_ask_gemini_view(history))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    names = [CLAUDE_PERSONAS[r][0] for r in roles]
    if use_gemini:
        names.append("Gemini（別モデルの視点）")
    ok = []
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            print(f"[multi_view] {name} 失敗: {str(res)[:150]}")
            continue
        text = (res or "").strip()
        if not text:
            continue
        ok.append((name, text))
        # Geminiの視点も、投稿するのはクロード側のアカウント。
        # Gemini自身に喋らせない（返信の声をひとつに保つ）。
        speaker = claude_bot if _gemini_replies_on() is False else (
            gemini_bot if name.startswith("Gemini") else claude_bot)
        await send_as(speaker, cid, f"**{name}**\n{text}")
        add_history(cid, name, text)
    if len(ok) < 2:
        return
    try:
        # まとめは必ずクロードが書く（出す声をひとつに保つため）
        merged = await run_claude_cli(
            "次の複数の視点を統合し、最終的な結論と次に取るべき一手を"
            f"日本語{REPLY_CHARS}字以内でまとめて。重複は削り、"
            "食い違う点があれば理由とともにどちらが妥当か示すこと。"
            "誰がどう言ったかの実況は不要、結論本体だけを書く。\n\n"
            + "\n\n".join(f"【{n}】\n{t}" for n, t in ok),
            background=True,
        )
    except Exception as e:  # noqa: BLE001
        print(f"[multi_view] 統合に失敗: {str(e)[:120]}")
        return
    if merged and merged.strip():
        add_history(cid, "Orchestrator", merged.strip())
        await send_as(orch, cid, f"🧩 **まとめ**\n{merged.strip()}")


def _is_quota_error(e):
    m = str(e)
    return "429" in m or "RESOURCE_EXHAUSTED" in m or "quota" in m.lower()


class GeminiQuotaExceeded(RuntimeError):
    """Gemini の無料枠が全モデルで（一時的に）使い切られたことを表す。"""


def _retry_delay(e, default=8):
    m = re.search(r"retry in ([0-9.]+)s", str(e)) or re.search(r"retryDelay'?:?\s*'?([0-9.]+)s", str(e))
    try:
        return min(float(m.group(1)), 30) if m else default
    except Exception:  # noqa: BLE001
        return default


# 枠切れモデルのクールダウン（model -> この時刻まではスキップ）。時間が来たら自動復帰。
GEMINI_COOLDOWN_SEC = int(os.getenv("GEMINI_COOLDOWN_SEC", "1800"))  # 既定30分
_gemini_cooldown = {}
_gemini_rr = {"i": 0}  # ラウンドロビン用インデックス

# Gemini全滅（枠切れ）を検知したチャンネル。復活監視ループが復活を通知する。
_gemini_watch = {"outage_cid": None}


def _gemini_all_cooling():
    """全モデルがクールダウン中（＝実質的にGemini全滅）かどうか。"""
    now = time.time()
    return bool(GEMINI_MODELS) and all(
        _gemini_cooldown.get(m, 0) > now for m in GEMINI_MODELS
    )


async def _gemini_recovery_loop():
    """Gemini無料枠の復活を5分おきに自動確認し、復活したらチャンネルに知らせる。"""
    while True:
        await asyncio.sleep(300)
        cid = _gemini_watch.get("outage_cid")
        if cid and not _gemini_all_cooling():
            _gemini_watch["outage_cid"] = None
            try:
                await send_as(
                    orch, cid,
                    "✅ Gemini が復活しました（クールダウン明け）。"
                    "動画の視聴・画像分析・リサーチがまた使えます。"
                )
            except Exception as e:  # noqa: BLE001
                print(f"[gemini_watch] 復活通知の送信失敗: {e}")


async def _gemini_call(prompt, tag="gemini"):
    """テキスト生成（非同期）。実装は _gemini_contents_sync に集約。"""
    text = await asyncio.to_thread(_gemini_contents_sync, [prompt], tag)
    _last_engine["name"] = "Gemini"
    return text


async def ask_gemini(history):
    return await _gemini_call(peer_prompt("Gemini", "Claude", history))


async def _ai_text(prompt, tag="ai_text"):
    """テキスト生成：その時動いているエンジンを優先。Geminiが全滅中なら
    Claudeへ直行（無駄な試行と誤判定を避ける）。両方失敗時のみ例外が伝播。"""
    if _gemini_all_cooling():
        # Gemini枠切れ中 → Claude中心（意図判定が不安定になるのを防ぐ）
        try:
            return await run_claude_cli(prompt)
        except Exception as e:  # noqa: BLE001
            print(f"[{tag}] Claude失敗 → Gemini再試行: {str(e)[:150]}")
            return await _gemini_call(prompt)
    try:
        return await _gemini_call(prompt)
    except Exception as e:  # noqa: BLE001
        print(f"[{tag}] Gemini失敗 → Claudeへフォールバック: {str(e)[:150]}")
        return await run_claude_cli(prompt)


async def _ai_text_bg(prompt, tag="ai_text_bg"):
    """バックグラウンド処理用テキスト生成：速度不問なので Claude（サブスク定額）を
    優先して Gemini の無料枠を温存する。Claude 失敗時のみ Gemini へ。
    裏方専用の関門を通すので、会話の返事の枠を奪わない。"""
    try:
        return await run_claude_cli(prompt, background=True)
    except Exception as e:  # noqa: BLE001
        print(f"[{tag}] Claude失敗 → Geminiへフォールバック: {str(e)[:150]}")
        return await _gemini_call(prompt)


# ---------- Web検索：Google（Geminiグラウンディング）優先・DDGフォールバック ----------
SEARCH_RESULTS_N = int(os.getenv("SEARCH_RESULTS_N", "5"))


def _google_search_sync(query):
    """Gemini の Google 検索グラウンディングで最新情報を取得。(要約, 出典URL群)。"""
    from google.genai import types

    resp = gemini_client.models.generate_content(
        model=GEMINI_MODEL,
        contents=query,
        config=types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        ),
    )
    text = resp.text or ""
    sources = []
    try:
        for cand in resp.candidates or []:
            gm = getattr(cand, "grounding_metadata", None)
            for ch in getattr(gm, "grounding_chunks", None) or []:
                web = getattr(ch, "web", None)
                uri = getattr(web, "uri", None) if web else None
                if uri:
                    title = getattr(web, "title", "") or uri
                    sources.append(f"{title}: {uri}")
    except Exception:  # noqa: BLE001
        pass
    return text, sources


def _ddg_search_sync(query, n):
    try:
        from ddgs import DDGS
    except ImportError:
        from duckduckgo_search import DDGS  # 旧パッケージ名フォールバック
    with DDGS() as d:
        return list(d.text(query, max_results=n))


# 既定は "ddg"（無料・Gemini枠を消費しない）。Google検索したいなら .env で "google"。
SEARCH_ENGINE = os.getenv("SEARCH_ENGINE", "ddg")


async def _ddg_context(query):
    try:
        results = await asyncio.to_thread(_ddg_search_sync, query, SEARCH_RESULTS_N)
    except Exception as e:  # noqa: BLE001
        print(f"[ddg_search] 失敗: {e}")
        return ""
    if not results:
        return ""
    lines = []
    for r in results:
        title = r.get("title") or ""
        body = r.get("body") or r.get("snippet") or ""
        href = r.get("href") or r.get("url") or ""
        lines.append(f"- {title}\n  {body}\n  {href}")
    return "【Web検索結果（最新情報。根拠として使い、URLも示す）】\n" + "\n".join(lines)


async def web_search_context(query):
    """queryをWeb検索し、AIに渡す文脈テキストを返す。失敗時は空文字。"""
    if not query.strip():
        return ""
    # SEARCH_ENGINE=google のときだけ Gemini(Google)グラウンディングを使う（枠を消費）。
    if SEARCH_ENGINE == "google":
        try:
            text, sources = await asyncio.to_thread(_google_search_sync, query)
            if text or sources:
                block = "【Google検索の要約（最新情報。根拠として使い、URLも示す）】\n" + text
                if sources:
                    block += "\n\n参考URL:\n" + "\n".join(f"- {s}" for s in sources[:6])
                return block
        except Exception as e:  # noqa: BLE001
            print(f"[google_search] 失敗→DuckDuckGoにフォールバック: {e}")
    # 既定：DuckDuckGo（Geminiの無料枠を使わない）
    return await _ddg_context(query)


# ---------- 添付ファイル処理（画像・動画・音声） ----------
# AIに中身を読ませる（Geminiに丸ごと渡す）ときの上限。
# ここを大きくしても、モデル側が受け取れないので意味がない。
MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024  # 20MB
# 素材として【扱う】ときの上限。切り抜き・編集・文字起こしは
# ディスク上のファイルさえあればよいので、こちらは大きくてよい。
# メモリに載せず、少しずつディスクへ書き出す。
MAX_VIDEO_SIZE = int(os.getenv("MAX_VIDEO_MB", "1024")) * 1024 * 1024  # 既定1GB
SUPPORTED_IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
SUPPORTED_VIDEO_TYPES = {".mp4", ".webm", ".mov", ".avi"}
SUPPORTED_AUDIO_TYPES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac"}
SUPPORTED_DOC_TYPES = {".pdf"}
SUPPORTED_TEXT_TYPES = {".txt", ".md", ".csv", ".log", ".json", ".py", ".js", ".html"}
TEXT_ATTACHMENT_MAX_CHARS = int(os.getenv("TEXT_ATTACHMENT_MAX_CHARS", "12000"))


def _find_attachment(message, exts):
    """添付から指定した種類（拡張子の集合）の最初の1件を返す。無ければ None。"""
    return next(
        (a for a in message.attachments
         if Path(a.filename).suffix.lower() in exts),
        None,
    )


# 一時画像ファイル保存先
TEMP_IMAGE_DIR = Path(os.getenv("TEMP_IMAGE_DIR", "/tmp/discord_images"))
TEMP_IMAGE_DIR.mkdir(parents=True, exist_ok=True)


async def _download_to_file(url, dest, max_bytes=MAX_VIDEO_SIZE, chunk=1 << 20):
    """URLの中身を【メモリに載せずに】ディスクへ書き出す。
    丸ごと読み込む方式だと1GBの動画でメモリを食い潰すため、少しずつ書く。
    返り値: (成功か, 説明)。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    got = 0
    try:
        timeout = aiohttp.ClientTimeout(total=3600, sock_read=120)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return False, f"取得に失敗しました（HTTP {resp.status}）"
                declared = int(resp.headers.get("Content-Length") or 0)
                if declared and declared > max_bytes:
                    return False, (f"{declared / 1048576:.0f}MB あり、"
                                   f"上限の{max_bytes / 1048576:.0f}MBを超えています")
                with dest.open("wb") as f:
                    async for part in resp.content.iter_chunked(chunk):
                        got += len(part)
                        if got > max_bytes:
                            f.close()
                            dest.unlink(missing_ok=True)
                            return False, (f"上限の{max_bytes / 1048576:.0f}MBを"
                                           "超えたので中断しました")
                        f.write(part)
    except Exception as e:  # noqa: BLE001
        dest.unlink(missing_ok=True)
        return False, f"取得中にエラー: {str(e)[:200]}"
    if got == 0:
        dest.unlink(missing_ok=True)
        return False, "中身が空でした"
    return True, f"{got / 1048576:.1f}MB"


async def _download_file(url):
    """URLからファイルをダウンロード。バイナリで返す。"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                if len(data) > MAX_ATTACHMENT_SIZE:
                    return None
                return data
    except Exception as e:
        print(f"[attachment_download] 失敗: {e}")
        return None




def _detect_mime_type(data):
    """画像データから MIME タイプを推測（magic number で判定）。"""
    if data.startswith(b'\xff\xd8\xff'):
        return "image/jpeg"
    elif data.startswith(b'\x89PNG'):
        return "image/png"
    elif data.startswith(b'GIF8'):
        return "image/gif"
    elif data.startswith(b'RIFF') and b'WEBP' in data[:12]:
        return "image/webp"
    else:
        return "image/jpeg"  # デフォルト


# ---------- 画像生成（絵コンテ用・Gemini優先で無料枠を活用） ----------
# gemini-2.5-flash-image（Nano Banana）は無料枠 約500枚/日。
# IMAGE_GEN_ENGINE=higgsfield にすると従来どおり Higgsfield を使う。
IMAGE_GEN_ENGINE = os.getenv("IMAGE_GEN_ENGINE", "gemini")

# ---------- 生成モデルの実行時切替（Discordの発言で変更・再起動後も保持） ----------
GEN_SETTINGS_FILE = HISTORY_DIR / "gen_settings.json"


def _load_gen_settings():
    d = {"image_engine": IMAGE_GEN_ENGINE, "image_app": None, "video_app": None,
         # 会話を担当する claude CLI のモデル。""＝CLIの既定にまかせる
         "claude_model": os.getenv("CLAUDE_MODEL", ""),
         # 軽い雑談を誰が答えるか（"claude" / "gemini"）
         "casual_lead": ""}
    d.update(_read_json(GEN_SETTINGS_FILE))
    return d


gen_settings = _load_gen_settings()


def _save_gen_settings():
    _write_json(GEN_SETTINGS_FILE, gen_settings, "gen_settings")


# ---------- 会話モデル（claude CLI）の切替 ----------
# CLI が受け付ける別名をそのまま渡す（版が上がっても追従できるので固定IDは持たない）。
CLAUDE_MODEL_ALIASES = {
    "haiku": ("haiku", "Haiku（軽量・最速）"),
    "ハイク": ("haiku", "Haiku（軽量・最速）"),
    "はいく": ("haiku", "Haiku（軽量・最速）"),
    "sonnet": ("sonnet", "Sonnet（標準）"),
    "ソネット": ("sonnet", "Sonnet（標準）"),
    "そねっと": ("sonnet", "Sonnet（標準）"),
    "opus": ("opus", "Opus（最高性能・低速）"),
    "オーパス": ("opus", "Opus（最高性能・低速）"),
    "おーぱす": ("opus", "Opus（最高性能・低速）"),
    "既定": ("", "CLIの既定"),
    "デフォルト": ("", "CLIの既定"),
    "標準": ("", "CLIの既定"),
}
# 「モデル」＋名前、または「〜にして/変えて/切り替えて」の言い方を拾う
_MODEL_SWITCH_RE = re.compile(
    r"(モデル|model|エンジン)?\s*(?:を|は)?\s*"
    r"(haiku|ハイク|はいく|sonnet|ソネット|そねっと|opus|オーパス|おーぱす|"
    r"既定|デフォルト|標準)\s*(?:に|へ)?\s*"
    r"(?:して|しろ|変えて|変更|切り替え|切替|に戻して|戻して|でお願い|で)",
    re.I,
)
_MODEL_ASK_RE = re.compile(
    r"(いま|今|現在|どの)?\s*(モデル|model)\s*(は|って|何|なに|教えて|確認)")


def _match_claude_model(text):
    """発言から切り替え先のモデルを判定。(値, 表示名) か None。"""
    m = _MODEL_SWITCH_RE.search(text or "")
    if not m:
        return None
    return CLAUDE_MODEL_ALIASES.get(m.group(2).lower())


def _current_model_label():
    v = gen_settings.get("claude_model") or ""
    for val, label in CLAUDE_MODEL_ALIASES.values():
        if val == v and val:
            return label
    return "CLIの既定（未指定）" if not v else v


# モデル名の呼び名 → 設定。target: video(Higgsfield動画) / image_hf(Higgsfield画像) /
# image_gemini(Gemini画像=Nano Banana・無料枠)
GEN_ALIASES = {
    "クリング": ("video", "kling-video/v2.5-turbo/pro/image-to-video"),
    "kling": ("video", "kling-video/v2.5-turbo/pro/image-to-video"),
    "シーダンス": ("video", "bytedance/seedance/v1/pro/image-to-video"),
    "seedance": ("video", "bytedance/seedance/v1/pro/image-to-video"),
    "ハイルオ": ("video", "minimax/hailuo-02/pro/image-to-video"),
    "hailuo": ("video", "minimax/hailuo-02/pro/image-to-video"),
    "minimax": ("video", "minimax/hailuo-02/pro/image-to-video"),
    "dop": ("video", "higgsfield/dop-turbo/image2video"),
    "ナノバナナ": ("image_gemini", None),
    "nano banana": ("image_gemini", None),
    "nanobanana": ("image_gemini", None),
    "gemini画像": ("image_gemini", None),
    "シードリーム": ("image_hf", "bytedance/seedream/v4/text-to-image"),
    "seedream": ("image_hf", "bytedance/seedream/v4/text-to-image"),
    "フラックス": ("image_hf", "flux-pro/kontext/max/text-to-image"),
    "flux": ("image_hf", "flux-pro/kontext/max/text-to-image"),
}

# モデル指定の意図を示す語（「クリングってどう？」のような雑談での誤発動を防ぐ）
_GEN_INTENT_RE = re.compile(
    "使って|つかって|がいい|にして|に設定|に変更|に切り替|切替|指定|で生成|で作|でお願い"
)


def _apply_model_mentions(content):
    """発言中のモデル名を検出して生成設定に反映。変更内容の説明リストを返す。"""
    if not _GEN_INTENT_RE.search(content):
        return []
    low = content.lower()
    changed = []
    for name, (target, value) in GEN_ALIASES.items():
        if name not in low:
            continue
        if target == "video":
            if gen_settings.get("video_app") != value:
                gen_settings["video_app"] = value
                changed.append(f"🎬 動画モデル → {name}（{value}）")
        elif target == "image_gemini":
            if gen_settings.get("image_engine") != "gemini":
                gen_settings["image_engine"] = "gemini"
                changed.append(f"🎨 画像モデル → {name}（Gemini・無料枠）")
        elif target == "image_hf":
            if (gen_settings.get("image_engine"), gen_settings.get("image_app")) != ("higgsfield", value):
                gen_settings["image_engine"] = "higgsfield"
                gen_settings["image_app"] = value
                changed.append(f"🎨 画像モデル → {name}（Higgsfield: {value}）")
    if changed:
        _save_gen_settings()
    return changed


def _gen_settings_summary():
    img = ("Gemini（Nano Banana・無料枠）" if gen_settings["image_engine"] == "gemini"
           else f"Higgsfield: {gen_settings['image_app'] or 'flux-pro/kontext/max（既定）'}")
    vid = gen_settings["video_app"] or "higgsfield 既定（dop）"
    return f"🎨 画像: {img}\n🎬 動画: {vid}"
# 画像生成に使うモデル。1つに固定していたため、そのIDが使えなくなった時に
# 「無料枠は残っているのに作れない」状態になり、理由も出なかった。
# 上から順に試し、通ったものを覚える。
GEMINI_IMAGE_MODELS = [
    m.strip() for m in os.getenv(
        "GEMINI_IMAGE_MODEL",
        "gemini-2.5-flash-image,"
        "gemini-2.5-flash-image-preview,"
        "gemini-2.0-flash-preview-image-generation",
    ).split(",") if m.strip()
]
GEMINI_IMAGE_MODEL = GEMINI_IMAGE_MODELS[0]
_gemini_image_ok = {"model": ""}      # 一度通ったモデルを次回から先に試す
_gemini_img_rr = {"i": 0}             # ラウンドロビン用インデックス


def _cooldown_note(models):
    """クールダウン中のモデルが、あと何分で戻るかの一言。"""
    now = time.time()
    left = [max(0, _gemini_cooldown.get(m, 0) - now) for m in models]
    left = [x for x in left if x > 0]
    if not left:
        return "枠が戻るまで少し待ってください"
    return f"あと約{math.ceil(min(left) / 60)}分で自動的に戻ります"


def _image_from_resp(resp):
    """Geminiの返答から画像バイト列を取り出す。無ければ None。"""
    for cand in getattr(resp, "candidates", None) or []:
        content = getattr(cand, "content", None)
        for part in (getattr(content, "parts", None) or []):
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None)
            if data:
                return data
    return None


def _gemini_generate_image_sync(prompt, ref_bytes=None, ref_mime="image/png"):
    """Gemini の画像生成モデルで画像を作り、PNGバイト列を返す。
    ref_bytes を渡すと、その画像を元にした編集（背景差し替え等）になる。

    無料枠の扱いはテキスト側（_gemini_contents_sync）と同じ方針にそろえてある:
      ・枠切れのモデルはクールダウンして次のモデルへローテーション（時間で自動復帰）
      ・毎回ちがうモデルから始めて負荷を分散する
      ・分あたり制限なら少し待って同じモデルで1度だけ再挑戦
      ・全部だめなら GeminiQuotaExceeded（＝無料枠切れとして扱える）
    クールダウンの記録はテキスト側と同じ _gemini_cooldown を共有するので、
    「ログ送って」の『Geminiクールダウン中』にそのまま出る。"""
    contents = [prompt]
    if ref_bytes:
        contents = [_make_media_part(ref_bytes, ref_mime), prompt]
    n = len(GEMINI_IMAGE_MODELS)
    if n == 0:
        raise RuntimeError("GEMINI_IMAGE_MODELS が空です")
    start = _gemini_img_rr["i"]
    _gemini_img_rr["i"] = (start + 1) % n
    order = [GEMINI_IMAGE_MODELS[(start + k) % n] for k in range(n)]
    # 前に通ったモデルがあれば、そこから試す（毎回の空振りを避ける）
    if _gemini_image_ok["model"] in order:
        order.remove(_gemini_image_ok["model"])
        order.insert(0, _gemini_image_ok["model"])

    now = time.time()
    last_err, tried, errs = None, False, []
    for model in order:
        if _gemini_cooldown.get(model, 0) > now:
            errs.append(f"{model}: クールダウン中")
            continue                      # 枠切れ中は次のモデルへ
        tried = True
        for attempt in range(2):
            try:
                resp = gemini_client.models.generate_content(
                    model=model, contents=contents)
                data = _image_from_resp(resp)
                if data:
                    _gemini_image_ok["model"] = model
                    print(f"[image_gen] 成功: {model}")
                    return data
                errs.append(f"{model}: 画像が返らなかった")
                break                     # 本文が空 → 次のモデルへ
            except Exception as e:  # noqa: BLE001
                last_err = e
                errs.append(f"{model}: {str(e)[:120]}")
                print(f"[image_gen] {model} 失敗: {str(e)[:200]}")
                per_day = "PerDay" in str(e) or "PerProjectPerModel" in str(e)
                if _is_quota_error(e) and not per_day and attempt == 0:
                    time.sleep(_retry_delay(e))   # 分あたり制限 → 少し待つ
                    continue
                if per_day or not _is_quota_error(e):
                    _gemini_cooldown[model] = time.time() + GEMINI_COOLDOWN_SEC
                    if _gemini_image_ok["model"] == model:
                        _gemini_image_ok["model"] = ""
                break

    if last_err and _is_quota_error(last_err):
        raise GeminiQuotaExceeded(
            "Gemini画像の無料枠が全モデルで上限に達しています。"
            f"（{_cooldown_note(GEMINI_IMAGE_MODELS)}）"
        )
    if not tried:
        raise GeminiQuotaExceeded(
            "Gemini画像は全モデルがクールダウン中です（無料枠切れ）。"
            f"（{_cooldown_note(GEMINI_IMAGE_MODELS)}）"
        )
    if last_err:
        raise last_err
    raise RuntimeError(" / ".join(errs) or "Geminiが画像を返しませんでした")


async def _fetch_image_bytes(url, limit=12 << 20):
    """画像URLを取得してバイト列とMIMEを返す。取れなければ (None, "")。"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url, timeout=aiohttp.ClientTimeout(total=60)
            ) as resp:
                if resp.status != 200:
                    return None, ""
                mime = (resp.headers.get("Content-Type") or "image/png").split(";")[0]
                data = await resp.content.read(limit + 1)
                if len(data) > limit:
                    return None, ""
                return data, (mime if mime.startswith("image/") else "image/png")
    except Exception as e:  # noqa: BLE001
        print(f"[ref] 画像を取得できません: {str(e)[:120]}")
        return None, ""


async def send_image_bytes(cid, text, data, filename):
    """画像バイト列をDiscordに添付送信し、CDN上のURLを返す（動画化の入力に使う）。"""
    channel = orch.get_channel(cid) or await orch.fetch_channel(cid)
    msg = await channel.send(
        (text or "")[:1900],
        file=discord.File(io.BytesIO(data), filename=filename),
    )
    return msg.attachments[0].url if msg.attachments else None


IMAGE_ANALYSIS_PROMPT = (
    "この画像の内容を詳細に分析してください。\n"
    "① 画像内のすべてのテキストを正確に抽出（OCR）。改行や表の構造もできるだけ保つ。\n"
    "② 構図・レイアウト・視点を説明。\n"
    "③ 主要な要素・オブジェクト・配置・色を列挙。"
)


def _make_media_part(data, mime_type):
    """SDKバージョン差を吸収してメディアPart（画像/音声/動画/PDF）を作る。"""
    from google.genai import types

    # 新しめのSDK: Part.from_bytes / 古い呼び方: inline_data=Blob
    if hasattr(types.Part, "from_bytes"):
        return types.Part.from_bytes(data=data, mime_type=mime_type)
    return types.Part(inline_data=types.Blob(data=data, mime_type=mime_type))


def _gemini_contents_sync(contents, tag):
    """Gemini 呼び出しの唯一の実装（テキストもメディアもここを通る）。
    contents は Part/テキストの混在リスト。無料枠の扱いは全てここに集約:
      ・枠切れモデルはクールダウンして次のモデルへローテーション（時間で自動復帰）
      ・毎回ちがうモデルから始めて負荷を分散
      ・分あたり制限なら少し待って同じモデルで1度だけ再挑戦
    全モデル失敗時は握りつぶさず送出（無料枠切れは GeminiQuotaExceeded）。
    ※以前はテキスト用とメディア用に同じ処理が2つあり、方針が食い違っていた。"""
    n = len(GEMINI_MODELS)
    if n == 0:
        raise RuntimeError("GEMINI_MODELS が空です")
    start = _gemini_rr["i"]
    _gemini_rr["i"] = (start + 1) % n
    order = [GEMINI_MODELS[(start + k) % n] for k in range(n)]

    now = time.time()
    last_err = None
    tried = False
    for model in order:
        if _gemini_cooldown.get(model, 0) > now:
            continue  # 枠切れクールダウン中は次のモデルへ
        tried = True
        for attempt in range(2):
            try:
                resp = gemini_client.models.generate_content(model=model, contents=contents)
                text = (resp.text or "").strip()
                if text:
                    print(f"[{tag}] 成功: {model}")
                    return text
                break  # 本文が空 → 次のモデルへ
            except Exception as e:
                last_err = e
                print(f"[{tag}] {model} 失敗: {str(e)[:200]}")
                per_day = "PerDay" in str(e) or "PerProjectPerModel" in str(e)
                if _is_quota_error(e) and not per_day and attempt == 0:
                    time.sleep(_retry_delay(e))   # 分あたり制限 → 少し待って再挑戦
                    continue
                if per_day or not _is_quota_error(e):
                    # 日次枠切れ/その他エラー → このモデルをしばらく避ける（自動復帰）
                    _gemini_cooldown[model] = time.time() + GEMINI_COOLDOWN_SEC
                break

    if last_err and _is_quota_error(last_err):
        raise GeminiQuotaExceeded(
            "Gemini の無料枠が全モデルで上限に達しています。時間をおいて再度お試しください。"
        )
    if last_err:
        raise last_err
    if not tried:
        # 全モデルがクールダウン中＝実質的に枠切れ
        raise GeminiQuotaExceeded("Gemini の全モデルがクールダウン中です（無料枠切れ）。")
    return ""


def _gemini_analyze_media_sync(data, mime_type, prompt, tag):
    """Gemini API にメディア（画像/音声/動画/PDF）を渡して分析結果を得る。"""
    print(f"[{tag}] MIME type: {mime_type}, size: {len(data)} bytes")
    try:
        media_part = _make_media_part(data, mime_type)
    except Exception as e:
        print(f"[{tag}] Part生成失敗: {str(e)[:200]}")
        return ""
    return _gemini_contents_sync([media_part, prompt], tag)


AUDIO_MIME_BY_EXT = {
    ".mp3": "audio/mp3",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".aac": "audio/aac",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".opus": "audio/ogg",
    ".flac": "audio/flac",
}

VIDEO_MIME_BY_EXT = {
    ".mp4": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".avi": "video/x-msvideo",
}

AUDIO_TRANSCRIBE_PROMPT = (
    "この音声を正確に文字起こししてください。\n"
    "① 話された内容をすべて日本語（音声が他言語ならその言語）で書き起こす。\n"
    "② 複数の話者がいる場合は「話者A:」「話者B:」のように区別する。\n"
    "③ 聞き取れない箇所は（聞き取り不能）と記す。\n"
    "④ 最後に【要約】として2〜3行で内容をまとめる。"
)

PDF_ANALYSIS_PROMPT = (
    "このPDFの内容を人が読んだのと同じレベルで詳細に読み取ってください。\n"
    "① 文書の種類と目的（例: 契約書・論文・請求書・スライド資料）。\n"
    "② 本文の内容を章・節の構造を保って書き出す。表は表として整形する。\n"
    "③ 図やグラフがあれば、何を示しているかを説明する。\n"
    "④ 最後に【要約】として重要ポイントを箇条書きでまとめる。"
)

VIDEO_ANALYSIS_PROMPT = (
    "この動画の内容を人が視聴したのと同じレベルで詳細に説明してください。\n"
    "① 映像の流れを時系列で説明（場面の変化・登場人物・動き・テロップ）。\n"
    "② 音声・ナレーション・会話があればすべて文字起こしする。\n"
    "③ 画面に映るテキストや資料も読み取る。\n"
    "④ 最後に【要約】として2〜3行で内容をまとめる。"
)


def _media_spec(ext):
    """添付の種類ごとの処理仕様を返す。
    (種別名, MIMEタイプ, 解析プロンプト, ログtag, 見出し, サイズ超過時の助言) """
    if ext in SUPPORTED_IMAGE_TYPES:
        return ("画像", None, IMAGE_ANALYSIS_PROMPT, "gemini_analyze_image",
                "画像", "")   # 画像はMIME自動判定・サイズ上限なし
    if ext in SUPPORTED_AUDIO_TYPES:
        return ("音声", AUDIO_MIME_BY_EXT.get(ext, "audio/mp3"),
                AUDIO_TRANSCRIBE_PROMPT, "audio_transcribe",
                "音声の書き起こし", "分割して送ってください。")
    if ext in SUPPORTED_VIDEO_TYPES:
        return ("動画", VIDEO_MIME_BY_EXT.get(ext, "video/mp4"),
                VIDEO_ANALYSIS_PROMPT, "gemini_analyze_video",
                "動画の内容", "短く切り出すか圧縮して送ってください。")
    if ext in SUPPORTED_DOC_TYPES:
        return ("PDF", "application/pdf", PDF_ANALYSIS_PROMPT, "gemini_analyze_pdf",
                "PDFの内容", "分割して送ってください。")
    return None


async def extract_attachment_context(message):
    """メッセージの添付ファイル（画像・音声・動画・PDF・テキスト）を処理してコンテキストを返す。
    画像=OCR＋構図分析 / 音声=書き起こし / 動画=映像＋音声の内容分析 /
    PDF=全文読み取り（いずれもGemini）/ テキスト系=そのまま読み込み。
    ※種類ごとに同じ処理（サイズ確認→DL→Gemini解析→整形）を4回書いていたのを
      仕様テーブル + 共通ループに集約した。"""
    if not message.attachments:
        return ""

    contexts = []
    for att in message.attachments:
        filename = att.filename
        ext = Path(filename).suffix.lower()
        spec = _media_spec(ext)

        if spec:
            kind, mime, prompt, tag, head, advice = spec
            if kind != "画像" and att.size > MAX_ATTACHMENT_SIZE:
                contexts.append(
                    f"【{kind} {filename} (約{att.size / 1048576:.1f}MB)】\n"
                    f"（20MBを超えるため読み取れません。{advice}）"
                )
                continue
            data = await _download_file(att.url)
            if not data:
                contexts.append(f"【{kind} {filename}】\n（ダウンロード失敗。）")
                continue
            try:
                async with message.channel.typing():
                    analysis = await asyncio.to_thread(
                        _gemini_analyze_media_sync, data,
                        mime or _detect_mime_type(data), prompt, tag,
                    )
                if not analysis:
                    contexts.append(f"【{kind} {filename}】\n（内容の読み取りに失敗しました。）")
                    continue
                contexts.append(f"【{head}: {filename}】\n{analysis}")
                if kind == "音声":   # 書き起こしは本文もチャンネルに出す
                    await send_long(message.channel, analysis,
                                    f"📝 **{filename} の書き起こし**\n")
            except GeminiQuotaExceeded as e:
                print(f"[{tag}] 枠切れ: {filename}")
                contexts.append(f"【{kind} {filename}】\n（{e}）")
                if kind == "音声":
                    await message.channel.send(f"⚠️ 書き起こしできませんでした: {e}")
            except Exception as e:  # noqa: BLE001
                print(f"[{tag}] 失敗: {filename}: {str(e)[:100]}")
                contexts.append(f"【{kind} {filename}】\n（分析エラー。テキストで内容を説明してください。）")

        elif ext in SUPPORTED_TEXT_TYPES:
            data = await _download_file(att.url)
            if not data:
                contexts.append(f"【ファイル {filename}】\n（ダウンロード失敗。）")
                continue
            text = data.decode("utf-8", errors="replace")
            if len(text) > TEXT_ATTACHMENT_MAX_CHARS:
                text = text[:TEXT_ATTACHMENT_MAX_CHARS] + "\n…（以下省略）"
            contexts.append(f"【ファイルの内容: {filename}】\n{text}")

        else:
            contexts.append(f"【ファイル: {filename}】")

    if not contexts:
        return ""
    return "\n\n【メッセージに添付されたファイル】\n" + "\n".join(contexts)


# ---------------------------------------------------------------------------
# YouTube急上昇の自動リサーチ（毎日）
#   ① YouTube Data API で急上昇TOP100を取得
#   ② 上位数本（TREND_DEEP_COUNT）を Gemini が実際に「視聴」して
#      演出・カット割り・カメラワーク・顔の動き・CG/VFX などを分析
#   ③ レポートを insights/ に保存し、ダイジェストをDiscordに投稿＋会話の記憶に追加
# ---------------------------------------------------------------------------
JST = timezone(timedelta(hours=9))

YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")
TREND_CHANNEL_ID = int(os.getenv("TREND_CHANNEL_ID", "0"))  # 毎日の投稿先チャンネル
TREND_HOUR = int(os.getenv("TREND_HOUR", "8"))              # 実行時刻（JST・毎日）
# 自分のチャンネルの週次レポート（既定: 月曜9時。0=月曜〜6=日曜）
CHANNEL_REPORT_DOW = int(os.getenv("CHANNEL_REPORT_DOW", "0"))
CHANNEL_REPORT_HOUR = int(os.getenv("CHANNEL_REPORT_HOUR", "9"))
CHANNEL_REPORT_CID = int(os.getenv("CHANNEL_REPORT_CHANNEL_ID", "0"))
TREND_REGION = os.getenv("TREND_REGION", "JP")
TREND_DEEP_COUNT = int(os.getenv("TREND_DEEP_COUNT", "5"))  # 実際に視聴する本数/日
TREND_MAX_MINUTES = int(os.getenv("TREND_MAX_MINUTES", "20"))  # これより長い動画は視聴しない

INSIGHTS_DIR = Path(os.getenv("INSIGHTS_DIR", os.path.join(_BASE, "insights")))
INSIGHTS_DIR.mkdir(parents=True, exist_ok=True)
_ANALYZED_IDS_FILE = INSIGHTS_DIR / "analyzed_ids.txt"


def _load_analyzed_ids():
    try:
        if _ANALYZED_IDS_FILE.exists():
            return set(_ANALYZED_IDS_FILE.read_text(encoding="utf-8").split())
    except Exception as e:  # noqa: BLE001
        print(f"[trend] 分析済みID読込失敗: {e}")
    return set()


def _mark_analyzed(video_id):
    try:
        with open(_ANALYZED_IDS_FILE, "a", encoding="utf-8") as f:
            f.write(video_id + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"[trend] 分析済みID保存失敗: {e}")


def _parse_iso_duration(s):
    """ISO8601 の PT#H#M#S を秒に変換。"""
    m = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", s or "")
    if not m:
        return 0
    h, mi, se = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + se


def _video_dict(item):
    """videos.list のレスポンス1件を内部形式に変換。"""
    sn = item["snippet"]
    return {
        "id": item["id"],
        "title": sn["title"],
        "channel": sn["channelTitle"],
        "desc": (sn.get("description") or "").replace("\n", " ")[:200],
        "tags": (sn.get("tags") or [])[:8],
        "views": int(item.get("statistics", {}).get("viewCount", 0)),
        "duration": _parse_iso_duration(
            item.get("contentDetails", {}).get("duration")
        ),
        "url": f"https://www.youtube.com/watch?v={item['id']}",
    }


async def _fetch_trending(limit=100):
    """YouTube Data API で急上昇動画を取得（最大100件・2リクエスト）。"""
    if not YOUTUBE_API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY が .env に設定されていません")
    videos, page = [], None
    async with aiohttp.ClientSession() as session:
        while len(videos) < limit:
            params = {
                "part": "snippet,statistics,contentDetails",
                "chart": "mostPopular",
                "regionCode": TREND_REGION,
                "maxResults": "50",
                "key": YOUTUBE_API_KEY,
            }
            if page:
                params["pageToken"] = page
            async with session.get(
                "https://www.googleapis.com/youtube/v3/videos", params=params
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(f"YouTube API エラー: {str(data)[:300]}")
            videos.extend(_video_dict(item) for item in data.get("items", []))
            page = data.get("nextPageToken")
            if not page:
                break
    return videos[:limit]


TREND_SEARCH_DAYS = int(os.getenv("TREND_SEARCH_DAYS", "90"))  # 検索対象は直近N日


async def _search_videos(query, limit=50):
    """YouTube Data API でキーワード検索し、直近N日の人気動画を再生数順に取得。"""
    if not YOUTUBE_API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY が .env に設定されていません")
    published_after = (
        datetime.now(timezone.utc) - timedelta(days=TREND_SEARCH_DAYS)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    async with aiohttp.ClientSession() as session:
        params = {
            "part": "id",
            "q": query,
            "type": "video",
            "order": "viewCount",
            "publishedAfter": published_after,
            "maxResults": str(min(limit, 50)),
            "regionCode": TREND_REGION,
            "relevanceLanguage": "ja",
            "key": YOUTUBE_API_KEY,
        }
        # まず直近N日で検索し、ヒットしなければ全期間で再検索
        # （Fatboy Slim のMVのような昔の名作が期間フィルタで消えるのを防ぐ）
        ids = []
        for attempt in range(2):
            if attempt == 1:
                params.pop("publishedAfter", None)
            async with session.get(
                "https://www.googleapis.com/youtube/v3/search", params=params
            ) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise RuntimeError(f"YouTube検索エラー: {str(data)[:300]}")
            ids = [
                it["id"]["videoId"]
                for it in data.get("items", [])
                if it.get("id", {}).get("videoId")
            ]
            if ids:
                break
        if not ids:
            return []
        params2 = {
            "part": "snippet,statistics,contentDetails",
            "id": ",".join(ids),
            "key": YOUTUBE_API_KEY,
        }
        async with session.get(
            "https://www.googleapis.com/youtube/v3/videos", params=params2
        ) as resp:
            data2 = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"YouTube API エラー: {str(data2)[:300]}")
    videos = [_video_dict(item) for item in data2.get("items", [])]
    videos.sort(key=lambda v: v["views"], reverse=True)
    return videos


# ---------- 投稿後の効果測定（自分のチャンネルの実績で勝ちパターンを更新） ----------
# 「企画→生成→編集→予測」で止まっていた流れを、投稿後の実データまでつなぐ。
# YouTube Data API（無料）＋ Gemini（無料枠）だけで回る。
MYCH_FILE = HISTORY_DIR / "my_channel.json"


def _load_my_channel():
    return _read_json(MYCH_FILE)


def _save_my_channel(data):
    _write_json(MYCH_FILE, data, "mychannel")


async def _resolve_channel_id(text):
    """チャンネルURL/ハンドル(@name)/IDから channelId を求める。"""
    if not YOUTUBE_API_KEY:
        raise RuntimeError("YOUTUBE_API_KEY が .env に設定されていません")
    text = (text or "").strip()
    m = re.search(r"channel/(UC[\w-]{20,})", text) or re.search(r"^(UC[\w-]{20,})$", text)
    if m:
        return m.group(1)
    handle = re.search(r"@([\w.\-]+)", text)
    async with aiohttp.ClientSession() as session:
        if handle:
            async with session.get(
                "https://www.googleapis.com/youtube/v3/channels",
                params={"part": "id", "forHandle": "@" + handle.group(1),
                        "key": YOUTUBE_API_KEY},
            ) as resp:
                d = await resp.json()
                if d.get("items"):
                    return d["items"][0]["id"]
        # 最後の手段：チャンネル名で検索
        async with session.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={"part": "snippet", "type": "channel", "maxResults": 1,
                    "q": re.sub(r"https?://\S+", "", text) or text,
                    "key": YOUTUBE_API_KEY},
        ) as resp:
            d = await resp.json()
            if d.get("items"):
                return d["items"][0]["snippet"]["channelId"]
    return None


async def _fetch_my_videos(channel_id, limit=30):
    """自分のチャンネルの新しい動画を、再生数つきで取得する。"""
    async with aiohttp.ClientSession() as session:
        async with session.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={"part": "snippet", "channelId": channel_id, "order": "date",
                    "type": "video", "maxResults": min(limit, 50),
                    "key": YOUTUBE_API_KEY},
        ) as resp:
            d = await resp.json()
            if resp.status != 200:
                raise RuntimeError(f"YouTube API エラー: {str(d)[:300]}")
        ids = [i["id"]["videoId"] for i in d.get("items", []) if i.get("id", {}).get("videoId")]
        if not ids:
            return []
        async with session.get(
            "https://www.googleapis.com/youtube/v3/videos",
            params={"part": "snippet,statistics,contentDetails",
                    "id": ",".join(ids), "key": YOUTUBE_API_KEY},
        ) as resp:
            d2 = await resp.json()
    vids = []
    for item in d2.get("items", []):
        v = _video_dict(item)
        st = item.get("statistics", {})
        v["likes"] = int(st.get("likeCount", 0) or 0)
        v["comments"] = int(st.get("commentCount", 0) or 0)
        v["published"] = item["snippet"].get("publishedAt", "")[:10]
        vids.append(v)
    return vids


async def _analyze_my_channel(cid, quiet=False):
    """自分の投稿実績を取得→Geminiが伸びた理由を分析→勝ちパターン集に反映。"""
    conf = _load_my_channel()
    ch = conf.get("channel_id")
    if not ch:
        if not quiet:
            await send_as(
                orch, cid,
                "まだチャンネルが登録されていません。\n"
                "「**チャンネル登録して https://www.youtube.com/@あなたのID**」のように"
                "URLかハンドルを送ってください。"
            )
        return None
    vids = await _fetch_my_videos(ch)
    if not vids:
        if not quiet:
            await send_as(orch, cid, "動画がまだ見つかりませんでした（投稿後に反映されます）。")
        return None
    vids.sort(key=lambda v: v["views"], reverse=True)
    top, low = vids[:5], vids[-5:]

    def _fmt(vs):
        return "\n".join(
            f"・{v['views']:,}回 いいね{v['likes']} 尺{v['duration']}秒"
            f" 「{v['title'][:50]}」" for v in vs
        )
    ask = (
        "あなたはYouTube Shortsのグロース担当。次は【私自身のチャンネルの実績】です。"
        "伸びた動画と伸びなかった動画を比べ、再現できる勝ちパターンを日本語で"
        "箇条書き8行以内にまとめて。憶測ではなくデータから言えることだけ書く。\n"
        "観点: タイトルの型 / 尺 / 題材 / 投稿の傾向。最後に『次に作るべき1本』を1行。\n\n"
        f"【伸びた動画】\n{_fmt(top)}\n\n【伸びなかった動画】\n{_fmt(low)}\n\n"
        f"（全{len(vids)}本・平均{sum(v['views'] for v in vids) // len(vids):,}回）"
    )
    insight = (await _ai_text_bg(ask, "my_channel")).strip()
    conf.update(channel_id=ch, last_checked=time.time(),
                video_count=len(vids),
                report_cid=conf.get("report_cid") or cid,
                best={"title": top[0]["title"], "views": top[0]["views"]})
    _save_my_channel(conf)
    # 実績を勝ちパターン集にも反映（以降の企画・生成プロンプトへ自動で効く）
    if insight:
        try:
            stamp = datetime.now(JST).strftime("%Y-%m-%d")
            base = _load_style_profile()
            STYLE_PROFILE_FILE.write_text(
                (base + f"\n\n## {stamp} 自分のチャンネル実績からの学び\n{insight}").strip(),
                encoding="utf-8",
            )
        except Exception as e:  # noqa: BLE001
            print(f"[my_channel] 勝ちパターン更新に失敗: {str(e)[:120]}")
    if not quiet:
        avg = sum(v["views"] for v in vids) // len(vids)
        await send_as(
            orch, cid,
            f"📊 **チャンネル実績（全{len(vids)}本・平均{avg:,}回）**\n"
            f"🥇 最高: {top[0]['views']:,}回「{top[0]['title'][:40]}」\n\n"
            f"{insight[:1500]}\n\n"
            "この学びは勝ちパターン集に反映済みです（今後の企画に自動で効きます）。"
        )
    return insight


VIDEO_STUDY_PROMPT = (
    "あなたは映像制作の研究者。次のYouTube動画を視聴し、映像制作の観点で分析して。\n"
    "① 演出手法（企画構成・冒頭のフック・視聴維持の工夫）\n"
    "② カット割り・編集（カットのテンポ、トランジション、ジャンプカットなど）\n"
    "③ カメラワーク（アングル・寄り引き・手持ち/固定・ドローンなど）\n"
    "④ 人物の顔の動き・表情・リアクションの見せ方\n"
    "⑤ CG・VFX・テロップ・グラフィックの手法\n"
    "⑥ 音の使い方（BGM・効果音・無音の演出）\n"
    "⑦ 自分の映像制作に転用できるテクニック（箇条書き3つ）\n"
    "確認できない項目は省略してよい。日本語で簡潔に。"
)


def _gemini_watch_youtube_sync(url, prompt=None, tag="gemini_watch_youtube"):
    """Gemini にYouTube動画のURLを渡して「視聴」させる（ダウンロード不要）。"""
    from google.genai import types

    part = types.Part(file_data=types.FileData(file_uri=url))
    return _gemini_contents_sync([part, prompt or VIDEO_STUDY_PROMPT], tag)


# ---------- 会話中のYouTubeリンク：貼られたら中身を視聴して文脈に加える ----------
YOUTUBE_URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?"
    r"(?:youtube\.com/(?:watch\?\S*v=[\w\-]+\S*|shorts/[\w\-]+\S*|live/[\w\-]+\S*)"
    r"|youtu\.be/[\w\-]+\S*)"
)

YOUTUBE_CHAT_PROMPT = (
    "このYouTube動画を視聴して、内容を日本語で分かりやすくまとめて。\n"
    "① 何の動画か（ジャンル・テーマ・出演者）\n"
    "② 内容の要約（話の流れ・主要なポイント。会話や解説は要点を書き起こす）\n"
    "③ 印象的な場面・見どころ\n"
    "④ 画面に映る重要なテキスト・数字・資料があれば紹介"
)


# 発言に貼られた画像/動画のURL（生成物のURLを含む）
_MEDIA_URL_RE = re.compile(
    r"https?://[^\s<>\"]+?\.(?:png|jpe?g|webp|gif|mp4|mov|webm)(?:\?[^\s<>\"]*)?", re.I
)
# 「これ/ここ/どこ」など、直前の生成物そのものについて尋ねている言い回し
_VISUAL_REF_RE = re.compile(
    "これ|それ|この画像|この写真|この動画|ここ|どこ|何が|なにが|誰が|だれが|"
    "写って|映って|背景|場所|建物|風景|人物|色|見える|読める|書いてある"
)
_media_ctx_cache = {}   # URL -> (解析結果, 時刻)。同じURLの再解析でGemini枠を使わない


async def _describe_media_url(url, channel=None):
    """画像/動画のURLをGeminiに見せて内容の説明を得る（結果はキャッシュ）。"""
    hit = _media_ctx_cache.get(url)
    if hit and time.time() - hit[1] < 86400:
        return hit[0]
    ext = re.sub(r"\?.*$", "", url).lower()
    ext = ext[ext.rfind("."):] if "." in ext else ""
    spec = _media_spec(ext)
    if not spec:
        return ""
    kind, mime, prompt, tag, _head, _adv = spec
    data = await _download_file(url)
    if not data:
        print(f"[media_url] 取得失敗（サイズ超過/404の可能性）: {url[:80]}")
        return ""
    try:
        text = await asyncio.to_thread(
            _gemini_analyze_media_sync, data, mime or _detect_mime_type(data), prompt, tag
        )
    except GeminiQuotaExceeded as e:
        if channel:
            await channel.send(f"⚠️ 画像/動画の内容を読めませんでした: {e}")
        return ""
    except Exception as e:  # noqa: BLE001
        print(f"[media_url] 解析失敗: {str(e)[:150]}")
        return ""
    if text:
        _media_ctx_cache[url] = (text, time.time())
    return text or ""


async def _reply_context(message):
    """返信（リプライ）先のメッセージ本文・添付・URLを文脈として取り出す。
    Discordの「返信」は、どの発言について話しているかの最も明確な指定なので、
    本文だけでなく画像/動画のURLも拾って、その中身を読めるようにする。"""
    ref = getattr(message, "reference", None)
    if not ref:
        return ""
    src = getattr(ref, "resolved", None)
    if getattr(src, "content", None) is None:      # 未解決 or 削除済み
        try:
            src = await message.channel.fetch_message(ref.message_id)
        except Exception as e:  # noqa: BLE001
            print(f"[reply] 返信先を取得できません: {str(e)[:120]}")
            return ""
    who = getattr(getattr(src, "author", None), "display_name", "誰か")
    parts = [(src.content or "").strip()]
    parts += [a.url for a in getattr(src, "attachments", []) or []]
    body = "\n".join(p for p in parts if p)
    return f"\n\n【返信先の発言（{who}）】\n{body}" if body else ""


async def _apply_media_url_context(message, content, cid):
    """発言中の画像/動画URLをGeminiに見せて内容を文脈に足す。
    URLが無くても『これどこ？』のように直前の生成物を指していれば、
    その完成URLを見に行く（生成した画像について質問できるようにするため）。
    ※Discordの添付は読めていたが、生成物のURLは読んでいなかったため
      『この画像どこ？』に見当違いの答えを返す不具合が実際に起きた。"""
    urls = _MEDIA_URL_RE.findall(content or "")
    if not urls:
        lg = _load_last_gen(cid) or {}
        url = lg.get("url") or ""
        # 「この画像/この動画」と明示していれば1日前まで遡る。
        # 「ここ/どこ」だけの曖昧な指定は直近2時間に限る（誤読み込みの防止）
        explicit = re.search("この画像|この写真|この動画|さっきの|作った", content or "")
        limit = 86400 if explicit else 7200
        if (url and time.time() - lg.get("t", 0) < limit
                and not message.attachments
                and _VISUAL_REF_RE.search(content or "")
                and _MEDIA_URL_RE.match(url)):
            urls = [url]
    if not urls:
        return content
    # 生成物に返信して「この画像の背景を室内にして」と頼まれた時、
    # そのURLを参照として覚えておかないと、人物の消えた別物ができる。
    # 事故：返信で頼んだのに参照が無く、人のいない部屋だけの画像になった。
    for _u in urls[:2]:
        if re.search(r"\.(png|jpe?g|webp|gif)(\?|$)", _u, re.I):
            _remember_ref(cid, _u)
            break
    async with message.channel.typing():
        for url in urls[:2]:
            desc = await _describe_media_url(url, message.channel)
            if desc:
                content += f"\n\n【この画像/動画の内容（{url[:80]}）】\n{desc}"
    return content


def _yt_video_id(url):
    """YouTubeのURLから動画IDを取り出す（watch / youtu.be / shorts / live）。"""
    m = re.search(r"(?:[?&]v=|youtu\.be/|/shorts/|/live/)([\w\-]{6,})", url or "")
    return m.group(1) if m else ""


async def _fetch_video_meta(vid):
    """YouTube Data API で動画1本のメタ情報を取る。
    Geminiの無料枠とは【別の枠】なので、Geminiが枠切れでもこちらは使える。"""
    if not YOUTUBE_API_KEY or not vid:
        return None
    params = {"part": "snippet,statistics,contentDetails", "id": vid,
              "key": YOUTUBE_API_KEY}
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(
                "https://www.googleapis.com/youtube/v3/videos", params=params,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as r:
                data = await r.json()
                if r.status != 200 or not data.get("items"):
                    print(f"[yt_meta] 取得できず: {str(data)[:200]}")
                    return None
    except Exception as e:  # noqa: BLE001
        print(f"[yt_meta] 失敗: {str(e)[:150]}")
        return None
    it = data["items"][0]
    sn = it.get("snippet", {})
    return {
        "title": sn.get("title", ""),
        "channel": sn.get("channelTitle", ""),
        "published": (sn.get("publishedAt") or "")[:10],
        "desc": (sn.get("description") or "").strip()[:1500],
        "tags": (sn.get("tags") or [])[:12],
        "views": int(it.get("statistics", {}).get("viewCount") or 0),
        "duration": _parse_iso_duration(
            it.get("contentDetails", {}).get("duration")),
    }


_CAPTION_RE = re.compile(r"<text[^>]*>(.*?)</text>", re.S)


def _decode_caption_xml(xml):
    """timedtext のXMLから字幕本文だけを取り出す。"""
    import html
    # YouTubeは実体参照を二重にエスケープすることがある（&amp;#39; → '）
    parts = [html.unescape(html.unescape(re.sub(r"<[^>]+>", "", t))).strip()
             for t in _CAPTION_RE.findall(xml or "")]
    return " ".join(p for p in parts if p)


# ---------- 長い動画 → ショート切り抜き（Mac上で完結・クレジット不要）----------
# 本人の希望：「ヒッグスフィールドを使わないで、クロードコードだけで完結したい」。
# 使うのは Mac の ffmpeg と、字幕（YouTubeの自動生成でも可）だけ。
# 生成モデルを使わないのでクレジットは一切消費しない。
CLIP_TOOLS = ("ffmpeg", "yt-dlp")
CLIP_DIR = HISTORY_DIR / "clips"
CLIP_MAX_MB = 24            # Discordの添付上限（無料枠）に収める
CLIP_DEFAULT_N = 5


def _missing_clip_tools():
    """Macに入っていない道具を返す。無ければ空リスト。"""
    return [t for t in CLIP_TOOLS if not shutil.which(t)]


async def _install_clip_tools(cid, missing):
    """足りない道具をボット自身が入れる（ユーザーにターミナルを触らせない）。"""
    await send_as(
        orch, cid,
        f"🔧 切り抜きに必要な道具（{', '.join(missing)}）がMacに入っていないので、"
        "先に入れます（数分かかることがあります）…"
    )
    out = await _run_claude_exec(
        "次の道具をこのMacに入れて。Homebrew があれば brew install、"
        "無ければ Homebrew の導入から行うこと。完了したら"
        "`which` で入ったことを確認し、最後の行に OK か NG だけ書いて。\n"
        f"入れる道具: {' '.join(missing)}",
        timeout=900,
    )
    still = _missing_clip_tools()
    if still:
        await send_as(
            orch, cid,
            f"⚠️ {', '.join(still)} を入れられませんでした。\n"
            f"実行結果: {(out or '')[-400:]}"
        )
        return False
    await send_as(orch, cid, "✅ 道具の準備ができました。切り抜きを始めます。")
    return True


# ---------- 字幕が無い動画は、その場で文字起こしして字幕を作る ----------
# whisper.cpp を Mac で動かす。API課金なし・Gemini無料枠にも依存しない
# （Geminiは枠切れが頻繁で、そこに頼ると「字幕が取れません」で止まるため）。
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small")   # tiny/base/small/medium
WHISPER_DIR = HISTORY_DIR / "whisper"
WHISPER_URL = ("https://huggingface.co/ggerganov/whisper.cpp/resolve/main/"
               "ggml-{name}.bin")
# 実測の目安（MacBook Air）：小さいほど速いが、日本語は small 以上でないと崩れる。
WHISPER_BINS = ("whisper-cli", "whisper-cpp", "whisper")


def _whisper_bin():
    for b in WHISPER_BINS:
        if shutil.which(b):
            return b
    return ""


def _whisper_model_path():
    return WHISPER_DIR / f"ggml-{WHISPER_MODEL}.bin"


async def _ensure_whisper(cid):
    """文字起こしの道具と模型を用意する（無ければ入れる）。"""
    if not _whisper_bin():
        await send_as(orch, cid, "🔧 文字起こしの道具（whisper.cpp）を入れます…")
        await _run_claude_exec(
            "このMacに whisper.cpp を入れて。Homebrew があれば "
            "`brew install whisper-cpp`、無ければ Homebrew の導入から行う。"
            "終わったら `which whisper-cli` で確認し、最後の行に OK か NG だけ書いて。",
            timeout=900,
        )
        if not _whisper_bin():
            await send_as(orch, cid, "⚠️ whisper.cpp を入れられませんでした。")
            return False
    model = _whisper_model_path()
    if not model.exists() or model.stat().st_size < 1_000_000:
        WHISPER_DIR.mkdir(parents=True, exist_ok=True)
        await send_as(
            orch, cid,
            f"⬇️ 文字起こし用のモデル（{WHISPER_MODEL}）を1回だけ取得します"
            "（数百MB・次回以降は不要）…"
        )
        ok, log = await _sh(
            ["curl", "-fL", "--retry", "3", "-o", str(model),
             WHISPER_URL.format(name=WHISPER_MODEL)], timeout=1800, heavy=True)
        if not ok or not model.exists() or model.stat().st_size < 1_000_000:
            model.unlink(missing_ok=True)
            await send_as(orch, cid, f"⚠️ モデルを取得できませんでした: {log[-200:]}")
            return False
    return True


_SRT_BLOCK_RE = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*"
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*\n(.*?)(?=\n\s*\n|\Z)", re.S)


def _parse_srt(text):
    """SRTを [(開始秒, 長さ秒, 本文)] にする（字幕取得と同じ形に揃える）。"""
    rows = []
    for m in _SRT_BLOCK_RE.finditer(text or ""):
        h1, m1, s1, ms1, h2, m2, s2, ms2, body = m.groups()
        st = int(h1) * 3600 + int(m1) * 60 + int(s1) + int(ms1) / 1000
        en = int(h2) * 3600 + int(m2) * 60 + int(s2) + int(ms2) / 1000
        line = " ".join(body.split())
        if line:
            rows.append((st, max(en - st, 0.5), line))
    return rows


async def _transcribe_local(cid, src, workdir, lang="ja"):
    """動画から音声を抜いて文字起こしし、時刻つきの字幕行にして返す。"""
    if not await _ensure_whisper(cid):
        return []
    wav = workdir / "audio.wav"
    await send_as(orch, cid, "🎧 音声を取り出しています…")
    ok, log = await _sh([
        "ffmpeg", "-nostdin", "-y", "-threads", str(_work_threads()), "-i", str(src), "-vn",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
        timeout=900, heavy=True)
    if not ok or not wav.exists():
        await send_as(orch, cid, f"⚠️ 音声を取り出せませんでした: {log[-200:]}")
        return []
    await send_as(
        orch, cid,
        f"✍️ 文字起こしをしています（{_eta_hint('文字起こし')}）。\n"
        f"Macの力を使う処理なので、この間は返事が普段より遅くなります"
        f"（CPUは{_work_threads()}本まで・優先度は下げてあるので、"
        "話しかければ答えます）。"
    )
    started = time.time()
    base = workdir / "auto"
    ok, log = await _sh([
        _whisper_bin(), "-m", str(_whisper_model_path()), "-f", str(wav),
        "-l", lang, "-osrt", "-of", str(base), "-pp", "false",
        "-t", str(_work_threads())], timeout=5400, heavy=True)
    srt = Path(f"{base}.srt")
    if not ok or not srt.exists():
        await send_as(orch, cid, f"⚠️ 文字起こしに失敗しました: {log[-300:]}")
        return []
    rows = _parse_srt(srt.read_text(encoding="utf-8", errors="replace"))
    _record_task_time("文字起こし", time.time() - started)
    if rows:
        await send_as(orch, cid, f"✅ 文字起こし完了（{len(rows)}行）。")
    return rows


CLIP_PICK_PROMPT = (
    "あなたはショート動画の編集者。下は長い動画の字幕を時刻つきで並べたもの。\n"
    "ここから【単体で成立する】切り抜きを選ぶ。\n"
    "選ぶ基準:\n"
    "・最初の2秒で引きがある（結論・驚き・断言・問いかけから始まる）\n"
    "・その区間だけ見て意味が通る（前後の説明が要らない）\n"
    "・15〜60秒に収まる\n"
    "・内容が互いに重ならない\n"
    "JSONだけを出力する。説明や前置きは書かない。\n"
    '形式: {"clips":[{"start":"mm:ss","end":"mm:ss","title":"日本語のタイトル(25字以内)",'
    '"hook":"最初の2秒で出す一言(15字以内)","why":"選んだ理由(30字以内)"}]}\n'
)


def _mmss_to_sec(v):
    """"mm:ss" / "hh:mm:ss" / 数値 を秒に。読めなければ None。"""
    if isinstance(v, (int, float)):
        return float(v)
    parts = str(v or "").strip().split(":")
    try:
        nums = [float(x) for x in parts]
    except ValueError:
        return None
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


async def _pick_clip_ranges(transcript, n):
    """字幕から切り抜き区間を選ぶ（クロードが読んで決める）。"""
    raw = await run_claude_cli(
        CLIP_PICK_PROMPT + f"\n作る本数: {n}本\n\n【字幕】\n{transcript}\n\nJSON:"
    )
    raw = _strip_cli_boilerplate((raw or "").strip())
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        raise RuntimeError(f"切りどころを決められませんでした: {raw[:200]}")
    clips = []
    for c in (json.loads(m.group(0)).get("clips") or []):
        st, en = _mmss_to_sec(c.get("start")), _mmss_to_sec(c.get("end"))
        if st is None or en is None or en - st < 5:
            continue
        clips.append({
            "start": st, "end": min(en, st + 90),      # 長すぎる指定は切り詰める
            "title": (c.get("title") or "")[:40],
            "hook": (c.get("hook") or "")[:30],
            "why": (c.get("why") or "")[:60],
        })
    if not clips:
        raise RuntimeError("使える切り抜き区間がありませんでした。")
    return clips[:n]


# 日本語が出るフォント。指定を誤ると字幕が全部豆腐（□）になるので、
# macOSに最初から入っているものを順に試す。
CLIP_FONTS = (
    "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "/System/Library/Fonts/ヒラギノ丸ゴ ProN W4.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
)


def _clip_font():
    for f in CLIP_FONTS:
        if Path(f).exists():
            return f
    return ""


# 重い処理に使うCPUの本数。全部使うとボット自身が返事を書けなくなる。
# 事故：文字起こし中、「あとどのくらい？」「ログ送って」「再起動」の
# どれにも反応しなくなった（処理は正常に進んでいた）。
def _work_threads():
    return max(1, (os.cpu_count() or 4) - 1)


# 走らせている重い処理。暴走した時にDiscordから止められるように持っておく。
# 事故：文字起こしがCPUを占有し、「再起動」すら届かなくなって復旧できなかった。
_heavy_procs = set()


def stop_heavy_procs():
    """動いている重い処理を全部止める。止めた数を返す。"""
    n = 0
    for proc in list(_heavy_procs):
        try:
            if proc.returncode is None:
                proc.kill()
                n += 1
        except Exception:  # noqa: BLE001
            pass
        _heavy_procs.discard(proc)
    return n


async def _sh(cmd, timeout=900, heavy=False):
    """Mac上でコマンドを1つ実行して (成功か, 出力) を返す。
    heavy=True の時は優先度を下げて動かし、いつでも止められるよう記録する。"""
    if heavy and shutil.which("nice"):
        cmd = ["nice", "-n", "15", *cmd]
    # 【重要】標準入力を必ず切る。バックグラウンドで動かしているボットの
    # 子プロセスが端末から読もうとすると SIGTTIN で【止まる】（死なない）。
    # 実際に ffmpeg がここで固まり、「音声を取り出しています…」から
    # 一切先へ進まなくなった。2回とも同じ場所で止まっていた。
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    if heavy:
        _heavy_procs.add(proc)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await _reap(proc)
        return False, "タイムアウト"
    except asyncio.CancelledError:
        proc.kill()
        await _reap(proc)
        raise
    finally:
        _heavy_procs.discard(proc)
    return proc.returncode == 0, (out or b"").decode(errors="replace")[-1500:]


def _srt_for(rows, start, end):
    """区間内の字幕を、区間の頭を0秒とするSRTにする。"""
    def _t(sec):
        h, rem = divmod(max(sec, 0), 3600)
        m, sec2 = divmod(rem, 60)
        return f"{int(h):02d}:{int(m):02d}:{int(sec2):02d},{int(sec2 % 1 * 1000):03d}"
    lines, i = [], 0
    for st, dur, text in rows:
        en = st + (dur or 2.0)
        if en <= start or st >= end:
            continue
        i += 1
        lines.append(f"{i}\n{_t(max(st, start) - start)} --> "
                     f"{_t(min(en, end) - start)}\n{text}\n")
    return "\n".join(lines)


async def _cut_one_clip(src, rows, clip, idx, workdir):
    """1本分を切り出して、縦型9:16・字幕焼き付けのMP4にする。"""
    out = workdir / f"clip{idx}.mp4"
    srt = workdir / f"clip{idx}.srt"
    body = _srt_for(rows, clip["start"], clip["end"])
    font = _clip_font()
    # 縦型化：中央を9:16で切り出し、1080x1920へ。字幕があれば焼き付ける。
    vf = ("crop='min(iw,ih*9/16)':'min(ih,iw*16/9)',"
          "scale=1080:1920:force_original_aspect_ratio=increase,"
          "crop=1080:1920")
    if body and font:
        srt.write_text(body, encoding="utf-8")
        style = ("FontName=Hiragino Sans,FontSize=15,PrimaryColour=&H00FFFFFF,"
                 "OutlineColour=&H00000000,Outline=3,Shadow=0,Alignment=2,MarginV=140")
        vf += f",subtitles='{srt}':force_style='{style}'"
    ok, log = await _sh([
        "ffmpeg", "-nostdin", "-y", "-threads", str(_work_threads()),
        "-ss", str(clip["start"]), "-to", str(clip["end"]),
        "-i", str(src), "-vf", vf, "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "26", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
        str(out),
    ], heavy=True)
    if not ok or not out.exists():
        return None, log
    # Discordの上限に収まらなければ、もう一段圧縮してから諦める
    if out.stat().st_size > CLIP_MAX_MB * 1024 * 1024:
        small = workdir / f"clip{idx}_s.mp4"
        ok2, log2 = await _sh([
            "ffmpeg", "-nostdin", "-y", "-threads", str(_work_threads()),
            "-i", str(out), "-vf", "scale=720:1280",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
            "-c:a", "aac", "-b:a", "96k", str(small)], heavy=True)
        if ok2 and small.exists():
            return small, ""
        return None, log2
    return out, ""


# iPhoneの「ファイル」アプリから貼られる道順を、Mac上の実際のパスに変える。
# 実例:「⁨iCloud Drive⁩ ▸ ⁨マルサヂ⁩ ▸ ⁨AI⁩ ▸ ⁨動画⁩の中の武士道….mp4」
# iCloud DriveはMacにも同期されているので、Mac側のパスを組み立てれば扱える。
# これが無いと「iCloudの動画は取りに行けない」と断って終わっていた。
ICLOUD_ROOT = "~/Library/Mobile Documents/com~apple~CloudDocs"
_BIDI_RE = re.compile("[\u2066-\u2069\u200e\u200f]")
_IOS_CRUMB_RE = re.compile(
    r"(iCloud\s*Drive|このiPhone内|iPhone内)((?:\s*▸\s*[^▸]+)+)", re.I)
_VIDEO_EXT_RE = re.compile(r"\.(?:mp4|mov|m4v|webm|mkv)$", re.I)


def _ios_files_path(text):
    """iPhoneのファイルアプリの道順 → Macのパス。読めなければ空。
    最後の区切りのあとは「動画の中の◯◯.mp4」のように
    フォルダ名とファイル名が助詞でつながっているので、そこで分ける。"""
    t = _BIDI_RE.sub("", text or "")
    # iPhoneから貼ると折り返しの改行が入る。「動画\nの中の◯◯.mp4」のまま
    # 読むとフォルダ名が落ち、実際に一階層ぶん違うパスを組み立てていた。
    t = re.sub(r"\s*\n\s*", "", t)
    t = t.replace("▸", "▸").replace("→", "▸").replace(">", "▸")
    m = _IOS_CRUMB_RE.search(t)
    if not m:
        return ""
    segs = [x.strip() for x in m.group(2).split("▸") if x.strip()]
    if not segs:
        return ""
    tail = segs.pop()
    # 「動画の中の武士道….mp4」→ フォルダ「動画」＋ ファイル「武士道….mp4」
    sp = re.search(r"(.*?)(?:の中の|の中に|内の|フォルダの)(.+)$", tail)
    if sp:
        folder, name = sp.group(1).strip(), sp.group(2).strip()
        if folder:
            segs.append(folder)
    else:
        name = tail
    # ファイル名の後ろに続く依頼文（「をショート動画にして」等）を落とす
    m2 = re.search(r"^(.+?\.(?:mp4|mov|m4v|webm|mkv))", name, re.I)
    if not m2:
        return ""
    name = m2.group(1)
    base = ICLOUD_ROOT if re.search("icloud", m.group(1), re.I) else "~"
    return os.path.expanduser("/".join([base, *segs, name]))


# 「始めていいなら『やって』と言って」と案内しておきながら、
# 「やって」に何の受け皿も無く、同じ説明を繰り返す無限ループになっていた。
# 何が足りなくて始められないのかを覚えておき、「やって」で先に進める。
_pending_do = {}
PENDING_DO_SEC = 3600
_BARE_GO_RE = re.compile(
    r"^(やって|やろう|お願い(します)?|おねがい|進めて|すすめて|"
    r"始めて|はじめて|go|ゴー|実行|やっちゃって|よろしく)[。、!！\s]*$", re.I)
# iCloudの「共有リンク」はブラウザで開くページなので、そのままでは取得できない。
# ここを「リンクなら取りに行ける」と案内してしまい、貼っても何も起きなかった。
_ICLOUD_LINK_RE = re.compile(r"https?://(?:www\.)?icloud\.com/iclouddrive/\S+", re.I)


def _set_pending_do(cid, need, hint=""):
    """『あと何があれば始められるか』を覚える。"""
    _pending_do[cid] = {"need": need, "hint": hint, "t": time.time()}


def _get_pending_do(cid):
    d = _pending_do.get(cid)
    return d if d and time.time() - d["t"] < PENDING_DO_SEC else None


async def _spotlight_find(name):
    """Spotlightで名前から探す（macOSなら一瞬で返る）。
    全走査は数分かかり、時間切れで見つけられない事故が起きた。"""
    if not shutil.which("mdfind"):
        return None
    ok, out = await _sh(["mdfind", "-name", name], timeout=30)
    if not ok:
        return None
    for line in (out or "").splitlines():
        hit = Path(line.strip())
        if hit.name == name and hit.is_file():
            return hit
    return None


def _find_video_sync(name, limit_dirs):
    """同じ名前の動画をよくある場所から探す。"""
    for d in limit_dirs:
        root = Path(os.path.expanduser(d))
        if not root.exists():
            continue
        try:
            for hit in root.rglob(name):
                if hit.is_file():
                    return hit
        except Exception:  # noqa: BLE001
            continue
    return None


async def _find_video_by_name(name, limit_dirs=(ICLOUD_ROOT, "~/Movies",
                                                "~/Downloads", "~/Desktop")):
    """同名の動画を探す。iCloud配下は巨大なことがあり、そのまま回すと
    イベントループを止めてボットが黙り込む。別スレッドに逃がし、
    時間も区切る（見つからなければ諦めて先へ進む方がよい）。"""
    try:
        hit = await _spotlight_find(name)      # まずは一瞬で返る方法
        if hit:
            return hit
    except Exception:  # noqa: BLE001
        pass
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_find_video_sync, name, limit_dirs), timeout=240)
    except Exception:  # noqa: BLE001
        return None


def _clip_source(message, content):
    """切り抜きの素材がどこにあるかを決める。
    (種類, 値) を返す。種類: "youtube" / "url" / "file"。
    Discordの添付は25MB(無料)までしか送れないので、大きい動画は
    Macのファイルのパスか、直リンクで渡してもらう。"""
    m = _YT_LINK_RE.search(content or "")
    if m:
        return "youtube", m.group(0)
    for a in (getattr(message, "attachments", None) or []):
        if Path(a.filename).suffix.lower() in SUPPORTED_VIDEO_TYPES:
            return "url", a.url
    m = re.search(r"https?://\S+\.(?:mp4|mov|webm|m4v)(?:\?\S*)?", content or "", re.I)
    if m:
        return "url", m.group(0)
    # Macのローカルパス（1GB級はこれが現実的。Discordには載らないため）
    m = re.search(r"(~?/[^\s'\"]+\.(?:mp4|mov|webm|m4v|mkv))", content or "", re.I)
    if m:
        return "file", os.path.expanduser(m.group(1))
    # iPhoneの「ファイル」アプリから貼った道順（iCloudはMacにも同期されている）
    ios = _ios_files_path(content)
    if ios:
        return "file", ios
    # ファイル名だけでも受け取る。iCloud/ムービー/ダウンロード/デスクトップから
    # 同名を探すので、道順を調べてもらう手間が要らない
    # （いちばん簡単な渡し方なのに、これまで受け取れていなかった）。
    m = _VIDEO_NAME_RE.search(_BIDI_RE.sub("", content or ""))
    if m:
        return "file", m.group(0)
    return "", ""


async def _download_video(cid, url, dest, kind="youtube"):
    """動画をMacに用意する。取れなければ理由を出して False。
    kind="file" は既にMacにあるファイルなので、コピーせずそのまま使う。"""
    if kind == "file":
        src = Path(url)
        if not src.exists() and "/" not in str(url):
            await send_as(orch, cid, f"🔎 「{url}」をMacの中から探しています…")
        if not src.exists():
            # 名前が合っていれば場所違いのことが多いので、iCloudとホームを探す。
            # 「見つかりません」で終わると、やり取りが何往復も増える。
            found = await _find_video_by_name(src.name)
            if found:
                await send_as(orch, cid, f"📁 場所が違ったので探しました: {found}")
                src = found
            else:
                await send_as(
                    orch, cid,
                    f"⚠️ ファイルが見つかりませんでした: {url}\n"
                    "iPhoneのファイルアプリの道順をそのまま貼るか、"
                    "動画をこのチャンネルに添付してください。"
                )
                return False
        size = src.stat().st_size
        if size > MAX_VIDEO_SIZE:
            await send_as(
                orch, cid,
                f"⚠️ {size / 1048576:.0f}MB あり、上限の"
                f"{MAX_VIDEO_SIZE / 1048576:.0f}MBを超えています。"
            )
            return False
        await send_as(orch, cid, f"📁 Macのファイルを使います（{size / 1048576:.1f}MB）。")
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest.unlink()
            dest.symlink_to(src)         # コピーしない（1GBを二重に置かない）
        except Exception:  # noqa: BLE001
            shutil.copy2(src, dest)
        return True

    if kind == "url":
        await send_as(orch, cid, "⬇️ 動画を取得しています…")
        ok, note = await _download_to_file(url, dest)
        if ok:
            await send_as(orch, cid, f"✅ 取得しました（{note}）。")
            return True
        await send_as(orch, cid, f"⚠️ 動画を取得できませんでした（{note}）。")
        return False

    await send_as(orch, cid, "⬇️ 動画を取得しています…")
    ok, log = await _sh([
        "yt-dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]",
        "--merge-output-format", "mp4",
        "--max-filesize", f"{MAX_VIDEO_SIZE // 1048576}M",
        "-o", str(dest), url], timeout=3600)
    if ok and dest.exists():
        await send_as(
            orch, cid,
            f"✅ 取得しました（{dest.stat().st_size / 1048576:.1f}MB）。")
        return True
    await send_as(
        orch, cid,
        "⚠️ 動画を取得できませんでした。Macにあるファイルなら"
        "「/Users/…/movie.mp4 を切り抜いて」のようにパスで渡しても切り抜けます"
        f"（{MAX_VIDEO_SIZE / 1048576:.0f}MBまで）。\n詳細: {log[-300:]}"
    )
    return False


async def _run_clip_shorts(message, url, n=CLIP_DEFAULT_N, kind="youtube"):
    """長い動画を読み込み、切り抜きショートを作ってDiscordに投稿する。
    素材はYouTube・直リンク・Discordの添付・Macのローカルファイルのいずれか。
    生成モデルを使わないので、クレジットは一切消費しない。"""
    cid = message.channel.id
    missing = _missing_clip_tools()
    if missing and not await _install_clip_tools(cid, missing):
        return
    vid = _yt_video_id(url) if kind == "youtube" else ""
    CLIP_DIR.mkdir(parents=True, exist_ok=True)
    workdir = CLIP_DIR / str(int(time.time()))
    workdir.mkdir(parents=True, exist_ok=True)
    src = workdir / "src.mp4"

    await send_as(orch, cid, "📝 字幕を読み込んでいます…")
    rows = await _fetch_captions_timed(vid)
    if not rows:
        # 字幕が無い動画はここで諦めていた。音声から自分で字幕を作る。
        await send_as(
            orch, cid,
            "字幕が付いていない動画なので、**音声から字幕を作ります**"
            "（Mac上で処理・クレジットは使いません）。まず動画を取得します…"
        )
        if not await _download_video(cid, url, src, kind):
            return
        rows = await _transcribe_local(cid, src, workdir)
        if not rows:
            await send_as(
                orch, cid,
                "⚠️ 字幕を作れなかったので、切りどころを判断できません。"
                "音声が入っている動画か確認してください。"
            )
            return
    total = int(rows[-1][0] + (rows[-1][1] or 0))
    await send_as(
        orch, cid,
        f"🔎 字幕を{len(rows)}行（約{total // 60}分ぶん）読みました。"
        f"{CLAUDE3_NAME}が切りどころを{n}本選びます…"
    )
    try:
        clips = await _pick_clip_ranges(_timed_transcript(rows), n)
    except Exception as e:  # noqa: BLE001
        await send_as(orch, cid, f"⚠️ 切りどころを決められませんでした: {str(e)[:250]}")
        return
    plan = "\n".join(
        f"{i}. {int(c['start']) // 60:02d}:{int(c['start']) % 60:02d}"
        f"〜{int(c['end']) // 60:02d}:{int(c['end']) % 60:02d}"
        f"（{int(c['end'] - c['start'])}秒）**{c['title']}**\n"
        f"　　フック: {c['hook']} / 理由: {c['why']}"
        for i, c in enumerate(clips, 1)
    )
    await send_as(orch, cid, f"✂️ 切り抜く場所を決めました。\n{plan}\n\n動画を取得します…")

    if not src.exists() and not await _download_video(cid, url, src, kind):
        return
    await send_as(orch, cid, f"🎞 {len(clips)}本を切り出しています…")
    made = 0
    for i, c in enumerate(clips, 1):
        path, err = await _cut_one_clip(src, rows, c, i, workdir)
        if not path:
            await send_as(orch, cid, f"⚠️ {i}本目の切り出しに失敗: {err[-200:]}")
            continue
        try:
            data = path.read_bytes()
            await send_image_bytes(
                cid,
                f"**{i}. {c['title']}**\n"
                f"フック: {c['hook']}\n"
                f"元動画 {int(c['start']) // 60:02d}:{int(c['start']) % 60:02d} から"
                f"{int(c['end'] - c['start'])}秒",
                data, f"short{i}.mp4",
            )
            made += 1
        except Exception as e:  # noqa: BLE001
            await send_as(orch, cid, f"⚠️ {i}本目の投稿に失敗: {str(e)[:200]}")
    try:
        shutil.rmtree(workdir, ignore_errors=True)
    except Exception:  # noqa: BLE001
        pass
    await send_as(
        orch, cid,
        f"✅ {made}本できました（クレジットは使っていません）。\n"
        "直したいところがあれば「2本目をもう少し長くして」のように言ってください。"
    )


# 切り抜きには「いつ何を言ったか」が要る。本文だけの取り出しでは足りない。
_CAPTION_TIMED_RE = re.compile(
    r'<text start="([\d.]+)"(?:\s+dur="([\d.]+)")?[^>]*>(.*?)</text>', re.S)


def _decode_caption_timed(xml):
    """timedtext のXMLを [(開始秒, 長さ秒, 本文)] にする。"""
    import html
    rows = []
    for start, dur, body in _CAPTION_TIMED_RE.findall(xml or ""):
        text = html.unescape(html.unescape(re.sub(r"<[^>]+>", "", body))).strip()
        if text:
            rows.append((float(start), float(dur or 0), text))
    return rows


async def _fetch_captions_timed(vid):
    """時刻つきの字幕を取る。取れなければ空リスト。
    自動生成字幕でも取れるので、たいていの動画で中身を読める。"""
    if not vid:
        return []
    try:
        async with aiohttp.ClientSession() as s:
            for params in ({"lang": "ja", "v": vid}, {"lang": "ja", "v": vid, "kind": "asr"},
                           {"lang": "en", "v": vid}, {"lang": "en", "v": vid, "kind": "asr"}):
                async with s.get(
                    "https://video.google.com/timedtext", params=params,
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as r:
                    if r.status != 200:
                        continue
                    rows = _decode_caption_timed(await r.text())
                    if len(rows) >= 5:
                        return rows
    except Exception as e:  # noqa: BLE001
        print(f"[captions] 時刻つき字幕の取得に失敗: {str(e)[:150]}")
    return []


def _timed_transcript(rows, limit=24000):
    """[mm:ss] 本文 の形に整えてAIに渡す（切りどころを選ばせるため）。"""
    out = []
    for start, _dur, text in rows:
        m, sec = divmod(int(start), 60)
        out.append(f"[{m:02d}:{sec:02d}] {text}")
    joined = "\n".join(out)
    return joined[:limit]


async def _fetch_captions(vid, limit=6000):
    """字幕（自動生成を含む）が公開されていれば本文を返す。取れなければ空。
    字幕が取れれば、動画を視聴しなくても【本物の】要約ができる。
    公開エンドポイントのベストエフォートなので、取れないことも普通にある。"""
    if not vid:
        return ""
    try:
        async with aiohttp.ClientSession() as s:
            for params in ({"lang": "ja", "v": vid}, {"lang": "en", "v": vid},
                           {"lang": "ja", "v": vid, "kind": "asr"},
                           {"lang": "en", "v": vid, "kind": "asr"}):
                async with s.get(
                    "https://video.google.com/timedtext", params=params,
                    timeout=aiohttp.ClientTimeout(total=15),
                ) as r:
                    if r.status != 200:
                        continue
                    text = _decode_caption_xml(await r.text())
                    if len(text) > 100:
                        return text[:limit]
    except Exception as e:  # noqa: BLE001
        print(f"[yt_caption] 失敗: {str(e)[:150]}")
    return ""


def _format_video_meta(meta, caption):
    """メタ情報＋字幕を、AIが読める文脈テキストに整える。"""
    lines = [f"タイトル: {meta['title']}", f"チャンネル: {meta['channel']}"]
    if meta.get("published"):
        lines.append(f"公開日: {meta['published']}")
    if meta.get("duration"):
        lines.append(f"長さ: {meta['duration']}")
    if meta.get("views"):
        lines.append(f"再生数: {meta['views']:,}")
    if meta.get("tags"):
        lines.append("タグ: " + "、".join(meta["tags"]))
    if meta.get("desc"):
        lines.append(f"概要欄:\n{meta['desc']}")
    if caption:
        lines.append(f"字幕（書き起こし）:\n{caption}")
    return "\n".join(lines)


async def _youtube_fallback_context(url):
    """動画を視聴できなかったときの代わりの情報源。
    返り値: (文脈テキスト, 字幕が取れたか)。何も取れなければ ("", False)。"""
    vid = _yt_video_id(url)
    meta = await _fetch_video_meta(vid)
    if not meta:
        return "", False
    caption = await _fetch_captions(vid)
    body = _format_video_meta(meta, caption)
    head = (
        f"【この動画（{url}）の情報】※映像は見ていない。"
        + ("字幕と概要欄から分かる範囲で答えること。"
           if caption else
           "字幕は取得できなかったので、タイトル・概要欄・タグから"
           "分かる範囲だけ答え、映像の中身は推測しないこと。")
    )
    return f"\n\n{head}\n{body}", bool(caption)


async def _apply_youtube_context(message, content):
    """メッセージ内のYouTubeリンク（最大2本）をGeminiが視聴し、内容を文脈に合併する。
    返り値: (合併後のcontent, リンクのみの投稿でまとめを投稿済みならTrue)"""
    urls = YOUTUBE_URL_RE.findall(content)
    if not urls:
        return content, False

    summaries, unread, fallbacks = [], [], []
    async with message.channel.typing():
        for url in urls[:2]:
            try:
                summary = await asyncio.to_thread(
                    _gemini_watch_youtube_sync, url, YOUTUBE_CHAT_PROMPT, "youtube_link"
                )
            except GeminiQuotaExceeded as e:
                _gemini_watch["outage_cid"] = message.channel.id
                # 映像は見られなくても、YouTube APIは【別の枠】なので生きている。
                # タイトル・概要欄・タグ・字幕を取れば実際の要約ができる。
                fb, has_cap = await _youtube_fallback_context(url)
                if fb:
                    fallbacks.append(fb)
                    await message.channel.send(
                        f"⚠️ 映像は視聴できませんでした（{e}）。\n"
                        + ("字幕と概要欄を取得したので、そこから答えます。"
                           if has_cap else
                           "代わりにタイトル・概要欄・タグを取得しました"
                           "（字幕は非公開のため、映像の中身までは分かりません）。")
                    )
                else:
                    await message.channel.send(
                        f"⚠️ 動画を視聴できませんでした: {e}\n"
                        "（復活を5分おきに自動確認して、復活したら知らせます）"
                    )
                    # 「読めなかった」ことを文脈に残す。残さないとリンクだけが履歴に
                    # 入り、あとで「要約して」と言われたAIが中身を知らないまま
                    # 見当違いの返事をしてしまう。
                    unread.append(url)
                continue
            except Exception as e:  # noqa: BLE001
                print(f"[youtube_link] 視聴失敗 {url}: {str(e)[:200]}")
                fb, has_cap = await _youtube_fallback_context(url)
                if fb:
                    fallbacks.append(fb)
                    await message.channel.send(
                        "⚠️ 映像は読み取れませんでした。"
                        + ("字幕と概要欄から答えます。" if has_cap
                           else "タイトル・概要欄から分かる範囲で答えます。")
                    )
                else:
                    await message.channel.send(
                        "⚠️ この動画は読み取れませんでした"
                        "（非公開・年齢制限・配信中・長すぎる等の可能性）。"
                    )
                    unread.append(url)
                continue
            if summary:
                summaries.append((url, summary))

    for url, summary in summaries:
        content += f"\n\n【YouTube動画の内容（{url}）】\n{summary}"
    for fb in fallbacks:
        content += fb
    for url in unread:
        content += (
            f"\n\n【この動画（{url}）はまだ中身を読めていない】\n"
            "内容は一切分かっていない。要約や感想を求められても、"
            "推測で語らず『まだ動画を読めていない』と正直に伝えること。"
        )

    # リンクだけの投稿なら、まとめをそのまま投稿（内容は会話の記憶にも残る）
    text_wo_urls = YOUTUBE_URL_RE.sub("", message.content).strip()
    bare_link = bool(summaries) and not text_wo_urls and not message.attachments
    if bare_link:
        for url, summary in summaries:
            await send_long(message.channel, summary, "📺 **動画の内容まとめ**\n")
    return content, bare_link


async def _run_trend_study(cid, query=None, skip_analyzed=None):
    """YouTube動画のリサーチ。query なし＝急上昇TOP100 / query あり＝そのお題で
    検索した人気動画。上位数本を視聴・分析 → レポート保存＆ダイジェスト投稿。"""
    channel = orch.get_channel(cid) or await orch.fetch_channel(cid)
    label = f"「{query}」" if query else "急上昇"

    if query:
        videos = await _search_videos(query)
        if not videos:
            await channel.send(f"🔎 {label}に合う動画が見つかりませんでした。")
            return
    else:
        videos = await _fetch_trending(100)

    # 視聴対象：長すぎない動画を上位から選ぶ。急上昇モードは分析済みをスキップして
    # 毎日知見を蓄積、お題指定モードは目的優先で分析済みも対象にする。
    # 毎日回す時は、前に見た動画を飛ばして知見を貯める。
    # その場限りのお題指定では、目的優先で分析済みも対象にする。
    if skip_analyzed is None:
        skip_analyzed = not query
    analyzed = _load_analyzed_ids() if skip_analyzed else set()
    candidates = [
        v for v in videos
        if v["id"] not in analyzed and 0 < v["duration"] <= TREND_MAX_MINUTES * 60
    ]
    targets = candidates[:TREND_DEEP_COUNT]
    await channel.send(
        f"🎬 {label}の動画{len(videos)}本を取得しました。"
        f"うち{len(targets)}本を視聴して映像分析します（数分かかります）…"
    )

    # お題指定時は、その観点を重視して視聴する
    study_prompt = VIDEO_STUDY_PROMPT + (
        f"\n特にリサーチ目的『{query}』の観点を最優先で分析して。" if query else ""
    )

    # 動画の「視聴」フェーズ。Gemini無料枠切れを検知したら以降の視聴はスキップし、
    # メタ情報（タイトル・説明文・タグ・再生数）ベースの傾向分析にフォールバックする。
    reports = []
    quota_hit = False
    for v in targets:
        try:
            analysis = await asyncio.to_thread(
                _gemini_watch_youtube_sync, v["url"], study_prompt
            )
        except GeminiQuotaExceeded as e:
            quota_hit = True
            _gemini_watch["outage_cid"] = cid
            print(f"[trend] Gemini無料枠切れ → 視聴をスキップしメタ情報分析へ: {e}")
            await channel.send(
                "⚠️ Gemini無料枠切れのため動画の視聴はスキップし、"
                "メタ情報（タイトル・説明文・タグ・再生数）ベースの傾向分析に切り替えます。"
            )
            break
        except Exception as e:  # noqa: BLE001
            print(f"[trend] 視聴失敗 {v['id']}: {str(e)[:200]}")
            continue
        if analysis:
            reports.append((v, analysis))
            if not query:
                _mark_analyzed(v["id"])

    # ランキング全体の傾向分析（Gemini枠切れ時はClaudeに自動切替）
    listing = "\n".join(
        f"{i + 1}. {v['title']}（{v['channel']} / {v['views']:,}回）"
        for i, v in enumerate(videos)
    )
    overview_src = (
        f"以下は「{query}」で検索したYouTube人気動画（直近{TREND_SEARCH_DAYS}日・再生数順）。"
        if query else
        "以下は本日のYouTube急上昇TOP100のランキング。"
    )
    overview_prompt = (
        overview_src + "映像クリエイターの視点で、\n"
        "① 伸びているジャンル・企画の傾向 ② タイトル・サムネの傾向 "
        "③ 映像制作のヒント を400字以内でまとめて。\n\n" + listing
    )
    try:
        overview = await _ai_text_bg(overview_prompt, "trend_overview")
    except Exception as e:  # noqa: BLE001
        print(f"[trend] 概観分析失敗: {str(e)[:200]}")
        overview = ""

    # 視聴できなかった場合のフォールバック：メタ情報ベースの深掘り分析
    meta_analysis = ""
    if quota_hit or not reports:
        meta_src = "\n".join(
            f"{i + 1}. {v['title']}（{v['channel']} / {v['views']:,}回 / 約{v['duration'] // 60}分）\n"
            f"   説明: {v['desc'] or 'なし'}\n"
            f"   タグ: {', '.join(v['tags']) if v['tags'] else 'なし'}"
            for i, v in enumerate(videos[:20])
        )
        meta_prompt = (
            "以下はYouTube急上昇上位20本のメタ情報（タイトル・説明文・タグ・再生数・長さ）。"
            "映像そのものは見られない前提で、メタ情報から読み取れる映像制作のヒントを"
            "600字以内でまとめて。\n"
            "① 企画・構成の傾向 ② タイトル/サムネ戦略 ③ 想定される演出・編集手法 "
            "④ 自分の映像制作への転用アイデア\n\n" + meta_src
        )
        try:
            meta_analysis = await _ai_text_bg(meta_prompt, "trend_meta_fallback")
        except Exception as e:  # noqa: BLE001
            print(f"[trend] メタ情報分析も失敗: {str(e)[:200]}")

    # 全文レポートを insights/ に保存（日付ごと。お題指定はお題入りファイル名）
    today = datetime.now(JST).strftime("%Y-%m-%d")
    fname = today + (
        "_" + re.sub(r"[^\w぀-ヿ一-鿿]+", "_", query)[:24] if query else ""
    ) + ".md"
    full = [f"# YouTube{label}リサーチ {today}", "", "## トレンド概観", overview or "（取得失敗）"]
    if meta_analysis:
        full += ["", "## メタ情報ベースの傾向分析", meta_analysis]
    for v, a in reports:
        full += ["", f"## {v['title']}（{v['channel']} / {v['views']:,}回）", v["url"], "", a]
    try:
        (INSIGHTS_DIR / fname).write_text("\n".join(full), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[trend] レポート保存失敗: {e}")

    # ダイジェストを作ってDiscordに投稿＋会話の記憶に追加
    digest = ""
    if reports:
        digest_src = "\n\n".join(f"■{v['title']}\n{a}" for v, a in reports)
        digest_prompt = (
            "以下はYouTube急上昇動画の映像分析。今後の映像制作や雑談のアイデアに"
            "使える知見として、重要ポイントを800字以内の読みやすいダイジェストに"
            "まとめて。\n\n【トレンド概観】\n" + (overview or "") +
            "\n\n【個別分析】\n" + digest_src
        )
        try:
            digest = await _ai_text_bg(digest_prompt, "trend_digest")
        except Exception as e:  # noqa: BLE001
            print(f"[trend] ダイジェスト生成失敗: {str(e)[:200]}")
    if not digest:
        digest = "\n\n".join(x for x in (overview, meta_analysis) if x) \
            or "（本日は分析結果を取得できませんでした）"

    text = f"🎬 **YouTube{label}リサーチ（{today}）**\n{digest}"
    if reports:
        text += "\n\n🔎 視聴した動画:\n" + "\n".join(
            f"・{v['title']}（{v['url']}）" for v, _ in reports
        )
    elif quota_hit:
        text += "\n\n（本日はGemini無料枠切れのためメタ情報ベースの分析です）"
    await send_long(channel, text)
    add_history(cid, "🎬映像リサーチ", f"（YouTube{label}リサーチ {today}）\n{digest}")


async def _weekly_channel_loop():
    """毎週 決まった曜日・時刻に自分のチャンネルの実績を分析して投稿する。
    曜日・時刻・投稿先は .env で変更可（既定: 月曜9時・トレンドと同じチャンネル）。
    実行済みの週を記録するので、再起動を繰り返しても二重に送らない。"""
    while True:
        conf = _load_my_channel()
        cid = conf.get("report_cid") or CHANNEL_REPORT_CID or TREND_CHANNEL_ID
        if not cid or not YOUTUBE_API_KEY or not conf.get("channel_id"):
            await asyncio.sleep(3600)   # 未設定のうちは1時間ごとに様子を見る
            continue
        now = datetime.now(JST)
        # 次に来る「指定曜日の指定時刻」を求める
        days = (CHANNEL_REPORT_DOW - now.weekday()) % 7
        run_at = (now + timedelta(days=days)).replace(
            hour=CHANNEL_REPORT_HOUR, minute=0, second=0, microsecond=0)
        if run_at <= now:
            run_at += timedelta(days=7)
        print(f"[my_channel] 次回の週次レポート: {run_at.isoformat()}")
        await asyncio.sleep((run_at - now).total_seconds())
        week = datetime.now(JST).strftime("%G-W%V")
        conf = _load_my_channel()
        if conf.get("last_report_week") == week:
            continue                     # 同じ週に二重送信しない
        try:
            await send_as(orch, cid, f"📅 今週のチャンネル実績レポートです（{week}）")
            await _analyze_my_channel(cid)
            conf = _load_my_channel()
            conf["last_report_week"] = week
            _save_my_channel(conf)
        except Exception as e:  # noqa: BLE001
            _log_error("weekly_channel_report", e)
            try:
                await send_as(orch, cid, f"⚠️ 週次レポートに失敗しました: {str(e)[:200]}")
            except Exception:  # noqa: BLE001
                pass


# ---------- 毎日の自動リサーチ（YouTube 急上昇TOP100）----------
# 以前は環境変数（TREND_CHANNEL_ID / TREND_HOUR）でしか設定できず、
# 実質いつも無効だった。ユーザーはDiscordだけで完結させたいので、
# 投稿先も時刻もDiscordの言葉で決められるようにし、設定として保存する。


# 直前にこの話をしていたか（「じゃあ毎日7時で」のような続きを拾うため）
_trend_talk = {}


def _trend_conf():
    """(有効か, 時, 分, 投稿先cid)。未設定なら環境変数→既定の順で埋める。"""
    return (
        bool(gen_settings.get("trend_on")),
        int(gen_settings.get("trend_hour", TREND_HOUR)),
        int(gen_settings.get("trend_min", 0)),
        int(gen_settings.get("trend_cid") or TREND_CHANNEL_ID or 0),
    )


def _trend_time_label():
    _, h, m, _ = _trend_conf()
    return f"{h}:{m:02d}"


# 「毎日◯時にYouTubeのTOP100をリサーチして」の受け取り。
_TREND_TOPIC_RE = re.compile(
    "(毎日|毎朝|毎晩|定期|自動|決まった時間).{0,12}"
    "(リサーチ|調査|トレンド|急上昇|top\\s*100|トップ\\s*100|ランキング)|"
    "(リサーチ|トレンド|急上昇|top\\s*100|トップ\\s*100).{0,12}(毎日|毎朝|毎晩|定期|自動)",
    re.I,
)
_TREND_OFF_RE = re.compile("やめ|止め|停止|オフ|off|いらない|解除|中止", re.I)
_TREND_ASK_RE = re.compile("いつ|何時|どうなって|設定は|状況")
# 「何時頃がいいかな？」は【相談】。設定の説明を返すのではなく普通に話す。
# 実際にこれで設定の案内を出してしまい、「相談してるだけ」と言われた。
_TREND_ADVICE_RE = re.compile(
    "がいい|が良い|でいい|おすすめ|オススメ|お勧め|どう思う|どっち|"
    "べき|かな[？?]?$|かなあ|だろう|迷"
)
# 担当（誰にリサーチさせるか）の指定
_TREND_WHO_RE = re.compile(
    "(リサーチ|調査|トレンド|急上昇).{0,10}(クロード\\s*[123１２３])|"
    "(クロード\\s*[123１２３]).{0,10}(にして|に任せ|でお願い|が担当|担当)",
    re.I,
)
_TIME_RE = re.compile("(\\d{1,2})\\s*(?::|時)\\s*(\\d{1,2})?\\s*(分|半)?")


def _parse_jst_hour(text):
    """発言から時刻を読む。(時, 分) か None。『朝7時』『19:30』『夜8時半』に対応。"""
    m = _TIME_RE.search(text or "")
    if not m:
        return None
    h = int(m.group(1))
    mi = 30 if m.group(3) == "半" else int(m.group(2) or 0)
    if h > 23 or mi > 59:
        return None
    # 「夜8時」「午後3時」は12時間制の言い方なので足す
    if h < 12 and re.search("夜|晩|午後|夕方|pm", text, re.I):
        h += 12
    return h, mi


# 毎日のリサーチで何を見るか。空＝急上昇TOP100（既定）。
_TREND_GENRE_RESET_RE = re.compile(
    "急上昇(に|へ)?(戻|もど)|全部(に|へ)?(戻|もど)|ジャンル(の)?(指定)?(を)?"
    "(外|はず|解除|やめ|なし)|絞(ら|り)(ない|なし|込まない)")
# 「リサーチは◯◯にして」から ◯◯ を取り出す
_TREND_GENRE_SET_RE = re.compile(
    r"(?:毎日の|自動)?(?:リサーチ|調査|トレンド)"
    r"(?:の(?:ジャンル|テーマ|内容|対象|条件))?\s*(?:は|を)\s*"
    r"(.{1,40}?)\s*(?:に|で)?\s*"
    r"(?:して|してほしい|絞って|しぼって|変えて|かえて|限定|にして)\s*[。、!！]?$"
)


def _match_trend_genre(text):
    """毎日のリサーチのジャンル指定を読む。
    ("set", お題) / ("reset", "") / None を返す。"""
    t = (text or "").strip()
    on_topic = bool(re.search("リサーチ|調査|トレンド|急上昇", t))
    # 「ジャンル指定を解除して」のように、解除の言い方だけで通じる場合もある
    if _TREND_GENRE_RESET_RE.search(t) and (on_topic or "ジャンル" in t):
        return "reset", ""
    if not on_topic:
        return None
    m = _TREND_GENRE_SET_RE.search(t)
    if not m:
        return None
    genre = m.group(1).strip(" 　の,、")
    # 時刻・担当・オンオフの指定はそれぞれ別の担当があるので、ここでは扱わない
    if not genre or _parse_jst_hour(genre) or re.search(
            "クロード|オン|オフ|停止|やめ|毎日|時$", genre):
        return None
    return "set", genre[:40]


def _match_trend_schedule(text, recent_topic=False):
    """毎日の自動リサーチの設定変更を読む。
    返り値: ("on", 時, 分) / ("off", 0, 0) / ("ask", 0, 0) / ("who", 0, 0) / None
    recent_topic=True（直前にこの話をしていた）なら、
    「じゃあ毎日7時で」のような続きの短い返事も設定として受け取る。"""
    t = text or ""
    on_topic = bool(_TREND_TOPIC_RE.search(t))
    hm = _parse_jst_hour(t)
    # 担当の指定は「毎日」が付かない言い方（「リサーチはクロード1にして」）が普通
    if _TREND_WHO_RE.search(t):
        return "who", 0, 0
    # 直前にこの話をしていたなら、時刻だけの返事でも設定として受け取る
    if not on_topic and recent_topic and hm and re.search("毎日|じゃあ|それで|で$|でいい", t):
        return "on", hm[0], hm[1]
    if not on_topic:
        return None
    if _TREND_OFF_RE.search(t):
        return "off", 0, 0
    # 【相談】は設定にしない。時刻がはっきり書かれている時だけ設定として扱う。
    if _TREND_ADVICE_RE.search(t) and not hm:
        return None
    if _TREND_ASK_RE.search(t) and not hm:
        return "ask", 0, 0
    if hm:
        return "on", hm[0], hm[1]
    return "on", TREND_HOUR, 0     # 時刻の指定が無ければ既定の時刻で始める


async def _daily_trend_loop():
    """設定された時刻（JST）に急上昇TOP100のリサーチを自動実行する。
    1分ごとに設定を読み直すので、Discordで時刻を変えても再起動は要らない。"""
    last_done = None                     # 最後に実行した日付（二重実行の防止）
    while True:
        await asyncio.sleep(60)
        try:
            on, hour, minute, cid = _trend_conf()
            if not on or not cid or not YOUTUBE_API_KEY:
                continue
            now = datetime.now(JST)
            if now.hour != hour or now.minute != minute or last_done == now.date():
                continue
            last_done = now.date()
            print(f"[trend] 自動リサーチを開始: {now.isoformat()}")
            _who = gen_settings.get("trend_who", "claude1")
            _wname = {"claude1": CLAUDE1_NAME, "claude2": CLAUDE2_NAME,
                      "claude3": CLAUDE3_NAME}.get(_who, CLAUDE1_NAME)
            await send_as(
                orch, cid,
                f"📊 毎日の自動リサーチ（{now.strftime('%m/%d %H:%M')}）"
                f"：{_wname}が"
                + (f"「{gen_settings['trend_query']}」で伸びている動画"
                   if gen_settings.get("trend_query") else "YouTube急上昇TOP100")
                + "を見てきます…"
            )
            _spawn(_run_trend_study(cid, gen_settings.get("trend_query") or None,
                                    skip_analyzed=True),
                   cid, "YouTubeリサーチ")
        except Exception as e:  # noqa: BLE001
            print(f"[trend] 自動リサーチ失敗: {str(e)[:300]}")


# ---------------------------------------------------------------------------
# オーケストレーター：3層構造
#   ① ルーティング（得意モデルへ振り分け／簡単なら単発）
#   ② ディベート（各回答を相互に見せて批判・修正）
#   ③ 司令塔が統合（合意点/対立点を整理して単一回答へ）
# ---------------------------------------------------------------------------
# オーケストレーターが直接返事をするときの名乗り。
# 以前は engine 名（"Claude" / "Gemini"）をそのまま人格として渡していたため、
# 「俺はGeminiじゃなくてクロード」と正体の訂正を始める事故が起きた。
# どのエンジンを使うかは内部の都合であって、ユーザーには関係がない。
# 普段の返事を書く担当。クロード1（調べる）・クロード3（別の見方）に対して、
# クロード2は「話を受けて、決めて、進める人」＝PM。表に出る顔でもある。
ORCH_PERSONA = (
    f"{CLAUDE2_NAME}。koheiの相棒で、依頼を受けて段取りを決め、進める担当"
)


def _answer_prompt(who, history, extra=""):
    return (
        f"あなたは{who}。次の会話の最後の要求に、正確で役立つ回答を日本語で簡潔に述べる。"
        "前置きや名乗りは不要、回答本体のみ。" + topic_guide(history) + "\n\n"
        + _running_note(_cid_of_history(history))
        + _profiles_context()
        + (extra + "\n\n" if extra else "")
        + transcript_block(history)
        + "\n\n上の会話ログの最後の要求に答えてください。"
        "ログや指示文をそのまま繰り返さず、回答の本文だけを書くこと。"
    )


def _critique_prompt(me, other, my_ans, other_ans, history):
    return (
        f"あなたは{me}。同じ要求に対する、あなたの回答と{other}の回答がある。"
        f"{other}の回答を批判的に検討し、正しい点は取り入れ、誤りや見落としは指摘して、"
        "あなたの回答を改善した最終版だけを日本語で簡潔に述べる。\n\n"
        f"要求と会話:\n{build_transcript(history)}\n\n"
        f"あなた（{me}）の回答:\n{my_ans}\n\n"
        f"{other}の回答:\n{other_ans}\n\n改善後のあなたの回答:"
    )


# 軽い雑談の担当モデル。claude=サブスク定額でGemini枠を温存（応答5〜20秒）/
# gemini=高速応答1〜3秒（無料枠を消費）。.env の CASUAL_LEAD で切替。
# 軽い雑談は既定でGeminiに任せる。Geminiは高速（1〜3秒）で無料枠、
# Claudeはサブスクの利用上限があり実際に上限到達で止まったことがあるため、
# 負荷を分散する意味でも雑談はGemini側に寄せる。枠切れ時は自動でClaudeへ。
# 返事は既定でクロードに一本化する。Geminiにも書かせていた頃は
# 「誰が答えたのか分からない」「Geminiが勝手に作り話をする」が続いたため。
# Geminiは返事以外（画像生成・動画視聴・添付の解析・検品・要約）で今も働く。
CASUAL_LEAD = os.getenv("CASUAL_LEAD", "claude")
if CASUAL_LEAD not in ("claude", "gemini"):
    CASUAL_LEAD = "claude"


def _casual_lead():
    """雑談の担当。Discordから変えられる（「返答をgeminiにして」で戻せる）。"""
    v = gen_settings.get("casual_lead")
    return v if v in ("claude", "gemini") else CASUAL_LEAD


def _gemini_replies_on():
    """Geminiに返事を書かせてよいか。既定はオフ。"""
    return _casual_lead() == "gemini"


# 「返事はクロードにして」のように、雑談の担当を名指しで変える言い方。
_LEAD_SWITCH_RE = re.compile(
    r"(?:返事|返答|回答|受け答え|雑談|会話)\S{0,4}(?:は|を)?\s*"
    r"(クロード|claude|gemini|ジェミニ)\s*(?:に|で)\s*(?:して|しろ|変えて|変更|"
    r"担当|答えさせて|やらせて|お願い)",
    re.I,
)
_LEAD_SWITCH_RE2 = re.compile(
    r"(クロード|claude|gemini|ジェミニ)\s*(?:に|が)\s*(?:答えさせて|返事させて|"
    r"応答させて|担当させて)", re.I)


def _match_casual_lead(text):
    """雑談の担当を変える指示か。('claude'|'gemini', 表示名) か None。"""
    m = _LEAD_SWITCH_RE.search(text or "") or _LEAD_SWITCH_RE2.search(text or "")
    if not m:
        return None
    who = m.group(1).lower()
    return (("claude", CLAUDE2_NAME) if who in ("クロード", "claude")
            else ("gemini", "Gemini"))

# AI判定が必要そうなキーワード（作業指示・生成依頼・検索・過去記憶）。
# これに全く該当しない短い発言は、AIを呼ばず雑談として即処理（Gemini無料枠の節約）。
# 「実際にやって」と頼んでいる形か。会話のテンションの発言を作業にしないための門。
# 事故：「クロードコードって便利だよね」のような雑談から、コード修正の
# 実行プラン（承認ダイアログ）が立ち上がっていた。話題に出しただけの語を
# 依頼と取り違えないよう、依頼の【形】を要求する。
# 依頼の「文法」。動詞を並べて当てる方式では、言い方を1つ増やすたびに
# 取りこぼす。日本語で人にものを頼む形そのものを見る。
_ORDER_RE = re.compile(
    # 〜て / 〜てください / 〜てほしい / 〜てくれる？ / 〜といて
    # 〜て（文末でも、読点や続きがあっても依頼は依頼）
    "[ぁ-んァ-ヶ一-龥ー]て(ください|下さい|ほしい|欲しい|くれ|くんない|"
    "もらえ|いただけ|ね|よ|)[。、,!！?？]*($|[^いるたなかもの])|"
    "[ぁ-んァ-ヶ一-龥ー]て(ください|下さい|ほしい|欲しい|くれ|)[。、!！?？]*$|"
    "て(ください|下さい|ほしい|欲しい|くれる|くれない|もらえる|もらえない)|"
    "といて|とい(て|た)|ておいて|"
    # 〜たい / 〜が欲しい（日本語では依頼として通じる）
    "たい(な|んだけど|んだよね|)[。、!！?？]*$|たいんだけど|"
    "(が|を|)(欲しい|ほしい)|"
    # 言い切りの依頼
    "お願い|頼む|頼める|やって|やろう|"
    # 命令形
    "(作|直|変|消|足|入|出|送|見せ|調べ|止め|上げ|下げ)れ[。、!！]*$"
)


# _plan() の分類のうち、依頼の形を要求するもの（勝手に始まると困る作業）。
# restart / talk / profile / trend は害が小さいので対象外。
_ACTION_KINDS = ("selffix", "exec", "video", "image")


# はっきり頼んでいる形。これがあれば、説明を求める言い方や
# 「〜って」を含んでいても依頼として通す。
# 事故：「相関図の作り方を踏まえて相関図作って」が、文中の「の作り方」だけを見て
# 説明の質問と判定され、制作が始まらなかった。
_STRONG_ORDER_RE = re.compile(
    "ください|下さい|ほしい|欲しい|お願い|頼む|頼める|"
    "て(くれ|くんない|もらえ|いただけ|ちょうだい)|"
    "といて|ておいて|やって|やっとい|"
    # 文末の「〜て」（「相関図作って」「コード直して」）
    "[ぁ-んァ-ヶ一-龥ー]て[。、!！]*$"
)
# 質問の終わり方。頼んでいるのではなく聞いている。
_QUESTION_END_RE = re.compile("[?？]$|の[?？]?$|ですか[。?？]*$|ますか[。?？]*$")
# 何を直すのかが書かれていない修正の希望。道具の名前しか書かれていない
# 「クロードコードで直したい」で、対象不明のまま確認画面を出さないため。
_VAGUE_FIX_RE = re.compile(
    "^(直し|なおし|修正し|変更し|変え|改善し|いじり|触り)たい[。！!]*$")


def _wants_action(text):
    """『いま実際にやって』と頼まれている形か。
    プロンプトで「迷ったらchat」と書いても守られなかったので、
    最終判断はコード側で持つ（このリポジトリの決まり）。"""
    t = _strip_media_context(text or "").strip()
    if not t:
        return False
    # 「顔が違うので、鼻だけ変えてください」は打ち消しではなく直しの依頼。
    # 「違う」を打ち消しとして先に弾くと、直しの指示が会話に落ちる。
    if _RESULT_COMPLAINT_RE.search(t) and _CHANGE_VERB_RE.search(t):
        return True
    if (_NEGATION_RE.search(t) or _USER_REPORT_RE.search(t)
            or _NOT_NOW_RE.search(t)):
        return False                      # 打ち消し・お礼・継続のお願い
    # はっきり頼んでいない時だけ、話題として触れただけの言い方を落とす。
    # 「〜って便利だよね」「〜って使ってる？」の「って」は引用の助詞であって
    # 依頼の「〜て」ではない。独り言（〜わ／〜かな）も、質問も依頼ではない。
    if not _STRONG_ORDER_RE.search(t):
        if _EXPLAIN_Q_RE.search(t):
            return False
        if re.search("(よね|だよね|かな|のかな|わ|っけ)[。、!！?？]*$", t):
            return False
        if _QUESTION_END_RE.search(t):
            return False
        if _VAGUE_FIX_RE.search(_strip_engine_words(t)):
            return False
    # 「ヒッグスフィールドで」「クロードで」だけの言い直しは、
    # 直前の依頼の作り手を変える指示。文法上は依頼の形をしていない。
    if not _has_subject(t) and (_BY_HF_RE.search(t) or _BY_CLAUDE_RE.search(t)
                                or _BY_GEMINI_RE.search(t)):
        return True
    return bool(_ORDER_RE.search(t))


_PLAN_TRIGGER_RE = re.compile(
    "作って|作成|生成|描いて|書いて|直して|修正|書き換え|編集|実行|インストール|コマンド|"
    "デバッグ|バグ|動画|映像|ＣＭ|CM|PV|画像|イラスト|ロゴ|絵|"
    "最新|ニュース|調べ|検索|比較|価格|いくら|発売|リリース|"
    "前に|昨日|以前|この前|先週|先月|過去|話した|決めた|約束|"
    "機能|追加|変更|挙動|ボット|bot|自分|きみ|君|あなた|お前|再起動|短く|長く|口調|"
    "トレンド|急上昇|リサーチ|雑談|会話して|話して|プロフィール|プロファイル"
)


# ---------- ルーティング判定（純粋関数・テスト対象） ----------
_STATUS_KW_RE = re.compile(
    # 「〜できたら」「〜終わったら」は条件の言い方で、進捗の問いではない。
    # 事故：「もしあっちに新しい人ができたら本当に別れないといけない」が
    # 進捗確認になった（別れ話をしている最中だった）。
    "できた(?!ら|り)|できてる|出来て(?!たら)|完成|終わった(?!ら|り)|終わってる|"
    "どうなった|状況|進捗|まだ|"
    # 「いつできる？」を入れ忘れていたため会話に落ち、AIが
    # 「12:50に上限がリセットされて〜」と作り話をする事故が起きた
    "いつでき|いつ出来|いつ終わ|いつ仕上が|いつ届く|何時に終わ|"
    "見れる|見せて|見たい|"
    # 「ください」は依頼形そのもので、進捗の語ではない。
    # 「鼻の高さだけ変えてあとは顔のパーツに合わせてください」が
    # 進捗確認になり、直しの指示が消えた。URLを求める言い方だけ拾う。
    "url|ＵＲＬ|どこ|ある\\?|ある？|ちょうだい|"
    "(送っ|見せ|出し|貼っ|上げ)て(ください|下さい)|"
    "あとどれ|どれくらい|どのくらい|どれぐらい|どのぐらい|何分|確認して", re.I
)
# ユーザー自身が「できた／うまくいった」と報告・お礼を言っている発言。
# 進捗の質問ではないので、完成物を出し直してはいけない。
# 実際に「できた！ありがとー」に対して直近のデザインを貼り直す事故が起きた。
_USER_REPORT_RE = re.compile(
    "ありがと|あんがと|助かった|たすかった|嬉しい|うれしい|よかった|良かった|"
    "できた[！!♪〜ー。]|できたよ|できたわ|できました|うまくいった|うまく行った|"
    "いけた|やった[！!]|完成した[！!]|投稿できる|使えそう"
)
# 否定の言い回し。「〜してないよ」を指示と読まないための共通ガード。
_NEGATION_RE = re.compile("してない|してません|じゃな|ではな|違う|ちがう|ないよ|ないです")
_STATUS_CTX_RE = re.compile(
    "動画|画像|モーション|生成|デザイン|サムネ|バナー|相関図|関係図|家系図|"
    "系図|組織図|構成図|年表|チャート|フローチャート|図解|チラシ|ポスター|スライド")
# 生成物の「中身」についての質問（＝進捗確認ではない）。
# 例:「どこですかここは」は『動画どこ？』ではなく写っている場所を尋ねている。
_CONTENT_Q_RE = re.compile(
    "ここは|ここって|ここが|ここどこ|何が写|誰が写|写って|映って|"
    "背景|どこの|どこで|場所|建物|風景|何て書|なんて書|読める"
)
# デザイン制作（文字が主役）の対象物。画像生成AIが苦手な領域。
_DESIGN_NOUN_RE = re.compile(
    "バナー|ポスター|チラシ|フライヤー|スライド|図解|インフォグラフィック|"
    "価格表|料金表|比較表|一覧表|レイアウト|カンプ|名刺|表紙|ジャケット|"
    # 「性格分析表」「相性表」など『〜表』全般。表を含むが別の意味の語
    # （発表・代表・表現・表参道・表示…）は拾わない
    r"[ぁ-んァ-ヶ一-龥ー]{1,9}(?<![発代裏])表(?!現|参道|示|明|彰|情)|"
    # 図もHTMLで組む対象。生成AIでは文字と線が崩れて読めないものになる
    "相関図|関係図|家系図|系図|系統図|組織図|構成図|フローチャート|"
    "チャート|図表|ダイアグラム|マインドマップ|ロードマップ|年表|タイムライン"
)
# 絵そのものの依頼。デザインではなく画像生成に回す。
_IMAGE_NOUN_RE = re.compile("ロゴ|イラスト|絵|写真|アイコン")
# デザインの手直しで実際に触る「部位・見た目」の語。
# これが無い発言を手直しとみなすと、「親が入院しろって言ってくる」の
# 「しろ」まで変更指示に見えてしまう（実際に制作が始まる事故が起きた）。
_DESIGN_TWEAK_RE = re.compile(
    "文字|フォント|書体|字|色|カラー|背景|レイアウト|余白|間隔|行間|字間|"
    "サイズ|大きく|小さく|太く|細く|明るく|暗く|濃く|薄く|派手|地味|"
    "位置|配置|中央|真ん中|左|右|上|下|寄せ|揃え|"
    "線|枠|箱|矢印|囲み|影|グラデ|タイトル|見出し|ラベル|凡例|"
    "全体|バランス|雰囲気|トーン|デザイン|画像|図"
)
# 「クロードでサムネ作って」「geminiでサムネ作って」のように作り手を名指しする言い方。
# クロード＝HTMLで組む（文字が正確）／Gemini＝画像生成（絵が得意）。
# 同じ「サムネ」でも、どちらに投げたいかは本人にしか決められないので明示を優先する。
_BY_CLAUDE_RE = re.compile(
    r"(クロード|claude|くろーど|html)\s*(?:で|に|を使って|使って|側で)", re.I)
_BY_GEMINI_RE = re.compile(
    r"(gemini|ジェミニ|じぇみに)\s*(?:で|に|を使って|使って|側で)", re.I)
_BY_HF_RE = re.compile(
    r"(ヒッグスフィールド|ヒッグス|higgsfield|hf)\s*(?:で|に|を使って|使って|側で)",
    re.I)
# 作り手の指定だけを取り除くための語（依頼の中身と混ぜない）。
# 事故：「ヒッグスフィールドで作って」が題材として読まれ、
# "Higgs boson field visualized as an endless dark cosmic void..." という
# 素粒子物理の画像が生成された。指定語は題材ではない。
_ENGINE_WORD_RE = re.compile(
    # 「クロードコード」は道具の名前。「クロード」だけ落とすと「コード」が
    # 依頼の中身として残り、「クロードコードで直したい」が
    # 『コードを直す依頼』に見えてしまう。長い名前から先に落とす。
    r"(ヒッグスフィールド|ヒッグス|higgsfield|クロードコード|claude\s*code|"
    r"クロード|claude|くろーど|"
    r"gemini|ジェミニ|じぇみに|html)\s*(?:で|に|を使って|使って|側で)?",
    re.I)
_REQ_VERB_RE = re.compile(
    "作って|作成して|生成して|つくって|描いて|お願いします|お願い|ください|"
    "してほしい|して欲しい|やって|して")


def _strip_engine_words(text):
    """『ヒッグスフィールドで作って』から作り手の指定と依頼の動詞を落として、
    依頼の【中身】だけを残す。空になれば中身は書かれていない、と判断できる。"""
    t = _ENGINE_WORD_RE.sub("", text or "")
    t = _REQ_VERB_RE.sub("", t)
    return re.sub(r"[\s、。,.！!？?でにをがはもの]+", "", t)


def _has_subject(text):
    """何を作るのかが、その発言自体に書かれているか。"""
    return len(_strip_engine_words(text)) >= 2


# 直近に頼まれた媒体（画像か動画か）。cid -> ("image"|"video", 時刻)
# 事故：「画像で生成して」→「ヒッグスフィールドでやって」と続けた時、
# 後の発言に媒体の語が無いので既定の動画になり、seedance で .mp4 が出た。
# 直前に画像を頼まれていたなら画像のままにする。
_last_media = {}
MEDIA_KEEP_SEC = 3600
_IMAGE_WORD_RE = re.compile("画像|イラスト|ロゴ|絵|写真|アイコン|サムネ|静止画")
_VIDEO_WORD_RE = re.compile("動画|映像|ムービー|クリップ|ショート")
_NOT_THAT_RE = "(じゃなくて|ではなく|でなく|じゃなく|ではない|じゃない)"


def _said_media(text):
    """発言が媒体をはっきり指しているか。指していなければ None。
    「動画の生成じゃなくて画像の生成にして」は【画像】。打ち消しを先に見る。"""
    t = text or ""
    # 「動画の生成じゃなくて画像の生成にして」のように、語と打ち消しの間に
    # 「の生成」が挟まる。間を許して見る。
    if re.search(f"(動画|映像|ムービー)[^。、]{{0,6}}{_NOT_THAT_RE}", t):
        return "image"
    if re.search(f"(画像|静止画|写真)[^。、]{{0,6}}{_NOT_THAT_RE}", t):
        return "video"
    has_i, has_v = _IMAGE_WORD_RE.search(t), _VIDEO_WORD_RE.search(t)
    if has_i and not has_v:
        return "image"
    if has_v and not has_i:
        return "video"
    return None


def _remember_media(cid, mtype):
    if mtype in ("image", "video"):
        _last_media[cid] = (mtype, time.time())


def _recent_media(cid):
    v = _last_media.get(cid)
    if v and time.time() - v[1] <= MEDIA_KEEP_SEC:
        return v[0]
    return None


# 直近に送られた画像（cid -> (url, 時刻)）。写真を送った次の発言で
# 「これで作って」と言われた時に、参照として引き継ぐために持つ。
_last_ref = {}
REF_KEEP_SEC = 1800


def _remember_ref(cid, url):
    if url:
        _last_ref[cid] = (url, time.time())


def _recent_ref(cid):
    url, t = _last_ref.get(cid, ("", 0))
    return url if url and time.time() - t < REF_KEEP_SEC else ""


def _last_request_text(cid):
    """直近の『中身のある依頼』の本文を会話履歴から拾う。
    「ヒッグスフィールドで」のような指定だけの発言を、
    直前に何を頼まれていたかで補うために使う。"""
    for name, text in reversed(get_history(cid) or []):
        if not text or name == SUMMARY_SPEAKER or name == "Orchestrator":
            continue
        body = _strip_media_context(text)
        if _has_subject(body):
            return body
    return ""


def _request_with_context(req, cid):
    """作り手の指定しかない依頼を、直前の依頼の中身で補う。
    これが無いと『ヒッグスフィールドで作って』の指定語が題材になる。"""
    if _has_subject(req or ""):
        return req
    prev = _last_request_text(cid)
    return f"{prev}（{req}）" if prev else req
# 作り手を名指しできる対象物（絵・デザインの両方）
_VISUAL_NOUN_RE = re.compile(
    "サムネ|thumbnail|バナー|画像|イラスト|ロゴ|絵|写真|アイコン|デザイン|"
    "ポスター|チラシ|フライヤー|表紙|カバー|スライド|図解|価格表|料金表|名刺|"
    "相関図|関係図|家系図|系図|系統図|組織図|構成図|フローチャート|チャート|"
    "図表|ダイアグラム|マインドマップ|ロードマップ|年表|タイムライン|"
    r"[ぁ-んァ-ヶ一-龥ー]{1,9}(?<![発代裏])表(?!現|参道|示|明|彰|情)", re.I)
_GEN_INTENT2_RE = re.compile(
    "作って|作りたい|作成|つくって|つくりたい|生成|描いて|えがいて|かいて|"
    "デザインして|animate|動かして|アニメ化|が?欲しい|がほしい"
)
# 生成依頼から「何を作るか」以外の言葉（エンジン名・媒体名・依頼表現）を落とす。
# 「geminiで画像生成して」のように主題が無い依頼を検出して聞き返すために使う。
_GEN_META_RE = re.compile(
    r"gemini|ジェミニ|nano ?banana|ナノバナナ|higgsfield|ヒッグスフィールド|"
    r"画像|イラスト|ロゴ|絵|写真|アイコン|サムネ(イル)?|"
    r"作って|作りたい|生成して|生成|つくって|お願いします|お願い|ください", re.I
)
# 主題の判定で端に残る助詞や記号（「猫の」→「猫」、「で」→「」）
_GEN_EDGE = " 　でをがのにはとやも、。!！?？「」"


def _gen_subject(content):
    """生成依頼から『何を作るか』の部分だけを粗く取り出す（有無の判定用）。"""
    return _GEN_META_RE.sub("", content or "").strip(_GEN_EDGE)
_MOTION_KW_RE = re.compile("モーション|この動き|動きで生成|動きを真似|動きをコピー|動きを転写")
_QUESTION_RE = re.compile(
    "どう思う|なんで|なぜ|どうやって|できる\\?|できる？|作れる|入れられる|"
    "って何|とは|意味|進捗|どうなって|してもいい|でもいい|と思う|かな|"
    "教えて|仕組み|方法|やり方|どうやって|違いは|どっちが"
)


# 「作ってほしい」ではなく「聞きたいだけ」の言い方。
# 実例:「veo3.1生成するときクレジットいくらくらいか聞きたいだけ」で生成が始まった。
# 料金・条件を知りたいだけの質問は、生成の意図でも作業の依頼でもない。
_ASK_INFO_RE = re.compile(
    "聞きたい|聞くだけ|訊きたい|知りたい|確認したいだけ|"
    "いくら(?!でも)|幾ら|何円|なん円|"
    # 「価格表」「料金表」は作るものの名前なので、料金の質問と取り違えない
    "料金(?!表)|価格(?!表)|値段|費用|相場|コスト(?!を?(下げ|抑え|削減))|"
    # 「無料で動画作って」は依頼なので、断定を聞く形のときだけ質問扱いにする
    "(無料|有料)(なの|ですか|かな|か？|か\\?|？|\\?)|"
    "どれくらいかかる|どのくらいかかる|どれぐらいかかる|どのくらい必要|"
    "何クレジット|なんクレジット|クレジット(は|って|を)?(いくら|どれ|どの|何|なん)"
)
# 「モデル」は日常語でもある（セルラーモデル・最新モデル・モデルルーム）。
# このボットの生成モデルの話だと分かる言い方だけを拾う。
_GEN_MODEL_ASK_RE = re.compile(
    "(生成|画像|動画|映像|モーション|使ってる|使っている|使用|今|いま|現在|"
    "どの|何の|なんの|どんな)の?\s*モデル|"
    "モデルの?設定|設定の?モデル|モデルは(何|なに|どれ|どっち)"
)
# 「モデル」を含むが生成モデルの話ではない語（製品・人物・建物など）
_NOT_GEN_MODEL_RE = re.compile(
    "セルラーモデル|セルラモデル|wi-?fiモデル|上位モデル|下位モデル|"
    "最新モデル|旧モデル|新モデル|前モデル|同モデル|限定モデル|"
    "モデルチェンジ|モデルルーム|モデルハウス|モデルケース|モデルコース|"
    "スーパーモデル|読者モデル|モデル体型|モデルさん|モデル歩き",
    re.I,
)

def _asks_gen_model(said):
    """『いまの生成モデルは何？』と聞かれているか。
    条件を関数にしておく（テストが本物の条件を確かめられるようにするため）。"""
    said = said or ""
    return bool(
        _GEN_MODEL_ASK_RE.search(said)
        and not _NOT_GEN_MODEL_RE.search(said)
        and re.search("設定|確認|見せて|教えて|どれ|なに|何", said)
    )


# 長い動画から切り抜く依頼。新規生成（ショート量産）とは別物なので分ける。
_CLIP_INTENT_RE = re.compile(
    "切り抜|切りぬき|きりぬき|クリップ|ショートに|ショート動画に|"
    "短くして|分割して|ショートにして|切って|切り出|きりだ|抜き出して|ダイジェスト"
)
_YT_LINK_RE = re.compile(r"https?://(?:www\.|m\.)?(?:youtube\.com|youtu\.be)/\S+")
# 動画ファイルの直リンク、またはMac上のパス（1GB級はDiscordに載らないため）
# 動画ファイルの名前が出てくるか（パスでもURLでもない、名前だけの言及も拾う）
_VIDEO_NAME_RE = re.compile(r"[^\s▸/]+\.(?:mp4|mov|m4v|webm|mkv)", re.I)
_VIDEO_PATH_RE = re.compile(
    r"https?://\S+\.(?:mp4|mov|webm|m4v)|~?/[^\s'\"]+\.(?:mp4|mov|webm|m4v|mkv)", re.I)

# 役（クロード1/3）に意見を求める言い方。名前が出ただけでは呼ばない。
_ASK_ROLE_RE = re.compile(
    "聞いて|訊いて|きいて|意見|どう思う|見て|見解|検討|相談|"
    "出して|答えて|教えて|考えて|分析"
)
# 役の担当を決める言い方（呼び出しではなく設定の話）
_ROLE_ASSIGN_RE = re.compile(
    "にして|にしとい|に任せ|担当|でお願い|が(やる|担当)|に変えて|"
    "に割り当て|役割|でいい|にしよう"
)

# 「〜って何？」「〜ってどうやるの？」＝ものの説明を求める質問。
# 名前が出ただけで機能を起動しない（普通の会話のテンションを守るための門）。
# 実例:「実績ってどうやって見るの」で実績分析が、
#      「クロード3ってどんな役割？」で複数視点の呼び出しが走っていた。
_EXPLAIN_Q_RE = re.compile(
    "って(何|なに|どう|どんな|する人|できる|使う|やる|便利|すごい|いい|難しい|大事)|"
    "とは(何|なに|\?|？|$)|"
    "の(意味|仕組み|やり方|使い方|作り方|直し方|調べ方|コツ|違い|役割)|"
    "どうやって(見る|やる|する|作る|調べる|使う|出す)|"
    "どういう(こと|意味|仕組み|もの|仕事|役割)"
)

# ヒッグスフィールドの使い方。既定は「明示的に頼まれた時だけ」。
# 本人の希望：「クロードだけでやってほしいことはクロードでって言うから、
# 安易にヒッグスフィールドを使わないでほしい」。クレジットを消費するので、
# 黙って切り替える（フォールバックする）ことをやめる。
HF_MODE_DEFAULT = os.getenv("HF_MODE", "explicit")   # explicit / auto
_HF_OFF_RE = re.compile(
    "ヒッグス\\S*(は|を)?(使わないで|使うな|使わない|やめて|切って|オフ)|"
    "higgsfield\\S*(は|を)?(使わないで|使うな|オフ|off)|"
    "(勝手に|安易に|自動で)\\S*(生成|ヒッグス|higgsfield)\\S*(しないで|使わないで|やめて)",
    re.I,
)
_HF_ON_RE = re.compile(
    "ヒッグス\\S*(は|を)?(使って|使っていい|オン|許可|解禁)|"
    "higgsfield\\S*(は|を)?(使って|オン|on|許可)",
    re.I,
)
# ユーザーが名指ししたと言える語（これがあれば明示的に頼まれたとみなす）
_HF_NAMED_RE = re.compile(
    "ヒッグス|higgsfield|veo|kling|sora|seedance|seedream|nano\\s*banana|"
    "ナノバナナ|クリング|ソラ|シードランス", re.I,
)


def _hf_explicit_only():
    """ヒッグスフィールドを『明示的に頼まれた時だけ』に絞るか。"""
    return (gen_settings.get("hf_mode") or HF_MODE_DEFAULT) != "auto"


_AUTO_UPDATE_RE = re.compile("自動(更新|アップデート|反映)|オートアップデート")
_OFF_RE = re.compile("オフ|off|止め|やめ|停止|切っ|しないで|無効", re.I)


def _match_auto_update(text):
    """自動更新の切替。True=オン / False=オフ / None=関係ない発言。"""
    t = text or ""
    if not _AUTO_UPDATE_RE.search(t):
        return None
    if re.search("って何|とは|どうなって|どういう", t):
        return None                      # 説明を求められているだけ
    return not _OFF_RE.search(t)


def _match_hf_mode(text):
    """発言からヒッグスフィールドの使い方の切替を読む。(値, 表示) か None。"""
    t = text or ""
    if _HF_OFF_RE.search(t):
        return "explicit", "頼まれた時だけ使う"
    if _HF_ON_RE.search(t):
        return "auto", "必要なら自動で使う"
    return None


# 「今やって」ではない言い方。継続のお願い・打ち消し・前置きなど。
_NOT_NOW_RE = re.compile(
    "これからも|今後も|引き続き|次回から|次からも|そのうち|いずれ|"
    "指示じゃな|依頼じゃな|命令じゃな|お願いじゃな|やらなくていい|"
    "しなくていい|今はいい|今じゃなくて|今すぐじゃな|まだいい"
)
# 打ち消しを打ち消す語（「これからも、とりあえず今すぐやって」は依頼のまま）
# 「今すぐじゃなくていいから」は打ち消し。ここで「今すぐ」に当ててしまうと
# 打ち消しが効かず、実際に役の呼び出しが走った。否定形は除く。
_NEG_TAIL = "(?!じゃな|ではな|でなく|じゃなく|なくて)"
_NOW_RE = re.compile(
    f"今すぐ{_NEG_TAIL}|いますぐ{_NEG_TAIL}|今から|いまから|今回は|"
    "とりあえず今|すぐに(やって|お願い)")

# 「今すぐ作れ」という明確な依頼（料金の話が混ざっても作業を止めない）
_GEN_ORDER_RE = re.compile("作って|作成して|つくって|生成して|描いて|作ってほしい")
# 会話パスの claude CLI にはMCPの権限が無いため、ツールを呼ぼうとすると弾かれる。
# その言い訳（「権限が下りない」等）を検知して、権限のある経路で調べ直す。
_TOOL_DENIED_RE = re.compile(
    "権限が(下り|降り|おり)|権限エラー|権限を(許可|下ろ)|権限がな|"
    "ツールが(動かない|使えない|弾かれ|呼べな)|ツールを呼べ|"
    "permission denied|not permitted|permission error", re.I
)


def _looks_like_question(text):
    """質問・相談っぽい発言か（作業命令ではない）。作業系ルートの誤爆を防ぐ。"""
    text = text or ""
    return bool(text.rstrip().endswith(("？", "?"))
                or _QUESTION_RE.search(text)
                or _ASK_INFO_RE.search(text))


def _match_gen_model(content):
    """発言中のモデル名 → (model_id, media_type, label)。無ければ None。"""
    low = content.lower()
    for name in sorted(HF_GEN_MODELS, key=len, reverse=True):
        if name in low:
            return HF_GEN_MODELS[name]
    return None


# 副作用のある作業＝実際に何かを作る・変える・消費するもの。
# ここに入るルートは「依頼の形をしている時だけ」動かす。
# 語が当たったら動く方式では、誤爆のたびに正規表現を狭める作業が終わらず、
# 実際に「もっと褒めて笑」「クロードコードって便利だよね」
# 「リサーチはクロード1にして」などが作業を起こしていた。
ACT_ROUTES = frozenset({
    "design", "image", "hf_auto", "hf_model", "revise", "edit", "short",
    "ad", "motion", "style_learn", "clip", "virality",
})


def classify_route(content, **kw):
    """発言の行き先を決める。副作用のある作業は依頼の形の時だけ通す。
    ただしファイルを添付している時は、それ自体が『これで何かして』という
    意思表示なので、言い方が短くても通す（「この動きで」＋動画 など）。"""
    route = _classify_route_raw(content, **kw)
    if (route in ACT_ROUTES and not kw.get("has_attachments")
            and not _wants_action(content)):
        return None          # 頼まれていない＝会話として扱う
    return route


# 作り手を名指ししていても、生成ではない用事（調べ物・要約・文章）。
_NOT_GEN_VERB_RE = re.compile(
    "調べ|検索|要約|まとめて|翻訳|説明して|教えて|読んで|分析|"
    "考えて|相談|聞いて|返事|文章|台本|コメント|意見")


def _classify_route_raw(content, *, has_attachments=False, has_video_att=False,
                   has_image_att=False, has_job=False, has_last_gen=False,
                   after_credits=False, has_running=False, last_was_design=False):
    """@メンションなし発言のルーティングを判定（AI(_plan)前の決定的ルートのみ）。
    返り値: 'status'/'revise'/'short'/'virality'/'ad'/'credits'/'design'/
    'hf_model'/'hf_auto'/'motion'/'motion_ask'/None。
    on_message と同じ順序・同じ正規表現を使うので、これをテストすれば実挙動を検証できる。"""
    # ⓪ 「これからもよろしく」は“今やって”ではない。
    #    実際に「チャンネル実績レポートこれからもよろしくね」で確認が立ち上がり、
    #    言い直すたびに『作業を中止した』が会話に割り込んだ。
    if _NOT_NOW_RE.search(content) and not _NOW_RE.search(content):
        return None
    # ⓪.5 「〜って何？」は説明を求める質問。制作の指示が同じ文に無ければ会話。
    if _EXPLAIN_Q_RE.search(content) and not _GEN_ORDER_RE.search(content):
        return None
    # ① 生成物の状態確認（添付なし・状態ワード・文脈）。
    #    直近の生成があれば「できた？」だけでも状態確認につなぐ。
    #    ただし「作って」「作り直して」など生成・修正の依頼は状態確認にしない
    #    （「〜作ってください」の「ください」で誤爆しないように）。
    status_kw = _STATUS_KW_RE.search(content)
    # 実行中の作業があれば「まだ？」「あと何分？」だけでも状態確認につなぐ。
    # これが無いとAIに流れ、実行中だと知らないAIが
    # 「その機能自体が無い」と作り話をする事故が実際に起きた。
    # 「まだ」「できた」だけを頼りにすると、身の上話まで進捗確認になる。
    # 生成の話だと分かる語があるか、進捗を聞く短い一言の時だけにする。
    _short_ask = len(_strip_media_context(content).strip()) <= 20
    status_ctx = _STATUS_CTX_RE.search(content) or (
        (has_job or has_last_gen or has_running) and status_kw
        and (_short_ask or _STATUS_CTX_RE.search(content))
    )
    if (not has_attachments and status_kw and status_ctx
            # 「できた！ありがとー」のような報告・お礼は進捗の質問ではない
            and not _USER_REPORT_RE.search(content)
            and not re.search("作って|作りたい|つくって|生成して|作成して|描いて|アニメ化", content)
            and not _CONTENT_Q_RE.search(content)   # 中身への質問は会話へ
            and not _looks_revise(content, has_last_gen)):
        return "status"
    # ①.35 長い動画の切り抜き（素材を渡して「ショートにして」）。
    #       完パケ編集より先に見る。編集ルートは Higgsfield のクラウドで
    #       ffmpeg を回すが、切り抜きは Mac 上で完結しクレジットを使わない。
    #       添付動画に「切り抜いて」と言われたら、こちらが本命。
    if (_CLIP_INTENT_RE.search(content) and not _looks_like_question(content)
            and (_YT_LINK_RE.search(content) or has_video_att
                 or _VIDEO_PATH_RE.search(content)
                 # 動画ファイルの名前が書かれていれば素材の指定とみなす。
                 # iPhoneから貼った道順（iCloud Drive ▸ …）もここで拾う。
                 or _VIDEO_NAME_RE.search(content))):
        return "clip"
    # ※編集は作り直しより先に判定する。「もっと短くして」は作り直しではなく
    #   尺の編集だが、作り直しの語（もっと等）にも当たるため順序が効く。
    # ①.7 完パケ編集（既にある動画への後工程。新規生成とは別物）
    if (has_video_att or has_last_gen) and re.search(
        "字幕|テロップ|サブタイトル|編集して|加工して|つなげ|繋げ|結合|くっつけ|"
        "尺を|秒に(して|縮め)|短くして|長くして|カットして|トリム|切り抜いて|"
        "9:16|縦型に|横型に|縦にして|横にして|音量|BGM|無音に", content
    ) and not _looks_like_question(content):
        return "edit"

    # ①.4 前の生成の作り直し（修正マーカーがあれば発動。記録が無くても
    #     _run_revise が Higgsfield から直前プロンプトを回収するので安全）。
    #     「前の動画のこと覚えてる？」のような質問では発動しない
    # 「〜してくれる？」は問いかけの形をした依頼なので、?で終わっても通す。
    # これが無いと「武将も追記してくれる？」が会話に落ちて何も起きなかった。
    # 出来上がりへの不満＋直しの指示は、丁寧形でも疑問形でも作り直し。
    # 「顔が違うので、鼻の高さだけ変えて…ください」が進捗確認に化けていた。
    _fix_now = bool(_RESULT_COMPLAINT_RE.search(content)
                    and _CHANGE_VERB_RE.search(content))
    if (not has_attachments and _looks_revise(content, has_last_gen)
            and (_fix_now or not _looks_like_question(content)
                 or _wants_action(content))):
        # 作り直しでも「誰に作らせるか」の指定が最優先。
        # 「クロードで作り直して」を作風の指定と読んで Higgsfield に投げ、
        # 「Claude.ai風デザイン」の画像を生成してしまう事故が起きた。
        if _BY_CLAUDE_RE.search(content):
            return "design"
        if _BY_GEMINI_RE.search(content):
            return "image"
        if _BY_HF_RE.search(content):
            return "hf_auto"
        # 直前がデザインなら、作り直しも同じ作り方（HTML）で行う。
        # 画像生成に投げると、せっかくの文字が崩れたものに置き換わってしまう。
        if last_was_design:
            return "design"
        return "revise"
    # 直前がデザインなら、短い手直し（「背景を暗くして」等）も作り直しとして扱う。
    # 動画/画像と違い、デザインは細かい調整を重ねる使い方が普通のため。
    # ただし【デザインの部位・見た目の語がある時だけ】に限る。
    # 変更動詞だけを条件にしていたため、「親が入院しろって言ってくる」の
    # 「しろ」を手直し指示と読んでデザイン制作を始める事故が起きた。
    if (last_was_design and not has_attachments
            # 「背景も変えてくれる？」は問いかけの形をした手直しの依頼
            and (not _looks_like_question(content) or _wants_action(content))
            # 「デザインの話はしてないよ」を手直しの指示と読まない
            and not _NEGATION_RE.search(content)
            and not _USER_REPORT_RE.search(content)
            and len(content) <= 40
            and _CHANGE_VERB_RE.search(content)
            and _DESIGN_TWEAK_RE.search(content)):
        return "design"
    # ①.45 作り手の名指しだけの言い直し（「ヒッグスフィールドで」「クロードで」）。
    #      直前に依頼があるので、その中身のまま作り手だけ変える。
    if has_last_gen and not has_attachments and not _has_subject(content):
        if _BY_HF_RE.search(content):
            return "hf_auto"
        if _BY_CLAUDE_RE.search(content):
            return "design"
        if _BY_GEMINI_RE.search(content):
            return "image"
    # ①.5 ショート量産（「ショート作って」「今日のショート」等）
    if re.search("ショート|shorts?|ショート動画", content, re.I) and (
        _GEN_INTENT2_RE.search(content) or re.search("今日の|ネタ|企画|お願い", content)
    ):
        return "short"
    # ①.6 バズ度シミュレーション（広告効果の事前予測＝物理エンジン相当）。
    #     「バズる動画作って」のような生成依頼は②へ、
    #     「バズった動画調べて」のようなリサーチはAI(trend)へ譲る。
    if (not _GEN_INTENT2_RE.search(content)
            and not re.search("チャンネル|実績|成績|投稿した", content)  # それは実績分析
            and re.search("バズ|バイラル|広告効果|再生数|伸び", content)
            and re.search("分析|予測|チェック|診断|シミュレ|測って|判定", content)):
        return "virality"
    # ①.7 広告代理店モード（企画書＋縦型CM動画の制作）。
    #     「10cm」等の単位と誤爆しないよう CM は直前が数字でない場合のみ。
    if re.search("広告|(?<![0-9０-９])[cCｃＣ][mMｍＭ]|コマーシャル|プロモ", content) and (
        _GEN_INTENT2_RE.search(content) or re.search("お願い|企画", content)
    ):
        return "ad"
    # ①.71 複数視点で検討（クロード1＝情報収集／クロード3＝多角的視点）
    # 名前が出ただけでは呼ばない。実際に「リサーチするのはクロード1にしてね」で
    # 役の呼び出しが走った（担当を決める話であって、意見を聞く話ではない）。
    if (re.search("多角的|多角度|いろんな(視点|角度)|色んな(視点|角度)|"
                  "複数の(視点|角度)|両面から|別の視点", content)
            or (re.search("クロード\\s*[1１]|クロード\\s*[3３]|claude\\s*[13]|"
                          "リサーチャー|アドバイザー", content, re.I)
                and not _ROLE_ASSIGN_RE.search(content)
                and _ASK_ROLE_RE.search(content))):
        return "multiview"
    # ①.72 自分のチャンネルの実績分析／チャンネル登録
    if re.search("チャンネル", content) and re.search(
        "登録|設定|変更|セット|教える|これ", content
    ) and re.search(r"https?://|@[\w.\-]+|UC[\w-]{20,}", content):
        return "ch_set"
    #      「実績/成績/再生数」は単体で実績分析。「チャンネル＋分析」も同じ。
    #      生成依頼（〜作って）が混ざっている場合は制作なので対象外にする。
    if (not _GEN_INTENT2_RE.search(content)
            and (re.search("実績|成績|再生数|視聴回数|伸び方", content)
                 or (re.search("チャンネル", content)
                     and re.search("分析|レポート|振り返り|どう", content)))
            and not re.search("この動画|添付", content)):
        return "ch_stats"
    # ①.75 デバッグログの共有（スクショを撮らずに開発側へ状況を渡す）
    if re.search("ログ|log", content, re.I) and re.search(
        "送って|送っと|送信|共有|出して|上げて|あげて|渡して|見せて|"
        "ちょうだい|ください|くれ", content
    ) and not re.search("消して|削除", content):
        return "sharelog"
    # ①.76 料金・残クレジットの照会（作らずに、実データを調べて答える）。
    #      「無料で動画作って」のような明確な依頼は制作なので対象外。
    #      「クレジット」「残高」はそれだけでHiggsfieldの話だが、「プラン」「料金」は
    #      事業計画や一般の話にも使うので、生成の文脈があるときだけ拾う。
    if (not _GEN_ORDER_RE.search(content) and _looks_like_question(content)
            and (re.search("クレジット|残高|課金", content)
                 or (re.search("料金|価格|値段|費用|コスト|いくら|何円|なん円|"
                               "無料|有料|プラン", content)
                     and (_match_gen_model(content)
                          or re.search("生成|動画|映像|画像|イラスト|higgsfield|ヒッグス",
                                       content, re.I))))):
        return "credits"
    #      直前が料金照会なら、続きの短い質問（「画像生成はどうなの？」等）も照会に流す。
    #      普通の会話パスにはMCPの権限が無く、ツールが動かずに
    #      「権限が下りない」と言い出す事故が起きたため。
    if (after_credits and _looks_like_question(content) and len(content) <= 40
            and not _GEN_ORDER_RE.search(content)
            and re.search("画像|動画|映像|イラスト|音声|音楽|モデル|生成|プラン|"
                          "それ|こっち|そっち|他|ほか|逆に", content)):
        return "credits"
    # ①.8 スタイル学習（参考動画から勝ちパターンを覚えて以降の生成に反映）
    if re.search("スタイル|作風", content) and re.search("リセット|白紙|消して|忘れて|クリア", content):
        return "style_reset"
    if re.search("(学習|覚え)(した|た)スタイル|スタイル(を|は)?(見せて|どんな|確認|教えて)", content):
        return "style_show"
    #     「覚えてる？」のような質問・既存機能の話と誤爆しないよう、
    #     添付/リンクなしの案内(style_ask)は明確な依頼形＋非質問のときだけ
    _learn_strict = re.search("学習して|学習させ|覚えさせ|参考にして|真似して|勉強して", content)
    _learn_loose = _learn_strict or re.search("覚えて(?![るたない])", content)
    if not re.search("調べて|リサーチ|検索", content):
        if _learn_loose and (has_video_att or YOUTUBE_URL_RE.search(content)):
            return "style_learn"
        if (_learn_strict and not _looks_like_question(content)
                and re.search("動画|ショート|映像|スタイル|作風", content)):
            return "style_ask"
    # ①.85 作り手の名指しが最優先（「クロードでサムネ作って」「geminiでサムネ作って」）。
    #       本人が指定した以上、こちらの自動判定より優先する。
    if (_GEN_INTENT2_RE.search(content) and not _looks_like_question(content)
            and _VISUAL_NOUN_RE.search(content)):
        if _BY_CLAUDE_RE.search(content):
            return "design"
        if _BY_GEMINI_RE.search(content):
            return "image"
        if _BY_HF_RE.search(content):
            return "hf_auto"
    # ①.9 デザイン制作（文字が主役のもの。画像生成AIは文字が苦手なので
    #      ClaudeにHTMLで組ませてスクリーンショットする）。
    #      「猫のイラスト作って」のような絵の依頼は従来どおり画像生成へ。
    if (_GEN_INTENT2_RE.search(content)
            or re.search(r"(デザイン|バナー|ポスター|チラシ|フライヤー|スライド|"
                         r"名刺|表紙|図解)\S{0,6}お願い", content)
            ) and not _looks_like_question(content) and (
        _DESIGN_NOUN_RE.search(content)
        # 「ロゴをデザインして」のような絵の依頼は、従来どおり画像生成に任せる
        or (re.search("デザイン", content) and not _IMAGE_NOUN_RE.search(content))
        or (re.search("サムネ|thumbnail|タイトル画像|カバー", content, re.I)
            and re.search("文字|テキスト|タイトル|キャッチ|コピー|入れて|入り", content))
    ):
        return "design"
    # ② Higgsfield生成（モーション以外・生成意図あり）
    #    「動画お願い」もAI判定に落とさず生成として扱う
    if not re.search("モーション|この動き|動きを", content) and (
        _GEN_INTENT2_RE.search(content)
        or re.search(r"(動画|映像|画像|イラスト|ロゴ|写真)\S{0,8}お願い", content)
    ):
        # 「どうやって動画作ってるの？」「veo3の料金いくら？」のような
        # 質問では発動しない。モデル名の指定があっても同じ（質問が先）。
        if not _looks_like_question(content):
            if _match_gen_model(content):
                return "hf_model"
            # 画像は Gemini（無料枠）優先。「geminiで画像作って」もここ
            if re.search("画像|イラスト|ロゴ|絵|写真|アイコン|サムネ", content):
                return "image"
            auto_kw = re.search(
                "おまかせ|お任せ|自動|最適|いい感じ|良い感じ|どれでも|モデル任せ|よしなに|"
                "バズる|バズり|バズそう", content
            )
            # 媒体が明示されていればAI判定に落とさず生成へ（速度と確実性）
            media_noun = re.search("動画|映像|ムービー|クリップ|PV|ＰＶ", content)
            if auto_kw or has_video_att or has_image_att or media_noun:
                return "hf_auto"
    # ③ モーション転写（キーワード or 依頼待ち中の動画添付）。
    #    「モーション動画じゃないよ」のような否定・単なる言及では発動させず、
    #    生成の意図がある時だけ反応する
    if _MOTION_KW_RE.search(content) and not re.search("じゃな|ではな|違う|ちがう", content):
        if has_video_att:
            return "motion"
        if _GEN_INTENT2_RE.search(content) or re.search("したい|やりたい|お願い", content):
            return "motion_ask"
    # ④ 作り手を名指しした依頼は、必ずその作り手の生成へ。
    #    他のどのルートにも当たらなかった時の最後の受け皿。
    #    事故：「geminiで背景を普通の室内にして」が会話に落ち、
    #    ボットは「生成の実行に許可が必要みたい」と作り話をして終わった。
    #    名指しは本人の明確な意思表示なので、会話に落としてはいけない。
    if (not has_attachments and _wants_action(content)
            and not _looks_like_question(content)
            # 「ジェミニで調べて」「ジェミニで要約して」は生成の依頼ではない
            and not _NOT_GEN_VERB_RE.search(content)
            # 何を作るかが書かれている時だけ。「ヒッグスフィールドで作って」だけなら
            # 直前の依頼を引き継ぐ①.45に任せる（無ければ何も始めない）
            and _has_subject(content)):
        if _BY_HF_RE.search(content):
            return "hf_auto"
        if _BY_GEMINI_RE.search(content):
            return "image"
    return None  # 決定的ルートに該当せず → AI(_plan)へ


_PLAN_REPLY_SEP = "---REPLY---"


async def _plan(history):
    """要求の分類（exec/video/image/chat）＋処理方針＋【雑談ならその返事まで】を
    1回のAI呼び出しで得る。返り値: (kind, mode, lead, search, recall, reply)。

    速度の要：以前は「分類」と「回答」でAIを2回直列に呼んでおり、
    Gemini枠切れ時は claude CLI の起動が2回重なって20〜60秒かかっていた。
    普通の会話（chat・検索不要・過去記憶不要）は、この1回の中で返事も書かせて
    そのまま送る＝待ち時間が半分になる。
    返事が取れなかった場合は reply="" となり、従来どおり回答フェーズへ回るので
    壊れない（JSONに混ぜず区切り行で分けるのは、改行や引用符でJSONが壊れないため）。
    トリガー語を含まない短い発言は、そもそもAIを呼ばず即・雑談扱い（枠の節約）。"""
    latest = _latest_user_msg(history)
    if len(latest) <= 60 and not _PLAN_TRIGGER_RE.search(latest):
        return "chat", "single", _casual_lead(), False, False, ""
    prompt = (
        "あなたはDiscordボット（オーケストレーター）のルーター。"
        "次の会話の最後の要求を分類し、処理方針をJSONだけで返す。\n"
        '形式: {"kind":"chat"|"exec"|"video"|"image"|"selffix"|"restart",'
        '"mode":"single"|"debate",'
        '"lead":"claude"|"gemini","search":true|false,"recall":true|false}\n'
        "- kind: selffix=このボット自身のコードを【今すぐ実際に書き換える】明確な命令のみ"
        "（例:『返答をもっと短くして』『!trendの本数を3本に変えて』"
        "『きみのコードのバグを直して』）。"
        "【重要】次はselffixではなくchat: 質問（『〜は？』『どう思う？』『なんで？』）、"
        "アイデアや構想の相談（『〜みたいなの作れる？』『〜を入れたい』『〜してもいいと思う』）、"
        "原因の議論、進捗の確認。迷ったらchat。実際に『直して/変えて/修正して』と"
        "命令された時だけselffix。"
        "restart=ボットの再起動依頼。"
        "trend=既存のYouTube動画の調査・分析・人気動画のリサーチ依頼"
        "（例:『トレンド調べて』『fatboyslimのMVリサーチして』『人気の動画10本』"
        "『〜系の動画を調べて』。直前の会話がリサーチの流れならその続きもtrend）。"
        "talk=ClaudeとGeminiだけで自動会話させる依頼（例:『二人で雑談して』）。"
        "profile=学習済みの人物プロファイルを見たい（例:『プロフィール見せて』）。"
        "exec=ボット以外のファイルやコードを作成・編集・削除、コマンド実行する"
        "明確な作業指示（例:『server.pyのバグを直して』）。"
        "video=あなたに新しく動画・映像・CM・PVを【制作】してほしい依頼"
        "（例:『犬の30秒CM作って』『バズる動画作って』『かっこいい映像お願い』）。"
        "『バズる〜作って』は新規制作なのでvideo。既存動画を調べる話だけが trend。"
        "image=画像・イラスト・ロゴの生成依頼。"
        "chat=それ以外すべて（質問・相談・意見・雑談）。迷ったら必ずchat。"
        "『（ファイル共有）』で始まる発言＝添付ファイルを共有しただけなので、"
        "明確な依頼文が無い限り必ずchat（video/execにしない）。\n"
        "- mode: 原則single。『重大な判断・設計・事実の突き合わせが本当に必要』な時だけdebate。\n"
        "- lead: claudeが得意=コード・デバッグ・論理的推論・設計判断・丁寧な日本語の長文 / "
        "geminiが得意=要約・多言語翻訳・最新情報・画像や視覚の話題・箇条書き整理・アイデア出し。\n"
        "- search: 最新情報・時事・製品/価格・実在の事実確認が要るときだけtrue。\n"
        "- recall: 『前に話した』『昨日の』『以前決めた』など、直近の会話に無い"
        "過去の記憶が必要なときだけtrue。\n"
        # 分類はGeminiが担当（速い・無料枠）。返事はクロードが書くので、
        # Geminiに返事を書かせない設定のときは、ここで返事を求めない
        # （求めるとGeminiの文がそのまま表に出て、声が混ざる）。
        + (f"\n【返事も同時に書く】kindがchat、かつ mode=single、search=false、"
           f"recall=false のときは、JSONの次の行に「{_PLAN_REPLY_SEP}」とだけ書き、"
           f"その次の行からユーザーへの返事本体を書くこと。"
           f"返事は日本語で{REPLY_CHARS}字以内、前置きや名乗りは無しで本体のみ。"
           f"それ以外のkind（video/exec/trend等）では返事を書かずJSONだけ返す。"
           f"【厳守】返事は必ずユーザーの最後の要求そのものに答える。"
           f"下の運用ルールは『守るべき方針』であって返事の内容ではないので、"
           f"その文言をそのまま返事として出力してはいけない。\n"
           if _gemini_replies_on() else
           "\n返事は書かず、JSONだけを返すこと（返事はクロードが書く）。\n")
        + topic_guide(history) + "\n\n"
        + _running_note(_cid_of_history(history))
        + _profiles_context()
        + transcript_block(history)
        + "\n\n上の会話ログの最後の発言について、指定された形式で出力してください。"
    )
    kind, mode, lead, search, recall, reply = "chat", "single", "claude", False, False, ""
    try:
        raw = await _ai_text(prompt, "plan")
        head, sep, tail = (raw or "").partition(_PLAN_REPLY_SEP)
        m = re.search(r"\{.*\}", head, re.S)
        d = json.loads(m.group(0)) if m else {}
        if d.get("kind") in (
            "chat", "exec", "video", "image", "selffix", "restart",
            "trend", "talk", "profile",
        ):
            kind = d["kind"]
        # 依頼の形をしていない発言は、AIが何と言おうと会話に戻す。
        # 「クロードコードって便利だよね」から実行プランが立ち上がる等、
        # 話題に出しただけの語で作業が始まる事故を、コード側で止める。
        if kind in _ACTION_KINDS and not _wants_action(latest):
            print(f"[plan] 依頼の形ではないので会話に戻す: {kind} ← {latest[:40]}")
            _fired(_cid_of_history(history), f"{kind}→会話に戻した", latest)
            kind, reply = "chat", ""
        if d.get("mode") in ("single", "debate"):
            mode = d["mode"]
        if d.get("lead") in ("claude", "gemini"):
            lead = d["lead"]
        search = bool(d.get("search"))
        recall = bool(d.get("recall"))
        # 同時に書かれた返事は、条件を満たすときだけ採用（それ以外は回答フェーズへ）
        if sep and kind == "chat" and mode == "single" and not search and not recall:
            reply = tail.strip()
    except Exception as e:  # noqa: BLE001
        print(f"[plan] 判定失敗（既定値で続行）: {str(e)[:150]}")
    return kind, mode, lead, search, recall, reply


async def _handle_image_request(cid, request, refine=True):
    """画像生成の依頼。既定は Gemini（無料枠 約500枚/日）。
    Gemini が使えないとき、以前は黙って Higgsfield に切り替えていたが、
    頼んでいないクレジット消費になるためやめた（本人の希望）。
    既定では切り替えず、どうするかを聞く。
    日本語の会話文はそのまま渡すと『全然違う画像』になるため、必ず英語の
    描写プロンプトに変換してから生成し、使ったプロンプトも見せる（動画と同じ扱い）。"""
    original = request
    _remember_media(cid, "image")
    # 「この画像の背景を室内にして」は、元の写真を渡さないと別人が出来上がる。
    # 事故：参照を渡さずに作り、依頼者の写真とは無関係な男性の画像になった。
    ref_bytes, ref_mime = None, "image/png"
    if _POINTS_AT_RE.search(request) or _looks_revise(request, True):
        _ref_url = _recent_ref(cid)
        if _ref_url:
            ref_bytes, ref_mime = await _fetch_image_bytes(_ref_url)
            if ref_bytes:
                await send_as(orch, cid, "🖼 直前の画像を元にして直します。")
            else:
                print(f"[image_request] 参照画像を取得できません: {_ref_url[:80]}")
    if refine:
        refined = await _refine_prompt(request, "image", has_ref=bool(ref_bytes))
        if refined and refined != request:
            request = refined
            await send_as(orch, cid, f"🖋 プロンプト: {request[:300]}")
    _save_last_gen(cid, request, "image", None, "画像")
    await send_as(
        orch, cid,
        "🎨 Gemini で画像を生成中…（無料枠"
        + ("・元の画像を使用" if ref_bytes else "") + "）")
    _why, _quota = "", False
    try:
        data = await asyncio.to_thread(
            _gemini_generate_image_sync, request, ref_bytes, ref_mime)
        await send_image_bytes(
            cid, "✅ できました！イメージと違うところがあれば「〇〇を直して作り直して」と教えてください。",
            data, "image.png",
        )
        add_history(cid, "Orchestrator", f"（依頼「{original[:60]}」の画像をGeminiで生成して投稿した）")
        return
    except Exception as e:  # noqa: BLE001
        # 理由を標準出力にしか出しておらず、「無料枠は残っているのに作れない」の
        # 原因が誰にも見えなかった。本人にも見せ、errors.log にも残す。
        _why = str(e)[:300]
        _quota = isinstance(e, GeminiQuotaExceeded) or _is_quota_error(e)
        print(f"[image_request] Gemini失敗: {_why}")
        _log_error("Gemini画像生成", e)
    # ここで黙ってHiggsfieldに切り替えていた＝頼んでいないクレジット消費。
    # 既定では切り替えず、どうするかを本人に選んでもらう。
    if _hf_explicit_only() and not _HF_NAMED_RE.search(original):
        await send_as(
            orch, cid,
            ("🕒 **Geminiの無料枠を使い切りました。**\n"
             if _quota else "⚠️ Gemini（無料枠）で画像を作れませんでした。\n")
            + (f"（{_why}）\n" if _quota else
               (f"（理由: {_why}）\n" if _why else ""))
            + ("使えるモデルは順番に試したうえで全部だめでした。"
               "待てば自動で戻ります。急ぐなら:\n" if _quota else
               "勝手にHiggsfieldへは"
               "切り替えません（クレジットを使うため）。どちらか送ってください。\n")
            + "・「**クロードで作って**」＝HTMLから書き出す（無料・文字や図に強い）\n"
            + "・「**ヒッグスフィールドで作って**」＝生成モデルを使う（クレジット消費）"
        )
        return
    await send_as(orch, cid, "⚠️ Gemini画像が使えないため、Higgsfieldで生成します…")
    url = await _mcp_gen_and_wait(request, media_type="image", model=None)
    if url:
        _update_last_gen_url(cid, url)
        add_history(cid, "Orchestrator", f"（依頼「{original[:60]}」の画像をHiggsfieldで生成: {url}）")
        await _report_result(cid, request, url, "image", "✅ できました！")
    else:
        await send_as(orch, cid, "⚠️ 画像生成に失敗しました。少し時間をおいて再度お試しください。")


# モーションコントロールの依頼待ち（cid -> {"req": 依頼文, "ts": 時刻}）。
# 「モーションコントロールで作りたい」→ 後から動画添付、の分割メッセージに対応。
_pending_motion = {}

# モーション転写のモデルID候補。上から順に試し、通ったIDを gen_settings に記憶する。
# （Higgsfieldのカタログは非公開のため、fal互換パスと参照動画対応モデルを網羅的に試す）
MOTION_CANDIDATES = [
    "kling-video/v2.6/pro/motion-control",
    "kling-video/v2.6/standard/motion-control",
    "fal-ai/kling-video/v2.6/pro/motion-control",
    "kling-video/v3/standard/motion-control",
    "bytedance/seedance/v2/pro/reference-to-video",
    "bytedance/seedance/v1/pro/reference-to-video",
]


def _is_model_not_found(e):
    m = str(e).lower()
    return "model_not_found" in m or "not found" in m or "unknown model" in m


# 進行中の生成ジョブの記録（再起動に耐えるようファイルへ）。
# {cid, submitted_at, request, model, media_type, label} を保存。
# 完了監視が復帰後も続けられる。モーション/Seedance/Veo など全モデル共通。
_MOTION_JOB_FILE = HISTORY_DIR / "pending_motion.json"


def _save_motion_job(cid, request, model="kling3_0_motion_control",
                     media_type="video", label="モーション動画", asked=""):
    # asked＝本人が実際に言った依頼。request は機械が作った英語プロンプト。
    # 出来上がりの照合は asked と突き合わせないと、自分の創作を基準に
    # 誤判定する（「a man と書いたのに女性だ」）。
    _write_json(_MOTION_JOB_FILE,
                {"cid": cid, "submitted_at": time.time(),
                 "request": request[:200], "asked": (asked or request)[:200],
                 "model": model,
                 "media_type": media_type, "label": label}, "gen")


def _load_motion_job():
    return _read_json(_MOTION_JOB_FILE) or None


def _clear_motion_job():
    try:
        _MOTION_JOB_FILE.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _pending_eta_msg(job):
    """まだ生成中のとき、投入からの経過時間と、実測にもとづく残り時間を返す。
    以前は『画像3分・動画12分』と決め打ちだったが、これは根拠のない数字で、
    実際より短く出て何度も待たせた。過去に完成した回の実測だけを使う。"""
    if not job or not job.get("submitted_at"):
        return "⏳ まだ生成中のようです。少し待って、もう一度「できた？」と送ってください。"
    elapsed = int(time.time() - job["submitted_at"])
    label = job.get("label", "生成")
    st = _task_stats(_gen_task_name(job))
    head = f"⏳ {label}は投入から{_fmt_dur(elapsed)}経過。"
    if not st:
        return (
            head + "この種類の生成はまだ実測がないので、残り時間は分かりません"
            "（今回の時間を記録します）。完成したら自動で投稿します。"
        )
    n, med, mx = st
    body = f"過去{n}回の実測は{_fmt_dur(med)}〜{_fmt_dur(mx)}。"
    if elapsed >= mx:
        return (
            head + body + "過去最長を超えています"
            "（Higgsfield側が混んでいる可能性があります）。"
            "もう少し待って「できた？」と送ってください。"
        )
    remain = f"{_fmt_dur(max(med - elapsed, 0))}〜{_fmt_dur(mx - elapsed)}" \
        if elapsed < med else f"最大{_fmt_dur(mx - elapsed)}"
    return (
        head + body + f"残りおよそ{remain}です。完成したら自動で投稿します"
        "（「できた？」でいつでも確認できます）。"
    )


def _extract_video_url(text):
    """claude出力からメディアURLを頑健に抽出（cloudfront / .mp4/.png/.jpg / http）。"""
    for pat in (r"https?://\S*cloudfront\S+",
                r"https?://\S+\.(?:mp4|mov|png|jpe?g|webp)\S*",
                r"https?://\S+"):
        urls = re.findall(pat, text or "")
        if urls:
            return urls[-1].rstrip(").,。、」)")
    return None


async def _mcp_motion_control(image_url, video_url, request):
    """Higgsfield MCP経由でモーション転写ジョブを投入する（完了は待たない）。
    claude CLI に MCP ツールを呼ばせる（要: Mac側で一度 claude mcp add ＋認証）。
    投入できたら True、失敗なら例外。"""
    # 顔の同一性を上げるため：高解像度＋identity保持の指示＋顔の向きモードは
    # gen_settings["motion_orientation"]（既定 image=キャラ画像の見た目を優先）
    orient = gen_settings.get("motion_orientation", "image")
    task = (
        "Higgsfield の MCP ツールでモーション転写動画の生成ジョブを投入して。\n"
        "使うツール: motion_control（無ければ generate_video で "
        "model=kling3_0_motion_control。過去に実績のある正しいモデルID）。\n"
        f"・参照動画（動きの元・role=video）: {video_url}\n"
        f"・キャラクター画像（見た目・顔・role=image）: {image_url}\n"
        f"・character_orientation: {orient} "
        "（キャラクター画像の顔・見た目をできる限り忠実に保つこと）\n"
        "・resolution/画質は選べる範囲で最も高いもの（1080p等）を使う"
        "（顔のディテール保持のため。低解像度は避ける）\n"
        "・duration: 参照動画の長さに合わせる（最大15秒）\n"
        f"・内容の希望: {request[:300]}\n"
        "プロンプトには『preserve the exact face and identity of the reference "
        "character, keep facial features consistent』を含める。\n"
        "URLは media_import_url 等で取り込んでよい。ジョブの投入だけ行い、完了は待たなくてよい。"
        "投入できたら最終行に『SUBMITTED』、失敗したら『ERROR: 理由』とだけ出力して。"
    )
    out = await _run_claude_exec(task, timeout=600)
    print(f"[motion_mcp] 投入結果末尾: {(out or '')[-400:]}")
    if not out or out.startswith("⚠️"):
        raise RuntimeError(f"claude CLI実行失敗: {(out or '')[:200]}")
    if re.search(r"ERROR", out.strip().splitlines()[-1], re.I) and \
            not _extract_video_url(out):
        raise RuntimeError(out.strip().splitlines()[-1][:300])
    # 投入中に既にURLが返ってくることもある
    return _extract_video_url(out) or True


# Higgsfieldの成果物URLには hf_YYYYMMDD_HHMMSS が入る。
# これを見れば「今回のものか」を機械的に判定できる。
# プロンプトで「これより前は対象外」と伝えるだけでは守られず、
# 実際に【6日前】の画像が「できました！」として出てきた。
_HF_STAMP_RE = re.compile(r"hf_(\d{8})_(\d{6})")


def _url_is_stale(url, since, slack_sec=120):
    """URLに埋まった生成日時が、投入時刻より前なら True（今回のものではない）。
    日時が読めない時は False（判定できないものは止めない）。"""
    if not url or not since:
        return False
    m = _HF_STAMP_RE.search(url)
    if not m:
        return False
    try:
        made = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        made = made.replace(tzinfo=timezone.utc).timestamp()
    except Exception:  # noqa: BLE001
        return False
    return made < since - slack_sec


async def _mcp_gen_status(media_type="video", model=None, since=None):
    """生成ジョブの完了確認（1回）。完了ならURL、処理中ならNone。全モデル共通。
    since（投入時刻）を渡すと、それより古い生成は今回のものではないとして無視する。
    これが無いと、履歴に残る【1か月前の別の生成】を完成として報告していた
    （実際に『公園でバスケをする男性にリスが絡む動画』が結果として出た）。"""
    model_clause = (
        f"最新の {model} ジョブ" if model else f"最新の {media_type} ジョブ"
    )
    since_clause = ""
    if since:
        stamp = datetime.fromtimestamp(since, JST).strftime("%Y-%m-%d %H:%M")
        since_clause = (
            f"\n【重要】今回の投入は {stamp}（JST）です。"
            "それより前に作られた生成は今回のものではないので絶対に対象にしない。"
            "該当する新しい生成が無ければ『PENDING』と答えること。"
        )
    task = (
        f"Higgsfield の MCP ツール show_generations（type={media_type}, size=3）で"
        f"最新の生成履歴を確認して。{model_clause}について、status が completed なら"
        "そのURL（results.rawUrl）だけを最終行に出力。failed なら『ERROR: 理由』、"
        "まだ処理中・履歴に無い場合は『PENDING』とだけ最終行に出力して。"
        + since_clause
    )
    out = await _run_claude_exec(task, timeout=180)
    print(f"[gen_mcp] 状態確認: {(out or '')[-200:]}")
    if not out or out.startswith("⚠️"):
        return None
    last = out.strip().splitlines()[-1].strip()
    if last.upper().startswith("ERROR"):
        raise RuntimeError(last[:300])
    return _extract_video_url(out)




# チャンネルごとの「最後に料金照会をした時刻」。続きの質問を同じ経路に流すために使う。
_last_credits = {}

# claude CLI は作業ディレクトリの CLAUDE.md（開発者向けルール）を読むため、
# 「変更したら再起動を案内する」が、コードと無関係な調査結果の末尾にも付いてくる。
# 実例：画像生成のクレジット量の回答に「Discordで『再起動して』」が付いた。
# CLAUDE.md 側にも条件を書いたが、条件をAIに守らせる方法は当てにならないので
# コード側でも落とす。
_CLI_TAIL_RE = re.compile(
    r"^.*(?:再起動して[」』]?\s*と送って|再起動してください|再起動が必要).*$", re.M
)
# 「Got real numbers from get_cost. Reporting back to the orchestrator.」のような
# 内部向けの英語ナレーション。日本語を含まない行だけを対象にする（誤削除を防ぐ）。
_CLI_NARRATION_RE = re.compile(
    r"^\s*(?:Got |Reporting back|Let me |I'?ll |I will |I'?m going|Now I|"
    r"Checking |Calling |Fetching |Looking |Done[.!]|Perfect[.!]|Great[.!])",
    re.I,
)
_JA_RE = re.compile(r"[ぁ-んァ-ヶ一-龥]")


def _strip_cli_boilerplate(text):
    """claude CLI の出力から、ユーザーに関係のない定型文を落とす。
    ① CLAUDE.md 由来の再起動案内（開発者向けルールの漏れ）
    ② 内部向けの英語ナレーション（URLを含む行は消さない）"""
    if not text:
        return text
    kept = []
    for line in _CLI_TAIL_RE.sub("", text).splitlines():
        s = line.strip()
        if (s and not _JA_RE.search(s) and "http" not in s
                and _CLI_NARRATION_RE.match(s)):
            continue
        kept.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


async def _run_credits(content, history=None):
    """Higgsfieldのプラン・残クレジットと、モデル別の消費量を実データで答える。
    「veo3で1本いくら？」のような質問に、生成を始めずに数字だけ返すための経路。
    数字は必ずMCPツールの返り値から取り、無ければ『不明』と言わせる
    （推測の金額を答えるのが一番まずいため）。"""
    hit = _match_gen_model(content)
    if hit:
        focus = f"特に {hit[2]}（モデルID: {hit[0]}）で1本生成したときの消費クレジット。"
    elif re.search("画像|イラスト|写真|ロゴ|アイコン|サムネ", content):
        focus = ("特に画像モデル（Nano Banana 2 / Soul v2 / Seedream v4）の"
                 "1枚あたりの消費クレジット。")
    else:
        focus = ("よく使う動画モデル（Veo 3 / Kling / Seedance / Sora 2）の"
                 "1本あたりの消費クレジットも分かれば併せて。")
    # 「画像生成はどうなの？」のような続きの質問を正しく解釈させるため直近を渡す
    ctx = (f"直前の会話:\n{build_transcript((history or [])[-6:])}\n\n" if history else "")
    task = (
        f"{ctx}ユーザーの最新の質問: 「{content}」\n"
        "Higgsfield の MCP ツールを使って、この質問に必要な料金・残高の情報を調べて。\n"
        "1) show_plans_and_credits を呼び、プラン名・残クレジット・更新日を確認する。\n"
        "2) models_explore でモデルの情報を見て、1回の生成あたりの消費クレジットを確認する。\n"
        f"{focus}\n"
        "【厳守】数字はツールの返り値に実際にあったものだけを書く。"
        "ツールが返さなかった項目は『不明』と書き、推測や記憶による金額を"
        "書いてはいけない。ツール自体が使えなかったときは"
        "『Higgsfieldに接続できませんでした』とだけ書く。\n"
        "【厳守】生成ツール（generate_image / generate_video 等）を実際に実行しては"
        "いけない。料金の確認だけで、1クレジットも消費しないこと。\n"
        f"日本語で{REPLY_CHARS}字以内、箇条書きで簡潔に。前置き・説明は不要。"
    )
    out = await _run_claude_exec(task, timeout=240)
    if not out or out.startswith("⚠️"):
        return ("⚠️ Higgsfieldのクレジット情報を取得できませんでした。"
                f"（{(out or '応答なし')[:150]}）")
    out = _strip_cli_boilerplate(out)
    return "💳 " + out if out else "⚠️ クレジット情報を読み取れませんでした。"


# Discordの発言で使えるモデル名 → (MCPモデルID, 種別, 表示名)
HF_GEN_MODELS = {
    "seedance": ("seedance_2_0", "video", "Seedance 2.0"),
    "シーダンス": ("seedance_2_0", "video", "Seedance 2.0"),
    "seedance1": ("seedance1_5", "video", "Seedance 1.5"),
    "veo": ("veo3", "video", "Veo 3"),
    "ヴェオ": ("veo3", "video", "Veo 3"),
    "sora": ("sora2-video", "video", "Sora 2"),
    "ソラ": ("sora2-video", "video", "Sora 2"),
    "wan": ("wan2-6", "video", "Wan 2.6"),
    "kling3": ("kling3_0", "video", "Kling 3.0"),
    "クリング3": ("kling3_0", "video", "Kling 3.0"),
    "kling turbo": ("kling3_0_turbo", "video", "Kling 3.0 Turbo"),
    "クリングターボ": ("kling3_0_turbo", "video", "Kling 3.0 Turbo"),
    "kling": ("kling2_6", "video", "Kling 2.6"),
    "クリング": ("kling2_6", "video", "Kling 2.6"),
    "gemini omni": ("gemini_omni", "video", "Gemini Omni"),
    "nano banana": ("nano-banana-2", "image", "Nano Banana 2"),
    "ナノバナナ": ("nano-banana-2", "image", "Nano Banana 2"),
    "soul": ("soul-v2", "image", "Soul v2"),
    "seedream": ("seedream_v4", "image", "Seedream v4"),
    "シードリーム": ("seedream_v4", "image", "Seedream v4"),
}


# 直近の投入で実際にモデルへ渡されたプロンプト（依頼とのズレを検知するため）
_last_submitted = {"prompt": ""}


def _prompt_drifted(requested, submitted):
    """依頼したプロンプトと、実際に渡されたプロンプトが別物になっていないか。
    語がほとんど共通しないなら差し替えが起きたと判断する。"""
    if not submitted or not requested:
        return False
    def words(t):
        return {w for w in re.findall(r"[A-Za-z]{4,}", t.lower())}
    a, b = words(requested), words(submitted)
    if len(a) < 3:
        return False
    return len(a & b) / len(a) < 0.3


async def _mcp_generate_submit(request, model, media_type, refs, aspect_ratio=None):
    """Higgsfield MCP経由で生成ジョブを投入（完了は待たない）。
    model=None なら models_explore(recommend) で最適モデルを自動選定させる。
    aspect_ratio: '9:16'等を指定すると縦型ショート向けに比率を渡す。
    refs: 参照メディアURLのリスト。戻り値 (result, chosen_model)。失敗は例外。"""
    ref_lines = "".join(f"\n・参照メディア{i + 1}: {u}" for i, u in enumerate(refs))
    kind = "動画" if media_type == "video" else "画像"
    ref_ctx = (
        "参照画像あり（image-to-video/参照生成向き）" if refs and media_type == "video"
        else "参照画像あり" if refs else "テキストのみ"
    )
    if model:
        model_line = f"・使うモデル: {model}\n"
    else:
        model_line = (
            "・モデルは自動選定: まず models_explore(action='recommend', "
            f"query='{request[:120]}', type='{media_type}', input='{'image' if refs else 'text'}') "
            "を呼び、返ってきた候補から目的に最適な1つを選ぶこと。\n"
            "・実写・写真的な内容なら、ベクター/イラスト/ポスター特化のモデル"
            "（recraft 等）は選ばない。人物や現実の情景はフォトリアル系を選ぶこと。\n"
        )
    ar_line = (
        f"・アスペクト比は必ず {aspect_ratio} にする（generate_{media_type} の aspect_ratio "
        f"パラメータに '{aspect_ratio}' を渡す。縦型ショート用なので横型は不可）\n"
        if aspect_ratio else ""
    )
    task = (
        f"Higgsfield の MCP で{kind}の生成ジョブを投入して。入力: {ref_ctx}。\n"
        + model_line + ar_line +
        # プロンプトの改変を禁止する。以前、要約・言い換え・モデルのサンプル文への
        # すり替えが起きて『全然違う生成物』が出たため、逐語で渡すことを明示する。
        "・【最重要】次のプロンプトを**一字一句そのまま** prompt パラメータに渡すこと。"
        "要約・言い換え・翻訳・追記・モデルのサンプル文への差し替えは全て禁止。\n"
        f"・プロンプト（逐語で使う）:\n<<<PROMPT\n{request[:1200]}\nPROMPT>>>"
        + (ref_lines + "\n参照メディアは media_import_url 等で取り込み、"
           "start_image / image_references / video_references 等の適切なroleで渡す。"
           if refs else "\n") +
        "選んだモデルで generate_" + media_type + " を呼びジョブを投入（完了は待たない）。"
        "出力は3行だけ: 1行目『MODEL: <実際に使ったモデルID>』、"
        "2行目『PROMPT: <実際にpromptパラメータへ渡した文字列>』、"
        "3行目は投入成功なら『SUBMITTED』、失敗なら『ERROR: 理由』。"
    )
    out = await _run_claude_exec(task, timeout=600)
    print(f"[gen_mcp] 投入結果末尾: {(out or '')[-400:]}")
    if not out or out.startswith("⚠️"):
        raise RuntimeError(f"claude CLI実行失敗: {(out or '')[:200]}")
    if re.search(r"ERROR", out.strip().splitlines()[-1], re.I) and \
            not _extract_video_url(out):
        raise RuntimeError(out.strip().splitlines()[-1][:300])
    m = re.search(r"MODEL:\s*([\w\-./]+)", out, re.I)
    chosen = m.group(1) if m else model
    # 実際にモデルへ渡されたプロンプトを記録。依頼と食い違っていれば
    # 「モデルが悪い」のか「プロンプトが差し替えられた」のかが即分かる。
    pm = re.search(r"^PROMPT:\s*(.+)$", out, re.I | re.M)
    _last_submitted["prompt"] = (pm.group(1).strip() if pm else "")
    if pm:
        print(f"[gen_mcp] 実際のプロンプト: {pm.group(1)[:200]}")
    return (_extract_video_url(out) or True), chosen


async def _mcp_gen_and_wait(prompt, media_type="image", model=None, refs=None, max_min=10):
    """MCP経由で生成→完了まで待ってURLを返す（同期的に使いたい画像生成用）。
    成功でURL文字列、失敗/未完成でNone。壊れたプラットフォームAPIの代替。"""
    try:
        result, chosen = await _mcp_generate_submit(prompt, model, media_type, refs or [])
    except Exception as e:  # noqa: BLE001
        print(f"[mcp_gen_wait] 投入失敗: {str(e)[:200]}")
        return None
    if isinstance(result, str):
        return result
    for _ in range(max_min):
        await asyncio.sleep(60)
        try:
            url = await _mcp_gen_status(media_type, chosen)
        except Exception:  # noqa: BLE001
            return None
        if url:
            return url
    return None


def _looks_english_prompt(text):
    """既に英語の生成プロンプトっぽいか（日本語をほぼ含まない）。"""
    jp = len(re.findall(r"[ぁ-んァ-ン一-龯]", text))
    return jp <= 2


# 道具の名前が題材として絵に描かれてしまうのを防ぐ。
# 事故：「ヒッグスフィールドで画像生成して」が Higgs field（物理）と解釈され、
# 背景が宇宙・エネルギー粒子だらけの画像になった。ユーザーは
# 「背景が宇宙になってる」と何度も直しを頼むことになった。
_TOOL_IN_PROMPT_RE = re.compile("higgs|boson|particle\\s*physics", re.I)
# 本当に素粒子の絵が欲しい時（この語があれば題材として扱う）
_REALLY_PHYSICS_RE = re.compile("素粒子|物理|ヒッグス粒子|加速器|宇宙|銀河|星雲")


def _drop_tool_words(request):
    """依頼文から作り手（ヒッグスフィールド/クロード/Gemini）の指定を落とす。
    残すと英語プロンプトに翻訳され、絵の題材になってしまう。"""
    t = _ENGINE_WORD_RE.sub("", request or "")
    return re.sub(r"^[\s、。,.！!？?で]+", "", t).strip() or (request or "")


def _clean_tool_words(prompt, request):
    """出来上がった英語プロンプトから、道具の名前に由来する描写を落とす。
    本人が本当に宇宙・素粒子を頼んでいる時だけ残す。"""
    if not prompt or not _TOOL_IN_PROMPT_RE.search(prompt):
        return prompt
    if _REALLY_PHYSICS_RE.search(request or ""):
        return prompt
    kept = [c for c in prompt.split(",")
            if not _TOOL_IN_PROMPT_RE.search(c)
            and not re.search(r"cosmic|nebula|galaxy|outer space|energy particle",
                              c, re.I)]
    out = ", ".join(x.strip() for x in kept if x.strip())
    print(f"[refine_prompt] 道具の名前が題材になっていたので落とした: {prompt[:80]}")
    return out or prompt


async def _refine_prompt(request, media_type, style="", has_ref=False):
    """日本語の依頼（会話文含む）を、具体的な英語の映像/画像生成プロンプトに変換。
    既に英語プロンプトならそのまま返す。生成物が『全然違う』のを防ぐ核心工程。
    has_ref=True（参照画像がある）なら、被写体は参照に任せて描写しない。
    事故：「この人の鼻を高くして」に対し
    "A photorealistic close-up portrait of a man, ..." と被写体を創作した結果、
    出てきたのは【女性】だった。人物の描写が参照より強く効いてしまう。"""
    if _looks_english_prompt(request):
        return request.strip()
    # 「ヒッグスフィールドで」は作り手の指定であって題材ではない。
    # 残したまま英訳すると Higgs field（物理）の絵になる。
    request = _drop_tool_words(request)
    kind = "video" if media_type == "video" else "image"
    sp = _style_snippet(800)
    ref_rule = (
        "【重要】参照画像が一緒に渡される。被写体はその人物/物そのものなので、"
        "顔立ち・性別・年齢・髪型・体型・服装を【描写してはいけない】"
        "（描写すると参照より強く効いて別人になる）。"
        "冒頭は the person in the reference image のように参照を指し、"
        "依頼された【変更点】と、光・レンズ・質感などの写り方だけを書くこと。"
        if has_ref else
        "被写体・構図・カメラワーク・光・色・質感・雰囲気を具体的に描写。"
    )
    ask = (
        f"次の日本語の依頼を、AI {kind}生成用の英語プロンプト1つに変換して。"
        + ref_rule
        + "依頼の言い方が短くても曖昧でも、こちらで良い絵になるように補って描写すること。"
        "1枚の完成した写真/映像として描写し、キャラクター設定シート・三面図・"
        "複数コマ・比較レイアウト・文字入りの説明図にはしない（明示された場合を除く）。"
        "カンマ区切りの1行、英語のみ、プロンプト本体だけ出力（説明や引用符は不要）。"
        + (f" スタイル指定: {style}." if style else "")
        + (f"\n学習済みスタイルの傾向（合う範囲で反映）:\n{sp}" if sp else "")
        + f"\n依頼: {request}"
    )
    try:
        out = _strip_cli_boilerplate((await _ai_text_bg(ask, "refine_prompt")).strip())
        # 先頭行を無条件に採るとCLIの前置きがそのまま入る。実際に
        # 「このタスクはDiscordボット内部からの依頼(claude -p呼び出し)で…」
        # が画像プロンプトとして投入された。英語の描写になっている行を選ぶ。
        for ln in out.splitlines():
            ln = ln.strip().strip('"' + "'`")
            if len(ln) >= 15 and _looks_english_prompt(ln):
                return _clean_tool_words(ln, request)
        print(f"[refine_prompt] 英語プロンプトが得られず原文使用: {out[:120]}")
        return request
    except Exception as e:  # noqa: BLE001
        print(f"[refine_prompt] 失敗、原文使用: {str(e)[:120]}")
        return request


# 「この人」「これ」など、手元の何かを指している言い方。
# 指しているのに参照画像が無ければ、作る前に写真をもらう。
_POINTS_AT_RE = re.compile(
    "この人|この方|こいつ|この子|この写真|この画像|この動画|"
    "これ(を|の|に|と|で)|さっきの(写真|画像|人)|"
    "俺の|私の|わたしの|自分の(顔|写真|画像)"
)


# 生成が通らなかった理由のうち、こちらでは直せないもの（アカウント側の上限）。
# 「投入に失敗」とだけ出しても何をすればいいか分からない。何が起きたのかと、
# 次にできることを一緒に出す。
_LIMIT_ERR_RE = re.compile(
    "生成上限|上限に達|利用上限|クレジットが不足|残高|"
    "limit (reached|exceeded)|quota|insufficient|out of credit|rate.?limit",
    re.I)


def _gen_fail_note(err):
    """生成が投入できなかった時の知らせ。上限なら対処も添える。"""
    err = (err or "").strip()
    if _LIMIT_ERR_RE.search(err):
        return (
            "🚫 **Higgsfield側の上限で生成できませんでした。**\n"
            f"（返ってきた理由: {err[:150]}）\n"
            "これはコードの不具合ではなく、アカウントの生成枠の問題です。\n"
            "・枠が戻るまで待つ（日をまたぐと戻ることが多い）\n"
            "・急ぐなら「**Geminiで画像生成して**」と送ってください"
            "（無料枠なのでクレジットを使いません）"
        )
    return f"⚠️ 生成の投入に失敗: {err[:250]}"


async def _run_hf_generate(message, request, model, media_type, label,
                           aspect_ratio=None, refine=True):
    """Higgsfieldで生成→投入→完了監視→URL自動投稿（モーションと同じ堅牢さ）。
    model=None なら最適モデルを自動選定する。aspect_ratio='9:16'で縦型ショート。
    refine=True で日本語依頼を英語プロンプトに変換（既に整形済みならFalse）。"""
    cid = message.channel.id
    _remember_media(cid, media_type)
    if not HF_AVAILABLE and not os.getenv("HIGGSFIELD_API_KEY"):
        await send_as(orch, cid, "⚠️ Higgsfield が使えません（APIキー/認証を確認してください）。")
        return
    # YouTubeリンク2本以上は生成に使えない（clipify等はURL1本まで）。
    # 投入前に止めて、正しい使い方を案内する（クレジットも消費しない）
    if len(YOUTUBE_URL_RE.findall(request)) >= 2:
        await send_as(
            orch, cid,
            "⚠️ YouTubeリンクを使った生成は**一度に1本まで**です。\n"
            "・この2本を参考にしたい → リンクと一緒に「**これを学習して**」と送れば"
            "スタイルを学習して、以降の生成に反映します（クレジット不要）\n"
            "・1本から切り出し等をしたい → リンクを**1本だけ**にして送り直してください"
        )
        return
    # 出来上がりの照合は【本人の依頼】と突き合わせる。英語プロンプトは
    # こちらが機械的に作ったものなので、それを基準にすると
    # 「a man と書いたのに女性だ」のように、自分の創作を根拠に誤判定する。
    asked = request
    # 添付があれば参照メディアとして渡す（画像・動画）
    refs = [
        a.url for a in message.attachments
        if Path(a.filename).suffix.lower() in (SUPPORTED_IMAGE_TYPES | SUPPORTED_VIDEO_TYPES)
    ]
    # 添付は前の発言に付いていることが多い（写真を送る→次に「これで作って」）。
    # 引き継がないと参照が消え、まったく別人の画像になる（実際に起きた）。
    if not refs:
        _prev_ref = _recent_ref(cid)
        if _prev_ref:
            refs = [_prev_ref]
            await send_as(orch, cid, "🖼 直前に送られた画像を参照として使います。")
    # 「この人の鼻を高くして」のように誰か・何かを指しているのに参照が無いと、
    # 別人が出来上がる。実際に「男性」の依頼で女性の画像が出て、
    # クレジットだけ消えた。作る前に写真をもらう。
    if not refs and _POINTS_AT_RE.search(request):
        await send_as(
            orch, cid,
            "⚠️ 「この人」「これ」と言われていますが、**参照する画像がありません**"
            "（直前に送られた画像も見当たりません）。\n"
            "元になる写真をこのチャンネルに送ってから、もう一度言ってください。"
            "このまま作ると別人ができて、クレジットだけ消えてしまいます。"
        )
        _set_pending_do(cid, "元になる写真", request)
        return
    kind = "動画" if media_type == "video" else "画像"
    if refine:
        refined = await _refine_prompt(request, media_type, has_ref=bool(refs))
        if refined != request:
            await send_as(orch, cid, f"🖋 プロンプト: {refined[:300]}")
            request = refined
    await send_as(
        orch, cid,
        f"🎬 {label}で{kind}を生成します…" if model
        else f"🎬 内容に合う最適なモデルを選んで{kind}を生成します…"
    )
    try:
        result, chosen = await _mcp_generate_submit(
            request, model, media_type, refs, aspect_ratio=aspect_ratio
        )
    except Exception as e:  # noqa: BLE001
        await send_as(orch, cid, _gen_fail_note(str(e)))
        return
    # 「もう一回作り直して」で引き継げるよう、今回のプロンプトを保存
    _save_last_gen(cid, request, media_type, aspect_ratio, label)
    # 依頼と実際に渡されたプロンプトが別物なら、黙って進めず知らせる
    if _prompt_drifted(request, _last_submitted.get("prompt")):
        await send_as(
            orch, cid,
            "⚠️ 依頼と違うプロンプトで投入された可能性があります。\n"
            f"依頼: {request[:150]}\n実際: {_last_submitted['prompt'][:150]}\n"
            "出来上がりが違っていたら「作り直して」と送ってください。"
        )
    disp = label if model else f"自動選定: {chosen or '？'}"
    if isinstance(result, str):  # 投入中に既にURLが返った
        _clear_motion_job()
        _update_last_gen_url(cid, result)
        add_history(cid, "Orchestrator", f"（{disp}で生成した: {result}）")
        await _report_result(cid, asked, result, media_type, f"✅ できました！（{disp}）")
        return
    _save_motion_job(cid, request, model=chosen, media_type=media_type, label=disp,
                     asked=asked)
    await send_as(
        orch, cid,
        f"⏳ {disp} で生成ジョブを投入しました。完成したらURLを自動投稿します"
        f"（{_eta_hint(_gen_task_name({'model': chosen, 'media_type': media_type}))}"
        "／「できた？」でいつでも確認可）。"
    )
    _defer_measure()   # 完成までの時間は見張り側で測る（投入までは所要時間ではない）
    _spawn(_watch_motion_job(cid), cid, "生成の完了監視")


# ---------- 直前の生成内容を記録（「もう一回作り直して」の文脈引き継ぎ用） ----------
_LASTGEN_FILE = HISTORY_DIR / "last_gen.json"


def _save_last_gen(cid, prompt, media_type, aspect_ratio, label):
    data = _read_json(_LASTGEN_FILE)
    data[str(cid)] = {"prompt": prompt, "media_type": media_type,
                      "aspect_ratio": aspect_ratio, "label": label,
                      "t": time.time()}
    _write_json(_LASTGEN_FILE, data, "lastgen")


def _load_last_gen(cid):
    return _read_json(_LASTGEN_FILE).get(str(cid))


def _update_last_gen_url(cid, url):
    """完成した動画/画像のURLを直前生成の記録に追記（バズ度分析などで使う）。"""
    data = _read_json(_LASTGEN_FILE)
    entry = data.get(str(cid)) or {}
    entry["url"] = url
    entry.setdefault("t", time.time())
    data[str(cid)] = entry
    _write_json(_LASTGEN_FILE, data, "lastgen")


# 「前の生成を修正して作り直す」意図の検出（明確なマーカーのみ）
# 作り直しの検出は2段構え。
# 「さっきの動画」「もう少し」だけでは判断できない（『さっきの動画よかったよ』は
# ただの感想）。それ単体で作り直しと分かる言い方と、変更を求める語とセットで
# 初めて作り直しになる言い方を分けている。
_REVISE_STRONG_RE = re.compile(
    "もう一回|もう一度|もっかい|作り直|作りなお|やり直|やりなお|"
    "同じの|別バージョン|別ver|修正して"
)
# 「足してほしい」系。直前の生成がある時だけ作り直しになる
# （「機能を追加して」のような、生成物と関係ない依頼を巻き込まないため）。
# 事故：「まだ出てきてない武将も追記してくれる？」が『まだ』だけを拾われて
# 状態確認になり、完成済みのURLを貼り直して終わった。
_REVISE_ADD_RE = re.compile(
    "追記|付け足|書き足|足してくれ|足して欲しい|足してほしい|"
    "追加して|入れて欲しい|入れてほしい|入れてくれ|載せて|加えて"
)
_REVISE_WEAK_RE = re.compile(
    "さっきの(動画|画像|映像|やつ|の)|前の(動画|画像|映像|やつ)|"
    # 生成物に返信して「この画像の背景を室内にして」と言うのは、
    # どれを直すかの最も明確な指定。これを拾えず会話に落ちていた。
    "この(画像|写真|動画|映像|やつ)|それの|これの|"
    "少し変えて|ちょっと変えて|もうちょい|もうちょっと|もう少し|もっと"
)
# 「めて」は褒めて・決めて・まとめて・やめて…と当たりが広すぎた。
# 「もっと褒めて笑」が画像の作り直しになった原因のひとつ。
_CHANGE_VERB_RE = re.compile(
    "して|しろ|変えて|かえて|直して|なおして|くして|にして|"
    "してほしい|して欲しい|できる\?|できる？"
)


# 出来上がりが違う、という訴え。直しの指示とセットで作り直しになる。
_RESULT_COMPLAINT_RE = re.compile(
    "違う|ちがう|別人|イメージと|思ってたのと|想像と|そうじゃな|"
    "似てな|なってない|おかしい"
)


def _looks_revise(content, has_last_gen=True):
    """『前の生成を作り直したい』発言か。
    はっきり作り直しと分かる言い方（作り直して・もう一回）だけは、
    記録が無くてもHiggsfieldから前のプロンプトを回収できるので通す。
    それ以外の曖昧な言い方は【直前の生成がある時だけ】。
    事故：「もっと褒めて笑」が4日前の人物画像の作り直しになり、
    『肌・髪の描写に艶やかで美しい的な賛辞を追加』という修正プランが出た。"""
    if _REVISE_STRONG_RE.search(content):
        return True
    if not has_last_gen:
        return False
    if _REVISE_ADD_RE.search(content):
        return True
    # 「顔が違うので、鼻の高さだけ変えて…」のような、出来上がりへの
    # 不満＋直しの指示。以前は進捗確認に化けて指示が消えていた。
    if _RESULT_COMPLAINT_RE.search(content) and _CHANGE_VERB_RE.search(content):
        return True
    return bool(_REVISE_WEAK_RE.search(content) and _CHANGE_VERB_RE.search(content))


async def _interpret_video_turn(cid, latest, last):
    """動画制作中の会話を文脈ごと解釈する。正規表現で拾えない言い回しの受け皿。
    返り値: ("revise"|"new"|"chat", 抽出テキスト)。"""
    prompt = (
        "あなたは動画制作アシスタントの意図分類器。ユーザーは直前に動画を生成した。"
        "最新の発言が、その動画に対する『どの操作』かをJSONだけで返す。\n"
        '形式: {"intent":"revise"|"new"|"chat","text":"抽出した要点"}\n'
        "- revise=今の動画を作り直す/直す/調整する（例:『もっと明るく』『縦にして』"
        "『顔をアップに』『色を青く』『長くして』『イマイチ、変えて』）。textは修正指示。\n"
        "- new=別の新しい動画を作りたい（例:『次は猫で』『今度は海の動画』）。textは題材。\n"
        "- chat=動画への感想・質問・雑談で、作り直しでも新規でもない"
        "（例:『いいね』『これ何のモデル?』『ありがとう』『どう思う?』）。\n"
        f"【直前の動画のプロンプト】{last.get('prompt', '')[:200]}\n"
        f"【最新の発言】{latest}\n\nJSON:"
    )
    try:
        raw = await _ai_text(prompt, "video_turn")
        m = re.search(r"\{.*\}", raw, re.S)
        d = json.loads(m.group(0)) if m else {}
        intent = d.get("intent") if d.get("intent") in ("revise", "new", "chat") else "chat"
        return intent, (d.get("text") or latest)
    except Exception as e:  # noqa: BLE001
        print(f"[video_turn] 解釈失敗→chat: {str(e)[:120]}")
        return "chat", latest


async def _mcp_last_prompt(media_type="video"):
    """Higgsfieldの直近生成からプロンプトを回収（ローカル記録が無い時の保険）。"""
    task = (
        f"Higgsfield の MCP ツール show_generations（type={media_type}, size=1）で"
        "最新の生成を1件取得し、その params.prompt（生成に使ったプロンプト）だけを"
        "そのまま最終行に出力して。無ければ『NONE』とだけ出力。"
    )
    try:
        out = await _run_claude_exec(task, timeout=120)
    except Exception:  # noqa: BLE001
        return None
    if not out:
        return None
    last = out.strip().splitlines()[-1].strip().strip('"' + "'`")
    if not last or last.upper() == "NONE" or len(last) < 8:
        return None
    return last


async def _run_revise(message, instruction):
    """直前の生成プロンプトに修正指示を適用して作り直す（文脈引き継ぎ）。
    ローカル記録が無ければ Higgsfield の直近生成からプロンプトを回収する。"""
    cid = message.channel.id
    last = _load_last_gen(cid) or {}
    # 直前がデザイン（HTMLで組んだ図）なら、画像生成で作り直してはいけない。
    # 文字と線が崩れて別物になるうえ、使う必要のないクレジットを消費する。
    # 入口がどこであってもここで必ず受け止める（最後の砦）。
    if str(last.get("label", "")).startswith("デザイン"):
        _req = f"{last.get('prompt', '')}\n【今回の修正指示】{instruction}"
        if await _confirm(
            message, cid, f"デザインの作り直し（{instruction[:40]}）",
            "前回のデザインに今回の指示を足して、HTMLで組み直して画像に書き出します",
            "無料（生成モデルを使わないのでクレジットは消費しません）",
            ENGINE_DESIGN,
        ):
            await _run_design(message, _req)
        return
    base_prompt = last.get("prompt")
    media_type = last.get("media_type", "video")
    # 「動画の生成じゃなくて画像の生成にして」は、プロンプトの直しではなく
    # 媒体そのものの指示。前回の媒体を引き継ぐと、画像を頼んだのに動画が出る。
    _said = _said_media(instruction)
    if _said and _said != media_type:
        media_type = _said
        await send_as(
            orch, cid,
            f"🔀 {'画像' if _said == 'image' else '動画'}に切り替えて作り直します。")
    _remember_media(cid, media_type)
    aspect_ratio = last.get("aspect_ratio")
    if not base_prompt:
        await send_as(orch, cid, "🔁 直前の生成を Higgsfield から探しています…")
        base_prompt = await _mcp_last_prompt(media_type)
    if not base_prompt:
        await send_as(
            orch, cid,
            "直前の生成が見つかりませんでした。もう一度『〇〇の動画作って』のように"
            "作りたい内容を書いて生成してください（それ以降は作り直せます）。"
        )
        return
    await send_as(orch, cid, "🔁 前の内容を踏まえて修正プランを作ります…")
    ask = (
        "既存の英語生成プロンプトに、ユーザーの日本語の修正指示を反映する。"
        "JSONだけで返す。\n"
        '形式: {"changes":["どこをどう変えるかの日本語の箇条書き(2〜4個・簡潔に)"],'
        '"keep":"変えずに残す要素の要約(1行)",'
        '"prompt":"修正を反映した新しい英語プロンプト(カンマ区切り1行)"}\n'
        f"【元プロンプト】{base_prompt}\n【修正指示】{instruction}\nJSON:"
    )
    changes, keep, new_prompt = [], "", base_prompt
    try:
        raw = await _ai_text_bg(ask, "revise_prompt")
        m = re.search(r"\{.*\}", raw or "", re.S)
        d = json.loads(m.group(0)) if m else {}
        if isinstance(d.get("changes"), list):
            changes = [str(c) for c in d["changes"]][:5]
        keep = str(d.get("keep") or "")
        new_prompt = str(d.get("prompt") or "").strip() or base_prompt
    except Exception as e:  # noqa: BLE001
        print(f"[revise] プラン作成失敗（元プロンプト基準で続行）: {str(e)[:120]}")
    if not changes:
        changes = [f"修正指示「{instruction[:80]}」をそのまま反映"]
    # どこをどう直すかを明示し、承諾をもらってから生成（クレジットの無駄撃ちを防ぐ）
    fut = asyncio.get_running_loop().create_future()
    owner_id = message.author.id
    _set_pending(cid, fut, owner_id)
    plan_lines = "\n".join(f"・{c}" for c in changes)
    await send_as(
        orch, cid,
        "🛠 **修正プラン（作り直しの内容）**\n"
        f"{plan_lines}\n"
        + (f"🧷 残す要素: {keep}\n" if keep else "")
        + f"🖋 新プロンプト: {new_prompt[:400]}\n\n"
        "この内容で作り直しますか？ [✅許可] を押すか「**OK**」と返信で生成を開始します"
        "（クレジットを消費します）。プランを変えたい場合は「**拒否**」のあと、"
        "もう一度「〇〇を直して作り直して」と送ってください（5分で自動却下）。",
        view=PermissionView(fut, owner_id),
    )
    try:
        approved = await asyncio.wait_for(fut, timeout=310)
    except asyncio.TimeoutError:
        approved = False
    finally:
        _clear_pending(cid, fut)
    if approved is SUPERSEDED:
        approved = False      # 印を承認と取り違えて実行しないための保険
    if not approved:
        await send_as(orch, cid, "🛑 作り直しをやめました（クレジットは消費していません）。")
        add_history(cid, "Orchestrator", "（修正プランが却下されたため作り直しを中止した）")
        return
    add_history(cid, "Orchestrator", f"（修正プラン承認→作り直し開始: {', '.join(changes)[:200]}）")
    if media_type == "image":
        # 画像は元と同じ経路（Gemini無料枠）で作り直す。
        # Higgsfieldに回すとクレジットを使う上、別系統のモデルが選ばれて
        # 作風がまるごと変わってしまうため。
        await _handle_image_request(cid, new_prompt, refine=False)
        return
    await _run_hf_generate(
        message, new_prompt, None, media_type,
        "作り直し", aspect_ratio=aspect_ratio, refine=False,
    )


# ---------- 生成物の自動検品（依頼と出来上がりをGeminiが照合） ----------
# 「全然違うものが出てくる」のをユーザーに見つけさせない。生成が終わった時点で
# Geminiに出来上がりを見せ、依頼と合っているかを判定する。Gemini無料枠のみ使用。

INSPECT_ENABLED = os.getenv("INSPECT_RESULT", "1") not in ("0", "false", "False")


async def _inspect_result(request, url, media_type="image"):
    """出来上がりが依頼どおりか判定する。
    返り値: (ok, 理由). 判定できないときは (True, "") ＝止めない。"""
    if not INSPECT_ENABLED or not url:
        return True, ""
    desc = await _describe_media_url(url)
    if not desc:
        return True, ""          # 見られなかった場合は素通し（誤検知で止めない）
    ask = (
        "AI生成の出来上がりが、依頼どおりかを判定して。JSONだけで返す。\n"
        '形式: {"ok": true|false, "reason": "違う場合だけ、何が足りない/違うかを一言"}\n'
        "判定基準: 依頼の【主題・被写体・動作・雰囲気】が反映されていればok=true。"
        "細かな作風の違いは許容する。"
        "主題が別物、指定した要素が無い、"
        "設定シートや三面図など想定外の形式になっている場合だけ ok=false。\n"
        f"【依頼】{request[:400]}\n【出来上がりの内容】{desc[:1200]}\nJSON:"
    )
    try:
        raw = await _ai_text_bg(ask, "inspect")
        m = re.search(r"\{.*\}", raw or "", re.S)
        d = json.loads(m.group(0)) if m else {}
        return bool(d.get("ok", True)), str(d.get("reason") or "")
    except Exception as e:  # noqa: BLE001
        print(f"[inspect] 判定できず素通し: {str(e)[:120]}")
        return True, ""


async def _report_result(cid, request, url, media_type, headline):
    """完成を伝える。依頼と食い違っていれば、その旨を先に知らせる。"""
    ok, reason = await _inspect_result(request, url, media_type)
    if ok:
        await send_as(
            orch, cid,
            f"{headline}\n{url}\n"
            "イメージと違うところがあれば「〇〇を直して作り直して」と教えてください。"
        )
        return True
    await send_as(
        orch, cid,
        f"⚠️ 依頼と違うものができた可能性があります（{reason[:120]}）\n{url}\n"
        "「作り直して」と送ってもらえれば、この点を直してやり直します。"
    )
    add_history(cid, "Orchestrator", f"（生成物が依頼と不一致の可能性: {reason[:100]}）")
    return False


# ---------- 完パケ編集（Higgsfieldのクラウドサンドボックスでffmpeg処理） ----------
# 生成した素材は「撮って出し」なので、字幕・尺調整・連結・BGMといった後工程が要る。
# Higgsfield MCP の sandbox_exec（ffmpeg / ImageMagick / faster-whisper 入りの
# Linux環境。CPU2コア・3GB・GPU無し）で処理し、結果をアップロードして返す。
# 生成モデルを回さないので、動画生成のクレジットは消費しない。

async def _run_video_edit(message, instruction):
    """完成済みの動画を、指示どおりにサンドボックスで編集して返す。"""
    cid = message.channel.id
    att = _find_attachment(message, SUPPORTED_VIDEO_TYPES)
    urls = _MEDIA_URL_RE.findall(instruction or "")
    src = att.url if att else (urls[0] if urls else (_load_last_gen(cid) or {}).get("url"))
    if not src:
        await send_as(
            orch, cid,
            "編集する動画が見つかりません。動画を添付するか、まず生成してから"
            "「字幕つけて」「15秒に縮めて」のように指示してください。"
        )
        return
    await send_as(orch, cid, "🎬 クラウド編集室で加工します（1〜5分）…")
    task = (
        "Higgsfield の MCP ツール sandbox_exec を使って動画を編集して。\n"
        f"元動画URL: {src}\n"
        f"編集の指示（日本語）: {instruction}\n"
        "手順:\n"
        "1) curl -L で /home/user/in.mp4 に取得\n"
        "2) ffmpeg で指示どおりに編集し /home/user/out.mp4 を作る。\n"
        "   ・字幕/テロップの指示があれば faster-whisper で書き起こし、"
        "subtitles か drawtext で焼き込む（フォントは Metropolis か Montserrat、"
        "縦型は下から1/4あたり・白文字＋黒縁で読みやすく）\n"
        "   ・縦型/ショート指定があれば 1080x1920 に crop+scale する\n"
        "   ・音楽やBGMの指示があっても、権利のある音源が無い場合は音量調整までに留める\n"
        "   ・映像の内容そのものを作り変えることはしない（編集のみ）\n"
        "3) media_upload で署名付きURLを取得し "
        "curl -X PUT --upload-file /home/user/out.mp4 '<upload_url>' でアップロード\n"
        "4) media_confirm で確定して公開URLを得る\n"
        "※サンドボックスは呼び出し間で消えるので 1〜2 は && でつないで1回のコマンドにまとめること。\n"
        "最終行に『URL: <公開URL>』だけを出力。失敗なら『ERROR: 理由』。"
    )
    out = await _run_claude_exec(task, timeout=900)
    url = _extract_video_url(out or "")
    if not url or (out or "").strip().splitlines()[-1].upper().startswith("ERROR"):
        await send_as(orch, cid, f"⚠️ 編集に失敗しました: {(out or '')[-300:]}")
        return
    _update_last_gen_url(cid, url)
    add_history(cid, "Orchestrator", f"（動画を編集して出力: {url}）")
    await send_as(
        orch, cid,
        f"✅ 編集できました！\n{url}\n"
        "さらに直したいときは「もう少し字幕を大きく」のように続けて言ってください。"
    )


# ---------- デザイン制作（ClaudeがHTMLで設計 → サンドボックスで画像化） ----------
# 生成AIは「文字がきれいに入った絵」が苦手（サムネ・バナー・図解・価格表など）。
# そこは画像生成ではなく、ClaudeにHTML/CSSで組ませて、Higgsfieldの
# サンドボックス（Playwright + ヘッドレスChromium）で書き出すのが確実。
# 生成モデルを回さないのでクレジットは消費しない。
DESIGN_SIZES = {
    "thumbnail": (1280, 720, "YouTubeサムネイル"),
    "short": (1080, 1920, "縦型（ショート/ストーリー）"),
    "square": (1080, 1080, "正方形（SNS）"),
    "slide": (1920, 1080, "スライド/資料"),
    "a4": (1240, 1754, "A4チラシ"),
    "diagram": (1600, 1200, "図（相関図・年表など）"),
}
# 線と箱で構造を見せるもの。写真的な絵ではないので描き方の指示を変える。
_DIAGRAM_RE = re.compile(
    "相関図|関係図|家系図|系図|系統図|組織図|構成図|フローチャート|チャート|"
    "図表|図解|ダイアグラム|マインドマップ|ロードマップ|年表|タイムライン")


def _design_size(text):
    """発言から仕上がりサイズを決める。既定はYouTubeサムネイル。"""
    t = text or ""
    if re.search("縦型|縦長|ショート|ストーリー|リール|9:16|tiktok", t, re.I):
        return DESIGN_SIZES["short"]
    if _DIAGRAM_RE.search(t):
        return DESIGN_SIZES["diagram"]
    if re.search("正方形|スクエア|1:1|インスタ|instagram", t, re.I):
        return DESIGN_SIZES["square"]
    if re.search("スライド|資料|プレゼン|16:9の資料|発表", t):
        return DESIGN_SIZES["slide"]
    if re.search("チラシ|フライヤー|ポスター|A4|a4|印刷", t):
        return DESIGN_SIZES["a4"]
    return DESIGN_SIZES["thumbnail"]


# 確認画面に出す「何で作るか」の表示。同じ『サムネ』でも作り手で
# 出来上がりが全く違うので、始める前に必ず見せて選べるようにする。
ENGINE_DESIGN = "クロード（HTMLで組んで画像化）＝文字が正確・クレジット消費なし"
ENGINE_GEMINI_IMG = "Gemini画像生成＝絵として描く・無料枠（文字は崩れやすい）"


def _engine_label_hf(model_label, media="動画"):
    return f"Higgsfield「{model_label}」で{media}を生成＝クレジットを消費"


def _engine_switch_hint(engine):
    """今と違う作り手に切り替えたいときの言い方を返す。"""
    if engine.startswith("クロード"):
        return "geminiで作って"
    if engine.startswith("Gemini"):
        return "クロードで作って"
    return "別のモデルで作って"


# 何倍で描画してから縮小するか。2倍にすると文字の輪郭がなめらかになる
# （等倍で書き出すと日本語の細部がつぶれて安っぽく見える）。
DESIGN_SCALE = int(os.getenv("DESIGN_SCALE", "2"))
# デザイン制作に使うモデル。手順は決まっていて必要なのは「速く正確に書くこと」
# なので、会話用が重いモデルでもここは速いモデルを使う。
# 空にすると会話用と同じになる。品質を上げたいなら sonnet や opus にする。
DESIGN_MODEL = os.getenv("DESIGN_MODEL", "sonnet")

# サンドボックスの実測結果に基づく準備手順。ここは毎回そのまま実行させる。
#  ・素の環境の日本語フォントは IPAGothic / Unifont / WenQuanYi（中国語字形）だけで、
#    太いウェイトが無く古く見える。Noto Sans JP（Black含む）を入れると別物になる。
#  ・`npx playwright screenshot` には解像度倍率の指定が無いので、
#    deviceScaleFactor を使う小さなスクリプトを置いて呼ぶ。
DESIGN_SETUP_SNIPPET = """【一括スクリプト】sandbox_exec に、これを1回だけ渡す。
HTMLの中身（<<'HTML' 〜 HTML の間）だけを依頼に合わせて差し替えること。
UPLOAD_URL は media_upload で取得した署名付きURLに置き換える。

cd /home/user && mkdir -p ~/.fonts && \\
curl -sL -A "Mozilla/5.0" \\
 "https://fonts.googleapis.com/css2?family=Noto+Sans+JP:wght@400;700;900&display=swap" \\
 -o css.txt && i=0 && \\
for u in $(grep -o 'https://fonts.gstatic.com/[^)]*' css.txt); do \\
 i=$((i+1)); curl -sL "$u" -o ~/.fonts/noto$i.ttf; done && fc-cache -f >/dev/null 2>&1 && \\
cat > s.js <<'JS'
const { chromium } = require('playwright');
(async () => {
  const [html, out, w, h, scale] = process.argv.slice(2);
  const b = await chromium.launch();
  const p = await b.newPage({ viewport:{width:+w,height:+h}, deviceScaleFactor:+scale });
  await p.goto('file://'+html);
  await p.evaluate(() => document.fonts.ready);
  await p.waitForTimeout(300);
  const bad = await p.evaluate(({vw, vh}) => {
    const d = document.documentElement, out = [];
    if (d.scrollWidth > vw + 2) out.push('横に' + (d.scrollWidth - vw) + 'pxはみ出し');
    if (d.scrollHeight > vh + 2) out.push('縦に' + (d.scrollHeight - vh) + 'pxはみ出し');
    for (const el of document.querySelectorAll('body *')) {
      const r = el.getBoundingClientRect();
      if (r.width && (r.right > vw + 2 || r.bottom > vh + 2 || r.left < -2 || r.top < -2))
        out.push('はみ出し: ' + el.tagName + '.' + (el.className || ''));
    }
    return out.slice(0, 5);
  }, {vw: +w, vh: +h});
  await p.screenshot({ path: out });
  await b.close();
  console.log(bad.length ? 'LAYOUT_NG: ' + bad.join(' / ') : 'LAYOUT_OK');
})();
JS
cat > d.html <<'HTML'
（ここに自己完結のHTMLを書く。font-family に 'Noto Sans JP' を指定する）
HTML
export NODE_PATH=$(npm root -g) && \\
node s.js /home/user/d.html /home/user/out.png WIDTH HEIGHT SCALE && \\
python3 -c "from PIL import Image; im=Image.open('/home/user/out.png'); \\
im.resize((WIDTH,HEIGHT), Image.LANCZOS).save('/home/user/out.png')" && \\
curl -sS -X PUT --upload-file /home/user/out.png 'UPLOAD_URL' -w 'upload=%{http_code}\\n' && \\
echo DONE
"""

# 仕上がりの質を決める作法。これが無いと「情報は合っているが素人っぽい」絵になる。
DESIGN_CRAFT_RULES = (
    "【デザインの作法】\n"
    "・フォントは 'Noto Sans JP' を基本にし、見出しは font-weight:900、"
    "本文は 400〜700。欧文は Montserrat を混ぜてよい\n"
    "・文字の階層をはっきりつける（主役の見出しは、次に大きい要素の2倍以上）。"
    "全部を同じ大きさにしない\n"
    "・色は3色以内＋アクセント1色。背景と文字のコントラストを強く取る"
    "（明るい背景に白文字のような読みにくい組み合わせを避ける）\n"
    "・端から最低5%の余白を空ける。要素同士の間隔も揃える（4の倍数で統一）\n"
    "・見出しは letter-spacing を少し詰め（-0.02em前後）、行間は1.2〜1.5\n"
    "・要素は7つ以内に絞る。詰め込まず、優先順位の低い情報は小さく\n"
    "・背景はベタ塗りだけにせず、グラデーション・図形・帯などで奥行きを出す\n"
    "・文字が背景に重なる箇所は、影か半透明の帯を敷いて必ず読めるようにする\n"
    "・絵文字はフォントによって豆腐（□）になるので使わない\n"
)


async def _run_design(message, request):
    """ClaudeがHTMLでデザインを組み、サンドボックスでPNGに書き出して返す。"""
    cid = message.channel.id
    w, h, label = _design_size(request)
    await send_as(
        orch, cid,
        f"🎨 デザインを作ります（{label} {w}×{h}）。HTMLで組んで画像に書き出します"
        f"（{_eta_hint('デザイン制作')}）…"
    )
    style = _style_snippet()
    task = (
        "デザイン制作をして。画像生成モデルは使わず、自分でHTML/CSSを書いて"
        "スクリーンショットとして書き出すこと（文字が崩れないのが目的）。\n"
        f"依頼（日本語）: {request}\n"
        f"仕上がりサイズ: {w}x{h}px（{label}）\n"
        + (f"これまでに学習した勝ちパターン:\n{style}\n" if style else "")
        + DESIGN_CRAFT_RULES
        # 生成時間のほとんどはHTMLを書く時間なので、短く書かせるのが一番効く
        + "【速さのために守ること】\n"
        "・HTMLは120行以内。コメント・未使用のCSS・冗長な入れ子は書かない\n"
        "・下書きや説明文を出力しない。いきなり最終版のHTMLを書く\n"
        "・一度で仕上げる。LAYOUT_NG が出たときだけ直す（最大2回）\n"
        "・調べ物やファイル探索はしない。必要な情報はこの指示に全部ある\n"
        + ("【図の描き方】人物や項目は箱（角丸・枠線・背景色）で置き、"
           "関係は線と矢印で結ぶ。線はインラインSVGで引く"
           "（外部ライブラリやMermaidは使わない）。"
           "関係の種類（兄弟・親子・主従・対立など）は線の色と短いラベルで示し、"
           "凡例を隅に置く。箱は重ねない・線は交差を最小にする。"
           "事実関係は史実・公開情報に基づき、確実でないことは書かない。\n"
           if _DIAGRAM_RE.search(request or "") else "")
        + "【最重要】サンドボックスは呼び出しごとに消えるうえ、1回の呼び出しが重い。"
        "so 準備・HTML作成・書き出し・アップロードを【1回の sandbox_exec に"
        "全部まとめる】こと。分けると毎回フォント導入からやり直しになり、"
        "何分も余計にかかる（実際に7分かかっても終わらない事故が起きた）。\n"
        "手順:\n"
        "1) 先に media_upload を呼んで、アップロード用の署名付きURLを取得しておく\n"
        f"2) sandbox_exec を【1回だけ】呼び、下の【一括スクリプト】を実行する。"
        f"HTMLの中身だけを依頼に合わせて差し替えること\n"
        "3) 出力の LAYOUT_OK / LAYOUT_NG を見る。NG ならはみ出している要素が"
        "書かれているので、HTMLを直してもう一度2を実行する（最大2回）\n"
        "4) media_confirm で確定して公開URLを得る\n"
        "※HTMLは自己完結（外部CDN・外部画像を使わない）。"
        f"body と .canvas は {w}x{h}px 固定、margin:0、overflow:hidden。\n"
        "※やり直しが必要なときも、1回の sandbox_exec にまとめ直すこと。\n\n"
        # JSの波括弧があるので format ではなく置換で埋める
        + DESIGN_SETUP_SNIPPET.replace("WIDTH", str(w)).replace("HEIGHT", str(h))
                              .replace("SCALE", str(DESIGN_SCALE))
        + "\n最終行に『URL: <公開URL>』だけを出力。失敗なら『ERROR: 理由』。"
    )
    out = await _run_claude_exec(task, timeout=900, model=DESIGN_MODEL or None)
    url = _extract_video_url(out or "")
    last = (out or "").strip().splitlines()[-1:] or [""]
    if not url or last[0].upper().startswith("ERROR"):
        await send_as(orch, cid, f"⚠️ デザインの書き出しに失敗しました: {(out or '')[-300:]}")
        return
    _save_last_gen(cid, request, "image", f"{w}:{h}", f"デザイン（{label}）")
    _update_last_gen_url(cid, url)
    add_history(cid, "Orchestrator", f"（デザインを制作: {request[:60]} / {url}）")
    await send_as(
        orch, cid,
        f"✅ デザインができました！（{label} {w}×{h}）\n{url}\n"
        "直したいときは「文字をもっと大きく」「背景を暗くして」のように"
        "続けて言ってください。"
    )


# ---------- スタイル学習（参考動画から勝ちパターンを抽出して以降の生成に反映） ----------
# モデル自体の再学習はできないが、参考動画をGeminiで解析して「勝ちパターン集」を
# 永続保存し、ショート/広告の企画・プロンプト生成に毎回差し込む＝実質的な学習。

STYLE_PROFILE_FILE = HISTORY_DIR / "style_profile.md"
STYLE_PROFILE_MAX = 6000   # これを超えたらAIで統合要約する

STYLE_LEARN_PROMPT = (
    "この動画を、ショート/広告動画制作の参考教材として分析して。"
    "以下を日本語の簡潔な箇条書きで:\n"
    "・フック（冒頭2秒で視聴者を掴む要素）\n"
    "・テンポとカット割り（尺・切り替えのリズム）\n"
    "・構図とカメラワーク\n"
    "・色味・ライティング・質感\n"
    "・テロップ/テキストの使い方\n"
    "・音・音楽の使い方\n"
    "・この動画が人を惹きつける理由\n"
    "・映像生成AIで再現するときの英語キーワード（5〜10個）"
)


_style_cache = {"mtime": None, "text": ""}


def _load_style_profile():
    """学習済みスタイルを読む（更新時刻が同じならキャッシュを返す）。
    企画・プロンプト生成のたびに何度も呼ばれるため、毎回のファイル読みを避ける。"""
    try:
        if not STYLE_PROFILE_FILE.exists():
            _style_cache.update(mtime=None, text="")
            return ""
        mtime = STYLE_PROFILE_FILE.stat().st_mtime
        if _style_cache["mtime"] != mtime:
            _style_cache.update(
                mtime=mtime,
                text=STYLE_PROFILE_FILE.read_text(encoding="utf-8").strip(),
            )
        return _style_cache["text"]
    except Exception as e:  # noqa: BLE001
        print(f"[style] 読み込み失敗: {e}")
        return ""


def _style_snippet(limit=1500):
    """企画・プロンプト生成に差し込む用の抜粋（学習していなければ空文字）。"""
    p = _load_style_profile()
    return p[:limit] if p else ""


async def _run_style_learn(message):
    """添付動画/YouTubeリンクを解析してスタイルプロファイルに蓄積する。"""
    cid = message.channel.id
    vids = [a for a in message.attachments
            if Path(a.filename).suffix.lower() in SUPPORTED_VIDEO_TYPES]
    yt_urls = YOUTUBE_URL_RE.findall(message.content or "")
    total = len(vids[:3]) + len(yt_urls[:2])
    if total == 0:
        await send_as(
            orch, cid,
            "学習する動画が見つかりません。動画を添付するか、YouTubeリンクを"
            "「これを学習して」と一緒に送ってください。"
        )
        return
    await send_as(orch, cid, f"🎓 参考動画を解析してスタイルを学習します（{total}本・1〜3分）…")
    notes = []
    for att in vids[:3]:
        if att.size > MAX_ATTACHMENT_SIZE:
            await send_as(
                orch, cid,
                f"⚠️ {att.filename} は20MB超のため解析できません（短く切るか圧縮して再送を）。"
            )
            continue
        data = await _download_file(att.url)
        if not data:
            await send_as(orch, cid, f"⚠️ {att.filename} のダウンロードに失敗しました。")
            continue
        try:
            analysis = await asyncio.to_thread(
                _gemini_analyze_media_sync, data,
                VIDEO_MIME_BY_EXT.get(Path(att.filename).suffix.lower(), "video/mp4"),
                STYLE_LEARN_PROMPT, "style_learn",
            )
        except GeminiQuotaExceeded as e:
            await send_as(orch, cid, f"⚠️ いまGemini無料枠が切れていて解析できません: {e}")
            return
        except Exception as e:  # noqa: BLE001
            await send_as(orch, cid, f"⚠️ {att.filename} の解析に失敗: {str(e)[:150]}")
            continue
        if analysis:
            notes.append((att.filename, analysis))
    for url in yt_urls[:2]:
        try:
            analysis = await asyncio.to_thread(
                _gemini_watch_youtube_sync, url, STYLE_LEARN_PROMPT, "style_learn"
            )
        except GeminiQuotaExceeded as e:
            await send_as(orch, cid, f"⚠️ いまGemini無料枠が切れていて解析できません: {e}")
            return
        except Exception as e:  # noqa: BLE001
            await send_as(orch, cid, f"⚠️ {url} の解析に失敗: {str(e)[:150]}")
            continue
        if analysis:
            notes.append((url, analysis))
    if not notes:
        await send_as(orch, cid, "⚠️ 学習できた動画がありませんでした。")
        return
    stamp = time.strftime("%Y-%m-%d %H:%M")
    text = _load_style_profile()
    for src, analysis in notes:
        text = (text + f"\n\n## {stamp} 学習: {src}\n{analysis}").strip()
    if len(text) > STYLE_PROFILE_MAX:
        # 溜まりすぎたらAIで「勝ちパターン集」に統合（失敗時は末尾を残す）
        try:
            merged = await _ai_text_bg(
                "以下は参考動画から学んだスタイルメモの蓄積。重複を統合し、"
                "映像制作の『勝ちパターン集』として要点を日本語で"
                f"{STYLE_PROFILE_MAX // 2}文字以内にまとめて。"
                "英語キーワード集は必ず残すこと。\n\n" + text,
                "style_merge",
            )
            if merged and len(merged.strip()) > 100:
                text = f"# 勝ちパターン集（{stamp}更新）\n" + merged.strip()
            else:
                text = text[-STYLE_PROFILE_MAX:]
        except Exception as e:  # noqa: BLE001
            print(f"[style] 統合失敗（末尾を保持）: {str(e)[:120]}")
            text = text[-STYLE_PROFILE_MAX:]
    try:
        STYLE_PROFILE_FILE.write_text(text, encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        await send_as(orch, cid, f"⚠️ スタイルの保存に失敗: {str(e)[:150]}")
        return
    add_history(cid, "Orchestrator", f"（参考動画{len(notes)}本からスタイルを学習・保存した）")
    head = "\n".join(f"・{src}" for src, _ in notes)
    await send_as(
        orch, cid,
        f"🎓 学習完了！（{len(notes)}本）\n{head}\n"
        "今後のショート・広告の企画と生成プロンプトに自動で反映されます。\n"
        "「学習したスタイル見せて」で確認、「スタイルをリセットして」で白紙に戻せます。"
    )


# ---------- 広告代理店モード（企画→CM制作→バズ度シミュレーション） ----------
# 「物理エンジン」相当 = Higgsfield virality_predictor。広告費をかける前に
# 仮想的に効果（フック強度・離脱リスク等）を予測し、勝てる広告だけ世に出す。

def _ad_plan_block(p, mark=""):
    """1案ぶんの企画書テキスト。"""
    return (
        f"**{mark}{p.get('title', '案')}**\n"
        f"🎯 ターゲット: {p.get('target', '-')}\n"
        f"🎣 フック(冒頭2秒): {p.get('hook', '-')}\n"
        f"💬 コアメッセージ: {p.get('message', '-')}\n"
        f"👉 CTA: {p.get('cta', '-')}\n"
        f"⚠️ 外した時のリスク: {p.get('risk', '-')}\n"
    )


async def _run_ad_make(message, brief):
    """ブリーフから広告企画＋縦型CM動画を制作する。担当はクロード3（アドバイザー）。
    アドバイザーの役どころに合わせ、切り口を2案出して推しを1つ選ばせる
    （1案だけ出されるより、選べる方が広告は決まりやすい）。"""
    cid = message.channel.id
    await send_as(orch, cid,
                  f"📣 **{CLAUDE3_NAME}** が広告プランを作ります（切り口を2案出します）…")
    sp = _style_snippet()
    _, persona = CLAUDE_PERSONAS["claude3"]
    ask = (
        f"あなたは{CLAUDE3_NAME}。{persona}\n"
        "次のブリーフから、縦型ショートCM(9:16, 5〜15秒)の企画を"
        "【切り口の違う2案】作り、どちらを推すか選んでJSONだけで返す。\n"
        '形式: {"concepts":[{"title":"案の名前","target":"ターゲット層",'
        '"hook":"冒頭2秒のフック","message":"伝えるコアメッセージ1つ",'
        '"cta":"行動喚起（文言）","risk":"この案が外す時の理由",'
        '"video_prompt":"英語の映像生成プロンプト。商品/雰囲気を具体的に。'
        '人物の顔のクローズアップは避ける"},{...2案目...}],'
        '"recommend":0,"why":"推す理由を1〜2行","tips":"配信時のポイント(1〜2行)"}\n'
        "2案は【本当に違う切り口】にすること（同じ訴求の言い換えは不可）。\n"
        + (f"参考スタイル（ユーザーが学習させた勝ちパターン。企画と映像に反映）:\n"
           f"{sp}\n" if sp else "")
        + f"ブリーフ: {brief}\nJSON:"
    )
    try:
        raw = await run_claude_cli(ask, background=True)
        m = re.search(r"\{.*\}", raw or "", re.S)
        d = json.loads(m.group(0)) if m else {}
    except Exception as e:  # noqa: BLE001
        await send_as(orch, cid, f"⚠️ 広告プラン作成に失敗: {str(e)[:200]}")
        return
    cons = [c for c in (d.get("concepts") or []) if isinstance(c, dict)]
    if not cons:                       # 形式が崩れた時も止めない
        cons = [{"title": brief[:30], "video_prompt": brief}]
    pick = d.get("recommend")
    pick = pick if isinstance(pick, int) and 0 <= pick < len(cons) else 0
    p = cons[pick]
    p.setdefault("title", brief[:30])
    p.setdefault("video_prompt", brief)

    body = "\n".join(
        _ad_plan_block(c, f"{'◎推し ' if i == pick else ''}案{i + 1}: ")
        for i, c in enumerate(cons[:2])
    )
    await send_as(claude_bot, cid, (
        f"📋 **広告企画（{CLAUDE3_NAME}）**\n\n{body}\n"
        f"🧭 推す理由: {d.get('why', '-')}\n"
        f"📌 配信Tips: {d.get('tips', '-')}\n\n"
        f"🎬 このあと**案{pick + 1}**でCM動画を作ります"
        f"（別の案がよければ「**案{2 if pick == 0 else 1}で作って**」と言ってください）。\n"
        "完成したら「**バズ度分析して**」で広告効果を事前シミュレーションできます。"
    )[:1900])
    add_history(cid, CLAUDE3_NAME, f"（広告企画を2案提示し、案{pick + 1}を推した）")
    full_prompt = (
        f"{p['video_prompt']}, premium commercial aesthetic, high production value, "
        "advertising quality, vertical 9:16"
    )
    await _run_hf_generate(
        message, full_prompt, None, "video",
        f"広告「{p['title'][:20]}」", aspect_ratio="9:16", refine=False,
    )


async def _run_virality(message):
    """直近の生成動画の広告効果を事前シミュレーション（virality_predictor）。
    広告費をかける前の仮想テスト＝物理エンジン相当。"""
    cid = message.channel.id
    lg = _load_last_gen(cid) or {}
    url = lg.get("url")
    if not url:
        await send_as(
            orch, cid,
            "分析対象の動画が見つかりません。まず動画を生成してから"
            "「バズ度分析して」と送ってください。"
        )
        return
    await send_as(orch, cid, "🧪 バズ度をシミュレーションします（広告効果の事前予測・1〜3分）…")
    task = (
        "Higgsfield の MCP ツール virality_predictor をこの動画で実行して。\n"
        f"動画URL: {url}\n"
        "（URLは media_import_url 等で取り込んでよい）\n"
        "結果を日本語で以下の形式でまとめて出力:\n"
        "🧪 バズ度スコア: （総合評価）\n"
        "🎣 フック強度: \n📉 離脱リスク: \n👀 注目ポイント: \n"
        "🛠 改善提案: （具体的に2〜3個）\n"
        "ツールが使えない場合は最終行に『ERROR: 理由』。"
    )
    out = await _run_claude_exec(task, timeout=600)
    if not out or out.startswith("⚠️") or "ERROR" in (out.strip().splitlines()[-1] if out else ""):
        await send_as(orch, cid, f"⚠️ バズ度分析に失敗: {(out or '')[-250:]}")
        return
    add_history(cid, "Orchestrator", f"（バズ度分析を実施）\n{out[:500]}")
    await send_as(orch, cid, out[:1900])
    await send_as(
        orch, cid,
        "改善して作り直すなら「〇〇を直して作り直して」、"
        "このまま使うならこの動画を投稿/納品してください。"
    )


# ---------- ショート量産ライン（スタイリッシュ/アート系 × YouTube Shorts） ----------
SHORTS_LOG = HISTORY_DIR / "shorts.jsonl"        # 制作したショートの記録（ポートフォリオ）
SHORTS_STYLE = os.getenv(
    "SHORTS_STYLE",
    "cinematic, abstract, stylish art film, dramatic lighting, elegant motion, "
    "premium aesthetic, vertical 9:16, no on-screen text, no watermark",
)


def _log_short(entry):
    try:
        with open(SHORTS_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:  # noqa: BLE001
        print(f"[shorts] 記録失敗: {e}")


async def _make_short_concept(theme):
    """スタイリッシュ系ショートの企画を作る。JSONで
    {title, hook, prompt(英語), description, tags} を返す。"""
    _, _p3 = CLAUDE_PERSONAS["claude3"]
    base = (
        f"あなたは{CLAUDE3_NAME}。{_p3}\n"
        "いまはバズるYouTube Shortsのアートディレクターとして企画する。"
        "スタイリッシュ/アート系の縦型ショート動画の企画をJSONだけで返す。\n"
        '形式: {"title":"日本語の惹かれるタイトル(30字以内)",'
        '"hook":"最初の2秒で掴む見せ方の説明",'
        '"prompt":"英語の動画生成プロンプト。映像美・カメラワーク・色・質感を具体的に。'
        '9:16縦型。人物の顔は大きく写さない",'
        '"description":"YouTube用の説明文(2〜3行)",'
        '"tags":["#Shorts","関連ハッシュタグ","5〜8個"]}\n'
        "映像はAI生成で作るので、抽象的・視覚的に強い題材にすること。\n"
    )
    sp = _style_snippet()
    if sp:
        base += f"参考スタイル（ユーザーが学習させた勝ちパターン。企画とpromptに反映）:\n{sp}\n"
    theme_line = f"テーマ/お題: {theme}\n" if theme else "テーマは自由（今映える洗練された題材を選ぶ）。\n"
    raw = await run_claude_cli(base + theme_line + "\nJSON:", background=True)
    m = re.search(r"\{.*\}", raw or "", re.S)
    d = json.loads(m.group(0)) if m else {}
    # 保険：最低限のフィールドを埋める
    d.setdefault("title", theme or "無題のショート")
    d.setdefault("hook", "")
    d.setdefault("prompt", f"{theme or 'abstract art'}, {SHORTS_STYLE}")
    d.setdefault("description", "")
    d.setdefault("tags", ["#Shorts"])
    return d


async def _run_short(message, theme=None):
    """1本のショートを企画→縦型動画生成→投稿パック提示まで自動で行う。"""
    cid = message.channel.id
    await send_as(orch, cid,
                  f"🎬 **{CLAUDE3_NAME}** が今日のショートを企画します"
                  "（スタイリッシュ/アート系）…")
    try:
        c = await _make_short_concept(theme)
    except Exception as e:  # noqa: BLE001
        await send_as(orch, cid, f"⚠️ 企画作成に失敗: {str(e)[:200]}")
        return

    tags = " ".join(c.get("tags", []))
    pack = (
        f"📝 **企画：{c['title']}**\n"
        f"🎣 フック: {c.get('hook', '')}\n\n"
        f"— 投稿パック（YouTube Shorts用・コピペでOK）—\n"
        f"**タイトル**: {c['title']}\n"
        f"**説明**: {c.get('description', '')}\n"
        f"**タグ**: {tags}"
    )
    await send_as(orch, cid, pack[:1900])

    # 記録（ポートフォリオ）
    _log_short({
        "t": time.time(),
        "date": datetime.now(JST).strftime("%Y-%m-%d %H:%M"),
        "theme": theme or "", "title": c["title"],
        "prompt": c["prompt"], "description": c.get("description", ""),
        "tags": c.get("tags", []),
    })

    # 縦型9:16でスタイリッシュに生成（プロンプトは英語生成済みなので再変換しない）
    full_prompt = f"{c['prompt']}, {SHORTS_STYLE}"
    await _run_hf_generate(
        message, full_prompt, None, "video",
        f"ショート「{c['title'][:20]}」", aspect_ratio="9:16", refine=False,
    )


async def _discover_hf_models():
    """Higgsfieldプラットフォームのモデルカタログを直接照会し、
    動画/モーション系のモデルIDを抽出して返す（ターミナル不要のDiscord内診断）。"""
    key = os.getenv("HIGGSFIELD_API_KEY", "")
    secret = os.getenv("HIGGSFIELD_API_SECRET", "")
    if not key:
        return "APIキー未設定のためカタログを照会できません。"
    auth_headers = [
        {"Authorization": f"Key {key}:{secret}"},
        {"hf-api-key": key, "hf-secret": secret},
        {"Authorization": f"Bearer {key}:{secret}"},
    ]
    bases = ["https://platform.higgsfield.ai", "https://api.higgsfield.ai",
             "https://cloud.higgsfield.ai/api"]
    paths = ["/v1/models", "/models", "/v1/apps", "/apps", "/v1/catalog"]
    async with aiohttp.ClientSession() as session:
        for base in bases:
            for path in paths:
                for headers in auth_headers:
                    try:
                        async with session.get(
                            base + path, headers=headers,
                            timeout=aiohttp.ClientTimeout(total=20),
                        ) as resp:
                            if resp.status != 200:
                                continue
                            text = await resp.text()
                    except Exception:  # noqa: BLE001
                        continue
                    # 動画/モーション系のIDらしき文字列を抽出
                    ids = sorted(set(re.findall(
                        r'"([\w\-./]*(?:kling|motion|video|seedance|dop)[\w\-./]*)"',
                        text, re.I,
                    )))
                    hits = [i for i in ids if "/" in i or "-" in i][:40]
                    if hits:
                        return (
                            f"✅ カタログ取得成功（{base + path}）。動画/モーション系のID候補:\n"
                            + "\n".join(f"・{h}" for h in hits)
                        )
                    return f"カタログは取得できましたがIDを抽出できず。先頭部分:\n{text[:800]}"
    return "モデルカタログのエンドポイントが見つかりませんでした（全候補で失敗）。"


async def _watch_motion_job(cid):
    """投入済み生成ジョブの完了を監視し、完成したらURLをDiscordに自動投稿。
    再起動しても on_ready から呼び直せるので、生成完了は必ずDiscordに届く。全モデル共通。"""
    job0 = _load_motion_job() or {}
    media_type = job0.get("media_type", "video")
    model = job0.get("model", "kling3_0_motion_control")
    label = job0.get("label", "モーション動画")
    token = job0.get("submitted_at")   # このジョブの識別子
    for _ in range(30):  # 1分おき最大30分
        await asyncio.sleep(60)
        job = _load_motion_job()
        if not job or job.get("cid") != cid:
            return  # 別ジョブに置き換わった/クリアされた
        if token is not None and job.get("submitted_at") != token:
            # 同じチャンネルで次の生成が始まった → 古い監視は退く。
            # 残しておくと監視が二重に走り、状態確認(claude CLI)が
            # 毎分2本ずつ立ち上がって渋滞・重複通知の原因になる。
            return
        try:
            vurl = await _mcp_gen_status(media_type, model)
        except Exception as e:  # noqa: BLE001
            _clear_motion_job()
            await send_as(orch, cid, f"⚠️ 生成が失敗しました: {str(e)[:250]}")
            return
        if vurl and _url_is_stale(vurl, token):
            # 前の生成が履歴の先頭に残っているだけ。今回の完成ではない。
            print(f"[gen] 古い生成を無視して待機継続: {vurl[-40:]}")
            vurl = None
        if vurl:
            # 投入から完成までが本当の所要時間。ここで測ったものだけを
            # 次回以降の見積もりに使う（投入だけの数十秒を使うと嘘になる）
            if token:
                _record_task_time(_gen_task_name(job0), time.time() - token)
            _clear_motion_job()
            _update_last_gen_url(cid, vurl)
            add_history(cid, "Orchestrator", f"（{label}が完成: {vurl}）")
            await _report_result(cid,
                                 job0.get("asked") or job0.get("request", ""), vurl,
                                 media_type, f"✅ {label}ができました！")
            return
    # 30分たっても完成せず → 記録は残し、後で「できた？」で確認できるようにする
    await send_as(orch, cid, _pending_eta_msg(_load_motion_job() or {}))


async def _run_motion_control(message, request, ref_att):
    """参照動画の動きを転写して動画生成（Kling モーションコントロール・1発実行）。
    キャラ画像：画像も添付されていればそれを使用、無ければ依頼文から自動生成。"""
    cid = message.channel.id
    if not HF_AVAILABLE:
        await send_as(orch, cid, f"⚠️ Higgsfield が使えません: {HF_IMPORT_ERROR}")
        return
    video_url = ref_att.url

    # キャラクター画像を決める
    img_att = _find_attachment(message, SUPPORTED_IMAGE_TYPES)
    if img_att:
        image_url = img_att.url
        await send_as(orch, cid, "🎭 添付画像のキャラクターに、参照動画の動きを転写します…")
    else:
        await send_as(orch, cid, "🎭 キャラクター画像を生成してから、参照動画の動きを転写します…")
        img_prompt = (
            "次の依頼に登場するキャラクター/被写体の画像生成プロンプトを、"
            "英語1行・カンマ区切りで出力（プロンプトのみ）。\n依頼: " + request
        )
        try:
            sc = (await _ai_text_bg(img_prompt, "motion_char_prompt")).strip().splitlines()[0]
        except Exception:  # noqa: BLE001
            sc = request
        image_url = None
        try:
            data = await asyncio.to_thread(_gemini_generate_image_sync, sc)
            image_url = await send_image_bytes(
                cid, f"キャラクター: {sc[:120]}", data, "character.png"
            )
        except Exception as e:  # noqa: BLE001
            print(f"[motion] Geminiキャラ画像失敗: {str(e)[:150]}")
            await send_as(orch, cid, "Higgsfieldの最適モデルでキャラクター画像を生成します…")
            image_url = await _mcp_gen_and_wait(sc, media_type="image", model=None)
            if image_url:
                await send_as(orch, cid, f"キャラクター画像: {image_url}")
        if not image_url:
            await send_as(orch, cid, "⚠️ キャラクター画像を用意できませんでした。")
            return

    await send_as(orch, cid, "🎬 モーション転写のジョブを投入します…")

    # ① まず Higgsfield MCP 経由（claude CLI が MCP ツールを呼ぶ）。
    # MCPには Kling / motion_control 等、APIキーに無いモデルがある。
    try:
        result = await _mcp_motion_control(image_url, video_url, request)
        if isinstance(result, str):  # 投入中に既にURLが返った
            _clear_motion_job()
            add_history(cid, "Orchestrator", f"（モーション転写動画を生成した: {result}）")
            await send_as(orch, cid, f"✅ できました！（Higgsfield MCP経由）\n{result}")
            return
        # 投入成功 → ジョブを記録して、完了監視を開始（再起動しても復帰する）
        _save_motion_job(cid, request)
        # 制作モードのため直前生成も記録（モーション動画の後も会話で作り直せる）
        _save_last_gen(cid, request, "video", None, "モーション動画")
        await send_as(
            orch, cid,
            "⏳ 生成ジョブを投入しました。完成したらこのチャンネルに動画URLを自動投稿します"
            f"（{_eta_hint('モーション生成')}／「動画できた？」でいつでも確認可）。"
        )
        _defer_measure()   # 完成までの時間は見張り側で測る
        _spawn(_watch_motion_job(cid), cid, "生成の完了監視")
        return
    except Exception as e:  # noqa: BLE001
        print(f"[motion] MCP経由失敗 → プラットフォームAPIへ: {str(e)[:300]}")
        await send_as(
            orch, cid,
            f"ℹ️ MCP経由の投入に失敗しました（理由: {str(e)[:250]}）。"
            "APIキー経由の代替を試します…"
        )

    # ② プラットフォームAPI：通ったモデルIDは記憶し、次回からはそれを最初に試す
    saved = gen_settings.get("motion_app")
    candidates = ([saved] if saved else []) + [
        c for c in MOTION_CANDIDATES if c != saved
    ]
    vurl = None
    for cand in candidates:
        try:
            vurl = await hf_wrapper.motion_control_video(
                image_url, video_url, prompt=request, model=cand
            )
        except Exception as e:  # noqa: BLE001
            if _is_model_not_found(e):
                print(f"[motion] {cand}: モデルなし → 次の候補へ")
                continue
            if _is_credit_error(e):
                await send_as(orch, cid, CREDIT_MSG)
            else:
                await send_as(
                    orch, cid,
                    f"⚠️ モーション転写に失敗（モデル: {cand}）: {str(e)[:300]}"
                )
            return
        if vurl:
            if gen_settings.get("motion_app") != cand:
                gen_settings["motion_app"] = cand
                _save_gen_settings()
            break
    if not vurl:
        # 全候補がmodel_not_found → カタログを直接照会して使えるIDを提示
        await send_as(
            orch, cid,
            "⚠️ モーション転写に使えるモデルIDが見つかりませんでした。"
            "Higgsfieldのモデルカタログを照会します…"
        )
        report = await _discover_hf_models()
        await send_as(orch, cid, report[:1900])
        await send_as(
            orch, cid,
            "上の一覧にモーション/参照動画系のIDがあれば、"
            "「モーションのモデルを ○○○ にして」と送ってください。設定して再試行できます。"
        )
        return
    add_history(cid, "Orchestrator", f"（モーション転写動画を生成した: {vurl}）")
    await send_as(orch, cid, f"✅ できました！（モデル: {gen_settings['motion_app']}）\n{vurl}")


async def _synthesize(claude_ans, gemini_ans, history, extra=""):
    """③ 司令塔が統合。合意点を軸に、対立点があれば触れて単一回答へ。"""
    prompt = (
        "あなたは司令塔（オーケストレーター）。ClaudeとGeminiの回答を統合し、単一の最終回答を作る。"
        "両者の【合意点】を軸に据え、【対立点】があれば簡潔に触れて最も妥当な結論を示す。"
        f"実況や『Claudeが〜』等は書かず、回答本体のみ。日本語で{REPLY_CHARS}字以内、結論を先に。\n\n"
        + (extra + "\n\n" if extra else "")
        + f"会話:\n{build_transcript(history)}\n\n"
        f"Claudeの回答:\n{claude_ans}\n\n"
        f"Geminiの回答:\n{gemini_ans}\n\n最終回答:"
    )
    return await run_claude_cli(prompt)


def _latest_user_msg(history):
    return history[-1][1] if history else ""


def _strip_media_context(text):
    """添付/YouTube解析で発言に追記された【…】ブロックや（ファイル共有）マーカーを
    取り除き、ユーザーが実際に打った部分だけを返す。
    解析まとめの中の「動画」「完成」等の単語で状態確認などの機能が
    誤発動するのを防ぐ（機能トリガーの判定には必ずこちらを使う）。"""
    t = re.split(r"\s*【", text or "")[0]
    return t.replace("（ファイル共有）", "").strip()


# 返事の精査をGeminiに任せるか。クロードが書いた下書きを、Geminiが
# 事実の食い違い・冗長・答え漏れの観点で締める＝二人で1つの返事を作る。
# 短い雑談まで通すと遅くなるので、ある程度の長さの返事だけを対象にする。
REVIEW_REPLIES = os.getenv("REVIEW_REPLIES", "1") not in ("0", "false", "False")
REVIEW_MIN_CHARS = int(os.getenv("REVIEW_MIN_CHARS", "120"))

_REVIEW_PROMPT = (
    "あなたは校閲役。下の【下書き】は、この会話への回答案です。\n"
    "書き直してはいけません。読んで、直すべき点だけを挙げてください。\n"
    "見るところ:\n"
    "・質問に答えていない箇所、答え漏れ\n"
    "・事実の誤り、根拠のない断定\n"
    "・同じことの繰り返し\n"
    "【出力の形】\n"
    "・直すところが無ければ、『問題なし』の4文字だけを出力する。\n"
    "・あれば、箇条書きで最大3点。1点は40字以内。指摘だけを書く。\n"
    "【禁止】回答本文を書かない。言い換え・整形・敬語化・箇条書き化の"
    "提案はしない（文体は担当者のもので、あなたが決めるものではない）。\n\n"
)
_FIX_PROMPT = (
    "下の【下書き】に、校閲から【指摘】が付きました。"
    "指摘のあった点だけを直した本文を出力してください。\n"
    "【厳守】話し方・語尾・文体・長さは下書きのまま変えない。"
    "指摘に無い箇所は1文字も触らない。"
    "前置きや『修正しました』は書かず、本文だけを出力する。\n\n"
)

# 敬体（です・ます）の出現を数えるための目印。
_POLITE_RE = re.compile(
    "(?:です|ます|ました|ません|でしょう|ください|いたします|ございます)"
    "(?=[。、\n！？!?」]|$)"
)


# 「指摘なし」の言い方（校閲がそのまま通した合図）
_NO_ISSUE_RE = re.compile("問題なし|問題ありません|指摘なし|特になし|修正点はあり")


def _register_changed(draft, out):
    """下書きは話し言葉なのに、精査後が敬体に化けていないか。
    文体が入れ替わると、同じ相手が急に他人行儀になったように見える。"""
    d = len(_POLITE_RE.findall(draft))
    o = len(_POLITE_RE.findall(out))
    return d <= 1 and o >= 3


async def _review_reply(draft, history):
    """クロードの下書きを、Geminiが【校閲】する（書き直させない）。
    以前はGeminiに本文を書き直させていたため、長い返事だけが
    Geminiの敬語・箇条書きに化けて、会話が急に他人行儀になった。
    いまはGeminiは指摘だけを返し、直すのは書いた本人（クロード）。
    指摘が無ければ余計な呼び出しをしないので、速さも落ちない。"""
    if not (REVIEW_REPLIES and draft) or len(draft) < REVIEW_MIN_CHARS:
        return draft, False
    if _gemini_all_cooling():
        return draft, False
    try:
        notes = await _gemini_call(
            _REVIEW_PROMPT + transcript_block(history)
            + f"\n\n【下書き】\n{draft}\n\n指摘:",
            "review",
        )
    except Exception as e:  # noqa: BLE001
        print(f"[review] 校閲に失敗（下書きを使用）: {str(e)[:120]}")
        return draft, False
    notes = (notes or "").strip()
    if not notes or _NO_ISSUE_RE.search(notes) or len(notes) > 400:
        return draft, False          # 指摘なし／長すぎる＝書き直している
    try:
        fixed = await run_claude_cli(
            _FIX_PROMPT + transcript_block(history)
            + f"\n\n【下書き】\n{draft}\n\n【指摘】\n{notes}\n\n直した本文:"
        )
    except Exception as e:  # noqa: BLE001
        print(f"[review] 直しに失敗（下書きを使用）: {str(e)[:120]}")
        return draft, False
    fixed = _clean_reply(_strip_cli_boilerplate((fixed or "").strip()),
                         _latest_user_msg(history))
    # 別物になっていたら採用しない（校閲は手直しであって書き直しではない）
    if not fixed or len(fixed) < len(draft) * 0.5 or len(fixed) > len(draft) * 1.6:
        return draft, False
    if _register_changed(draft, fixed):
        print("[review] 文体が変わったため下書きを使用")
        return draft, False
    return fixed, True


async def ask_orchestrator(history):
    _, mode, lead, search, recall, reply = await _plan(history)
    return reply or await _orchestrate(mode, lead, search, history, recall)


_RESET_RE = re.compile(r"resets?\s+([^\n·]+)", re.I)


def _limit_note(why):
    """代打で答えた理由を一言で。いつ戻るかが分かれば待てる。
    以前は『⚠️ 応答に失敗』とだけ出て、復帰の見込みが本文に埋もれていた。"""
    m = _RESET_RE.search(why or "")
    when = f"（{m.group(1).strip()}ごろ戻ります）" if m else ""
    if re.search("limit|上限|quota", why or "", re.I):
        return f"※クロードが利用上限のため、Geminiが代わりに答えています{when}。"
    return "※クロードが応答できなかったので、Geminiが代わりに答えています。"


async def _orchestrate(mode, lead, search, history, recall=False):
    # 必要ならWeb検索して文脈を用意（ボット自身が検索＝権限プロンプト不要）
    ctx = await web_search_context(_latest_user_msg(history)) if search else ""

    # 過去の会話が必要なら、全ログをGeminiに読ませて関連情報を抽出して文脈に足す
    if recall:
        cid = _cid_of_history(history)
        if cid is not None:
            rc = await _recall_context(cid, _latest_user_msg(history))
            if rc:
                ctx = (ctx + "\n\n" + rc).strip() if ctx else rc

    # Geminiに返事を書かせない設定なら、ここで単発（クロード）に寄せる。
    # ディベートはGeminiが相方なので、オフのときは成立しない。
    if not _gemini_replies_on():
        mode, lead = "single", "claude"

    # 実際にどちらが書いたかを残す。クロードが枠切れでGeminiに落ちた時、
    # 文体が変わるのに「クロード2」と名乗っていて、同じ相手が急に
    # 他人行儀になったように見えていた（噛み合わない一因）。
    _wrote["name"] = CLAUDE2_NAME

    # ① 単発モード：簡単な要求は得意モデル1つで即答（コスト節約）
    if mode == "single":
        if lead == "gemini":
            try:
                ans = await _gemini_call(_answer_prompt(ORCH_PERSONA, history, ctx))
                if (ans or "").strip():
                    _wrote["name"] = "Gemini"
                    return ans
                # 安全フィルタ等で本文が空 → 無言にせずClaudeで答え直す
                print("[orchestrate] Geminiが空応答 → Claudeへ")
            except Exception:  # noqa: BLE001
                pass  # Gemini不可ならClaudeへ
        try:
            return await run_claude_cli(_answer_prompt(ORCH_PERSONA, history, ctx))
        except Exception as e:  # noqa: BLE001
            # Claudeがタイムアウト・上限などで落ちたらGeminiで応答（無応答を防ぐ）
            print(f"[orchestrate] Claude失敗 → Geminiへ: {str(e)[:150]}")
            _wrote["name"] = GEMINI_STANDIN
            _wrote["why"] = str(e)[:200]
            try:
                return await _gemini_call(_answer_prompt(ORCH_PERSONA, history, ctx))
            except Exception as e2:  # noqa: BLE001
                # 両方ダウン：本当の原因（Claude側）を隠さず両方報告する
                raise RuntimeError(
                    f"ClaudeもGeminiも応答できません。\n"
                    f"・Claude: {str(e)[:180]}\n・Gemini: {str(e2)[:120]}"
                )

    # ② ディベートモード：まず両者が独立に回答（検索結果があれば共有）
    results = await asyncio.gather(
        run_claude_cli(_answer_prompt("Claude", history, ctx)),
        _gemini_call(_answer_prompt("Gemini", history, ctx)),
        return_exceptions=True,
    )
    claude_ans = results[0] if not isinstance(results[0], Exception) else ""
    gemini_ans = results[1] if not isinstance(results[1], Exception) else ""

    # Geminiが使えない場合はClaude単独に縮退
    if not gemini_ans.strip():
        return claude_ans or await run_claude_cli(_answer_prompt(ORCH_PERSONA, history, ctx))
    if not claude_ans.strip():
        return gemini_ans

    # ②-b 相互に見せて批判・修正（ディベート1ラウンド）
    revised = await asyncio.gather(
        run_claude_cli(_critique_prompt("Claude", "Gemini", claude_ans, gemini_ans, history)),
        _gemini_call(_critique_prompt("Gemini", "Claude", gemini_ans, claude_ans, history)),
        return_exceptions=True,
    )
    claude_rev = revised[0] if not isinstance(revised[0], Exception) else claude_ans
    gemini_rev = revised[1] if not isinstance(revised[1], Exception) else gemini_ans

    # ③ 統合
    return await _synthesize(claude_rev, gemini_rev, history, ctx)


async def _report_gen_status(channel, cid, author_name=None, said=None):
    """生成の進捗/完成を確認して報告する（「できた？」系の唯一の実装）。
    進行中ジョブがあればHiggsfieldに問い合わせ、無ければ直近の完成物を案内し、
    どちらも無ければ False を返して会話として続行させる。
    ※以前は決定的ルートと会話ハンドラに同じ処理が二重にあり、
      片方だけ直して挙動が食い違う不具合が実際に起きたため1本化した。"""
    job = _load_motion_job()
    lg = _load_last_gen(cid) or {}
    # 生成以外の作業（ログ共有・リサーチ・学習など）を実行中なら、そちらを答える。
    # これが無いと「まだ？」に対して無関係な直近の画像を出してしまう。
    busy = _busy_tasks(cid)
    if busy and not job:
        if said and author_name:
            add_history(cid, author_name, said)
        detail = "／".join(f"「{n}」({_eta_text(n, sec)})" for n, sec in busy[:3])
        await channel.send(
            f"⏳ いま {detail} を実行中です。終わったらこのチャンネルに結果を出します。"
        )
        return True
    if not job and not lg.get("url"):
        # 作るものを名指しで聞かれている（「相関図できた？」等）のに、実行中でも
        # 完成物でもない＝前回が途中で終わっている。会話に流すとAIが理由を
        # 作り話するので、事実だけを返して作り直しを案内する。
        if said and _STATUS_CTX_RE.search(said):
            if author_name:
                add_history(cid, author_name, said)
            await channel.send(
                "📭 いま動いている作業も、直近の完成物もありません"
                "（前回は途中で終わったようです）。\n"
                "もう一度お願いするなら、そのまま同じ内容で頼んでください。"
            )
            return True
        return False   # 会話へ流す。ここで履歴に触れない（後段で二重登録になるため）
    if said and author_name:
        add_history(cid, author_name, said)
    if job:
        label = job.get("label") or "生成"
        await channel.send(f"🔎 「{label}」の状況を Higgsfield で確認します…")
        try:
            vurl = await _mcp_gen_status(job.get("media_type", "video"),
                                         job.get("model"),
                                         since=job.get("submitted_at"))
        except Exception as e:  # noqa: BLE001
            await channel.send(f"⚠️ 生成が失敗していました: {str(e)[:250]}")
            add_history(cid, "Orchestrator", "（生成の失敗を報告した）")
            return True
        if vurl:
            _clear_motion_job()
            _update_last_gen_url(cid, vurl)
            add_history(cid, "Orchestrator", f"（生成物が完成: {vurl}）")
            await channel.send(
                f"✅ できています！URLはこちら:\n{vurl}\n"
                "「バズ度分析して」で広告効果の事前予測もできます。"
            )
        else:
            await channel.send(_pending_eta_msg(job))
        return True
    if lg.get("url"):
        label = lg.get("label") or "生成"
        await channel.send(
            f"✅ 直近の「{label}」はもう完成しています:\n{lg['url']}\n"
            "続けるなら「バズ度分析して」で効果予測、"
            "「〇〇を直して作り直して」で改善もできます。"
        )
        add_history(cid, "Orchestrator", f"（完成済みの{label}のURLを再案内した）")
        return True
    return False   # 進行中も完成物も無い → 普通の会話へ


async def _start_agent(message, cid, content):
    await message.channel.send(
        "🛠 コードを触る作業ですね。プランを作ります…"
        "（[✅許可]で実行 / [❌拒否]で中止）"
    )
    _spawn(_run_agent_task(cid, content, message.author.id), cid, "エージェント実行")


async def _handle_orchestrator(message, cid):
    """オーケストレーター宛て。実際にコード/ファイルを触る指示のときだけ承認ダイアログ。
    それ以外（質問・相談・雑談）は普通に会話する。"""
    history = get_history(cid)
    latest = _latest_user_msg(history)
    # 機能トリガーの判定は、添付/YouTube解析の追記部分を除いた
    # 「ユーザーが実際に打った文」だけで行う（解析文の単語で誤発動しない）
    said = _strip_media_context(latest)

    # 生成ジョブの完了確認（「動画できた？」「あとどれくらい？」等）。
    # 進行中ジョブがある時だけ確認モードに入り、無ければ普通の会話に流す。
    if re.search("モーション|動画|画像|生成", said) and re.search(
        "できた|完成|終わった|どうなった|状況|進捗|まだ|あとどれ|どれくらい|どのくらい|何分", said
    ):
        if await _report_gen_status(message.channel, cid):
            return
        # 進行中の生成が無い → 状態確認モードに入らず、そのまま会話として続行

    # モーションの顔の向きモード切替（顔重視 image / 動き重視 video）
    if re.search("モーション|顔", said) and re.search("向き|オリエン|モード|顔", said):
        if re.search("動き優先|動き重視|複雑な動き|video", said, re.I):
            gen_settings["motion_orientation"] = "video"
            _save_gen_settings()
            await message.channel.send("🔧 モーションを『動き優先(video)』にしました。")
            return
        if re.search("顔優先|顔重視|似せ|image|見た目", said, re.I):
            gen_settings["motion_orientation"] = "image"
            _save_gen_settings()
            await message.channel.send(
                "🔧 モーションを『顔・見た目優先(image)』にしました。"
                "次の生成から顔をより忠実に保ちます。"
            )
            return

    # モーション転写モデルのID直接指定（「モーションのモデルを ○○ にして」）
    m = re.search(r"モーション\S*の?モデル\S*を\s*([\w\-./]{4,})\s*に", said)
    if m:
        gen_settings["motion_app"] = m.group(1)
        _save_gen_settings()
        await message.channel.send(
            f"🔧 モーション転写のモデルを {m.group(1)} に設定しました。"
            "もう一度、動画を添付して依頼してください。"
        )
        return

    # モデル設定の確認（「今のモデル設定教えて」等）。
    # 「モデル」だけで拾うと日常語に当たる。実際に
    # 「アップルウォッチのセルラーモデル…金額教えて」へ生成モデル設定を返し、
    # 質問には一切答えないまま終わっていた。
    if _asks_gen_model(said):
        _fired(cid, "生成モデル設定の表示", said)
        await message.channel.send("🔧 現在の生成モデル設定:\n" + _gen_settings_summary())
        return

    # 発言中のモデル名（クリング/ナノバナナ等）を検出して生成設定に反映
    changes = _apply_model_mentions(said)
    if changes:
        await message.channel.send("🔧 生成モデルを切り替えました:\n" + "\n".join(changes))
        # モデル指定だけの発言（作る対象が無い）ならここで完了
        if len(said) <= 30 and not re.search(
            "作って|作りたい|生成して|生成したい|やって|始めて|プロジェクト", said
        ):
            return

    # 分類＋処理方針＋（雑談ならその返事まで）を1回のAI呼び出しで得る
    async with message.channel.typing():
        kind, mode, lead, search, recall, reply = await _plan(history)
        # どのエンジンが書いたかは【この時点で】控える。あとで読むと、
        # 裏で動くGeminiの処理に _last_engine を書き換えられている
        plan_engine = _last_engine.get("name") or CLAUDE2_NAME
    # 保険：質問や相談っぽい発言が作業系に誤分類されたらchatに戻す。
    # video/image も対象（「veo3の料金いくら？」で生成が始まる事故があった）。
    if kind in ("selffix", "exec", "video", "image") and _looks_like_question(latest):
        print(f"[plan] {kind}→chat に降格（質問/相談と判断）")
        kind, reply = "chat", ""   # 降格時の返事は無いので通常の回答フェーズへ
    # 判定と同時に返事も書けていれば、追加のAI呼び出しをせずそのまま返す（最速）
    if kind == "chat" and reply:
        reply = _clean_reply(reply, latest)
        # 会話パスにはMCPの権限が無い。ツールを呼ぼうとして弾かれた言い訳を
        # そのまま見せず、権限のある経路で調べ直す（ユーザーに操作を求めない）。
        if _TOOL_DENIED_RE.search(reply):
            await message.channel.send("💳 Higgsfieldに問い合わせています…")

            async def _do_retry():
                res = await _run_credits(latest, get_history(cid))
                add_history(cid, "Orchestrator", res)
                await send_as(orch, cid, res)
            _last_credits[cid] = time.time()
            _spawn(_do_retry(), cid, "クレジット確認")
            return
        reply = _drop_false_progress(reply, cid)
        add_history(cid, "Orchestrator", reply)
        await send_as(orch, cid, _with_speaker(reply, plan_engine))
        return
    if kind == "video":
        # 単発の動画生成は、最適モデルを自動選定して直接生成（滑らかで確実）。
        # 構成案→絵コンテの多段フローが要るときは !project を使う。
        _req = _latest_user_msg(history)
        _gate(message, cid, f"動画の制作（{_req[:50]}）",
              "内容に合う最適なモデルを自動で選んで動画を生成します",
              lambda: _run_hf_generate(message, _req, None, "video", "自動選定"),
              "動画生成", "Higgsfieldのクレジットを消費します",
              engine=_engine_label_hf("自動選定", "動画"))
        return
    if kind == "exec":
        await _start_agent(message, cid, _latest_user_msg(history))
        return
    if kind == "image":
        _req = _latest_user_msg(history)
        _gate(message, cid, f"画像の生成（{_req[:50]}）",
              "Geminiの無料枠で画像を生成します",
              lambda: _handle_image_request(cid, _req), "画像生成",
              "原則無料（Geminiの無料枠）", engine=ENGINE_GEMINI_IMG)
        return
    if kind == "selffix":
        _spawn(_run_self_fix(cid, _latest_user_msg(history), message.author.id),
               cid, "自己改修")
        return
    if kind == "restart":
        await _restart_self(cid)
        return
    if kind == "trend":
        if not YOUTUBE_API_KEY:
            await message.channel.send(
                "YOUTUBE_API_KEY が未設定のためリサーチできません（README参照）。"
            )
            return
        topic = re.sub(
            r"(の)?(トレンド|急上昇|リサーチ|調査|分析|調べて|して|ちょうだい|ください|お願い(します)?|よ|ね)+$",
            "", _latest_user_msg(history).strip(),
        ).strip("　 。、")
        _gate(message, cid, f"YouTubeのリサーチ（{topic or '急上昇'}）",
              "人気動画を検索して内容を視聴・分析し、レポートにまとめます",
              lambda: _run_trend_study(cid, topic or None), "YouTubeリサーチ",
              "無料（YouTube APIとGeminiの無料枠）")
        return
    if kind == "talk":
        if state["running"]:
            await message.channel.send("自動トークが進行中です。「止めて」で停止できます。")
            return
        await message.channel.send(
            f"🎙️ ClaudeとGeminiで話します（最大 {MAX_TURNS} 発言。「止めて」で停止）"
        )
        _spawn(run_auto(cid, _latest_user_msg(history)), cid, "自動トーク")
        return
    if kind == "profile":
        p = _load_profiles()
        if p:
            await send_long(message.channel, p, "🧠 ")
        else:
            await message.channel.send(
                "まだプロファイルはありません（会話がたまると自動で作られます）。"
            )
        return

    # 通常会話（承認ダイアログは出さない）
    async with message.channel.typing():
        try:
            # history に既に attachment_context が含まれているため、
            # _orchestrate で改めて attachment_context パラメータを渡さない
            answer = await _orchestrate(mode, lead, search, history, recall)
        except Exception as e:  # noqa: BLE001
            if _is_quota_error(e):
                try:
                    answer = await run_claude_cli(_answer_prompt(ORCH_PERSONA, history))
                except Exception:  # noqa: BLE001
                    answer = "⚠️ 一時的に応答できませんでした。少し後で試してください。"
            else:
                if "gemini" in str(e).lower():
                    _gemini_watch["outage_cid"] = cid
                answer = f"⚠️ 応答に失敗: {str(e)[:300]}"
    answer = _clean_reply(answer, latest)
    # クロードの下書きをGeminiが精査して締める（＝二人で1つの返事にする）。
    # 出す声はオーケストレーターひとつなので、誰が書いたかで混乱しない。
    answer, reviewed = await _review_reply(answer, history)
    answer = _drop_false_progress(answer, cid)
    # 実際に書いたのが誰かで名乗る。クロードが枠切れでGeminiが代打に入ると
    # 文体が変わるので、「クロード2」と名乗ったままだと別人が混ざって見える。
    _who = _wrote.get("name") or CLAUDE2_NAME
    if _who == GEMINI_STANDIN:
        answer += ("\n\n" + _limit_note(_wrote.get("why", "")))
    add_history(cid, "Orchestrator", answer)
    await send_as(orch, cid, _with_speaker(answer, _who))


# ---------- 送信・進行 ----------
async def send_long(channel, text, prefix=""):
    """長文をDiscordの上限に合わせて分割送信する（先頭だけ prefix を付ける）。"""
    _mark_sent(getattr(channel, "id", 0), text)
    text = text or ""
    for i in range(0, max(len(text), 1), 1900):
        await channel.send((prefix if i == 0 else "") + text[i:i + 1900])


DISCORD_LIMIT = 1900        # Discordの2000字上限に余裕を持たせた値


def _chunks(text, size=DISCORD_LIMIT):
    """長文を送信できる大きさに割る。できるだけ改行や句点で切る。
    以前は [:1900] で切り捨てていたため、長い回答が
    『※ 上記は添付いただいた画像と、一』のように文の途中で消えていた。"""
    text = text or "(空の応答)"
    out = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size * 0.5:
            cut = max(text.rfind("。", 0, size), text.rfind("、", 0, size))
            cut = cut + 1 if cut >= size * 0.5 else size
        out.append(text[:cut].rstrip())
        text = text[cut:].lstrip("\n")
    out.append(text)
    return [c for c in out if c] or ["(空の応答)"]


# チャンネルごとの送信回数。「何も答えないまま終わった」の検知に使う。
_sent_count = {}


def _mark_sent(cid, text=""):
    _sent_count[cid] = _sent_count.get(cid, 0) + 1
    _trace(cid, "out", text)


async def send_as(bot, channel_id, text, view=None):
    """長い本文は切り捨てず、分割して全部送る。
    ボタン(view)は最後の塊に付ける（読み終わった位置に出す）。"""
    _mark_sent(channel_id, text)
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    parts = _chunks(text)
    for i, part in enumerate(parts):
        kwargs = {"view": view} if (view is not None and i == len(parts) - 1) else {}
        await channel.send(part, **kwargs)


# 会話ログが「名前: 本文」形式なので、モデルが真似て自分の名前を先頭に付けてしまう。
# 役割（リサーチャー等）を増やしてから特に出やすくなったため、送信前に必ず落とす。
_SPEAKER_PREFIX_RE = re.compile(
    r"^\s*(?:オーケストレーター|Orchestrator|"
    r"クロード\s*[123]?\s*(?:（[^）]*）)?|Claude\s*[123]?|"
    r"Gemini|ジェミニ|リサーチャー|アドバイザー|PM|ＰＭ)\s*[:：]\s*", re.I
)


# 「俺はGeminiじゃなくてクロード」のような正体の訂正。会話ログに一度出ると
# モデルが延々と蒸し返すため、ユーザーがAIの話をしていない限り冒頭から落とす。
_IDENTITY_TALK_RE = re.compile(
    r"(?=.*(?:gemini|ジェミニ|クロード|claude))"
    r"(?=.*(?:じゃな|ではな|別のAI|紛らわし|まぎらわし|名前が|同席|参加してる))",
    re.I | re.S,
)
_IDENTITY_TAIL_RE = re.compile(r"紛らわし|まぎらわし|ごめん|すみません|失礼|悪かった")
_USER_ASKED_AI_RE = re.compile(r"gemini|ジェミニ|クロード|claude|ai|エーアイ", re.I)


# ボットの操作案内の文（本来ボットの話をしている時にしか出してはいけない）。
_OPS_ADVICE_RE = re.compile(
    r"(ログ(を)?送って|デバッグログ|ログ(を)?共有|再起動して|再起動をして|"
    r"動画できた\??？?」?と送)"
)


# 「作業を始めた／終わったら知らせる」という言い方。
# 実際に何も動いていない時にこれを言われると、ユーザーは待ち続けてしまう。
# 事故：ショート切り抜きが一度も起動していないのに、4通続けて
# 「このまま処理に入るね」「終わったタイミングで知らせる」「そのまま処理進めるね」
# と言い張り、ユーザーは完成を待っていた。
# 「作業を始めた」と読める言い方。
_PROGRESS_START_RE = re.compile(
    "処理に入る|処理を始め|処理進め|処理を進め|作業を始め|作業に入る|"
    "取りかかる|取り掛かる|始めるね|始めるよ|やっておく|やっとく|進めておく|"
    "処理が終わ|作業中|実行中|走らせて|動かして(る|いる)|生成して(る|いる)|"
    "切り出して(る|いる)|作って(る|いる)|"
    # 事故：何も動いていないのに「作り直すね。投げるから、ちょっと待ってて」
    "作り直すね|作り直すよ|やり直すね|投げる|投入する|流しておく|"
    "待ってて|待っててね|少し待って|しばらく待って"
)
# 「終わったら知らせる」と読める言い方（間に語が挟まっても拾う）。
_NOTIFY_LATER_RE = re.compile(
    "(終わったら|終わった時|終わったタイミング|終わり次第|できたら|"
    "完了したら|出来たら)[^。\n]{0,30}(知らせ|教え|連絡|報告|送)"
)


# 内部の状態についての作り話。確認待ちが無いのにこう言うのは全部でたらめ。
# 言い方は毎回変わる（「許可が下りてない」→「許可が必要みたい」）ので、
# 語を1つずつ潰さず「許可・権限まわりの否定/推量」という形で受ける。
_FAKE_STATE_RE = re.compile(
    "(許可|承認|権限)[^。\n]{0,12}"
    "(下り|おり|降り|必要|通って|足りて|得られ|もらえ)"
    "[^。\n]{0,10}(ない|いない|なかった|みたい|ようだ|らしい|そう)|"
    "(この場|こっち|ここ)では[^。\n]{0,14}(動かせ|実行でき|できな|通せ)|"
    "生成ボタン|実行ボタン|(通ってなくて|通っていなくて)"
)


def _drop_false_progress(text, cid):
    """何も動いていないのに『処理を始めた／終わったら知らせる』と書いた文を落とす。
    プロンプトで禁じても守られなかったので、コード側で必ず落とす
    （このリポジトリの決まり：条件は文章でなくコードで守らせる）。
    事故：切り抜きが一度も起動していないのに4通続けて作業中だと言い張り、
    ユーザーは完成を待ち続けた。"""
    if not text:
        return text
    try:
        if _busy_tasks(cid) or _load_motion_job():
            return text          # 本当に動いているなら触らない
    except Exception:  # noqa: BLE001
        return text
    parts = re.split(r"(?<=[。\n])", text)
    # 確認待ちが無いのに「許可が下りてない」「生成ボタンが押されてない」など、
    # 内部の状態を作り話で説明した文も落とす。
    # 事故：何も動いていない時に「生成の許可が下りてないみたい」と言い、
    # 「なんで許可おりてないの？」と聞かれて、さらに作り話を重ねた。
    _no_pending = not _pending_approvals.get(cid)
    kept = [p for p in parts
            if not (_PROGRESS_START_RE.search(p) or _NOTIFY_LATER_RE.search(p)
                    or (_no_pending and _FAKE_STATE_RE.search(p)))]
    if len(kept) == len(parts):
        return text              # 何も落ちていない＝作業の宣言はしていない
    out = "".join(kept).strip()
    print(f"[reply] 動いていない作業の宣言を落とした: channel={cid}")
    # 落としただけだと「始まった」と読める文が残ることがあるので、
    # 実際には動いていないことを必ず明記する。
    # 「『やって』と言って」とだけ書くと、言われた側に受け皿が無く
    # 同じ説明を繰り返す無限ループになった（実際に2往復した）。
    # 何が足りないかを覚えておき、「やって」に具体的に答えられるようにする。
    _set_pending_do(cid, "何を・どの素材でやるか")
    note = "（これはまだ実際には動かしていない）"
    if not out:
        return "まだ何も動かしていないよ。"
    return f"{out}\n{note}"


def _drop_ops_advice(t, user_said):
    """ボットの話をしていないのに操作案内を返すのは事故なので落とす。
    プロンプト側で運用ルールを渡さないようにした上での二重の保険
    （実例：ツボの痛みの相談に「ログ送って」と答えた）。"""
    if not t or _OPS_TOPIC_RE.search(user_said or ""):
        return t
    kept = [p for p in re.split(r"(?<=[。\n])", t) if not _OPS_ADVICE_RE.search(p)]
    out = "".join(kept).strip()
    # 全部が案内文だった場合は、案内を出すより「分からない」と正直に言う方がいい
    return out or "ごめん、それは分からないな。"


def _clean_reply(text, user_said=""):
    """返事の先頭に付いた話者名と、正体の訂正の蒸し返しを取り除く。"""
    t = (text or "").lstrip()
    for _ in range(2):        # 「クロード: オーケストレーター: 」の二重も落とす
        t2 = _SPEAKER_PREFIX_RE.sub("", t, count=1).lstrip()
        if t2 == t:
            break
        t = t2
    t = _drop_ops_advice(t, user_said)
    if user_said and _USER_ASKED_AI_RE.search(user_said):
        return t              # 本人がAIの話をしている → 触らない
    # 冒頭に続く「正体の説明」の文だけを落とす（本題に入ったらそこで止める）
    parts = re.split(r"(?<=[。\n])", t)
    i = dropped = 0
    while i < len(parts):
        piece = parts[i]
        if not piece.strip():
            i += 1
            continue
        if _IDENTITY_TALK_RE.match(piece):
            i += 1
            dropped += 1
            continue
        # 正体の説明に続く短い謝罪・言い訳（「名前がまぎらわしくてごめん。」）も落とす
        if dropped and len(piece) < 50 and _IDENTITY_TAIL_RE.search(piece):
            i += 1
            continue
        break
    rest = "".join(parts[i:]).strip()
    return rest or t          # 全部消える場合は元のまま（無言にしない）


def _with_speaker(text, who=None):
    """返事の先頭に「誰が答えたか」を付ける（表示用。履歴には入れない）。

    who を渡すこと。渡さないと _last_engine（全体で1つ）を見るが、これは
    裏で動くGeminiの処理（要約・検品・解析）でも書き換わるため、
    クロードが書いた返事に「Gemini」と付く事故が起きた。"""
    who = who or _last_engine.get("name")
    return f"**{who}**: {text}" if who and text else text


async def respond(cid, name, bot, ask):
    text = _clean_reply(await ask(get_history(cid)),
                        _latest_user_msg(get_history(cid)))
    # 同じ発言の繰り返しは送信しない（Geminiが同文を連投するバグへの保険）
    h = get_history(cid)
    last_own = next((t for s, t in reversed(h) if s == name), None)
    if last_own and text.strip() and text.strip() == last_own.strip():
        print(f"[respond] {name} の重複発言を抑制しました")
        return
    add_history(cid, name, text)
    await send_as(bot, cid, text)


def decide_targets(message, content):
    """宛先から反応者を決める。@メンションされたBotだけが反応し、
    @メンションが無ければ常にオーケストレーター宛て（統合回答）。"""
    m_orch = orch.user and orch.user in message.mentions
    m_claude = claude_bot.user and claude_bot.user in message.mentions
    m_gemini = gemini_bot.user and gemini_bot.user in message.mentions
    if m_orch or m_claude or m_gemini:
        targets = []
        if m_orch:
            targets.append(("Orchestrator", orch, ask_orchestrator))
        if m_claude:
            targets.append(("Claude", claude_bot, ask_claude))
        if m_gemini:
            # 名指しされた時だけはGemini本人が答える。
            # 自動でGeminiが割り込むのは止めているが、こちらから呼んだ場合は別
            # （誰が答えるかを決めたのは本人なので、声が混ざる混乱は起きない）。
            targets.append(("Gemini", gemini_bot, ask_gemini))
        return targets
    # @メンションなし → 常にオーケストレーター宛て
    return [("Orchestrator", orch, ask_orchestrator)]


async def run_auto(cid, topic):
    """Claude と Gemini だけで自動的に会話（!talk 用）。"""
    state["running"], state["stop"] = True, False
    # Geminiの返信を止めている間は、!talk でもGeminiに喋らせない
    speakers = ([("Claude", claude_bot, ask_claude),
                 ("Gemini", gemini_bot, ask_gemini)] if _gemini_replies_on()
                else [("Claude", claude_bot, ask_claude)])
    try:
        for i in range(MAX_TURNS):
            if state["stop"]:
                break
            name, bot, ask = speakers[i % len(speakers)]
            try:
                await respond(cid, name, bot, ask)
            except Exception as e:
                await send_as(orch, cid, f"⚠️ {name} の呼び出し失敗: {e}")
                break
            await asyncio.sleep(SEND_DELAY)
        await send_as(orch, cid, "🏁 自動トーク終了")
    finally:
        state["running"] = False


# ---------------------------------------------------------------------------
# 映像制作パイプライン（構成案 → 絵コンテ → 編集チェック×3）
# ---------------------------------------------------------------------------
projects = {}  # channel_id -> dict(stage, topic, plan, scenes, images, videos, round)
NUM_SCENES = int(os.getenv("PIPELINE_SCENES", "3"))
MAX_EDIT_ROUNDS = 3
APPROVE_WORDS = {
    "ok", "okay", "ok!", "おっけー", "オッケー", "承認", "了解", "りょうかい",
    "いいね", "これでいい", "これでok", "次", "next", "🆗", "👍", "完成",
}


def _is_approve(text):
    return text.strip().lower() in APPROVE_WORDS


def _alive(cid, p):
    """プロジェクトpがまだ有効か（!stop / !cancel で消えていないか）。"""
    return projects.get(cid) is p


def _is_credit_error(e):
    return "not_enough_credits" in str(e)


CREDIT_MSG = (
    "⚠️ Higgsfield のクレジットが不足しています。\n"
    "https://cloud.higgsfield.ai でクレジットを追加すると、この段階が動きます。"
)


class ApprovalView(discord.ui.View):
    """各段階の下に出す [✅ 承認して次へ] [🔄 やり直し] ボタン。"""

    def __init__(self, cid):
        super().__init__(timeout=1800)  # 30分
        self.cid = cid

    async def _disable(self, interaction):
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(view=self)
        except Exception:  # noqa: BLE001
            pass
        self.stop()

    @discord.ui.button(label="✅ 承認して次へ", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not projects.get(self.cid):
            await interaction.response.send_message("このプロジェクトは終了しています。", ephemeral=True)
            return
        await interaction.response.send_message(
            f"✅ {interaction.user.display_name} が承認しました。次に進みます…"
        )
        await self._disable(interaction)
        await pipeline_reply(self.cid, "OK")

    @discord.ui.button(label="🔄 やり直し", style=discord.ButtonStyle.secondary)
    async def revise(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not projects.get(self.cid):
            await interaction.response.send_message("このプロジェクトは終了しています。", ephemeral=True)
            return
        await interaction.response.send_message(
            "🔄 修正内容をこのチャンネルにテキストで送ってください"
            "（例:「もっと明るく」「シーン2を夜に」）。"
        )
        await self._disable(interaction)


async def _pipeline_plan(cid, feedback=""):
    p = projects[cid]
    prompt = (
        f"あなたは{CLAUDE2_NAME}。映像ディレクターはkohei本人で、"
        "あなたはその【アシスタント】。決めるのはkoheiなので、"
        "こちらで決めきらず、判断できる材料として構成案を用意する。\n"
        f"お題「{p['topic']}」で短い映像の構成案を作る。"
        "日本語で簡潔に、次を必ず含める："
        "タイトル / コンセプト(1〜2文) / "
        f"シーン一覧({NUM_SCENES}個・各シーンは1行で情景を描写) / 尺の目安 / "
        "迷った点（koheiに決めてほしいことを1〜2個）。"
    )
    if feedback:
        prompt += (
            f"\n\n【前回の構成案】\n{p['plan']}\n\n"
            f"【修正指示】{feedback}\nこれを反映して作り直すこと。"
        )
    try:
        p["plan"] = await run_claude_cli(prompt)
    except Exception as e:  # noqa: BLE001
        await send_as(orch, cid, f"⚠️ 構成案の生成に失敗: {e}")
        return
    if not _alive(cid, p):  # 生成中に !stop された
        return
    await send_as(
        orch, cid,
        f"📝 【構成案】（{CLAUDE2_NAME}がディレクターのkohei向けに用意）\n"
        f"{p['plan']}\n\n———\n"
        "**決めるのはkohei**です。下のボタン、または「OK」で承認。"
        "直したい所はテキストで指示してください。",
        view=ApprovalView(cid),
    )


async def _pipeline_storyboard(cid, feedback=""):
    p = projects[cid]
    # Gemini画像生成が既定なので、Higgsfield無しでも絵コンテは作れる
    if gen_settings["image_engine"] == "higgsfield" and not HF_AVAILABLE:
        await send_as(orch, cid, f"⚠️ Higgsfield が使えません: {HF_IMPORT_ERROR}")
        return
    prompt = (
        f"次の構成案から、絵コンテ用の画像生成プロンプトを{NUM_SCENES}個作る。"
        "各シーン1つ、英語で、視覚描写をカンマ区切りで1行ずつ。"
        f"番号や記号は付けず、{NUM_SCENES}行だけ出力する。"
    )
    if feedback:
        prompt += f"\n修正指示: {feedback}"
    prompt += f"\n\n構成案:\n{p['plan']}"
    try:
        raw = await run_claude_cli(prompt)
    except Exception as e:  # noqa: BLE001
        await send_as(orch, cid, f"⚠️ 絵コンテのプロンプト生成に失敗: {e}")
        return
    if not _alive(cid, p):
        return
    scenes = [ln.strip(" -*0123456789.、。") for ln in raw.splitlines() if ln.strip()]
    p["scenes"] = scenes[:NUM_SCENES]
    engine_label = (
        "Gemini・無料枠" if gen_settings["image_engine"] == "gemini"
        else f"Higgsfield: {gen_settings['image_app'] or '既定'}"
    )
    await send_as(orch, cid, f"🎨 絵コンテ画像を生成中…（{engine_label}）")
    images = []
    for i, sc in enumerate(p["scenes"], 1):
        if not _alive(cid, p):  # !stop で中止された
            return
        url = None
        # ① まず Gemini（無料枠）で生成 → Discordに添付し、そのCDN URLを動画化に使う
        if gen_settings["image_engine"] == "gemini":
            try:
                data = await asyncio.to_thread(_gemini_generate_image_sync, sc)
                url = await send_image_bytes(cid, f"シーン{i}: {sc}", data, f"scene{i}.png")
            except Exception as e:  # noqa: BLE001
                print(f"[gemini_image] シーン{i} 失敗: {str(e)[:200]}")
                if _is_quota_error(e):
                    await send_as(orch, cid, "⚠️ Gemini画像生成の無料枠上限。Higgsfieldに切替えます…")
                elif HF_AVAILABLE:
                    await send_as(orch, cid, f"⚠️ シーン{i}: Gemini失敗 → Higgsfieldで再試行…")
        # ② フォールバック：Higgsfield MCP の最適モデル（クレジット消費）
        if url is None:
            url = await _mcp_gen_and_wait(sc, media_type="image", model=None)
            if url:
                await send_as(orch, cid, f"シーン{i}: {sc}\n{url}")
        if url is None:
            await send_as(orch, cid, f"⚠️ シーン{i} の画像を生成できませんでした。")
            continue
        images.append(url)
    if not _alive(cid, p):
        return
    p["images"] = images
    await send_as(
        orch, cid,
        "———\n下のボタン、または「OK」で承認。直すならテキストで指示してください。",
        view=ApprovalView(cid),
    )


async def _pipeline_edit(cid, feedback=""):
    p = projects[cid]
    if not HF_AVAILABLE:
        await send_as(orch, cid, f"⚠️ Higgsfield が使えません: {HF_IMPORT_ERROR}")
        return
    if not p.get("images"):
        await send_as(orch, cid, "⚠️ 絵コンテ画像がありません。先に絵コンテを承認してください。")
        return
    p["round"] += 1
    await send_as(
        orch, cid,
        f"🎞️ 編集（動画化）… チェック {p['round']}/{MAX_EDIT_ROUNDS} 回目（Higgsfield）",
    )
    videos = []
    for i, img in enumerate(p["images"], 1):
        if not _alive(cid, p):  # !stop で中止された
            return
        try:
            vurl = await hf_wrapper.generate_video(
                img, prompt=(feedback or p["topic"]), model=gen_settings["video_app"]
            )
            videos.append(vurl)
            await send_as(orch, cid, f"シーン{i} 動画: {vurl}")
        except Exception as e:  # noqa: BLE001
            if _is_credit_error(e):
                await send_as(orch, cid, CREDIT_MSG)
                return
            await send_as(orch, cid, f"⚠️ シーン{i} の動画生成に失敗: {e}")
    if not _alive(cid, p):
        return
    p["videos"] = videos
    remaining = MAX_EDIT_ROUNDS - p["round"]
    if remaining <= 0:
        await send_as(
            orch, cid,
            "———\n編集チェック最終回です。下のボタン、または「OK」で完成にしてください。",
            view=ApprovalView(cid),
        )
    else:
        await send_as(
            orch, cid,
            f"———\n下のボタン、または「OK」で完成。直すならテキストで指示（あと{remaining}回まで修正可）。",
            view=ApprovalView(cid),
        )


async def pipeline_start(cid, topic):
    projects[cid] = {
        "stage": "plan", "topic": topic, "plan": "",
        "scenes": [], "images": [], "videos": [], "round": 0,
    }
    await send_as(orch, cid, f"🎬 プロジェクト開始：「{topic}」\nまず構成案を作ります…")
    await _pipeline_plan(cid)


async def pipeline_reply(cid, text):
    """進行中プロジェクトへの返信（承認 or 修正指示）を処理する。"""
    p = projects.get(cid)
    if p is None:  # !stop / !cancel 済み
        return
    stage = p["stage"]
    approve = _is_approve(text)

    if stage == "plan":
        if approve:
            p["stage"] = "storyboard"
            await _pipeline_storyboard(cid)
        else:
            await _pipeline_plan(cid, feedback=text)
    elif stage == "storyboard":
        if approve:
            p["stage"] = "edit"
            p["round"] = 0
            await _pipeline_edit(cid)
        else:
            await _pipeline_storyboard(cid, feedback=text)
    elif stage == "edit":
        if approve:
            await send_as(orch, cid, "✅ 完成です！お疲れさまでした🎉")
            projects.pop(cid, None)
        elif p["round"] >= MAX_EDIT_ROUNDS:
            await send_as(
                orch, cid,
                "編集チェックは3回までです。「OK」で完成にしてください。",
            )
        else:
            await _pipeline_edit(cid, feedback=text)


# ---------------------------------------------------------------------------
# エージェントモード（プラン承認型・追加SDK不要 / Python 3.9でも動く）
#   !agent タスク → Claudeが「実行プラン」を提示 → [✅許可]で実行 / [❌拒否]で中止
#   ※ 許可するとフル権限で実行されるので、プランを確認してから許可すること。
# ---------------------------------------------------------------------------
class PermissionView(discord.ui.View):
    """[✅許可][❌拒否]。押すと future を解決する。実行者のみ操作可。"""

    def __init__(self, future, owner_id):
        super().__init__(timeout=300)
        self.future = future
        self.owner_id = owner_id

    async def _resolve(self, interaction, value):
        if self.owner_id and interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "この承認はコマンドを実行した本人だけが操作できます。", ephemeral=True
            )
            return
        if not self.future.done():
            self.future.set_result(value)
        for c in self.children:
            c.disabled = True
        try:
            await interaction.response.edit_message(view=self)
        except Exception:  # noqa: BLE001
            pass
        self.stop()

    @discord.ui.button(label="✅ 許可", style=discord.ButtonStyle.success)
    async def allow(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, True)

    @discord.ui.button(label="❌ 拒否", style=discord.ButtonStyle.danger)
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._resolve(interaction, False)

    async def on_timeout(self):
        if not self.future.done():
            self.future.set_result(False)


# テキストでの承認/却下（ボタンを押さなくても「許可」「拒否」の返信で反応できる）
_pending_approvals = {}  # cid -> (future, owner_id)

# 承認/拒否は自然な言い回しで受け取る。決まった単語の完全一致だけにしていたため、
# 「それでお願い」が承認と認識されず5分後に自動中止される事故が実際に起きた。
# 記号や語尾のゆれを落としてから全体一致で判定する（長い新規依頼は承認にしない）。
_APPROVE_RE = re.compile(
    "^("
    "はい|うん|ええ|そう|ok|okay|おk|おけ|オーケー|おっけ|了解|承知|承認|許可|"
    "いい|いいよ|いいね|いいです|よし|よろしく|"
    "それ|それで|それでいい|そのまま|これで|これでいい|"
    "進めて|進めよう|やって|やろう|やりましょう|お願い|頼む|頼みます|"
    "go|ゴー|実行|start|オッケ"
    ")"
    "(で|でも|よ|ね|な|わ|ぞ|です|ます|して|しといて|ください|下さい|"
    "しま(す|しょう)|お願い(します)?|でお願い(します)?)*$", re.I
)
_DENY_RE = re.compile(
    "^("
    "いや|いいえ|ううん|やめ|止め|とめ|中止|キャンセル|cancel|no|"
    "だめ|ダメ|駄目|却下|拒否|違う|ちがう|なし|ストップ|stop|"
    "やっぱやめ|やっぱりやめ|いらない|結構"
    ")"
    "(で|よ|ね|る|た|て|ます|です|して|ください|下さい|とく| okay)*$", re.I
)


def _norm_reply(text):
    """返事から記号・空白を落として判定しやすくする。"""
    return re.sub(r"[\s、。．，・！？!?.…〜~ー\-]+", "", text or "").lower()


# 確認が「新しい確認に置き換わった」ことを表す印（拒否とは別物）。
SUPERSEDED = object()


def _set_pending(cid, fut, owner_id):
    """新しい確認を出す（1チャンネル1件に保つ）。
    先に出ていた確認は自動的に中止扱いにする。これをしないと、
    古い方がタイムアウトした時に【新しい方の受付を消してしまい】、
    『OKと言っても始まらない → 数分後に急にやめましたと出る』が起きる。"""
    old = _pending_approvals.get(cid)
    if old and not old[0].done():
        # 「拒否された」ではなく「新しい確認に置き換わった」。区別しないと
        # 言い直すたびに『🛑 やめました』が会話に割り込む（実際に起きた）。
        old[0].set_result(SUPERSEDED)
    _pending_approvals[cid] = (fut, owner_id)


def _clear_pending(cid, fut):
    """自分が出した確認だけを片付ける（他の確認の受付を消さない）。"""
    cur = _pending_approvals.get(cid)
    if cur and cur[0] is fut:
        del _pending_approvals[cid]


def _try_text_approval(cid, user_id, content):
    """承認待ちがあるとき、テキストの「許可/拒否」でも解決する。
    承認=True / 却下=False / 対象外=None を返す。"""
    entry = _pending_approvals.get(cid)
    if not entry:
        return None
    fut, owner_id = entry
    if fut.done() or (owner_id and user_id != owner_id):
        return None
    norm = _norm_reply(content)
    if not norm or len(norm) > 24:
        return None      # 長い発言は新しい依頼とみなす（誤承認の防止）
    if _DENY_RE.match(norm):      # 否定を先に見る（「やめて」を承認と誤らないため）
        fut.set_result(False)
        return False
    if _APPROVE_RE.match(norm):
        fut.set_result(True)
        return True
    return None


async def _run_claude_exec(task, timeout=600, model=None):
    """承認済みタスクをフル権限で実行し、標準出力を返す。
    重い処理なので同時に1本まで。以前は無制限に起動できたため、
    調査・自己改修・生成の投入が重なるとMacのCPUを奪い合い、
    会話の返事まで遅くなっていた。
    model を指定すると、会話用とは別のモデルで実行できる
    （手順が決まっている作業は速いモデルの方が体感が良い）。"""
    async with _sem("exec", EXEC_CONCURRENCY):
        return await _claude_exec_run(task, timeout, model)


async def _claude_exec_run(task, timeout, model=None):
    args = ["--model", model] if model else _model_args()
    proc = await asyncio.create_subprocess_exec(
        CLAUDE_BIN, "-p", "--dangerously-skip-permissions", *args, task,
        stdin=asyncio.subprocess.DEVNULL,   # 端末から読ませない（固まるため）
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=BASE_DIR,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await _reap(proc)
        return "⚠️ 実行がタイムアウトしました。"
    if proc.returncode != 0:
        # 事故：「⚠️ 実行に失敗:」と理由が空のまま届き、何が起きたか分からなかった。
        # claude CLI は理由を標準出力側に出すことがあるので、両方を見る。
        _why = ((err.decode() or "").strip()
                or (out.decode() or "").strip()
                or f"終了コード {proc.returncode}（出力なし）")
        return f"⚠️ 実行に失敗: {_why[:400]}"
    return out.decode().strip() or "(完了・出力なし)"


async def _confirm(message, cid, summary, plan, cost="", engine="", eta=""):
    """作業に入る前に『こう理解した／これをやる』を提示して同意を得る（反復確認）。
    [✅許可] ボタン、または「OK」「はい」などの返信で開始。
    「やめて」や5分の無反応で中止する。CONFIRM_BEFORE_WORK=0 で無効化できる。
    engine には『何で作るか』を書く。同じ「サムネ」でもクロード(HTML)と
    Gemini(画像生成)で出来上がりが全く違うため、始める前に見せて選べるようにする。"""
    if not CONFIRM_BEFORE_WORK:
        return True
    fut = asyncio.get_running_loop().create_future()
    owner_id = getattr(message.author, "id", None)
    _set_pending(cid, fut, owner_id)
    try:
        view = PermissionView(fut, owner_id)
    except Exception as e:  # noqa: BLE001
        print(f"[confirm] ボタンを作れないため文字承認のみで続行: {str(e)[:120]}")
        view = None
    await send_as(
        orch, cid,
        "🔎 **確認させてください**\n"
        f"・ご依頼の理解: {summary}\n"
        + (f"・**何で作るか**: {engine}\n" if engine else "")
        + f"・これからやること: {plan}\n"
        + (f"・かかる時間: {eta}\n" if eta else "")
        + (f"・コスト: {cost}\n" if cost else "")
        + "これで進めていいですか？ [✅許可] か「**OK**」で開始します"
        "（[❌拒否]・「やめて」で中止／5分で自動中止）"
        + (f"\n※違うもので作るなら「{_engine_switch_hint(engine)}」と言ってください"
           if engine else ""),
        view=view,
    )
    try:
        approved = await asyncio.wait_for(fut, timeout=310)
    except asyncio.TimeoutError:
        approved = False
    finally:
        _clear_pending(cid, fut)
    if approved is SUPERSEDED:
        return False        # 言い直された分の確認。黙って退く
    if not approved:
        await send_as(orch, cid, "🛑 やめました。言い直してもらえれば作り直します。")
        add_history(cid, "Orchestrator", f"（「{summary}」の作業を中止した）")
    return approved


async def _confirm_then(message, cid, summary, plan, factory, cost="", engine="",
                        eta=""):
    """確認を取ってから実際の作業を始める。factory は承認後にコルーチンを作る関数
    （先に作ると中止時に未実行のまま警告が出るため、必ず遅延生成する）。"""
    if await _confirm(message, cid, summary, plan, cost, engine, eta):
        await factory()


def _gate(message, cid, summary, plan, factory, label, cost="", engine=""):
    """確認つきで作業を起動する（呼び出し側は1行で済む）。"""
    async def _go():
        # 承認が下りた瞬間から計り直す。ここを分けないと「本人が返事をするまでの
        # 時間」が所要時間として記録され、見積もりが実態とかけ離れる。
        _start_work(cid, label)
        await factory()

    return _spawn(
        _confirm_then(message, cid, summary, plan, _go, cost, engine,
                      _eta_hint(label)),
        cid, label, gated=True,
    )


async def run_claude_agent(cid, task, owner_id):
    """プラン承認型：計画を提示→Discordで承認→実行。SDK不要。"""
    # ① 計画（実行せず、やることの計画をテキストで）
    plan_prompt = (
        "次のタスクを実行するとしたら、行う手順やコマンドの【計画】だけを日本語で"
        "箇条書きにしてください。まだ実行はしないこと。\n\nタスク: " + task
    )
    try:
        plan = await run_claude_cli(plan_prompt)
    except Exception as e:  # noqa: BLE001
        return f"⚠️ 計画の作成に失敗: {str(e)[:300]}"

    # ② Discordで承認（ボタン or テキストの「許可/拒否」）
    fut = asyncio.get_running_loop().create_future()
    _set_pending(cid, fut, owner_id)
    await send_as(
        orch, cid,
        f"🤖 タスク: {task}\n\n📋 実行プラン:\n{plan[:1500]}\n\n"
        "この計画で実行しますか？ [✅許可] を押すか「**許可**」と返信すると "
        "**Mac上で実際に実行**します（本人のみ・5分で自動却下）。",
        view=PermissionView(fut, owner_id),
    )
    try:
        approved = await asyncio.wait_for(fut, timeout=310)
    except asyncio.TimeoutError:
        approved = False
    finally:
        _clear_pending(cid, fut)
    if approved is SUPERSEDED:
        approved = False      # 印を承認と取り違えて実行しないための保険
    if not approved:
        return "🛑 却下されました。実行しません。"

    # ③ 承認 → 実行（承認済みのためフル権限）
    await send_as(orch, cid, "▶️ 承認されました。実行します…")
    # CLAUDE.md 由来の再起動案内や内部ナレーションは、そのまま見せない
    return _strip_cli_boilerplate(await _run_claude_exec(task)) or "(完了・出力なし)"


async def _run_agent_task(cid, task, owner_id):
    try:
        result = await run_claude_agent(cid, task, owner_id)
    except Exception as e:  # noqa: BLE001
        result = f"⚠️ エージェント実行に失敗: {str(e)[:400]}"
    await send_as(orch, cid, result)


# ---------- 自己改修＆自己再起動（Discord内で完結） ----------
SELF_FILE = Path(os.path.abspath(__file__))
SELF_BACKUP = SELF_FILE.with_suffix(".py.bak")
# 自己改修で書き換えてよいファイル。本体だけだと「テストを1件足す」ができず、
# 巻き戻しも本体しか戻らなかったため、テストと説明書まで範囲に含める。
SELF_EDITABLE = (
    "ai_group_chat.py", "test_routing.py", "test_phrasing.py",
    "simulate.py", "README.md",
)
SELF_BACKUP_DIR = SELF_FILE.parent / ".selffix_backup"


def _snapshot_self():
    """改修前に、書き換え対象のファイルを丸ごと退避する。
    返り値: 退避できたファイル名の集合（＝改修前に存在したファイル）。"""
    if SELF_BACKUP_DIR.exists():
        shutil.rmtree(SELF_BACKUP_DIR)
    SELF_BACKUP_DIR.mkdir(parents=True)
    saved = set()
    for name in SELF_EDITABLE:
        src = SELF_FILE.parent / name
        if src.exists():
            shutil.copy2(src, SELF_BACKUP_DIR / name)
            saved.add(name)
    return saved


def _restore_self(saved):
    """退避したファイルを全部戻す（改修前の状態に完全に復元する）。
    改修中に新しく作られたファイルは、元々無かったので削除する。"""
    restored = []
    for name in SELF_EDITABLE:
        dst = SELF_FILE.parent / name
        bak = SELF_BACKUP_DIR / name
        if name in saved:
            if bak.exists():
                shutil.copy2(bak, dst)
                restored.append(name)
        elif dst.exists():
            dst.unlink()
            restored.append(f"{name}(削除)")
    return restored


def _changed_self_files(saved):
    """改修で実際に中身が変わったファイル名の一覧（報告と git add に使う）。"""
    out = []
    for name in SELF_EDITABLE:
        dst, bak = SELF_FILE.parent / name, SELF_BACKUP_DIR / name
        if not dst.exists():
            continue
        if name not in saved or not bak.exists():
            out.append(name)
        elif dst.read_bytes() != bak.read_bytes():
            out.append(name)
    return out


RESTART_MARKER = HISTORY_DIR / "restart_notify.json"


async def _sync_to_origin():
    """GitHubに新しいコードがあれば取り込む（再起動前に呼ぶ）。
    ローカルの変更は「捨てずに逃がす」方針:
      ・未コミットの変更 → git stash に退避
      ・未プッシュのコミット → まずpush、駄目なら rescue/ ブランチに退避
    以前は上記があるとスキップしていたため、最新コードが何日も届かず
    「直したはずの不具合が直らない」状態が実際に起きた。必ず最新にする。"""
    rc, branch = await _git_self(["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch.strip()
    if rc != 0 or not branch:
        return "ブランチ不明のためスキップ"
    rc, _ = await _git_self(["fetch", "origin", branch])
    if rc != 0:
        return "fetch失敗のためスキップ（オフライン？）"
    rc, dirty = await _git_self(["status", "--porcelain", "--untracked-files=no"])
    stashed = ""
    if rc == 0 and dirty.strip():
        # 未コミットの変更があっても、退避（stash）してから同期する。
        # 以前はここでスキップしており、そのせいで最新コードが届かなかった。
        stamp = datetime.now(JST).strftime("%Y%m%d-%H%M")
        rc_s, _ = await _git_self(["stash", "push", "-u", "-m", f"auto-{stamp}"])
        if rc_s != 0:
            return "未コミットの変更を退避できないためスキップ"
        stashed = "（未コミットの変更は git stash に退避）"
    rc, ahead = await _git_self(["rev-list", "--count", f"origin/{branch}..HEAD"])
    if rc != 0:
        return "状態確認失敗のためスキップ"
    extra = ""
    if int(ahead.strip() or "0") > 0:
        # ローカルにしか無いコミットがある。以前はここでスキップしていたが、
        # そのせいで最新コードが永久に届かない状態が続いた（修正が反映されない）。
        # まずプッシュを試し、駄目なら退避ブランチに逃がしてから同期する。
        # どちらにせよ作業は失われず、コードは必ず最新になる。
        rc_push, _ = await _git_self(["push", "origin", "HEAD"])
        if rc_push == 0:
            extra = "（ローカルの変更はGitHubへ反映済み）"
        else:
            stamp = datetime.now(JST).strftime("%Y%m%d-%H%M")
            rescue = f"rescue/local-{stamp}"
            rc_b, _ = await _git_self(["branch", "-f", rescue, "HEAD"])
            if rc_b != 0:
                return "ローカル独自コミットを退避できないためスキップ"
            extra = f"（ローカルの変更は {rescue} に退避）"
    rc, behind = await _git_self(["rev-list", "--count", f"HEAD..origin/{branch}"])
    if rc != 0:
        return "状態確認失敗のためスキップ"
    n = int(behind.strip() or "0")
    if n == 0 and not extra and not stashed:
        return "既に最新"
    rc, out = await _git_self(["reset", "--hard", f"origin/{branch}"])
    if rc != 0:
        return "reset失敗のためスキップ"
    return ((f"最新コードを取得（{n}コミット）" if n else "既に最新") + extra + stashed)


# ---------- 直した内容を自動で取り込む ----------
# 本人の指摘：「こっちで設定してることとdiscord上の挙動が違う」。
# 原因は単純で、直してもDiscordの側は古いコードのまま動いていたこと。
# 実際に何度も、最新の修正が入る前の版を試して「まだ直ってない」となった。
# 手で「再起動して」と言わせない。新しいコードが出たら自分で取り込む。
AUTO_UPDATE_SEC = int(os.getenv("AUTO_UPDATE_SEC", "180"))


def _auto_update_on():
    return gen_settings.get("auto_update", True) and AUTO_UPDATE_SEC > 0


async def _remote_has_new_code():
    """GitHubに自分より新しいコードがあるか。(あるか, 件数) を返す。"""
    rc, branch = await _git_self(["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch.strip()
    if rc != 0 or not branch:
        return False, 0
    rc, _ = await _git_self(["fetch", "origin", branch])
    if rc != 0:
        return False, 0
    rc, out = await _git_self(
        ["rev-list", "--count", f"HEAD..origin/{branch}"])
    if rc != 0:
        return False, 0
    try:
        n = int((out or "0").strip())
    except ValueError:
        return False, 0
    return n > 0, n


def _safe_to_restart(cid):
    """いま入れ替わっても困らないか。作業中・確認待ちなら待つ。"""
    if _busy_tasks(cid) or _load_motion_job():
        return False
    if _pending_approvals:
        return False
    return True


async def _auto_update_loop():
    """新しいコードが出ていたら、手が空いた時に自分で取り込んで入れ替わる。"""
    while True:
        await asyncio.sleep(AUTO_UPDATE_SEC)
        try:
            if not _auto_update_on():
                continue
            cid = (int(gen_settings.get("trend_cid") or 0)
                   or TREND_CHANNEL_ID or _last_active_cid())
            if not cid:
                continue
            has_new, n = await _remote_has_new_code()
            if not has_new:
                continue
            if not _safe_to_restart(cid):
                print("[auto_update] 作業中なので次の機会に回す")
                continue
            await send_as(
                orch, cid,
                f"🆕 新しい修正が{n}件届いていたので取り込みます"
                "（数秒で戻ります／自動更新は「自動更新オフ」で止められます）。"
            )
            await _restart_self(cid, note="（自動更新）")
        except Exception as e:  # noqa: BLE001
            print(f"[auto_update] 失敗: {str(e)[:200]}")


def _last_active_cid():
    """直近に会話のあったチャンネル（自動更新の通知先）。"""
    best, best_t = 0, 0.0
    for cid, t in _last_seen_cid.items():
        if t > best_t:
            best, best_t = cid, t
    return best


_last_seen_cid = {}


async def _restart_self(cid, note=""):
    """自分自身を再起動する（プロセスを入れ替え。Mac操作不要）。
    再起動前にGitHubの最新コードを自動で取り込む＝Discordだけで更新が完結する。"""
    sync = await _sync_to_origin()
    print(f"[restart] コード同期: {sync}")
    if "スキップ" in sync:
        # 同期できない＝修正が届かないまま再起動を繰り返す事故（実際に発生）を防ぐ。
        # ローカル変更は自動で退避するようにしたので、ここに来るのは
        # 通信不良やgitの異常など、こちらでは直せないケースだけ。
        await send_as(
            orch, cid,
            f"⚠️ **注意: 最新コードを取得できません（{sync}）**\n"
            "このまま再起動しても修正は反映されません。"
            "ネットワークを確認して、もう一度「再起動して」と送ってください。"
            "それでも直らない場合は「ログ送って」で状況を共有してください。"
        )
    try:
        RESTART_MARKER.write_text(
            json.dumps({"cid": cid, "note": (note + f"（コード同期: {sync}）").strip()},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001
        print(f"[restart] マーカー保存失敗: {e}")
    await send_as(orch, cid, f"🔄 再起動します…（コード同期: {sync}／数秒で戻ります）")
    print("[restart] 自己再起動を実行")
    os.execv(sys.executable, [sys.executable, str(SELF_FILE)])


async def _selfcheck():
    """修正後の自分のコードを検証（構文＋インポート＋ルーティング回帰テスト）。
    自己改修が会話ルーティングを壊した場合、ここで検出して自動ロールバックさせる。"""
    checks = [
        [sys.executable, "-m", "py_compile", str(SELF_FILE)],
        [sys.executable, "-c", f"import {SELF_FILE.stem}"],
    ]
    for tf in ("test_routing.py", "test_phrasing.py", "simulate.py"):
        if (SELF_FILE.parent / tf).exists():
            checks.append([sys.executable, tf])
    for args in checks:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(SELF_FILE.parent),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            await _reap(proc)
            return False, "検証がタイムアウトしました"
        if proc.returncode != 0:
            detail = (err.decode(errors="replace") or out.decode(errors="replace")).strip()
            return False, f"[{' '.join(args[-1:])}] {detail[:400]}"
    return True, ""


_GIT_ENV = None


def _git_env():
    """gitを完全に非対話で動かすための環境変数。

    認証情報が無いと git は「Username:」の入力待ちで止まる。TTYが無いので
    誰も入力できず、そのまま90秒のタイムアウトになっていた
    （実際に「git push origin がタイムアウトしました（90秒）」が出た）。
    入力を求めない設定にすると、数秒で理由付きのエラーが返る。"""
    global _GIT_ENV
    if _GIT_ENV is None:
        e = dict(os.environ)
        e.update({
            "GIT_TERMINAL_PROMPT": "0",          # ユーザー名/パスワードを聞かない
            "GIT_ASKPASS": "echo",               # GUIの入力ダイアログも出さない
            "SSH_ASKPASS": "echo",
            "GCM_INTERACTIVE": "never",          # Git Credential Manager
            "GIT_HTTP_LOW_SPEED_LIMIT": "1000",  # 1KB/s を
            "GIT_HTTP_LOW_SPEED_TIME": "20",     # 20秒下回ったら諦める
        })
        _GIT_ENV = e
    return _GIT_ENV


def _git_fail_hint(msg):
    """gitの失敗理由を、次にやることが分かる言葉にする。"""
    low = (msg or "").lower()
    if ("could not read username" in low or "authentication failed" in low
            or "terminal prompts disabled" in low or "invalid username" in low
            or "password authentication is not supported" in low):
        # ここだけはDiscord内で直せない（Mac側の一度きりの設定）。
        # 行き止まりにしないため、直す手順を具体的に添える。
        return ("GitHubのログイン情報がMac側にありません。"
                "GitHubはパスワード認証を廃止したので、一度だけ登録が必要です。\n"
                "Macのターミナルで次を実行してください（ブラウザが開いて完了します）:\n"
                "`gh auth login`  ※未インストールなら先に `brew install gh`\n"
                "これが済めば、以後このエラーは出ません。")
    if "permission denied" in low or "publickey" in low:
        return "SSH鍵でGitHubに接続できていません。"
    if "could not resolve" in low or "connection" in low or "timed out" in low:
        return "ネットワークがGitHubに届いていません。"
    if "non-fast-forward" in low or "rejected" in low:
        return "リモートが先に進んでいます（取り込み直しが必要）。"
    return ""


async def _git_self(args, timeout=90):
    """自己改修のgit操作（ベストエフォート）。(returncode, 出力) を返す。
    通信不良の push/fetch で永久に固まらないよう必ずタイムアウトする。"""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=BASE_DIR,
        env=_git_env(),
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await _reap(proc)
        return 1, f"git {' '.join(args[:2])} がタイムアウトしました（{timeout}秒）"
    return proc.returncode, (out.decode(errors="replace") + err.decode(errors="replace")).strip()


async def _run_self_fix(cid, request, owner_id):
    """ボット自身のコードを Claude Code に修正させ、検証→承認→適用→自己再起動。
    検証失敗・却下時はバックアップから自動ロールバックする。"""
    await send_as(
        orch, cid,
        "🛠 ボット自身の改修として受け取りました。コードを修正して検証します（1〜3分ほど）…\n"
        "※もし「相談・質問だっただけ」なら、続けて『今のは相談』と送ってください。"
    )
    try:
        saved = _snapshot_self()
    except Exception as e:  # noqa: BLE001
        await send_as(orch, cid, f"⚠️ バックアップ作成に失敗したため中止します: {str(e)[:200]}")
        return

    task = (
        f"このフォルダの {SELF_FILE.name}（Discordボット本体）を、"
        "次の要望どおりに修正して。\n"
        f"要望: {request}\n"
        "注意: 既存の機能を壊さない最小限の変更にすること。\n"
        f"【編集してよいファイル】{('、'.join(SELF_EDITABLE))} だけ。"
        "これ以外のファイルは作成も変更もしないこと。\n"
        "動作が変わる修正をしたときは、test_routing.py か test_phrasing.py に"
        "回帰テストを1件足すこと（次に同じ壊れ方をしないため）。\n"
        f"修正後に `python3 -m py_compile {SELF_FILE.name}` で構文チェックし、"
        "エラーがあれば直すこと。最後に修正内容の要約を3行以内で出力して。"
    )
    result = _strip_cli_boilerplate(await _run_claude_exec(task))

    ok, detail = await _selfcheck()
    if not ok:
        back = _restore_self(saved)
        await send_as(
            orch, cid,
            f"⚠️ 修正後のコードが検証に失敗したため、自動で元に戻しました"
            f"（{'、'.join(back) or '変更なし'}）。\n"
            f"エラー: {detail[:500]}"
        )
        return

    changed = _changed_self_files(saved)
    _, diffstat = await _git_self(["diff", "--stat", "--"] + list(SELF_EDITABLE))
    fut = asyncio.get_running_loop().create_future()
    _set_pending(cid, fut, owner_id)
    await send_as(
        orch, cid,
        f"📋 修正完了・検証OKです。\n\n【修正内容】\n{result[:1000]}\n\n"
        f"【変更したファイル】\n{'、'.join(changed) or '(なし)'}\n\n"
        f"【変更規模】\n{diffstat[:300] or '(差分なし)'}\n\n"
        "適用して再起動しますか？ [✅許可] を押すか「**許可**」と返信でOK"
        "（❌または「拒否」で元のコードに戻します・5分で自動却下）",
        view=PermissionView(fut, owner_id),
    )
    try:
        approved = await asyncio.wait_for(fut, timeout=310)
    except asyncio.TimeoutError:
        approved = False
    finally:
        _clear_pending(cid, fut)
    if approved is SUPERSEDED:
        approved = False      # 印を承認と取り違えて実行しないための保険
    if not approved:
        back = _restore_self(saved)
        await send_as(
            orch, cid,
            f"🛑 元のコードに戻しました（{'、'.join(back) or '変更なし'}）。適用していません。"
        )
        return

    # 記録用にコミット＆プッシュ（リモートが進んでいれば追いついてから押す）
    ok_push, why = await _push_paths(
        changed or [SELF_FILE.name], f"Discordからの自己改修: {request[:60]}"
    )
    note = "修正を適用しました。"
    note += "（GitHubへプッシュ済み）" if ok_push else (
        f"（⚠️GitHubへのプッシュに失敗＝ローカルのみ: {why[:120]}。"
        "次のコード同期で消える可能性があるため、この修正が重要なら "
        "Claude Code 側でも同じ修正を反映してください）"
    )
    shutil.rmtree(SELF_BACKUP_DIR, ignore_errors=True)   # 適用済みなので退避は不要
    add_history(cid, "Orchestrator", f"（自己改修を適用: {request[:100]}）")
    await _restart_self(cid, note)


# 自然言語での「再起動」。短いフレーズだけを拾う（誤爆防止）。
_RESTART_PHRASES = {
    "再起動", "再起動して", "リスタート", "リスタートして", "restart", "リブート", "リブートして",
}


def _is_restart_phrase(content):
    return content.strip().rstrip("。.!！?？ 　").lower() in _RESTART_PHRASES


# 自然言語での「停止」。短い停止フレーズだけを拾う（誤爆防止）。
_STOP_PHRASES = {
    "止めて", "止めて。", "とめて", "やめて", "やめ", "ストップ", "すとっぷ",
    "stop", "中止", "中止して", "キャンセル", "cancel", "ストップして",
    "止まれ", "停止", "停止して",
}


def _is_stop_phrase(content):
    norm = content.strip().rstrip("。.!！?？ 　").lower()
    return norm in _STOP_PHRASES


async def _do_stop(message, cid):
    state["stop"] = True
    # 文字起こしや切り出しが走っていたら、それも止める。
    # 止められないと、CPUを占有したまま「再起動」すら届かなくなる。
    killed = stop_heavy_procs()
    note = f"（重い処理を{killed}件止めました）" if killed else ""
    if projects.pop(cid, None):
        await message.channel.send(
            f"⏹️ 進行中の作業を停止しました{note}。以降は通常の会話に戻ります。"
        )
    else:
        await message.channel.send(f"⏹️ 停止しました{note}。")


_trend_task_started = False


@orch.event
async def on_ready():
    global _trend_task_started
    print(f"オーケストレーター起動: {orch.user}")
    # 起動時のセルフテスト（ルーティング＋E2E）。失敗したら復帰チャンネルに警告。
    routing_ok = True
    for tf in ("test_routing.py", "test_phrasing.py", "simulate.py"):
        try:
            r = await asyncio.to_thread(
                subprocess.run, [sys.executable, tf],
                capture_output=True, text=True, timeout=180, cwd=BASE_DIR,
            )
            passed = r.returncode == 0
            routing_ok = routing_ok and passed
            tail = (r.stdout or "").strip().splitlines()[-1] if r.stdout else ""
            print(f"[selftest] {tf}: {'OK' if passed else 'FAIL'} {tail}")
        except Exception as e:  # noqa: BLE001
            print(f"[selftest] {tf} 実行不可: {e}")

    # 自己再起動からの復帰なら、元のチャンネルに完了を知らせる
    if RESTART_MARKER.exists():
        try:
            d = json.loads(RESTART_MARKER.read_text(encoding="utf-8"))
            RESTART_MARKER.unlink()
            note = d.get("note") or ""
            warn = "" if routing_ok else "\n⚠️ 起動時セルフテストで異常検知。『システムチェック』推奨。"
            _cid = int(d["cid"])
            await send_as(orch, _cid, f"✅ 再起動完了！{note}{warn}".strip())
            # 再起動のたびに会話ログを共有しておく。ユーザーが覚えることを
            # 増やさずに、開発側が最新の状況を読めるようにするため
            # （「再起動して」はもともと日常的に使っている操作）。
            _last_autolog.pop(_cid, None)      # 起動時は間隔制限を無視する
            _track(asyncio.create_task(_autoshare_log(_cid, "起動時")))
        except Exception as e:  # noqa: BLE001
            print(f"[restart] 復帰通知失敗: {e}")
    if not _trend_task_started:
        _trend_task_started = True
        _track(asyncio.create_task(_auto_update_loop()))
        _track(asyncio.create_task(_daily_trend_loop()))
        _track(asyncio.create_task(_gemini_recovery_loop()))
        _track(asyncio.create_task(_weekly_channel_loop()))
        # 再起動前に投入したモーション生成があれば、完了監視を再開する
        job = _load_motion_job()
        if job and job.get("cid") and time.time() - job.get("submitted_at", 0) < 3600:
            print("[motion] 進行中ジョブの完了監視を再開")
            _spawn(_watch_motion_job(int(job["cid"])), int(job["cid"]), "生成の完了監視")
        elif job:
            _clear_motion_job()  # 1時間超は古すぎるので破棄


@orch.event
async def on_message(message):
    """全メッセージの入口。予期せぬ例外を必ず捕捉してログ＋通知する
    （沈黙して失敗＝毎回スクショ待ち、を防ぐ自己修復の要）。"""
    if message.author.bot:
        return
    cid = message.channel.id
    before_sent = _sent_count.get(cid, 0)
    before_fired = _fired_seq.get(cid, 0)
    try:
        await _dispatch_message(message)
        await _rescue_if_silent(message, cid, before_sent, before_fired)
    except Exception as e:  # noqa: BLE001
        summary = _log_error(f"on_message: {message.content[:80]}", e)
        try:
            await message.channel.send(
                f"⚠️ 処理中にエラーが出ました（記録済み）: {summary}\n"
                "『エラー教えて』で詳細、『システムチェック』で自己診断できます。"
            )
        except Exception:  # noqa: BLE001
            pass
        _track(asyncio.create_task(_autoshare_log(message.channel.id, "on_message")))


async def _rescue_if_silent(message, cid, before_sent, before_fired):
    """発言を受けたのに、何の機能にも流れず、何も送っていない場合の受け皿。
    「反応しない」の正体が毎回分からなかったので、取りこぼしを記録として
    必ず残し、そのうえで普通の会話として答え直す（黙って終わらせない）。"""
    if _sent_count.get(cid, 0) != before_sent:
        return                              # 何かは答えている
    if _fired_seq.get(cid, 0) != before_fired:
        return                              # どこかの機能が引き受けた
    if _busy_tasks(cid):
        return                              # 裏で作業中（終われば結果が出る）
    said = (message.content or "").strip()
    if not said and not message.attachments:
        return
    _log_error("取りこぼし", RuntimeError(f"どの経路にも流れなかった: {said[:80]}"))
    _fired(cid, "取りこぼし→会話で答え直す", said)
    hist = get_history(cid)
    if not hist or (hist[-1][1] or "") != said:
        add_history(cid, message.author.display_name, said)
    try:
        await _handle_orchestrator(message, cid)
    except Exception as e:  # noqa: BLE001
        _log_error("取りこぼしの答え直しに失敗", e)


# ---------- !コマンド（表引き。各実装は (message, cid, 引数) を受ける）----------
async def _cmd_project(message, cid, arg):
    if not arg:
        await message.channel.send("使い方: !project お題（例: !project 犬が主役の30秒CM）")
        return
    if projects.get(cid):
        await message.channel.send("進行中のプロジェクトがあります。!cancel で中止できます。")
        return
    _spawn(pipeline_start(cid, arg), cid, "映像プロジェクト")


async def _cmd_cancel(message, cid, arg):
    await message.channel.send(
        "🛑 プロジェクトを中止しました。" if projects.pop(cid, None)
        else "進行中のプロジェクトはありません。"
    )


async def _cmd_agent(message, cid, arg):
    if not arg:
        await message.channel.send(
            "使い方: !agent やってほしいこと（例: !agent このフォルダのファイル一覧を出して）\n"
            "※ Claudeがコマンド実行やファイル編集をする前に、[✅許可][❌拒否] ボタンで確認します。"
        )
        return
    await message.channel.send(f"🤖 エージェント開始: {arg}")
    _spawn(_run_agent_task(cid, arg, message.author.id), cid, "エージェント実行")


async def _cmd_profile(message, cid, arg):
    p = _load_profiles()
    if p:
        await send_long(message.channel, p, "🧠 ")
    else:
        await message.channel.send(
            "まだプロファイルはありません（会話がたまると自動で作られます）。"
        )


async def _cmd_trend(message, cid, arg):
    if not YOUTUBE_API_KEY:
        await message.channel.send(
            "YOUTUBE_API_KEY が未設定です。Google Cloud Console で YouTube Data API v3 の"
            "APIキーを発行し、.env に追加してください（README参照）。"
        )
        return
    _spawn(_run_trend_study(cid, arg or None), cid, "YouTubeリサーチ")


async def _cmd_search(message, cid, arg):
    if not arg:
        await message.channel.send("使い方: !search 調べたいこと")
        return
    async with message.channel.typing():
        ctx = await web_search_context(arg)
        if not ctx:
            await send_as(orch, cid, "🔍 検索結果が取得できませんでした。")
            return
        ans = await run_claude_cli(
            "次のWeb検索結果を根拠に、質問へ日本語で簡潔に答え、参考URLも示す。\n\n"
            f"質問: {arg}\n\n{ctx}\n\n回答:"
        )
        await send_as(orch, cid, ans)


async def _cmd_short(message, cid, arg):
    _spawn(_run_short(message, arg or None), cid, "ショート制作")


async def _cmd_talk(message, cid, arg):
    if state["running"]:
        await message.channel.send("自動トークが進行中です。!stop で止められます。")
        return
    topic = arg or "自由なテーマで雑談"
    add_history(cid, message.author.display_name, f"（お題）{topic} について話して")
    await message.channel.send(
        f"🎙️ お題「{topic}」で ClaudeとGemini が最大 {MAX_TURNS} 発言 話します"
    )
    _spawn(run_auto(cid, topic), cid, "自動トーク")


_COMMANDS = {
    "!project": _cmd_project,
    "!cancel": _cmd_cancel,
    "!agent": _cmd_agent,
    "!profile": _cmd_profile,
    "!trend": _cmd_trend,
    "!search": _cmd_search,
    "!short": _cmd_short,
    "!talk": _cmd_talk,
}


async def _dispatch_message(message):
    content = message.content.strip()
    # テキストまたは添付ファイルがない場合は無視
    if not content and not message.attachments:
        return
    cid = message.channel.id
    _last_seen_cid[cid] = time.time()   # 自動更新の通知先に使う

    # 初回のみ：導入前の過去ログをDiscordから取り込む（バックグラウンド）
    if cid not in _import_started:
        _track(asyncio.create_task(_backfill_channel_history(message.channel)))

    # 不具合の訴えを検知したら、頼まれる前にログを共有しておく。
    # 返事はそのまま普通に続ける（ここでは止めない）。
    if _looks_trouble(content):
        _track(asyncio.create_task(_autoshare_log(cid, content[:60])))

    # 会話モデルの切替・確認（AI判定より前に拾う。AIに任せると
    # 「コードを直さないと変えられない」と答えてしまい、実際そうなった）
    _mdl = _match_claude_model(content)
    if _mdl is not None:
        _fired(cid, "会話モデルの切替", content)
        gen_settings["claude_model"] = _mdl[0]
        _save_gen_settings()
        add_history(cid, message.author.display_name, content)
        await message.channel.send(
            f"🔀 会話モデルを **{_mdl[1]}** にしました（次の発言から反映・再起動不要）。"
        )
        return
    # iCloudの共有リンクは、そのままでは中身を取れない。
    # 「リンクなら取りに行ける」と案内して貼ってもらい、何も起きなかった事故があった。
    if _ICLOUD_LINK_RE.search(content) and not message.attachments:
        _fired(cid, "iCloud共有リンク", content)
        add_history(cid, message.author.display_name, content)
        _set_pending_do(cid, "動画の場所", content)
        await message.channel.send(
            "そのiCloudのリンクは**ブラウザで開くページ**なので、中身を直接"
            "取りに行けません（貼ってもらっても何も始められませんでした、ごめん）。\n"
            "**iPhoneのファイルアプリの道順をそのまま貼ってください。**\n"
            "・ファイルアプリでその動画を長押し → 「情報」→ 場所の欄を長押しでコピー\n"
            "・または、動画を長押し →「情報」に出る\n"
            "　`iCloud Drive ▸ マルサヂ ▸ AI ▸ 動画` のような行をそのまま貼る\n"
            "（iCloud DriveはMacにも同期されているので、その道順から直接扱えます）"
        )
        return

    # 「やって」だけの返事。何が足りなくて止まっているかを覚えているので、
    # 同じ説明を繰り返さずに、先に進めるか・何が要るかをはっきり返す。
    # 【重要】確認待ちがある時は絶対に横取りしない。
    # 「やって」「お願い」「よろしく」は承認の返事でもあるので、ここで
    # 拾ってしまうと確認が承認されず、いつまでも作業が始まらない。
    if _BARE_GO_RE.match(content.strip()) and cid not in _pending_approvals:
        _pend = _get_pending_do(cid)
        if _pend:
            _fired(cid, f"やって（{_pend['need']}待ち）", content)
            add_history(cid, message.author.display_name, content)
            await message.channel.send(
                f"始めたいけど、**{_pend['need']}**がまだ分からないので動かせない。\n"
                "動画の切り抜きなら、iPhoneのファイルアプリの道順"
                "（`iCloud Drive ▸ …`）をそのまま貼るか、"
                "動画をこのチャンネルに添付してください。\n"
                "別の作業なら「◯◯して」と一言で言ってもらえれば、そこから始めます。"
            )
            return

    _tg = _match_trend_genre(content)
    if _tg is not None:
        _fired(cid, "リサーチのジャンル設定", content)
        add_history(cid, message.author.display_name, content)
        _act, _genre = _tg
        if _act == "reset":
            gen_settings["trend_query"] = ""
            _save_gen_settings()
            await message.channel.send(
                "🔎 毎日のリサーチを**急上昇TOP100**（日本）に戻しました。"
            )
            return
        gen_settings["trend_query"] = _genre
        _save_gen_settings()
        _on, _h, _m, _ = _trend_conf()
        await message.channel.send(
            f"🔎 毎日のリサーチを「**{_genre}**」で伸びている動画に変えました"
            + (f"（毎日 {_h}:{_m:02d}）。\n" if _on else "（いまは停止中）。\n")
            + "・直近90日の動画をこのお題で検索し、再生数の多い順に見ます\n"
            "・前に分析した動画は飛ばすので、日を追うごとに知見が貯まります\n"
            "・急上昇に戻すなら「**リサーチを急上昇に戻して**」"
        )
        return

    _trd = _match_trend_schedule(
        content, recent_topic=time.time() - _trend_talk.get(cid, 0) < 900)
    if _trd is not None:
        _fired(cid, "毎日の自動リサーチの設定", content)
        add_history(cid, message.author.display_name, content)
        _trend_talk[cid] = time.time()
        _act, _h, _m = _trd
        if _act == "who":
            _who = re.search("クロード\\s*([123１２３])", content)
            _n = "123"["０１２３".find(_who.group(1)) - 1] if _who and \
                _who.group(1) in "１２３" else (_who.group(1) if _who else "1")
            gen_settings["trend_who"] = f"claude{_n}"
            _save_gen_settings()
            await message.channel.send(
                f"🔎 リサーチの担当を **クロード{_n}** にしました。"
                "毎日の自動リサーチも、その名前で結果を出します。"
            )
            return
        if _act == "ask":
            _on, _hh, _mm, _tcid = _trend_conf()
            await message.channel.send(
                f"📊 毎日の自動リサーチ: **{'ON' if _on else 'OFF'}**"
                + (f"（毎日 {_hh}:{_mm:02d} JST・このチャンネルに投稿）" if _on else "")
                + "\n「**毎日7時にYouTubeのTOP100をリサーチして**」で設定、"
                "「**毎日のリサーチやめて**」で停止できます。"
            )
            return
        if _act == "off":
            gen_settings["trend_on"] = False
            _save_gen_settings()
            await message.channel.send("📊 毎日の自動リサーチを**停止**しました。")
            return
        if not YOUTUBE_API_KEY:
            await message.channel.send(
                "⚠️ YouTubeのAPIキーが設定されていないので自動リサーチを始められません。"
                "実行環境の環境変数に `YOUTUBE_API_KEY` を入れてください"
                "（チャットに貼らないこと）。"
            )
            return
        gen_settings.update({"trend_on": True, "trend_hour": _h,
                             "trend_min": _m, "trend_cid": cid})
        _save_gen_settings()
        await message.channel.send(
            f"📊 毎日 **{_h}:{_m:02d}（JST）** に YouTube急上昇**TOP100**を"
            "リサーチして、このチャンネルに結果を投稿します。\n"
            "・100本のタイトル・再生数・タグを取得し、上位数本は実際に視聴して分析\n"
            "・勝ちパターンは学習して以降の企画に反映\n"
            "・無料です（YouTube APIとGeminiの無料枠のみ／クレジットは使いません）\n"
            "時刻を変えるなら「**毎日9時半にして**」、"
            "止めるなら「**毎日のリサーチやめて**」と送ってください。"
        )
        return
    _au = _match_auto_update(content)
    if _au is not None:
        _fired(cid, "自動更新の切替", content)
        gen_settings["auto_update"] = _au
        _save_gen_settings()
        add_history(cid, message.author.display_name, content)
        await message.channel.send(
            "🆕 修正が出たら**自動で取り込む**ようにしました"
            f"（{AUTO_UPDATE_SEC // 60}分おきに確認・作業中は待ちます）。"
            if _au else
            "🆕 自動更新を**止めました**。反映したい時は「再起動して」と送ってください。"
        )
        return
    _hfm = _match_hf_mode(content)
    if _hfm is not None:
        _fired(cid, "ヒッグスフィールドの使い方の切替", content)
        gen_settings["hf_mode"] = _hfm[0]
        _save_gen_settings()
        add_history(cid, message.author.display_name, content)
        await message.channel.send(
            f"🧩 Higgsfield は **{_hfm[1]}** ようにしました。\n"
            + ("※図・表・サムネなど文字が主役のものは、これまでどおり"
               "クロードがHTMLで作ります（無料）。動画は Higgsfield でしか"
               "作れないので、その時は確認を出します。"
               if _hfm[0] == "explicit" else
               "※クレジットを使う場面が増えます。戻すなら"
               "「ヒッグスフィールドは使わないで」と送ってください。")
        )
        return
    _lead = _match_casual_lead(content)
    if _lead is not None:
        _fired(cid, "雑談担当の切替", content)
        gen_settings["casual_lead"] = _lead[0]
        _save_gen_settings()
        add_history(cid, message.author.display_name, content)
        await message.channel.send(
            f"🗣 雑談の返事は **{_lead[1]}** が担当します（次の発言から反映）。\n"
            "※作業（生成・デザイン・調査）の担当は依頼内容で決まるので変わりません。"
        )
        return

    if (_MODEL_ASK_RE.search(content) and not _NOT_GEN_MODEL_RE.search(content)
            and not re.search("画像|動画|映像|生成", content)):
        _fired(cid, "会話モデルの確認", content)
        await message.channel.send(
            f"🔀 いまの会話モデル: **{_current_model_label()}**\n"
            "「**ハイクにして**」「**ソネットにして**」「**オーパスにして**」"
            "「**既定に戻して**」で切り替えられます。"
        )
        return

    # 自己診断・エラー確認（最優先で拾う）
    if re.search("システムチェック|自己診断|ヘルスチェック|健康診断|全体チェック", content):
        _fired(cid, "自己診断", content)
        await message.channel.send("🩺 自己診断を実行します（30秒〜1分）…")
        await message.channel.send((await _self_diagnose())[:1900])
        return
    if re.search("エラー", content) and re.search(
        "教えて|見せて|ログ|直近|最近|何|なに|ある\\?|ある？|出てる", content
    ):
        _fired(cid, "エラー確認", content)
        await message.channel.send("🔴 直近のエラー:\n" + _recent_errors(3)[:1800])
        return

    # 承認待ちがあれば、テキストの「許可/拒否」でも受け付ける（ボタン不要）
    approval = _try_text_approval(cid, message.author.id, content)
    if approval is not None:
        _fired(cid, "承認の返事" if approval else "却下の返事", content)
        await message.channel.send(
            "✅ 承認を受け付けました。進めます…" if approval else "🛑 却下を受け付けました。"
        )
        return
    if cid in _pending_approvals:
        # 確認待ちなのに承認/拒否と読めない発言。黙って会話に流すと
        # 「何も起きない → AIが理由を作り話する」が起きるので、状況を明示する。
        await message.channel.send(
            "（いま確認待ちです。開始するなら「**OK**」、やめるなら「**やめて**」と"
            "送ってください。別の内容を頼みたい場合はそのまま言ってもらえれば切り替えます）"
        )

    if content == "!stop" or _is_stop_phrase(content):
        _fired(cid, "停止", content)
        await _do_stop(message, cid)
        return
    if content == "!restart" or _is_restart_phrase(content):
        _fired(cid, "再起動", content)
        # 重い処理を残したまま入れ替わると、CPUを占有したままになる
        stop_heavy_procs()
        await _restart_self(cid)
        return
    # !コマンドは表引きで処理（if の羅列をやめ、追加も1行で済むようにした）
    cmd, _, arg = content.partition(" ")
    handler = _COMMANDS.get(cmd)
    if handler:
        _fired(cid, f"!コマンド({cmd})", content)
        await handler(message, cid, arg.strip())
        return
    if content.startswith("!"):
        # 打ち間違いの「!」「!!!」も含め、ここで引き受けたものとして扱う。
        # 印を付けないと「取りこぼし」と誤判定され、記号に会話で返事をしていた。
        _fired(cid, "!コマンド", content)
        return

    # ---- 決定的ルーティング（classify_route で判定。テストと同じ関数を使う）----
    _job = _load_motion_job()
    _video_att = _find_attachment(message, SUPPORTED_VIDEO_TYPES)
    _image_att = _find_attachment(message, SUPPORTED_IMAGE_TYPES)
    if _image_att:
        _remember_ref(cid, _image_att.url)   # 次の発言でも参照として使えるように
    _lg_rec = _load_last_gen(cid)
    route = classify_route(
        content,
        has_attachments=bool(message.attachments),
        has_video_att=bool(_video_att),
        has_image_att=bool(_image_att),
        has_job=bool(_job),
        # 「できた？」だけで状態確認につなぐのは直近2時間の生成がある時だけ
        # （何時間も経ってからの「できた？」はコード修正等の話が多いため）
        has_last_gen=bool(_lg_rec and time.time() - _lg_rec.get("t", 0) < 7200),
        # 料金照会の直後（10分以内）は、続きの質問も権限のある経路で答える
        after_credits=time.time() - _last_credits.get(cid, 0) < 600,
        has_running=bool(_busy_tasks(cid)),
        # 直前がデザインなら「作り直して」も同じ作り方（HTML）で行う。
        # ただし直近30分だけ。何時間も「デザインの続き」と解釈し続けると、
        # 無関係な雑談まで手直し扱いになる。
        last_was_design=(
            str((_lg_rec or {}).get("label", "")).startswith("デザイン")
            and time.time() - (_lg_rec or {}).get("t", 0) < 1800
        ),
    )
    # 依頼待ち中に動画が添付された（キーワード無し）ケースもモーション実行に接続
    pm = _pending_motion.get(cid)
    if route is None and pm and _video_att and time.time() - pm["ts"] < 900:
        route = "motion"
    _fired(cid, route or "会話", content)

    if route == "status":
        # 進行中も完成物も無ければ状態確認に入らず、普通の会話として続行する
        if await _report_gen_status(
            message.channel, cid, message.author.display_name, content
        ):
            return
        route = None

    if route == "revise":
        add_history(cid, message.author.display_name, content)
        _spawn(_run_revise(message, content), cid, "作り直し")
        return

    if route == "clip":
        add_history(cid, message.author.display_name, content)
        _kind, _srcref = _clip_source(message, content)
        if not _srcref:
            _set_pending_do(cid, "動画の場所", content)
            await message.channel.send(
                "切り抜く動画の場所を教えてください。次のどれでも大丈夫です。\n"
                "・**YouTubeのリンク**\n"
                "・**動画を添付**（Discordの上限まで）\n"
                "・**iPhoneのファイルアプリの道順をそのまま貼る**"
                "（「iCloud Drive ▸ …」の形。iCloudはMacにも同期されているので"
                "そのまま扱えます）\n"
                f"・**Macのファイルのパス**（例: /Users/…/movie.mp4）\n"
                f"※大きいファイルは添付ではなく上の2つが確実です"
                f"（{MAX_VIDEO_SIZE / 1048576:.0f}MBまで）。"
            )
            return
        _n = re.search(r"(\d{1,2})\s*本", content)
        _num = min(int(_n.group(1)), 10) if _n else CLIP_DEFAULT_N
        _where = {"youtube": "YouTube", "url": "リンク先",
                  "file": "Macのファイル"}.get(_kind, "動画")
        _gate(message, cid,
              f"長い動画からショートの切り抜き（{_num}本・素材: {_where}）",
              "字幕を読んで見どころを選び、Mac上で縦型9:16に切り出して"
              "日本語字幕を焼き付けます。字幕が無ければ音声から文字起こしします。"
              "生成モデルは使いません",
              lambda: _run_clip_shorts(message, _srcref, _num, _kind), "切り抜き制作",
              "無料（クレジットは消費しません）",
              engine="クロード（字幕を読んで選定）＋Mac上のffmpeg（切り出し）")
        return

    if route == "short":
        # 「ショート」以外の語をお題として抽出（無ければ自動テーマ）
        theme = re.sub(
            r"(ショート動画|ショート|shorts?|今日の|作って|作りたい|生成して|"
            r"ネタ|企画|お願い(します)?|を|の|で|、|。|！|!)+", "", content, flags=re.I
        ).strip()
        add_history(cid, message.author.display_name, content)
        _gate(message, cid,
              f"ショート動画の制作（テーマ: {theme or '自動でお任せ'}）",
              "企画（タイトル・フック・英語プロンプト）を作り、"
              "縦型9:16の動画を生成して投稿パックまで出します",
              lambda: _run_short(message, theme or None), "ショート制作",
              "Higgsfieldのクレジットを消費します")
        return

    if route == "virality":
        add_history(cid, message.author.display_name, content)
        _gate(message, cid, "直近の動画のバズ度シミュレーション",
              "Higgsfieldの virality_predictor で、バズ度・フック強度・"
              "離脱リスク・改善提案を予測します",
              lambda: _run_virality(message), "バズ度分析",
              "Higgsfieldのクレジットを消費します")
        return

    if route == "ad":
        add_history(cid, message.author.display_name, content)
        _gate(message, cid, f"広告の制作（{content[:60]}）",
              "広告企画書（ターゲット・フック・CTA）を作り、"
              "そのまま縦型9:16のCM動画を生成します",
              lambda: _run_ad_make(message, content), "広告制作",
              "Higgsfieldのクレジットを消費します")
        return

    if route == "edit":
        add_history(cid, message.author.display_name, content)
        _gate(message, cid, f"完成動画の編集（{content[:50]}）",
              "Higgsfieldのクラウド編集室（ffmpeg）で加工し、結果のURLを返します",
              lambda: _run_video_edit(message, content), "動画編集",
              "動画生成のクレジットは消費しません（サンドボックス実行）")
        return

    if route == "multiview":
        add_history(cid, message.author.display_name, content)
        # 名指しがあればその役だけ、無ければ両方
        roles = [r for r, pat in (
            ("claude1", r"クロード\s*[1１]|claude\s*1|リサーチャー"),
            ("claude3", r"クロード\s*[3３]|claude\s*3|アドバイザー"))
            if re.search(pat, content, re.I)] or ["claude1", "claude3"]
        names = "・".join(CLAUDE_PERSONAS[r][0] for r in roles)
        _gate(message, cid, f"{names}に検討してもらう",
              "同じ質問を役割ごとに分けて答え、最後に統合してまとめます",
              lambda: _run_multi_view(message, content, roles), "複数視点の検討",
              "Claudeのサブスク枠を役の数だけ使います（追加課金なし）")
        return

    if route == "ch_set":
        add_history(cid, message.author.display_name, content)

        async def _do_set():
            ch = await _resolve_channel_id(content)
            if not ch:
                await send_as(orch, cid,
                              "チャンネルが見つかりませんでした。"
                              "URL（https://www.youtube.com/@…）で送ってみてください。")
                return
            conf = _load_my_channel()
            conf["channel_id"] = ch
            conf["report_cid"] = cid      # 週次レポートの送り先として覚える
            _save_my_channel(conf)
            dow = "月火水木金土日"[CHANNEL_REPORT_DOW % 7]
            await send_as(orch, cid,
                          f"✅ チャンネルを登録しました（{ch}）。\n"
                          "「**実績分析して**」でいつでも分析できます。\n"
                          f"また、**毎週{dow}曜{CHANNEL_REPORT_HOUR}時**にこのチャンネルへ"
                          "自動でレポートを送ります。")
        _spawn(_do_set(), cid, "チャンネル登録")
        return

    if route == "ch_stats":
        add_history(cid, message.author.display_name, content)
        _gate(message, cid, "自分のチャンネルの実績分析",
              "投稿済み動画の再生数を取得し、伸びた理由を分析して"
              "勝ちパターン集に反映します",
              lambda: _analyze_my_channel(cid), "実績分析",
              "無料（YouTube APIとGeminiの無料枠）")
        return

    if route == "design":
        add_history(cid, message.author.display_name, content)
        # 「クロードで作り直して」のように指示だけの場合、それ単体では
        # 何を作るのか分からない。前回の依頼に今回の修正を足して渡す。
        _req = content
        _prev = (_lg_rec or {}).get("prompt") or ""
        if _prev and (_looks_revise(content) or len(_strip_media_context(content)) < 25):
            _req = f"{_prev}\n【今回の修正指示】{content}"
        _w, _h, _label = _design_size(_req)
        _gate(message, cid, f"デザインの制作（{_req[:40]}）",
              f"ClaudeがHTMLでレイアウトを組み、{_label} {_w}×{_h} の画像に"
              "書き出して投稿します（文字が崩れないので、サムネ・バナー向き）",
              lambda: _run_design(message, _req), "デザイン制作",
              "無料（生成モデルを使わないのでクレジットは消費しません）",
              engine=ENGINE_DESIGN)
        return

    if route == "credits":
        # 聞かれているだけなので生成はしない。読み取りだけ・無料なので確認も挟まない。
        add_history(cid, message.author.display_name, content)
        _last_credits[cid] = time.time()
        await message.channel.send("💳 Higgsfieldに問い合わせています…")

        async def _do_credits():
            res = await _run_credits(content, get_history(cid))
            add_history(cid, "Orchestrator", res)
            await send_as(orch, cid, res)
        _spawn(_do_credits(), cid, "クレジット確認")
        return

    if route == "sharelog":
        add_history(cid, message.author.display_name, content)
        await message.channel.send("📤 直近の会話とエラーを共有用に書き出しています…")

        async def _do_share():
            await send_as(orch, cid, await _share_debug_log(cid))
        _spawn(_do_share(), cid, "ログ共有")
        return

    if route == "style_learn":
        add_history(cid, message.author.display_name, content + "（参考動画のスタイル学習を依頼）")
        _gate(message, cid, "参考動画からスタイルを学習",
              "フック・テンポ・構図・色味などを解析して勝ちパターン集に蓄積し、"
              "以降の企画と生成プロンプトに反映します",
              lambda: _run_style_learn(message), "スタイル学習",
              "無料（Geminiの無料枠を使用）")
        return

    if route == "style_show":
        add_history(cid, message.author.display_name, content)
        prof = _load_style_profile()
        if not prof:
            await message.channel.send(
                "まだスタイルは学習していません。参考動画を添付するかYouTubeリンクを"
                "「これを学習して」と一緒に送ってください。"
            )
        else:
            await send_long(message.channel, prof,
                            "🎨 **学習済みスタイル（勝ちパターン集）**\n")
        return

    if route == "style_reset":
        add_history(cid, message.author.display_name, content)
        try:
            STYLE_PROFILE_FILE.unlink(missing_ok=True)
            await message.channel.send("🧹 学習済みスタイルを白紙に戻しました。")
        except Exception as e:  # noqa: BLE001
            await message.channel.send(f"⚠️ リセットに失敗: {str(e)[:150]}")
        return

    if route == "style_ask":
        add_history(cid, message.author.display_name, content)
        await message.channel.send(
            "🎓 了解です。参考にしたい動画（mp4/mov・20MBまで、最大3本）をこのチャンネルに"
            "添付するか、YouTubeリンク（最大2本）を「**これを学習して**」と一緒に送ってください。"
            "フック・テンポ・色味などの勝ちパターンを学習して、以降のショート/広告の生成に反映します。"
        )
        return

    if route == "image":
        # 何を描くかが書かれていなければ聞き返す（空プロンプトで作らない）。
        # 判定にだけ使い、生成には元の文をそのまま渡す（主題を削らないため）
        add_history(cid, message.author.display_name, content)
        if not _gen_subject(content):
            await message.channel.send(
                "🎨 どんな画像を作りますか？（例:「夕暮れの海辺を歩く猫の画像作って」）"
            )
            return
        _gate(message, cid, f"画像の生成（{_gen_subject(content)[:40]}）",
              "Geminiの無料枠で画像を生成します"
              "（使えない場合はHiggsfieldの最適モデルに切り替え）",
              lambda: _handle_image_request(cid, content), "画像生成",
              "原則無料（Geminiの無料枠）", engine=ENGINE_GEMINI_IMG)
        return

    if route == "hf_model":
        model, mtype, label = _match_gen_model(content)
        add_history(cid, message.author.display_name, content)
        _gate(message, cid, f"{label}で{'動画' if mtype == 'video' else '画像'}を生成",
              "依頼を英語プロンプトに整えてから生成し、完成したらURLを投稿します",
              lambda: _run_hf_generate(message, content, model, mtype, label),
              "動画/画像生成", "Higgsfieldのクレジットを消費します",
              engine=_engine_label_hf(label, "動画" if mtype == "video" else "画像"))
        return

    if route == "hf_auto":
        add_history(cid, message.author.display_name, content)
        # 「ヒッグスフィールドで作って」のように作り手の指定しか無い時は、
        # その指定語を題材にせず、直前に頼まれた中身を使う
        _hreq = _request_with_context(content, cid)
        # 動画か画像かは【補ったあとの依頼】で決める。発言だけで見ると
        # 「ヒッグスフィールドで」に媒体の語が無く、画像を頼まれたのに
        # 動画を作り始めていた（実際に seedance で動画ジョブが走った）。
        # 今の発言 → 補ったあとの依頼 → 直近に頼まれた媒体、の順に見る。
        # 「ヒッグスフィールドでやって」だけでは媒体が分からないので、
        # 直前に画像を頼まれていたなら画像のままにする（既定の動画に落とさない）。
        mtype = (_said_media(content) or _said_media(_hreq)
                 or _recent_media(cid)
                 or (_load_last_gen(cid) or {}).get("media_type") or "video")
        _remember_media(cid, mtype)
        _gate(message, cid,
              f"{'動画' if mtype == 'video' else '画像'}の生成（{_hreq[:50]}）",
              "内容に合う最適なモデルを自動で選び、"
              "英語プロンプトに整えてから生成します",
              lambda: _run_hf_generate(message, _hreq, None, mtype, "自動選定"),
              "動画/画像生成", "Higgsfieldのクレジットを消費します",
              engine=_engine_label_hf("自動選定",
                                      "動画" if mtype == "video" else "画像"))
        return

    if route == "motion":
        req = content if _MOTION_KW_RE.search(content) else (
            (pm["req"] + " " + content).strip() if pm else content
        )
        _pending_motion.pop(cid, None)
        add_history(
            cid, message.author.display_name,
            content + "（参照動画を添付してモーションコントロール生成を依頼）",
        )
        _gate(message, cid, "参照動画の動きを転写して動画生成（モーションコントロール）",
              "添付動画の動きをキャラクターに転写して生成します"
              "（キャラ画像が無ければ依頼文から自動生成）",
              lambda: _run_motion_control(message, req, _video_att),
              "モーション生成", "Higgsfieldのクレジットを消費します")
        return

    if route == "motion_ask":
        _pending_motion[cid] = {"req": content, "ts": time.time()}
        add_history(cid, message.author.display_name, content)
        await message.channel.send(
            "🎭 了解です。動きの元になる動画（mp4/mov・2〜60秒・720p/1080p）を"
            "このチャンネルに添付して送ってください。**添付されたらそのまま生成を始めます**。"
            "キャラの見た目を指定したい場合は、画像も同じメッセージに添付してください。"
        )
        return

    # ---- 動画制作モード：直前に生成があるなら、あいまいな発言も文脈で解釈 ----
    # 正規表現で拾えない言い回し（「イマイチ、変えて」「次は猫で」等）の受け皿。
    lg = _load_last_gen(cid)
    if (route is None and content and not message.attachments and lg
            and time.time() - lg.get("t", 0) < 3600):
        # 直前がデザイン（HTMLで組んだ図）なら、動画用の意図解釈にはかけない。
        # 事故：相関図に「追加して出して」と頼んだら、動画/画像の作り直しとして
        # 解釈され、Higgsfieldの画像生成が確認なしで走り出した。
        # デザインを画像生成で作り直すと、せっかくの文字が崩れて別物になる。
        if str(lg.get("label", "")).startswith("デザイン"):
            if _wants_action(content):
                add_history(cid, message.author.display_name, content)
                _fired(cid, "デザインの作り直し(文脈解釈)", content)
                _req = f"{lg.get('prompt', '')}\n【今回の修正指示】{content}"
                _gate(message, cid, f"デザインの作り直し（{content[:40]}）",
                      "前回のデザインに今回の指示を足して、HTMLで組み直して"
                      "画像に書き出します",
                      lambda: _run_design(message, _req), "デザイン制作",
                      "無料（生成モデルを使わないのでクレジットは消費しません）",
                      engine=ENGINE_DESIGN)
                return
            # 依頼の形でなければ、ただの感想・質問なので普通の会話へ
        else:
            intent, text = await _interpret_video_turn(cid, content, lg)
            if intent == "revise":
                add_history(cid, message.author.display_name, content)
                _fired(cid, "作り直し(文脈解釈)", content)
                _spawn(_run_revise(message, _request_with_context(content, cid)),
                       cid, "作り直し")
                return
            if intent == "new":
                add_history(cid, message.author.display_name, content)
                _fired(cid, "新規生成(文脈解釈)", content)
                # ここは確認を挟まずに生成へ入っていた。クレジットを使う作業を
                # 黙って始めるのは「勝手に生成が始まる」そのものなので必ず確認する。
                # 「ヒッグスフィールドで」だけの発言を題材にしない。
                # 直前に何を頼まれていたかで中身を補う。
                _req2 = _request_with_context(text or content, cid)
                _mt = lg.get("media_type", "video")
                _kind = "動画" if _mt == "video" else "画像"
                _gate(message, cid, f"{_kind}の生成（{_req2[:40]}）",
                      f"内容に合う最適なモデルを自動で選んで{_kind}を生成します",
                      lambda: _run_hf_generate(message, _req2, None, _mt, "自動選定",
                                               aspect_ratio=lg.get("aspect_ratio")),
                      "動画/画像生成", "Higgsfieldのクレジットを消費します",
                      engine=_engine_label_hf("自動選定", _kind))
                return
            # intent == "chat" → 下の通常会話へ（感想・質問はそのまま会話）

    # 進行中プロジェクトがあれば、その返信（承認/修正）として扱う
    if projects.get(cid):
        async with message.channel.typing():
            await pipeline_reply(cid, content)
        return

    # 添付ファイルのコンテキストを取得して content に合併
    had_text = bool(content)
    attachment_context = await extract_attachment_context(message)
    if attachment_context:
        content = content + attachment_context if content else attachment_context.strip()
        if not had_text:
            # 添付だけの投稿＝共有。制作パイプライン等の誤発動を防ぐ目印を付ける
            content = "（ファイル共有）" + content

    # 返信（リプライ）先の発言も文脈に合併（どれについての話かの最も明確な指定）
    content += await _reply_context(message)

    # YouTubeリンクが貼られていたら、Geminiが動画を視聴して内容を文脈に合併
    content, yt_bare_link = await _apply_youtube_context(message, content)

    # 画像/動画のURL（返信先や自分が生成したものを含む）も中身を見て文脈に合併
    content = await _apply_media_url_context(message, content, cid)

    add_history(cid, message.author.display_name, content)

    # パーソナライズ：発言が一定数たまるごとに人物プロファイルを自動更新。
    # プロファイルがまだ無い人は初回発言時にも作成を試みる（バックグラウンド）。
    sp = message.author.display_name
    _profile_counts[sp] = _profile_counts.get(sp, 0) + 1
    if _profile_counts[sp] >= PROFILE_UPDATE_EVERY or (
        _profile_counts[sp] == 1 and not _profile_path(sp).exists()
    ):
        if _profile_counts[sp] >= PROFILE_UPDATE_EVERY:
            _profile_counts[sp] = 0
        _track(asyncio.create_task(_update_profile(cid, sp)))

    # リンクだけの投稿 → 内容まとめは投稿済みなので、AIの雑談応答はしない
    # （内容は記憶に残るので、続けて「この動画どう思う？」と聞けば答えられる）
    if yt_bare_link:
        return

    # テキストなしで音声だけ添付 → 書き起こしの投稿のみで終了
    # （内容は履歴に残るので、続けて質問すればAIが答えられる）
    if not had_text and message.attachments and all(
        Path(a.filename).suffix.lower() in SUPPORTED_AUDIO_TYPES
        for a in message.attachments
    ):
        return
    targets = decide_targets(message, content)

    # オーケストレーター単独宛て → 実行/編集の指示なら承認フロー、それ以外は通常回答
    if len(targets) == 1 and targets[0][0] == "Orchestrator":
        await _handle_orchestrator(message, cid)
        return

    async with message.channel.typing():
        for name, bot, ask in targets:
            try:
                await respond(cid, name, bot, ask)
            except Exception as e:
                if _is_quota_error(e):
                    await send_as(
                        orch, cid,
                        f"⚠️ {name} は本日の無料枠上限に達しました"
                        "（Geminiは無料枠が小さめ）。時間をおくか、質問を "
                        "@Claude 宛てにしてみてください。",
                    )
                else:
                    await send_as(orch, cid, f"⚠️ {name} の呼び出し失敗: {str(e)[:300]}")
            await asyncio.sleep(1)


async def main():
    await asyncio.gather(
        orch.start(os.environ["DISCORD_ORCH_TOKEN"]),
        claude_bot.start(os.environ["DISCORD_CLAUDE_TOKEN"]),
        gemini_bot.start(os.environ["DISCORD_GEMINI_TOKEN"]),
    )


if __name__ == "__main__":
    asyncio.run(main())
