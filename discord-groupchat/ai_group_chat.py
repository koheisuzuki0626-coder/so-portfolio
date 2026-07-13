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
import base64
import io
import json
import os
import re
import time
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
CLAUDE_TIMEOUT = 120

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
        try:
            new_summary = await _gemini_call(prompt)
        except Exception:  # noqa: BLE001
            new_summary = await run_claude_cli(prompt)
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
    """Claude Code CLI をヘッドレスで呼ぶ（サブスク利用・API課金なし）。"""
    # cwd を固定 → discord-groupchat/.claude/settings.json（WebSearch許可）が読まれる。
    # ※ワークスペースを一度「信頼(trust)」しておかないと settings.json は無視される。
    proc = await asyncio.create_subprocess_exec(
        CLAUDE_BIN, "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=BASE_DIR,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=CLAUDE_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError("claude CLI がタイムアウトしました")
    if proc.returncode != 0:
        raise RuntimeError((err.decode() or "claude CLI error").strip()[:300])
    return out.decode().strip()


def peer_persona(me, partner):
    return (
        f"あなたは{me}。人間たちと{partner}が参加するDiscordのグループチャットにいる。"
        f"{partner}も人間も対等な仲間。日本語で{REPLY_CHARS}字以内、1発言だけで自然に参加する。"
        "前置きや名乗りは不要。直前の流れを踏まえ、自分の視点も述べること。"
    )


def peer_prompt(me, partner, history):
    return (
        peer_persona(me, partner) + "\n\nこれまでの会話ログ:\n"
        f"{build_transcript(history)}\n\n次の {me} の発言:"
    )


async def ask_claude(history):
    return await run_claude_cli(peer_prompt("Claude", "Gemini", history))


def _is_quota_error(e):
    m = str(e)
    return "429" in m or "RESOURCE_EXHAUSTED" in m or "quota" in m.lower()


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


def _gemini_analyze_media_sync(data, mime_type, prompt, tag):
    """Gemini API にメディア（画像/音声/動画/PDF）を渡して分析結果を得る。
    テキスト版 _gemini_call と同じく、無料枠切れモデルはクールダウンして
    次のモデルへ自動ローテーションする。"""
    print(f"[{tag}] MIME type: {mime_type}, size: {len(data)} bytes")

    try:
        media_part = _make_media_part(data, mime_type)
    except Exception as e:
        print(f"[{tag}] Part生成失敗: {str(e)[:200]}")
        return ""

    contents = [media_part, prompt]

    now = time.time()
    last_err = None
    for model in GEMINI_MODELS:
        if _gemini_cooldown.get(model, 0) > now:
            continue  # 枠切れクールダウン中は次のモデルへ
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
        return "（Gemini の無料枠が全モデルで上限に達しています。時間をおいて再度お試しください。）"
    return ""


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
# オーケストレーター：3層構造
#   ① ルーティング（得意モデルへ振り分け／簡単なら単発）
#   ② ディベート（各回答を相互に見せて批判・修正）
#   ③ 司令塔が統合（合意点/対立点を整理して単一回答へ）
# ---------------------------------------------------------------------------
def _answer_prompt(who, history, extra=""):
    return (
        f"あなたは{who}。次の会話の最後の要求に、正確で役立つ回答を日本語で簡潔に述べる。"
        "前置きや名乗りは不要、回答本体のみ。\n\n"
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


async def _route(history):
    """① モデル振り分け・多段化・Web検索要否を判定。JSONで受ける。"""
    prompt = (
        "あなたはルーター。次の会話の最後の要求に最適な処理方針をJSONだけで返す。\n"
        '形式: {"mode":"single"|"debate","lead":"claude"|"gemini","search":true|false,"recall":true|false}\n'
        "- 原則 single（1モデルで即答）。debate は"
        "『重大な判断・設計・事実の突き合わせが本当に必要』な時だけ（省エネ重視）。\n"
        "- lead は適材適所で選ぶ（コスト節約ではなく品質基準で）：\n"
        "  claude が得意 → コード・デバッグ・論理的推論・設計判断・"
        "丁寧で構成の良い日本語の文章・長文の執筆\n"
        "  gemini が得意 → 長文の要約・多言語翻訳・最新情報・画像や視覚の話題・"
        "大量の箇条書き整理・幅広いアイデア出し\n"
        "  どちらでも良いタスクは、その要求に本当に向いている方を選ぶ\n"
        "- 最新情報・時事・製品/価格・実在の事実確認が要る → search=true、雑談や一般常識 → search=false\n"
        "- 『前に話した』『昨日の』『以前決めた』など過去の会話・経緯への言及や、"
        "直近の会話に無い過去の情報が必要 → recall=true、それ以外 → recall=false\n\n"
        f"会話:\n{build_transcript(history)}\n\nJSON:"
    )
    try:
        raw = await run_claude_cli(prompt)
        m = re.search(r"\{.*\}", raw, re.S)
        d = json.loads(m.group(0)) if m else {}
        mode = d.get("mode") if d.get("mode") in ("single", "debate") else "single"
        lead = d.get("lead") if d.get("lead") in ("claude", "gemini") else "claude"
        search = bool(d.get("search"))
        recall = bool(d.get("recall"))
        return mode, lead, search, recall
    except Exception:  # noqa: BLE001
        return "single", "claude", False, False


async def _classify_task(latest_msg):
    """最後の発言を厳密に分類：exec（実際にコード/ファイルを触る）/ video / image / chat。
    迷ったら chat（＝普段の会話を邪魔しない）。"""
    prompt = (
        "次のユーザー発言を1語だけで分類して返す: exec / video / image / chat\n"
        "exec = ファイルやコードを実際に作成・編集・削除する、またはコマンドを実行する"
        "明確な作業指示。例:『requirements.txtにyt-dlpを追加して』『server.pyのバグを直して』"
        "『このコード書き換えて』『npm installして』\n"
        "video = 動画・映像・CM・PVの制作依頼。例:『犬の動画作って』\n"
        "image = 画像・イラスト・ロゴ・絵の生成依頼。例:『猫のイラスト描いて』『ロゴ画像作って』\n"
        "chat = それ以外すべて（質問・相談・意見・説明・提案・雑談）。"
        "例:『どう思う?』『Pythonって何?』『どう直すのがいい?』\n"
        "重要：実際にファイルを触る/コマンドを走らせる明確な指示だけが exec。"
        "単なる質問や相談は必ず chat。迷ったら chat。\n\n"
        f"発言: {latest_msg}\n\n分類（1語だけ）:"
    )
    try:
        raw = (await run_claude_cli(prompt)).strip().lower()
    except Exception:  # noqa: BLE001
        return "chat"
    if "exec" in raw:
        return "exec"
    if "video" in raw:
        return "video"
    if "image" in raw:
        return "image"
    return "chat"


async def _handle_image_request(cid, request):
    """画像生成の依頼は Gemini（無料枠 約500枚/日）で完結。クレジット消費なし。"""
    await send_as(orch, cid, "🎨 Gemini で画像を生成中…（無料枠）")
    try:
        data = await asyncio.to_thread(_gemini_generate_image_sync, request)
        await send_image_bytes(cid, "✅ できました！修正したい点があれば教えてください。", data, "image.png")
        add_history(cid, "Orchestrator", f"（依頼「{request[:60]}」の画像をGeminiで生成して投稿した）")
    except Exception as e:  # noqa: BLE001
        print(f"[image_request] 失敗: {str(e)[:200]}")
        if _is_quota_error(e):
            await send_as(orch, cid, "⚠️ Gemini画像生成の本日の無料枠が上限です。時間をおいて再度お試しください。")
        else:
            await send_as(orch, cid, f"⚠️ 画像生成に失敗: {str(e)[:200]}")


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
    mode, lead, search, recall = await _route(history)
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
        return await run_claude_cli(_answer_prompt("Claude", history, ctx))

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
    asyncio.create_task(_run_agent_task(cid, content, message.author.id))


async def _handle_orchestrator(message, cid):
    """オーケストレーター宛て。実際にコード/ファイルを触る指示のときだけ承認ダイアログ。
    それ以外（質問・相談・雑談）は普通に会話する。"""
    history = get_history(cid)

    # 厳密判定：exec（コードを触る）/ video（映像制作）/ chat（それ以外）
    kind = await _classify_task(_latest_user_msg(history))
    if kind == "video":
        last_msg = _latest_user_msg(history)
        await message.channel.send("🎬 映像制作の依頼ですね。構成案から始めます…")
        asyncio.create_task(pipeline_start(cid, last_msg))
        return
    if kind == "exec":
        last_msg = _latest_user_msg(history)
        await _start_agent(message, cid, last_msg)
        return
    if kind == "image":
        asyncio.create_task(_handle_image_request(cid, _latest_user_msg(history)))
        return

    # 通常会話（承認ダイアログは出さない）
    mode, lead, search, recall = await _route(history)
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
    add_history(cid, name, text)
    await send_as(bot, cid, text)


def decide_targets(message, content):
    """宛先から反応者を決める。宛先が無ければオーケストレーターが統合回答。"""
    low = content.lower()
    named_claude = (
        "claude" in low or "クロード" in content
        or (claude_bot.user and claude_bot.user in message.mentions)
    )
    named_gemini = (
        "gemini" in low or "ジェミニ" in content or "ジェミナイ" in content
        or (gemini_bot.user and gemini_bot.user in message.mentions)
    )
    named_orch = (
        "オーケストレーター" in content or "orchestrator" in low
        or (orch.user and orch.user in message.mentions)
    )
    targets = []
    if named_orch:
        targets.append(("Orchestrator", orch, ask_orchestrator))
    if named_claude:
        targets.append(("Claude", claude_bot, ask_claude))
    if named_gemini:
        targets.append(("Gemini", gemini_bot, ask_gemini))
    if not targets:  # 宛先指定なし → 既定はオーケストレーターの統合回答
        targets.append(("Orchestrator", orch, ask_orchestrator))
    return targets


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
    if IMAGE_GEN_ENGINE == "higgsfield" and not HF_AVAILABLE:
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
    engine_label = "Gemini・無料枠" if IMAGE_GEN_ENGINE == "gemini" else "Higgsfield"
    await send_as(orch, cid, f"🎨 絵コンテ画像を生成中…（{engine_label}）")
    images = []
    for i, sc in enumerate(p["scenes"], 1):
        if not _alive(cid, p):  # !stop で中止された
            return
        url = None
        # ① まず Gemini（無料枠）で生成 → Discordに添付し、そのCDN URLを動画化に使う
        if IMAGE_GEN_ENGINE == "gemini":
            try:
                data = await asyncio.to_thread(_gemini_generate_image_sync, sc)
                url = await send_image_bytes(cid, f"シーン{i}: {sc}", data, f"scene{i}.png")
            except Exception as e:  # noqa: BLE001
                print(f"[gemini_image] シーン{i} 失敗: {str(e)[:200]}")
                if _is_quota_error(e):
                    await send_as(orch, cid, "⚠️ Gemini画像生成の無料枠上限。Higgsfieldに切替えます…")
                elif HF_AVAILABLE:
                    await send_as(orch, cid, f"⚠️ シーン{i}: Gemini失敗 → Higgsfieldで再試行…")
        # ② フォールバック：Higgsfield（クレジット消費）
        if url is None and HF_AVAILABLE:
            try:
                url = await hf_wrapper.generate_image(sc)
                await send_as(orch, cid, f"シーン{i}: {sc}\n{url}")
            except Exception as e:  # noqa: BLE001
                if _is_credit_error(e):
                    await send_as(orch, cid, CREDIT_MSG)
                    return
                await send_as(orch, cid, f"⚠️ シーン{i} の画像生成に失敗: {e}")
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
            vurl = await hf_wrapper.generate_video(img, prompt=(feedback or p["topic"]))
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


async def _run_claude_exec(task):
    """承認済みタスクをフル権限で実行し、標準出力を返す。"""
    proc = await asyncio.create_subprocess_exec(
        CLAUDE_BIN, "-p", "--dangerously-skip-permissions", task,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=BASE_DIR,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=600)
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

    # ② Discordで承認（ボタン）
    fut = asyncio.get_running_loop().create_future()
    await send_as(
        orch, cid,
        f"🤖 タスク: {task}\n\n📋 実行プラン:\n{plan[:1500]}\n\n"
        "この計画で実行しますか？ [✅許可] を押すと **Mac上で実際に実行**します"
        "（実行した本人のみ操作可・5分で自動却下）。",
        view=PermissionView(fut, owner_id),
    )
    try:
        approved = await asyncio.wait_for(fut, timeout=310)
    except asyncio.TimeoutError:
        approved = False
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


@orch.event
async def on_ready():
    print(f"オーケストレーター起動: {orch.user}")


@orch.event
async def on_message(message):
    if message.author.bot:
        return
    content = message.content.strip()
    # テキストまたは添付ファイルがない場合は無視
    if not content and not message.attachments:
        return
    cid = message.channel.id

    # 初回のみ：導入前の過去ログをDiscordから取り込む（バックグラウンド）
    asyncio.create_task(_backfill_channel_history(message.channel))

    if content == "!stop" or _is_stop_phrase(content):
        await _do_stop(message, cid)
        return
    if content.startswith("!project"):
        topic = content[len("!project"):].strip()
        if not topic:
            await message.channel.send("使い方: !project お題（例: !project 犬が主役の30秒CM）")
            return
        if projects.get(cid):
            await message.channel.send("進行中のプロジェクトがあります。!cancel で中止できます。")
            return
        asyncio.create_task(pipeline_start(cid, topic))
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
        asyncio.create_task(_run_agent_task(cid, task, message.author.id))
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

    add_history(cid, message.author.display_name, content)

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
