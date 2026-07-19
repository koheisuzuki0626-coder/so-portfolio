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
import io
import json
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
# claude CLI の同時実行を制限（プロファイル学習・要約などのバックグラウンド処理と
# 会話応答が同時に走ってタイムアウトするのを防ぐ。超過分は順番待ち）
_claude_sem = asyncio.Semaphore(int(os.getenv("CLAUDE_CONCURRENCY", "2")))

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


def _spawn(coro, cid, context):
    """背景タスクを、例外が沈黙しないラッパーで起動する。
    on_message のガードは create_task した処理には効かないため、ここで捕捉して
    errors.log 記録＋チャンネル通知する（裏側の静かな失敗を防ぐ）。"""
    async def _wrapped():
        try:
            await coro
        except Exception as e:  # noqa: BLE001
            summary = _log_error(f"bg:{context}", e)
            try:
                await send_as(orch, cid, f"⚠️ {context} でエラー（記録済み）: {summary}")
            except Exception:  # noqa: BLE001
                pass
    return asyncio.create_task(_wrapped())


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


async def _self_diagnose():
    """各サブシステムを能動チェックして健全性レポートを返す（Discord内で完結）。"""
    lines = ["🩺 **システム自己診断**"]

    # ① ルーティング＋E2Eの回帰テスト（別プロセスで隔離実行）
    for tf, name in (("test_routing.py", "ルーティング"), ("simulate.py", "E2Eシミュレーション")):
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


async def run_claude_cli(prompt):
    """Claude Code CLI をヘッドレスで呼ぶ（サブスク利用・API課金なし）。
    プロンプトは stdin で渡す（長文でOSの引数上限を超えないように）。
    同時実行はセマフォで制限し、渋滞によるタイムアウトを防ぐ。"""
    # cwd を固定 → discord-groupchat/.claude/settings.json（WebSearch許可）が読まれる。
    # ※ワークスペースを一度「信頼(trust)」しておかないと settings.json は無視される。
    async with _claude_sem:
        proc = await asyncio.create_subprocess_exec(
            CLAUDE_BIN, "-p",
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
            raise RuntimeError(f"claude CLI がタイムアウトしました（{CLAUDE_TIMEOUT}秒）")
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


# ボットの全応答に共通の運用ルール（ターミナル案内の禁止＝Discord内で完結）
BOT_OPS_GUIDE = (
    "運用ルール: ボットのコード反映が話題になったら、ターミナルコマンドは"
    "案内せず『Discordで「再起動して」と送るだけでOK（自動で最新コードを取得して"
    "再起動される）』と案内する。ユーザーの操作は常にDiscord内で完結させる。"
    "生成中の動画の進捗を聞かれたら『「モーション動画できた？」と送れば自動確認される』"
    "と案内する。進捗確認のために再起動を勧めるのは禁止"
    "（再起動すると進行中の完了監視が止まってしまうため）。"
)


def peer_persona(me, partner):
    return (
        f"あなたは{me}。人間たちと{partner}が参加するDiscordのグループチャットにいる。"
        f"{partner}も人間も対等な仲間。日本語で{REPLY_CHARS}字以内、1発言だけで自然に参加する。"
        "前置きや名乗りは不要。直前の流れを踏まえ、自分の視点も述べること。"
        "自分や相手の過去の発言と同じ内容の繰り返しは厳禁。毎回、新しい内容を足すこと。"
        + BOT_OPS_GUIDE
    )


def peer_prompt(me, partner, history):
    return (
        peer_persona(me, partner) + "\n\n" + _profiles_context()
        + "これまでの会話ログ:\n"
        f"{build_transcript(history)}\n\n次の {me} の発言:"
    )


async def ask_claude(history):
    return await run_claude_cli(peer_prompt("Claude", "Gemini", history))


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


def _gen_sync(model, prompt):
    return gemini_client.models.generate_content(model=model, contents=prompt).text


async def _gemini_call(prompt):
    """モデルをローテーション（毎回開始位置をずらす）しつつ、枠切れはクールダウンで
    一時スキップ。時間が経てば自動的に再挑戦＝枠が復活したらまた使う。"""
    now = time.time()
    n = len(GEMINI_MODELS)
    if n == 0:
        raise RuntimeError("GEMINI_MODELS が空です")
    start = _gemini_rr["i"]
    _gemini_rr["i"] = (start + 1) % n  # 次回は次のモデルから開始（負荷分散）
    order = [GEMINI_MODELS[(start + k) % n] for k in range(n)]

    last_err = None
    for model in order:
        if _gemini_cooldown.get(model, 0) > now:
            continue  # クールダウン中はスキップ（期限が来たら自動で対象に戻る）
        for attempt in range(2):
            try:
                return await asyncio.to_thread(_gen_sync, model, prompt)
            except Exception as e:  # noqa: BLE001
                last_err = e
                per_day = "PerDay" in str(e) or "PerProjectPerModel" in str(e)
                # 分あたり制限（日次でない）なら、同モデルで一度だけ待って再試行
                if _is_quota_error(e) and not per_day and attempt == 0:
                    await asyncio.sleep(_retry_delay(e))
                    continue
                # 日次枠切れ or その他エラー → 一定時間このモデルを避ける（後で自動復帰）
                if per_day or not _is_quota_error(e):
                    _gemini_cooldown[model] = time.time() + GEMINI_COOLDOWN_SEC
                break
    raise last_err or RuntimeError("Geminiの利用可能なモデルがありません（一時的にClaude中心）")


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
    優先して Gemini の無料枠を温存する。Claude 失敗時のみ Gemini へ。"""
    try:
        return await run_claude_cli(prompt)
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
MAX_ATTACHMENT_SIZE = 20 * 1024 * 1024  # 20MB
SUPPORTED_IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
SUPPORTED_VIDEO_TYPES = {".mp4", ".webm", ".mov", ".avi"}
SUPPORTED_AUDIO_TYPES = {".mp3", ".wav", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".flac"}
SUPPORTED_DOC_TYPES = {".pdf"}
SUPPORTED_TEXT_TYPES = {".txt", ".md", ".csv", ".log", ".json", ".py", ".js", ".html"}
TEXT_ATTACHMENT_MAX_CHARS = int(os.getenv("TEXT_ATTACHMENT_MAX_CHARS", "12000"))

# 一時画像ファイル保存先
TEMP_IMAGE_DIR = Path(os.getenv("TEMP_IMAGE_DIR", "/tmp/discord_images"))
TEMP_IMAGE_DIR.mkdir(parents=True, exist_ok=True)


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


def _has_attachments(message):
    """メッセージに画像・動画が添付されているか判定。"""
    if not message.attachments:
        return False, False
    has_image = any(Path(a.filename).suffix.lower() in SUPPORTED_IMAGE_TYPES for a in message.attachments)
    has_video = any(Path(a.filename).suffix.lower() in SUPPORTED_VIDEO_TYPES for a in message.attachments)
    return has_image, has_video


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
    d = {"image_engine": IMAGE_GEN_ENGINE, "image_app": None, "video_app": None}
    try:
        if GEN_SETTINGS_FILE.exists():
            d.update(json.loads(GEN_SETTINGS_FILE.read_text(encoding="utf-8")))
    except Exception as e:  # noqa: BLE001
        print(f"[gen_settings] 読込失敗: {e}")
    return d


gen_settings = _load_gen_settings()


def _save_gen_settings():
    try:
        GEN_SETTINGS_FILE.write_text(
            json.dumps(gen_settings, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except Exception as e:  # noqa: BLE001
        print(f"[gen_settings] 保存失敗: {e}")


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
GEMINI_IMAGE_MODEL = os.getenv("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")


def _gemini_generate_image_sync(prompt):
    """Gemini の画像生成モデルで画像を作り、PNGバイト列を返す。"""
    resp = gemini_client.models.generate_content(
        model=GEMINI_IMAGE_MODEL,
        contents=prompt,
    )
    for cand in resp.candidates or []:
        content = getattr(cand, "content", None)
        for part in (getattr(content, "parts", None) or []):
            inline = getattr(part, "inline_data", None)
            data = getattr(inline, "data", None)
            if data:
                return data
    raise RuntimeError("Geminiが画像を返しませんでした")


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
    """Gemini API に contents（Part/テキストの混在リスト）を渡して応答を得る。
    テキスト版 _gemini_call と同じく、無料枠切れモデルはクールダウンして
    次のモデルへ自動ローテーションする。
    全モデル失敗時は例外を握りつぶさず送出する（無料枠切れは GeminiQuotaExceeded）。
    呼び出し側はこれを捕まえてフォールバック（メタ情報分析 / Claude切替）する。"""
    now = time.time()
    last_err = None
    tried = False
    for model in GEMINI_MODELS:
        if _gemini_cooldown.get(model, 0) > now:
            continue  # 枠切れクールダウン中は次のモデルへ
        tried = True
        try:
            resp = gemini_client.models.generate_content(model=model, contents=contents)
            text = (resp.text or "").strip()
            if text:
                print(f"[{tag}] 成功: {model}")
                return text
        except Exception as e:
            last_err = e
            print(f"[{tag}] {model} 失敗: {str(e)[:200]}")
            per_day = "PerDay" in str(e) or "PerProjectPerModel" in str(e)
            if _is_quota_error(e) and per_day:
                # 日次枠切れ → このモデルをしばらく避ける（自動復帰あり）
                _gemini_cooldown[model] = time.time() + GEMINI_COOLDOWN_SEC
            # 枠切れ・その他エラーとも次のモデルで再挑戦

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


def _gemini_analyze_image_sync(image_data):
    """Gemini API で画像を分析（OCR・構図・テキスト抽出）。"""
    return _gemini_analyze_media_sync(
        image_data, _detect_mime_type(image_data), IMAGE_ANALYSIS_PROMPT, "gemini_analyze_image"
    )


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


def _gemini_transcribe_audio_sync(audio_data, ext):
    """Gemini API で音声を書き起こす。"""
    mime_type = AUDIO_MIME_BY_EXT.get(ext, "audio/mp3")
    return _gemini_analyze_media_sync(
        audio_data, mime_type, AUDIO_TRANSCRIBE_PROMPT, "gemini_transcribe_audio"
    )


async def extract_attachment_context(message):
    """メッセージの添付ファイル（画像・音声・動画・PDF・テキスト）を処理してコンテキストを返す。
    画像=OCR＋構図分析 / 音声=書き起こし / 動画=映像＋音声の内容分析 /
    PDF=全文読み取り（いずれもGemini）/ テキスト系=そのまま読み込み。"""
    if not message.attachments:
        return ""

    contexts = []

    for att in message.attachments:
        filename = att.filename
        ext = Path(filename).suffix.lower()
        size_mb = att.size / (1024 * 1024)

        # 画像処理（Gemini で OCR・分析）
        if ext in SUPPORTED_IMAGE_TYPES:
            file_data = await _download_file(att.url)
            if file_data:
                try:
                    # Gemini で画像を分析
                    analysis = await asyncio.to_thread(_gemini_analyze_image_sync, file_data)
                    if analysis:
                        contexts.append(f"【画像: {filename}】\n{analysis}")
                    else:
                        contexts.append(f"【画像: {filename}】\n（分析に失敗しました。テキストで内容を説明してください。）")
                except asyncio.TimeoutError:
                    print(f"[image_analysis] タイムアウト: {filename}")
                    contexts.append(f"【画像 {filename}】\n（分析がタイムアウトしました。テキストで内容を説明してください。）")
                except GeminiQuotaExceeded as e:
                    print(f"[image_analysis] 枠切れ: {filename}")
                    contexts.append(f"【画像 {filename}】\n（{e}）")
                except Exception as e:
                    print(f"[image_analysis] 失敗: {filename}: {str(e)[:100]}")
                    contexts.append(f"【画像 {filename}】\n（分析エラー。テキストで内容を説明してください。）")
            else:
                contexts.append(f"【画像 {filename}】\n（ダウンロード失敗。テキストで内容を説明してください。）")

        # 音声処理（Gemini で書き起こし）
        elif ext in SUPPORTED_AUDIO_TYPES:
            if att.size > MAX_ATTACHMENT_SIZE:
                contexts.append(
                    f"【音声 {filename} (約{size_mb:.1f}MB)】\n"
                    "（20MBを超えるため書き起こしできません。分割して送ってください。）"
                )
                continue
            file_data = await _download_file(att.url)
            if file_data:
                try:
                    async with message.channel.typing():
                        transcript = await asyncio.to_thread(
                            _gemini_transcribe_audio_sync, file_data, ext
                        )
                    if transcript:
                        contexts.append(f"【音声の書き起こし: {filename}】\n{transcript}")
                        # 書き起こし本文をチャンネルにも投稿（長文はチャンク分割）
                        full = f"📝 **{filename} の書き起こし**\n{transcript}"
                        for i in range(0, len(full), 1900):
                            await message.channel.send(full[i:i + 1900])
                    else:
                        contexts.append(f"【音声 {filename}】\n（書き起こしに失敗しました。）")
                except GeminiQuotaExceeded as e:
                    print(f"[audio_transcribe] 枠切れ: {filename}")
                    contexts.append(f"【音声 {filename}】\n（{e}）")
                    await message.channel.send(f"⚠️ 書き起こしできませんでした: {e}")
                except Exception as e:
                    print(f"[audio_transcribe] 失敗: {filename}: {str(e)[:100]}")
                    contexts.append(f"【音声 {filename}】\n（書き起こしエラー。）")
            else:
                contexts.append(f"【音声 {filename}】\n（ダウンロード失敗。）")

        # 動画処理（Gemini で映像＋音声を分析）
        elif ext in SUPPORTED_VIDEO_TYPES:
            if att.size > MAX_ATTACHMENT_SIZE:
                contexts.append(
                    f"【動画 {filename} (約{size_mb:.1f}MB)】\n"
                    "（20MBを超えるため内容を読み取れません。短く切り出すか圧縮して送ってください。）"
                )
                continue
            file_data = await _download_file(att.url)
            if file_data:
                try:
                    async with message.channel.typing():
                        analysis = await asyncio.to_thread(
                            _gemini_analyze_media_sync,
                            file_data,
                            VIDEO_MIME_BY_EXT.get(ext, "video/mp4"),
                            VIDEO_ANALYSIS_PROMPT,
                            "gemini_analyze_video",
                        )
                    if analysis:
                        contexts.append(f"【動画の内容: {filename}】\n{analysis}")
                    else:
                        contexts.append(f"【動画 {filename}】\n（内容の読み取りに失敗しました。）")
                except GeminiQuotaExceeded as e:
                    print(f"[video_analysis] 枠切れ: {filename}")
                    contexts.append(f"【動画 {filename}】\n（{e}）")
                except Exception as e:
                    print(f"[video_analysis] 失敗: {filename}: {str(e)[:100]}")
                    contexts.append(f"【動画 {filename}】\n（分析エラー。）")
            else:
                contexts.append(f"【動画 {filename}】\n（ダウンロード失敗。）")

        # PDF処理（Gemini で全文読み取り）
        elif ext in SUPPORTED_DOC_TYPES:
            if att.size > MAX_ATTACHMENT_SIZE:
                contexts.append(
                    f"【PDF {filename} (約{size_mb:.1f}MB)】\n"
                    "（20MBを超えるため読み取れません。分割して送ってください。）"
                )
                continue
            file_data = await _download_file(att.url)
            if file_data:
                try:
                    async with message.channel.typing():
                        analysis = await asyncio.to_thread(
                            _gemini_analyze_media_sync,
                            file_data,
                            "application/pdf",
                            PDF_ANALYSIS_PROMPT,
                            "gemini_analyze_pdf",
                        )
                    if analysis:
                        contexts.append(f"【PDFの内容: {filename}】\n{analysis}")
                    else:
                        contexts.append(f"【PDF {filename}】\n（読み取りに失敗しました。）")
                except GeminiQuotaExceeded as e:
                    print(f"[pdf_analysis] 枠切れ: {filename}")
                    contexts.append(f"【PDF {filename}】\n（{e}）")
                except Exception as e:
                    print(f"[pdf_analysis] 失敗: {filename}: {str(e)[:100]}")
                    contexts.append(f"【PDF {filename}】\n（分析エラー。）")
            else:
                contexts.append(f"【PDF {filename}】\n（ダウンロード失敗。）")

        # テキスト系ファイル（そのまま読み込み）
        elif ext in SUPPORTED_TEXT_TYPES:
            file_data = await _download_file(att.url)
            if file_data:
                try:
                    text = file_data.decode("utf-8", errors="replace")
                    if len(text) > TEXT_ATTACHMENT_MAX_CHARS:
                        text = text[:TEXT_ATTACHMENT_MAX_CHARS] + "\n…（以下省略）"
                    contexts.append(f"【ファイルの内容: {filename}】\n{text}")
                except Exception as e:
                    print(f"[text_attachment] 失敗: {filename}: {str(e)[:100]}")
                    contexts.append(f"【ファイル {filename}】\n（読み込みエラー。）")
            else:
                contexts.append(f"【ファイル {filename}】\n（ダウンロード失敗。）")

        # その他（スキップ）
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


async def _apply_youtube_context(message, content):
    """メッセージ内のYouTubeリンク（最大2本）をGeminiが視聴し、内容を文脈に合併する。
    返り値: (合併後のcontent, リンクのみの投稿でまとめを投稿済みならTrue)"""
    urls = YOUTUBE_URL_RE.findall(content)
    if not urls:
        return content, False

    summaries = []
    async with message.channel.typing():
        for url in urls[:2]:
            try:
                summary = await asyncio.to_thread(
                    _gemini_watch_youtube_sync, url, YOUTUBE_CHAT_PROMPT, "youtube_link"
                )
            except GeminiQuotaExceeded as e:
                _gemini_watch["outage_cid"] = message.channel.id
                await message.channel.send(
                    f"⚠️ 動画を視聴できませんでした: {e}\n"
                    "（復活を5分おきに自動確認して、復活したら知らせます）"
                )
                continue
            except Exception as e:  # noqa: BLE001
                print(f"[youtube_link] 視聴失敗 {url}: {str(e)[:200]}")
                await message.channel.send(
                    "⚠️ この動画は読み取れませんでした"
                    "（非公開・年齢制限・配信中・長すぎる等の可能性）。"
                )
                continue
            if summary:
                summaries.append((url, summary))

    for url, summary in summaries:
        content += f"\n\n【YouTube動画の内容（{url}）】\n{summary}"

    # リンクだけの投稿なら、まとめをそのまま投稿（内容は会話の記憶にも残る）
    text_wo_urls = YOUTUBE_URL_RE.sub("", message.content).strip()
    bare_link = bool(summaries) and not text_wo_urls and not message.attachments
    if bare_link:
        for url, summary in summaries:
            full = f"📺 **動画の内容まとめ**\n{summary}"
            for i in range(0, len(full), 1900):
                await message.channel.send(full[i:i + 1900])
    return content, bare_link


async def _run_trend_study(cid, query=None):
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
    analyzed = set() if query else _load_analyzed_ids()
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
    for i in range(0, len(text), 1900):
        await channel.send(text[i:i + 1900])
    add_history(cid, "🎬映像リサーチ", f"（YouTube{label}リサーチ {today}）\n{digest}")


async def _daily_trend_loop():
    """毎日 TREND_HOUR（JST）に急上昇リサーチを自動実行する。"""
    if not TREND_CHANNEL_ID or not YOUTUBE_API_KEY:
        print("[trend] TREND_CHANNEL_ID / YOUTUBE_API_KEY 未設定のため毎日の自動リサーチは無効")
        return
    while True:
        now = datetime.now(JST)
        run_at = now.replace(hour=TREND_HOUR, minute=0, second=0, microsecond=0)
        if run_at <= now:
            run_at += timedelta(days=1)
        print(f"[trend] 次回の自動リサーチ: {run_at.isoformat()}")
        await asyncio.sleep((run_at - now).total_seconds())
        try:
            await _run_trend_study(TREND_CHANNEL_ID)
        except Exception as e:  # noqa: BLE001
            print(f"[trend] 自動リサーチ失敗: {str(e)[:300]}")


# ---------------------------------------------------------------------------
# オーケストレーター：3層構造
#   ① ルーティング（得意モデルへ振り分け／簡単なら単発）
#   ② ディベート（各回答を相互に見せて批判・修正）
#   ③ 司令塔が統合（合意点/対立点を整理して単一回答へ）
# ---------------------------------------------------------------------------
def _answer_prompt(who, history, extra=""):
    return (
        f"あなたは{who}。次の会話の最後の要求に、正確で役立つ回答を日本語で簡潔に述べる。"
        "前置きや名乗りは不要、回答本体のみ。" + BOT_OPS_GUIDE + "\n\n"
        + _profiles_context()
        + (extra + "\n\n" if extra else "")
        + f"{build_transcript(history)}\n\nあなたの回答:"
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
CASUAL_LEAD = os.getenv("CASUAL_LEAD", "claude")
if CASUAL_LEAD not in ("claude", "gemini"):
    CASUAL_LEAD = "claude"

# AI判定が必要そうなキーワード（作業指示・生成依頼・検索・過去記憶）。
# これに全く該当しない短い発言は、AIを呼ばず雑談として即処理（Gemini無料枠の節約）。
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
    "できた|完成|終わった|どうなった|状況|進捗|まだ|見れる|見せて|見たい|"
    "url|ＵＲＬ|どこ|ある\\?|ある？|ちょうだい|ください|"
    "あとどれ|どれくらい|どのくらい|どれぐらい|どのぐらい|何分|確認して", re.I
)
_STATUS_CTX_RE = re.compile("動画|画像|モーション|生成")
_GEN_INTENT2_RE = re.compile("作って|作りたい|生成|つくって|animate|動かして|アニメ化")
_MOTION_KW_RE = re.compile("モーション|この動き|動きで生成|動きを真似|動きをコピー|動きを転写")
_QUESTION_RE = re.compile(
    "どう思う|なんで|なぜ|どうやって|できる\\?|できる？|作れる|入れられる|"
    "って何|とは|意味|進捗|どうなって|してもいい|でもいい|と思う|かな"
)


def _looks_like_question(text):
    """質問・相談っぽい発言か（作業命令ではない）。selffix/exec の誤爆を防ぐ。"""
    return bool(text.rstrip().endswith(("？", "?")) or _QUESTION_RE.search(text))


def _match_gen_model(content):
    """発言中のモデル名 → (model_id, media_type, label)。無ければ None。"""
    low = content.lower()
    for name in sorted(HF_GEN_MODELS, key=len, reverse=True):
        if name in low:
            return HF_GEN_MODELS[name]
    return None


def classify_route(content, *, has_attachments=False, has_video_att=False,
                   has_image_att=False, has_job=False, has_last_gen=False):
    """@メンションなし発言のルーティングを判定（AI(_plan)前の決定的ルートのみ）。
    返り値: 'status'/'revise'/'short'/'hf_model'/'hf_auto'/'motion'/'motion_ask'/None。
    on_message と同じ順序・同じ正規表現を使うので、これをテストすれば実挙動を検証できる。"""
    # ① 生成物の状態確認（添付なし・状態ワード・文脈）
    status_kw = _STATUS_KW_RE.search(content)
    status_ctx = _STATUS_CTX_RE.search(content) or (has_job and status_kw)
    if not has_attachments and status_kw and status_ctx:
        return "status"
    # ①.4 前の生成の作り直し（直前の生成があり、修正マーカーがあるとき）
    if has_last_gen and not has_attachments and _REVISE_RE.search(content):
        return "revise"
    # ①.5 ショート量産（「ショート作って」「今日のショート」等）
    if re.search("ショート|shorts?|ショート動画", content, re.I) and (
        _GEN_INTENT2_RE.search(content) or re.search("今日の|ネタ|企画|お願い", content)
    ):
        return "short"
    # ② Higgsfield生成（モーション以外・生成意図あり）
    if not re.search("モーション|この動き|動きを", content) and _GEN_INTENT2_RE.search(content):
        if _match_gen_model(content):
            return "hf_model"
        auto_kw = re.search("おまかせ|お任せ|自動|最適|いい感じ|良い感じ|どれでも|モデル任せ|よしなに", content)
        media_ref = has_video_att or has_image_att
        if auto_kw or media_ref:
            return "hf_auto"
    # ③ モーション転写（キーワード or 依頼待ち中の動画添付）
    if _MOTION_KW_RE.search(content):
        return "motion" if has_video_att else "motion_ask"
    return None  # 決定的ルートに該当せず → AI(_plan)へ


async def _plan(history):
    """要求の分類（exec/video/image/chat）と処理方針（mode/lead/search/recall）を
    1回のAI呼び出しでまとめて判定する。以前は Claude CLI を2回直列に起動していて
    毎回10〜30秒かかっていたが、Gemini優先（高速・無料枠）の1回に統合。
    さらに、トリガー語を含まない短い発言はAIを呼ばずに即・雑談扱い（枠の節約）。
    失敗時は Claude にフォールバックし、それも駄目なら安全な既定値で続行する。"""
    latest = _latest_user_msg(history)
    if len(latest) <= 60 and not _PLAN_TRIGGER_RE.search(latest):
        return "chat", "single", CASUAL_LEAD, False, False
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
        "（例:『犬の30秒CM作って』）。既存動画を調べる話は video ではなく trend。"
        "image=画像・イラスト・ロゴの生成依頼。"
        "chat=それ以外すべて（質問・相談・意見・雑談）。迷ったら必ずchat。"
        "『（ファイル共有）』で始まる発言＝添付ファイルを共有しただけなので、"
        "明確な依頼文が無い限り必ずchat（video/execにしない）。\n"
        "- mode: 原則single。『重大な判断・設計・事実の突き合わせが本当に必要』な時だけdebate。\n"
        "- lead: claudeが得意=コード・デバッグ・論理的推論・設計判断・丁寧な日本語の長文 / "
        "geminiが得意=要約・多言語翻訳・最新情報・画像や視覚の話題・箇条書き整理・アイデア出し。\n"
        "- search: 最新情報・時事・製品/価格・実在の事実確認が要るときだけtrue。\n"
        "- recall: 『前に話した』『昨日の』『以前決めた』など、直近の会話に無い"
        "過去の記憶が必要なときだけtrue。\n\n"
        f"会話:\n{build_transcript(history)}\n\nJSON:"
    )
    kind, mode, lead, search, recall = "chat", "single", "claude", False, False
    try:
        raw = await _ai_text(prompt, "plan")
        m = re.search(r"\{.*\}", raw, re.S)
        d = json.loads(m.group(0)) if m else {}
        if d.get("kind") in (
            "chat", "exec", "video", "image", "selffix", "restart",
            "trend", "talk", "profile",
        ):
            kind = d["kind"]
        if d.get("mode") in ("single", "debate"):
            mode = d["mode"]
        if d.get("lead") in ("claude", "gemini"):
            lead = d["lead"]
        search = bool(d.get("search"))
        recall = bool(d.get("recall"))
    except Exception as e:  # noqa: BLE001
        print(f"[plan] 判定失敗（既定値で続行）: {str(e)[:150]}")
    return kind, mode, lead, search, recall


async def _handle_image_request(cid, request):
    """画像生成の依頼。既定は Gemini（無料枠 約500枚/日）。
    Gemini が使えないときは Higgsfield MCP の最適モデルへ自動フォールバック。"""
    await send_as(orch, cid, "🎨 Gemini で画像を生成中…（無料枠）")
    try:
        data = await asyncio.to_thread(_gemini_generate_image_sync, request)
        await send_image_bytes(cid, "✅ できました！修正したい点があれば教えてください。", data, "image.png")
        add_history(cid, "Orchestrator", f"（依頼「{request[:60]}」の画像をGeminiで生成して投稿した）")
        return
    except Exception as e:  # noqa: BLE001
        print(f"[image_request] Gemini失敗: {str(e)[:200]}")
        await send_as(orch, cid, "⚠️ Gemini画像が使えないため、Higgsfieldの最適モデルで生成します…")
    url = await _mcp_gen_and_wait(request, media_type="image", model=None)
    if url:
        add_history(cid, "Orchestrator", f"（依頼「{request[:60]}」の画像をHiggsfieldで生成: {url}）")
        await send_as(orch, cid, f"✅ できました！\n{url}")
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
                     media_type="video", label="モーション動画"):
    try:
        _MOTION_JOB_FILE.write_text(
            json.dumps({"cid": cid, "submitted_at": time.time(),
                        "request": request[:200], "model": model,
                        "media_type": media_type, "label": label},
                       ensure_ascii=False),
            encoding="utf-8",
        )
    except Exception as e:  # noqa: BLE001
        print(f"[gen] ジョブ記録の保存失敗: {e}")


def _load_motion_job():
    try:
        if _MOTION_JOB_FILE.exists():
            return json.loads(_MOTION_JOB_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        pass
    return None


def _clear_motion_job():
    try:
        _MOTION_JOB_FILE.unlink(missing_ok=True)
    except Exception:  # noqa: BLE001
        pass


def _pending_eta_msg(job):
    """まだ生成中のとき、投入からの経過時間と完成目安を返す。"""
    if not job or not job.get("submitted_at"):
        return "⏳ まだ生成中のようです。数分後にもう一度「できた？」と送ってください。"
    elapsed = int((time.time() - job["submitted_at"]) / 60)  # 分
    label = job.get("label", "生成")
    # モーション/動画は概ね5〜12分、画像は1〜3分が目安
    typical = 3 if job.get("media_type") == "image" else 12
    remain = max(typical - elapsed, 0)
    if remain <= 0:
        return (
            f"⏳ {label}は投入から約{elapsed}分経過。目安を過ぎていますが"
            "Higgsfield側でまだ生成中のようです（混雑時は時間がかかります）。"
            "もう少し待って「できた？」と送ってください。"
        )
    return (
        f"⏳ {label}は投入から約{elapsed}分経過。通常{typical}分ほどなので、"
        f"あと目安 約{remain}分です。完成したら自動で投稿します"
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


async def _mcp_gen_status(media_type="video", model=None):
    """生成ジョブの完了確認（1回）。完了ならURL、処理中ならNone。全モデル共通。"""
    model_clause = (
        f"最新の {model} ジョブ" if model else f"最新の {media_type} ジョブ"
    )
    task = (
        f"Higgsfield の MCP ツール show_generations（type={media_type}, size=3）で"
        f"最新の生成履歴を確認して。{model_clause}について、status が completed なら"
        "そのURL（results.rawUrl）だけを最終行に出力。failed なら『ERROR: 理由』、"
        "まだ処理中・履歴に無い場合は『PENDING』とだけ最終行に出力して。"
    )
    out = await _run_claude_exec(task, timeout=180)
    print(f"[gen_mcp] 状態確認: {(out or '')[-200:]}")
    if not out or out.startswith("⚠️"):
        return None
    last = out.strip().splitlines()[-1].strip()
    if last.upper().startswith("ERROR"):
        raise RuntimeError(last[:300])
    return _extract_video_url(out)


async def _mcp_motion_status():
    """モーション転写ジョブの完了確認（後方互換）。"""
    return await _mcp_gen_status("video", "kling3_0_motion_control")


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
        )
    ar_line = (
        f"・アスペクト比は必ず {aspect_ratio} にする（generate_{media_type} の aspect_ratio "
        f"パラメータに '{aspect_ratio}' を渡す。縦型ショート用なので横型は不可）\n"
        if aspect_ratio else ""
    )
    task = (
        f"Higgsfield の MCP で{kind}の生成ジョブを投入して。入力: {ref_ctx}。\n"
        + model_line + ar_line +
        f"・内容（プロンプト）: {request[:400]}"
        + (ref_lines + "\n参照メディアは media_import_url 等で取り込み、"
           "start_image / image_references / video_references 等の適切なroleで渡す。"
           if refs else "\n") +
        "選んだモデルで generate_" + media_type + " を呼びジョブを投入（完了は待たない）。"
        "出力は2行だけ: 1行目『MODEL: <実際に使ったモデルID>』、"
        "2行目は投入成功なら『SUBMITTED』、失敗なら『ERROR: 理由』。"
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


async def _refine_prompt(request, media_type, style=""):
    """日本語の依頼（会話文含む）を、具体的な英語の映像/画像生成プロンプトに変換。
    既に英語プロンプトならそのまま返す。生成物が『全然違う』のを防ぐ核心工程。"""
    if _looks_english_prompt(request):
        return request.strip()
    kind = "video" if media_type == "video" else "image"
    ask = (
        f"次の日本語の依頼を、AI {kind}生成用の英語プロンプト1つに変換して。"
        "被写体・構図・カメラワーク・光・色・質感・雰囲気を具体的に描写。"
        "カンマ区切りの1行、英語のみ、プロンプト本体だけ出力（説明や引用符は不要）。"
        + (f" スタイル指定: {style}." if style else "")
        + f"\n依頼: {request}"
    )
    try:
        out = (await _ai_text_bg(ask, "refine_prompt")).strip()
        line = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
        line = line.strip('"' + "'`")
        return line or request
    except Exception as e:  # noqa: BLE001
        print(f"[refine_prompt] 失敗、原文使用: {str(e)[:120]}")
        return request


async def _run_hf_generate(message, request, model, media_type, label,
                           aspect_ratio=None, refine=True):
    """Higgsfieldで生成→投入→完了監視→URL自動投稿（モーションと同じ堅牢さ）。
    model=None なら最適モデルを自動選定する。aspect_ratio='9:16'で縦型ショート。
    refine=True で日本語依頼を英語プロンプトに変換（既に整形済みならFalse）。"""
    cid = message.channel.id
    if not HF_AVAILABLE and not os.getenv("HIGGSFIELD_API_KEY"):
        await send_as(orch, cid, "⚠️ Higgsfield が使えません（APIキー/認証を確認してください）。")
        return
    # 添付があれば参照メディアとして渡す（画像・動画）
    refs = [
        a.url for a in message.attachments
        if Path(a.filename).suffix.lower() in (SUPPORTED_IMAGE_TYPES | SUPPORTED_VIDEO_TYPES)
    ]
    kind = "動画" if media_type == "video" else "画像"
    if refine:
        refined = await _refine_prompt(request, media_type)
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
        await send_as(orch, cid, f"⚠️ 生成の投入に失敗: {str(e)[:250]}")
        return
    # 「もう一回作り直して」で引き継げるよう、今回のプロンプトを保存
    _save_last_gen(cid, request, media_type, aspect_ratio, label)
    disp = label if model else f"自動選定: {chosen or '？'}"
    if isinstance(result, str):  # 投入中に既にURLが返った
        _clear_motion_job()
        add_history(cid, "Orchestrator", f"（{disp}で生成した: {result}）")
        await send_as(orch, cid, f"✅ できました！（{disp}）\n{result}")
        return
    _save_motion_job(cid, request, model=chosen, media_type=media_type, label=disp)
    await send_as(
        orch, cid,
        f"⏳ {disp} で生成ジョブを投入しました。完成したらURLを自動投稿します"
        "（「できた？」でいつでも確認可）。"
    )
    asyncio.create_task(_watch_motion_job(cid))


# ---------- 直前の生成内容を記録（「もう一回作り直して」の文脈引き継ぎ用） ----------
_LASTGEN_FILE = HISTORY_DIR / "last_gen.json"


def _save_last_gen(cid, prompt, media_type, aspect_ratio, label):
    try:
        data = {}
        if _LASTGEN_FILE.exists():
            data = json.loads(_LASTGEN_FILE.read_text(encoding="utf-8"))
        data[str(cid)] = {"prompt": prompt, "media_type": media_type,
                          "aspect_ratio": aspect_ratio, "label": label,
                          "t": time.time()}
        _LASTGEN_FILE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        print(f"[lastgen] 保存失敗: {e}")


def _load_last_gen(cid):
    try:
        if _LASTGEN_FILE.exists():
            return json.loads(_LASTGEN_FILE.read_text(encoding="utf-8")).get(str(cid))
    except Exception:  # noqa: BLE001
        pass
    return None


# 「前の生成を修正して作り直す」意図の検出（明確なマーカーのみ）
_REVISE_RE = re.compile(
    "もう一回|もう一度|もっかい|作り直|作りなお|やり直|やりなお|"
    "さっきの(動画|画像|映像|やつ|の)|前の(動画|画像|映像|やつ)|"
    "同じの|別バージョン|別ver|少し変えて|ちょっと変えて|修正して"
)


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


async def _run_revise(message, instruction):
    """直前の生成プロンプトに修正指示を適用して作り直す（文脈引き継ぎ）。"""
    cid = message.channel.id
    last = _load_last_gen(cid)
    if not last:
        await send_as(orch, cid,
                      "直前の生成が見つかりません。まず何か生成してください。")
        return
    await send_as(orch, cid, "🔁 前の内容を踏まえて作り直します…")
    ask = (
        "既存の英語生成プロンプトに、ユーザーの日本語の修正指示を反映した"
        "新しい英語プロンプトを1つ作って。カンマ区切り1行、英語のみ、本体だけ出力。\n"
        f"【元プロンプト】{last['prompt']}\n【修正指示】{instruction}"
    )
    try:
        new_prompt = (await _ai_text_bg(ask, "revise_prompt")).strip().splitlines()[0]
        new_prompt = new_prompt.strip('"' + "'`") or last["prompt"]
    except Exception:  # noqa: BLE001
        new_prompt = last["prompt"]
    await _run_hf_generate(
        message, new_prompt, None, last.get("media_type", "video"),
        "作り直し", aspect_ratio=last.get("aspect_ratio"), refine=False,
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
    base = (
        "あなたはバズるYouTube Shortsのアートディレクター。"
        "スタイリッシュ/アート系の縦型ショート動画の企画をJSONだけで返す。\n"
        '形式: {"title":"日本語の惹かれるタイトル(30字以内)",'
        '"hook":"最初の2秒で掴む見せ方の説明",'
        '"prompt":"英語の動画生成プロンプト。映像美・カメラワーク・色・質感を具体的に。'
        '9:16縦型。人物の顔は大きく写さない",'
        '"description":"YouTube用の説明文(2〜3行)",'
        '"tags":["#Shorts","関連ハッシュタグ","5〜8個"]}\n'
        "映像はAI生成で作るので、抽象的・視覚的に強い題材にすること。\n"
    )
    theme_line = f"テーマ/お題: {theme}\n" if theme else "テーマは自由（今映える洗練された題材を選ぶ）。\n"
    raw = await _ai_text_bg(base + theme_line + "\nJSON:", "short_concept")
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
    await send_as(orch, cid, "🎬 今日のショートを企画します（スタイリッシュ/アート系）…")
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
    for _ in range(30):  # 1分おき最大30分
        await asyncio.sleep(60)
        job = _load_motion_job()
        if not job or job.get("cid") != cid:
            return  # 別ジョブに置き換わった/クリアされた
        try:
            vurl = await _mcp_gen_status(media_type, model)
        except Exception as e:  # noqa: BLE001
            _clear_motion_job()
            await send_as(orch, cid, f"⚠️ 生成が失敗しました: {str(e)[:250]}")
            return
        if vurl:
            _clear_motion_job()
            add_history(cid, "Orchestrator", f"（{label}が完成: {vurl}）")
            await send_as(orch, cid, f"✅ {label}ができました！\n{vurl}")
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
    img_att = next(
        (a for a in message.attachments
         if Path(a.filename).suffix.lower() in SUPPORTED_IMAGE_TYPES),
        None,
    )
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
            "（数分〜十数分／「モーション動画できた？」でいつでも確認可）。"
        )
        asyncio.create_task(_watch_motion_job(cid))
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


async def ask_orchestrator(history):
    _, mode, lead, search, recall = await _plan(history)
    return await _orchestrate(mode, lead, search, history, recall)


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

    # ① 単発モード：簡単な要求は得意モデル1つで即答（コスト節約）
    if mode == "single":
        if lead == "gemini":
            try:
                return await _gemini_call(_answer_prompt("Gemini", history, ctx))
            except Exception:  # noqa: BLE001
                pass  # Gemini不可ならClaudeへ
        try:
            return await run_claude_cli(_answer_prompt("Claude", history, ctx))
        except Exception as e:  # noqa: BLE001
            # Claudeがタイムアウト・上限などで落ちたらGeminiで応答（無応答を防ぐ）
            print(f"[orchestrate] Claude失敗 → Geminiへ: {str(e)[:150]}")
            try:
                return await _gemini_call(_answer_prompt("Gemini", history, ctx))
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
        return claude_ans or await run_claude_cli(_answer_prompt("Claude", history, ctx))
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

    # 生成ジョブの完了確認（「モーション動画できた？」「あとどれくらい？」等）
    if re.search("モーション|動画|画像|生成", latest) and re.search(
        "できた|完成|終わった|どうなった|状況|進捗|まだ|あとどれ|どれくらい|どのくらい|何分", latest
    ):
        job = _load_motion_job() or {}
        await message.channel.send("🔎 Higgsfield の生成状況を確認します…")
        try:
            vurl = await _mcp_gen_status(job.get("media_type", "video"), job.get("model"))
        except Exception as e:  # noqa: BLE001
            await message.channel.send(f"⚠️ 生成が失敗していました: {str(e)[:250]}")
            return
        if vurl:
            _clear_motion_job()
            add_history(cid, "Orchestrator", f"（生成物が完成: {vurl}）")
            await message.channel.send(f"✅ できています！\n{vurl}")
        else:
            await message.channel.send(_pending_eta_msg(job))
        return

    # モーションの顔の向きモード切替（顔重視 image / 動き重視 video）
    if re.search("モーション|顔", latest) and re.search("向き|オリエン|モード|顔", latest):
        if re.search("動き優先|動き重視|複雑な動き|video", latest, re.I):
            gen_settings["motion_orientation"] = "video"
            _save_gen_settings()
            await message.channel.send("🔧 モーションを『動き優先(video)』にしました。")
            return
        if re.search("顔優先|顔重視|似せ|image|見た目", latest, re.I):
            gen_settings["motion_orientation"] = "image"
            _save_gen_settings()
            await message.channel.send(
                "🔧 モーションを『顔・見た目優先(image)』にしました。"
                "次の生成から顔をより忠実に保ちます。"
            )
            return

    # モーション転写モデルのID直接指定（「モーションのモデルを ○○ にして」）
    m = re.search(r"モーション\S*の?モデル\S*を\s*([\w\-./]{4,})\s*に", latest)
    if m:
        gen_settings["motion_app"] = m.group(1)
        _save_gen_settings()
        await message.channel.send(
            f"🔧 モーション転写のモデルを {m.group(1)} に設定しました。"
            "もう一度、動画を添付して依頼してください。"
        )
        return

    # モデル設定の確認（「今のモデル設定教えて」等）
    if re.search("モデル", latest) and re.search("設定|確認|見せて|教えて|どれ|なに|何", latest):
        await message.channel.send("🔧 現在の生成モデル設定:\n" + _gen_settings_summary())
        return

    # 発言中のモデル名（クリング/ナノバナナ等）を検出して生成設定に反映
    changes = _apply_model_mentions(latest)
    if changes:
        await message.channel.send("🔧 生成モデルを切り替えました:\n" + "\n".join(changes))
        # モデル指定だけの発言（作る対象が無い）ならここで完了
        if len(latest) <= 30 and not re.search(
            "作って|作りたい|生成して|生成したい|やって|始めて|プロジェクト", latest
        ):
            return

    # 分類＋処理方針を1回のAI呼び出しで判定（旧: Claude CLI 2回直列で遅かった）
    async with message.channel.typing():
        kind, mode, lead, search, recall = await _plan(history)
    # 保険：質問や相談っぽい発言が作業系(selffix/exec)に誤分類されたらchatに戻す
    if kind in ("selffix", "exec") and _looks_like_question(latest):
        print(f"[plan] {kind}→chat に降格（質問/相談と判断）")
        kind = "chat"
    if kind == "video":
        # 単発の動画生成は、最適モデルを自動選定して直接生成（滑らかで確実）。
        # 構成案→絵コンテの多段フローが要るときは !project を使う。
        asyncio.create_task(
            _run_hf_generate(message, _latest_user_msg(history), None, "video", "自動選定")
        )
        return
    if kind == "exec":
        await _start_agent(message, cid, _latest_user_msg(history))
        return
    if kind == "image":
        _spawn(_handle_image_request(cid, _latest_user_msg(history)), cid, "画像生成")
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
        _spawn(_run_trend_study(cid, topic or None), cid, "YouTubeリサーチ")
        return
    if kind == "talk":
        if state["running"]:
            await message.channel.send("自動トークが進行中です。「止めて」で停止できます。")
            return
        await message.channel.send(
            f"🎙️ ClaudeとGeminiで話します（最大 {MAX_TURNS} 発言。「止めて」で停止）"
        )
        asyncio.create_task(run_auto(cid, _latest_user_msg(history)))
        return
    if kind == "profile":
        p = _load_profiles()
        if p:
            for i in range(0, len(p), 1900):
                await message.channel.send(("🧠 " if i == 0 else "") + p[i:i + 1900])
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
                    answer = await run_claude_cli(_answer_prompt("Claude", history))
                except Exception:  # noqa: BLE001
                    answer = "⚠️ 一時的に応答できませんでした。少し後で試してください。"
            else:
                if "gemini" in str(e).lower():
                    _gemini_watch["outage_cid"] = cid
                answer = f"⚠️ 応答に失敗: {str(e)[:300]}"
    add_history(cid, "Orchestrator", answer)
    await send_as(orch, cid, answer)


# ---------- 送信・進行 ----------
async def send_as(bot, channel_id, text, view=None):
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    kwargs = {"view": view} if view is not None else {}
    await channel.send((text or "(空の応答)")[:1900], **kwargs)


async def respond(cid, name, bot, ask):
    text = await ask(get_history(cid))
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
            targets.append(("Gemini", gemini_bot, ask_gemini))
        return targets
    # @メンションなし → 常にオーケストレーター宛て
    return [("Orchestrator", orch, ask_orchestrator)]


async def run_auto(cid, topic):
    """Claude と Gemini だけで自動的に会話（!talk 用）。"""
    state["running"], state["stop"] = True, False
    speakers = [("Claude", claude_bot, ask_claude), ("Gemini", gemini_bot, ask_gemini)]
    try:
        for i in range(MAX_TURNS):
            if state["stop"]:
                break
            name, bot, ask = speakers[i % 2]
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
        f"あなたは映像ディレクター。お題「{p['topic']}」で短い映像の構成案を作る。"
        "日本語で簡潔に、次を必ず含める："
        "タイトル / コンセプト(1〜2文) / "
        f"シーン一覧({NUM_SCENES}個・各シーンは1行で情景を描写) / 尺の目安。"
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
        f"📝 【構成案】\n{p['plan']}\n\n———\n"
        "下のボタン、または「OK」で承認。直したい所はテキストで指示してください。",
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

_APPROVE_PHRASES = {
    "許可", "許可する", "承認", "承認する", "ok", "okay", "おk", "オーケー",
    "おっけー", "おっけ", "いいよ", "いいですよ", "はい", "うん", "やって",
    "実行", "実行して", "go", "ゴー", "頼む", "お願い", "お願いします", "進めて",
}
_DENY_PHRASES = {
    "拒否", "拒否する", "だめ", "ダメ", "駄目", "やめて", "やめ", "中止",
    "中止して", "キャンセル", "cancel", "no", "いいえ", "なし", "却下",
}


def _try_text_approval(cid, user_id, content):
    """承認待ちがあるとき、テキストの「許可/拒否」でも解決する。
    承認=True / 却下=False / 対象外=None を返す。"""
    entry = _pending_approvals.get(cid)
    if not entry:
        return None
    fut, owner_id = entry
    if fut.done() or (owner_id and user_id != owner_id):
        return None
    norm = content.strip().rstrip("。.!！?？ 　").lower()
    if norm in _APPROVE_PHRASES:
        fut.set_result(True)
        return True
    if norm in _DENY_PHRASES:
        fut.set_result(False)
        return False
    return None


async def _run_claude_exec(task, timeout=600):
    """承認済みタスクをフル権限で実行し、標準出力を返す。"""
    proc = await asyncio.create_subprocess_exec(
        CLAUDE_BIN, "-p", "--dangerously-skip-permissions", task,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=BASE_DIR,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "⚠️ 実行がタイムアウトしました。"
    if proc.returncode != 0:
        return f"⚠️ 実行に失敗: {(err.decode() or '').strip()[:400]}"
    return out.decode().strip() or "(完了・出力なし)"


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
    _pending_approvals[cid] = (fut, owner_id)
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
        _pending_approvals.pop(cid, None)
    if not approved:
        return "🛑 却下されました。実行しません。"

    # ③ 承認 → 実行（承認済みのためフル権限）
    await send_as(orch, cid, "▶️ 承認されました。実行します…")
    return await _run_claude_exec(task)


async def _run_agent_task(cid, task, owner_id):
    try:
        result = await run_claude_agent(cid, task, owner_id)
    except Exception as e:  # noqa: BLE001
        result = f"⚠️ エージェント実行に失敗: {str(e)[:400]}"
    await send_as(orch, cid, result)


# ---------- 自己改修＆自己再起動（Discord内で完結） ----------
SELF_FILE = Path(os.path.abspath(__file__))
SELF_BACKUP = SELF_FILE.with_suffix(".py.bak")
RESTART_MARKER = HISTORY_DIR / "restart_notify.json"


async def _sync_to_origin():
    """GitHubに新しいコードがあれば取り込む（再起動前に呼ぶ）。
    ローカルにしか無いコミットや未コミット変更がある場合は、壊さないようスキップ。"""
    rc, branch = await _git_self(["rev-parse", "--abbrev-ref", "HEAD"])
    branch = branch.strip()
    if rc != 0 or not branch:
        return "ブランチ不明のためスキップ"
    rc, _ = await _git_self(["fetch", "origin", branch])
    if rc != 0:
        return "fetch失敗のためスキップ（オフライン？）"
    rc, dirty = await _git_self(["status", "--porcelain", "--untracked-files=no"])
    if rc == 0 and dirty.strip():
        return "未コミットの変更があるためスキップ"
    rc, ahead = await _git_self(["rev-list", "--count", f"origin/{branch}..HEAD"])
    if rc != 0:
        return "状態確認失敗のためスキップ"
    if int(ahead.strip() or "0") > 0:
        return "ローカル独自コミット（未プッシュの自己改修）があるためスキップ"
    rc, behind = await _git_self(["rev-list", "--count", f"HEAD..origin/{branch}"])
    if rc != 0:
        return "状態確認失敗のためスキップ"
    n = int(behind.strip() or "0")
    if n == 0:
        return "既に最新"
    rc, out = await _git_self(["reset", "--hard", f"origin/{branch}"])
    return f"最新コードを取得（{n}コミット）" if rc == 0 else "reset失敗のためスキップ"


async def _restart_self(cid, note=""):
    """自分自身を再起動する（プロセスを入れ替え。Mac操作不要）。
    再起動前にGitHubの最新コードを自動で取り込む＝Discordだけで更新が完結する。"""
    sync = await _sync_to_origin()
    print(f"[restart] コード同期: {sync}")
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
    for tf in ("test_routing.py", "simulate.py"):
        if (SELF_FILE.parent / tf).exists():
            checks.append([sys.executable, tf])
    for args in checks:
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(SELF_FILE.parent),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=120)
        except asyncio.TimeoutError:
            proc.kill()
            return False, "検証がタイムアウトしました"
        if proc.returncode != 0:
            detail = (err.decode(errors="replace") or out.decode(errors="replace")).strip()
            return False, f"[{' '.join(args[-1:])}] {detail[:400]}"
    return True, ""


async def _git_self(args):
    """自己改修のgit操作（ベストエフォート）。(returncode, 出力) を返す。"""
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=BASE_DIR,
    )
    out, err = await proc.communicate()
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
        shutil.copy2(SELF_FILE, SELF_BACKUP)
    except Exception as e:  # noqa: BLE001
        await send_as(orch, cid, f"⚠️ バックアップ作成に失敗したため中止します: {str(e)[:200]}")
        return

    task = (
        f"このフォルダの {SELF_FILE.name}（Discordボット本体）を、次の要望どおりに修正して。\n"
        f"要望: {request}\n"
        "注意: 既存の機能を壊さない最小限の変更にすること。"
        f"修正後に `python3 -m py_compile {SELF_FILE.name}` で構文チェックし、"
        "エラーがあれば直すこと。最後に修正内容の要約を3行以内で出力して。"
    )
    result = await _run_claude_exec(task)

    ok, detail = await _selfcheck()
    if not ok:
        shutil.copy2(SELF_BACKUP, SELF_FILE)
        await send_as(
            orch, cid,
            f"⚠️ 修正後のコードが検証に失敗したため、自動で元に戻しました。\n"
            f"エラー: {detail[:500]}"
        )
        return

    _, diffstat = await _git_self(["diff", "--stat", "--", SELF_FILE.name])
    fut = asyncio.get_running_loop().create_future()
    _pending_approvals[cid] = (fut, owner_id)
    await send_as(
        orch, cid,
        f"📋 修正完了・検証OKです。\n\n【修正内容】\n{result[:1000]}\n\n"
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
        _pending_approvals.pop(cid, None)
    if not approved:
        shutil.copy2(SELF_BACKUP, SELF_FILE)
        await send_as(orch, cid, "🛑 元のコードに戻しました。適用していません。")
        return

    # 記録用にコミット（プッシュできればプッシュ。失敗しても適用は続行）
    await _git_self(["add", SELF_FILE.name])
    rc, _ = await _git_self(["commit", "-m", f"Discordからの自己改修: {request[:60]}"])
    note = "修正を適用しました。"
    if rc == 0:
        rc_push, _ = await _git_self(["push", "origin", "HEAD"])
        if rc_push == 0:
            note += "（GitHubへプッシュ済み）"
        else:
            note += (
                "（⚠️GitHubへのプッシュに失敗＝ローカルのみ。"
                "次のコード同期で消える可能性があるため、この修正が重要なら "
                "Claude Code 側でも同じ修正を反映してください）"
            )
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
    if projects.pop(cid, None):
        await message.channel.send(
            "⏹️ 進行中の作業を停止しました。以降は通常の会話に戻ります。"
        )
    else:
        await message.channel.send("⏹️ 停止しました。")


_trend_task_started = False


@orch.event
async def on_ready():
    global _trend_task_started
    print(f"オーケストレーター起動: {orch.user}")
    # 起動時のセルフテスト（ルーティング＋E2E）。失敗したら復帰チャンネルに警告。
    routing_ok = True
    for tf in ("test_routing.py", "simulate.py"):
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
            await send_as(orch, int(d["cid"]), f"✅ 再起動完了！{note}{warn}".strip())
        except Exception as e:  # noqa: BLE001
            print(f"[restart] 復帰通知失敗: {e}")
    if not _trend_task_started:
        _trend_task_started = True
        asyncio.create_task(_daily_trend_loop())
        asyncio.create_task(_gemini_recovery_loop())
        # 再起動前に投入したモーション生成があれば、完了監視を再開する
        job = _load_motion_job()
        if job and job.get("cid") and time.time() - job.get("submitted_at", 0) < 3600:
            print("[motion] 進行中ジョブの完了監視を再開")
            asyncio.create_task(_watch_motion_job(int(job["cid"])))
        elif job:
            _clear_motion_job()  # 1時間超は古すぎるので破棄


@orch.event
async def on_message(message):
    """全メッセージの入口。予期せぬ例外を必ず捕捉してログ＋通知する
    （沈黙して失敗＝毎回スクショ待ち、を防ぐ自己修復の要）。"""
    if message.author.bot:
        return
    try:
        await _dispatch_message(message)
    except Exception as e:  # noqa: BLE001
        summary = _log_error(f"on_message: {message.content[:80]}", e)
        try:
            await message.channel.send(
                f"⚠️ 処理中にエラーが出ました（記録済み）: {summary}\n"
                "『エラー教えて』で詳細、『システムチェック』で自己診断できます。"
            )
        except Exception:  # noqa: BLE001
            pass


async def _dispatch_message(message):
    content = message.content.strip()
    # テキストまたは添付ファイルがない場合は無視
    if not content and not message.attachments:
        return
    cid = message.channel.id

    # 初回のみ：導入前の過去ログをDiscordから取り込む（バックグラウンド）
    if cid not in _import_started:
        asyncio.create_task(_backfill_channel_history(message.channel))

    # 自己診断・エラー確認（最優先で拾う）
    if re.search("システムチェック|自己診断|ヘルスチェック|健康診断|全体チェック", content):
        await message.channel.send("🩺 自己診断を実行します（30秒〜1分）…")
        await message.channel.send((await _self_diagnose())[:1900])
        return
    if re.search("エラー", content) and re.search(
        "教えて|見せて|ログ|直近|最近|何|なに|ある\\?|ある？|出てる", content
    ):
        await message.channel.send("🔴 直近のエラー:\n" + _recent_errors(3)[:1800])
        return

    # 承認待ちがあれば、テキストの「許可/拒否」でも受け付ける（ボタン不要）
    approval = _try_text_approval(cid, message.author.id, content)
    if approval is not None:
        await message.channel.send(
            "✅ 承認を受け付けました。進めます…" if approval else "🛑 却下を受け付けました。"
        )
        return

    if content == "!stop" or _is_stop_phrase(content):
        await _do_stop(message, cid)
        return
    if content == "!restart" or _is_restart_phrase(content):
        await _restart_self(cid)
        return
    if content.startswith("!project"):
        topic = content[len("!project"):].strip()
        if not topic:
            await message.channel.send("使い方: !project お題（例: !project 犬が主役の30秒CM）")
            return
        if projects.get(cid):
            await message.channel.send("進行中のプロジェクトがあります。!cancel で中止できます。")
            return
        _spawn(pipeline_start(cid, topic), cid, "映像プロジェクト")
        return
    if content == "!cancel":
        if projects.pop(cid, None):
            await message.channel.send("🛑 プロジェクトを中止しました。")
        else:
            await message.channel.send("進行中のプロジェクトはありません。")
        return
    if content.startswith("!agent"):
        task = content[len("!agent"):].strip()
        if not task:
            await message.channel.send(
                "使い方: !agent やってほしいこと（例: !agent このフォルダのファイル一覧を出して）\n"
                "※ Claudeがコマンド実行やファイル編集をする前に、[✅許可][❌拒否] ボタンで確認します。"
            )
            return
        await message.channel.send(f"🤖 エージェント開始: {task}")
        _spawn(_run_agent_task(cid, task, message.author.id), cid, "エージェント実行")
        return
    if content == "!profile":
        p = _load_profiles()
        if p:
            for i in range(0, len(p), 1900):
                await message.channel.send(("🧠 " if i == 0 else "") + p[i:i + 1900])
        else:
            await message.channel.send(
                "まだプロファイルはありません（会話がたまると自動で作られます）。"
            )
        return
    if content.startswith("!trend"):
        if not YOUTUBE_API_KEY:
            await message.channel.send(
                "YOUTUBE_API_KEY が未設定です。Google Cloud Console で YouTube Data API v3 の"
                "APIキーを発行し、.env に追加してください（README参照）。"
            )
            return
        topic = content[len("!trend"):].strip()
        _spawn(_run_trend_study(cid, topic or None), cid, "YouTubeリサーチ")
        return
    if content.startswith("!search"):
        q = content[len("!search"):].strip()
        if not q:
            await message.channel.send("使い方: !search 調べたいこと")
            return
        async with message.channel.typing():
            ctx = await web_search_context(q)
            if not ctx:
                await send_as(orch, cid, "🔍 検索結果が取得できませんでした。")
                return
            ans = await run_claude_cli(
                "次のWeb検索結果を根拠に、質問へ日本語で簡潔に答え、参考URLも示す。\n\n"
                f"質問: {q}\n\n{ctx}\n\n回答:"
            )
            await send_as(orch, cid, ans)
        return
    if content.startswith("!short"):
        theme = content[len("!short"):].strip()
        _spawn(_run_short(message, theme or None), cid, "ショート制作")
        return
    if content.startswith("!talk"):
        if state["running"]:
            await message.channel.send("自動トークが進行中です。!stop で止められます。")
            return
        topic = content[len("!talk"):].strip() or "自由なテーマで雑談"
        add_history(cid, message.author.display_name, f"（お題）{topic} について話して")
        await message.channel.send(
            f"🎙️ お題「{topic}」で ClaudeとGemini が最大 {MAX_TURNS} 発言 話します"
        )
        asyncio.create_task(run_auto(cid, topic))
        return
    if content.startswith("!"):
        return

    # ---- 決定的ルーティング（classify_route で判定。テストと同じ関数を使う）----
    _job = _load_motion_job()
    _video_att = next(
        (a for a in message.attachments
         if Path(a.filename).suffix.lower() in SUPPORTED_VIDEO_TYPES),
        None,
    ) if message.attachments else None
    _image_att = next(
        (a for a in message.attachments
         if Path(a.filename).suffix.lower() in SUPPORTED_IMAGE_TYPES),
        None,
    ) if message.attachments else None
    route = classify_route(
        content,
        has_attachments=bool(message.attachments),
        has_video_att=bool(_video_att),
        has_image_att=bool(_image_att),
        has_job=bool(_job),
        has_last_gen=bool(_load_last_gen(cid)),
    )
    # 依頼待ち中に動画が添付された（キーワード無し）ケースもモーション実行に接続
    pm = _pending_motion.get(cid)
    if route is None and pm and _video_att and time.time() - pm["ts"] < 900:
        route = "motion"

    if route == "status":
        job = _job or {}
        await message.channel.send("🔎 Higgsfield の生成状況を確認します…")
        try:
            vurl = await _mcp_gen_status(job.get("media_type", "video"), job.get("model"))
        except Exception as e:  # noqa: BLE001
            await message.channel.send(f"⚠️ 生成が失敗していました: {str(e)[:250]}")
            return
        if vurl:
            _clear_motion_job()
            add_history(cid, "Orchestrator", f"（生成物が完成: {vurl}）")
            await message.channel.send(f"✅ できています！URLはこちら:\n{vurl}")
        else:
            await message.channel.send(_pending_eta_msg(job))
        return

    if route == "revise":
        add_history(cid, message.author.display_name, content)
        _spawn(_run_revise(message, content), cid, "作り直し")
        return

    if route == "short":
        # 「ショート」以外の語をお題として抽出（無ければ自動テーマ）
        theme = re.sub(
            r"(ショート動画|ショート|shorts?|今日の|作って|作りたい|生成して|"
            r"ネタ|企画|お願い(します)?|を|の|で|、|。|！|!)+", "", content, flags=re.I
        ).strip()
        add_history(cid, message.author.display_name, content)
        _spawn(_run_short(message, theme or None), cid, "ショート制作")
        return

    if route == "hf_model":
        model, mtype, label = _match_gen_model(content)
        add_history(cid, message.author.display_name, content)
        _spawn(_run_hf_generate(message, content, model, mtype, label), cid, "動画/画像生成")
        return

    if route == "hf_auto":
        mtype = "image" if re.search("画像|イラスト|ロゴ|絵|写真|アイコン", content) else "video"
        add_history(cid, message.author.display_name, content)
        _spawn(_run_hf_generate(message, content, None, mtype, "自動選定"), cid, "動画/画像生成")
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
        _spawn(_run_motion_control(message, req, _video_att), cid, "モーション生成")
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
        intent, text = await _interpret_video_turn(cid, content, lg)
        if intent == "revise":
            add_history(cid, message.author.display_name, content)
            _spawn(_run_revise(message, content), cid, "作り直し")
            return
        if intent == "new":
            add_history(cid, message.author.display_name, content)
            _spawn(_run_hf_generate(message, text or content, None,
                                    lg.get("media_type", "video"), "自動選定",
                                    aspect_ratio=lg.get("aspect_ratio")),
                   cid, "動画生成")
            return
        # intent == "chat" → 下の通常会話へ（動画の感想・質問はそのまま会話）

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

    # YouTubeリンクが貼られていたら、Geminiが動画を視聴して内容を文脈に合併
    content, yt_bare_link = await _apply_youtube_context(message, content)

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
        asyncio.create_task(_update_profile(cid, sp))

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
