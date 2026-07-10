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
import json
import os
import re
import time

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
HISTORY_LIMIT = 24
CLAUDE_TIMEOUT = 120

gemini_client = genai.Client()  # 環境変数 GEMINI_API_KEY を自動参照

orch_intents = discord.Intents.default()
orch_intents.message_content = True
orch = discord.Client(intents=orch_intents)
claude_bot = discord.Client(intents=discord.Intents.default())
gemini_bot = discord.Client(intents=discord.Intents.default())

state = {"running": False, "stop": False}
histories = {}  # channel_id -> list[(speaker, text)]


def get_history(cid):
    return histories.setdefault(cid, [])


def add_history(cid, speaker, text):
    h = get_history(cid)
    h.append((speaker, text))
    del h[:-HISTORY_LIMIT]


def build_transcript(history):
    return "\n".join(f"{name}: {text}" for name, text in history) or "(まだ会話なし)"


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


# ---------------------------------------------------------------------------
# オーケストレーター：3層構造
#   ① ルーティング（得意モデルへ振り分け／簡単なら単発）
#   ② ディベート（各回答を相互に見せて批判・修正）
#   ③ 司令塔が統合（合意点/対立点を整理して単一回答へ）
# ---------------------------------------------------------------------------
NO_TOOLS_NOTE = (
    "重要：あなたはチャットで文章を返すだけで、コマンド実行・ファイル編集・"
    "許可ボタンの表示はできない。実際に実行/編集してほしい要求には、"
    "『許可ボタンを出した』等と偽らず、"
    "『Discordで `!agent <やりたいこと>` と打ってください（プラン提示→承認ボタン→実行）』"
    "と案内すること。"
)


def _answer_prompt(who, history, extra=""):
    return (
        f"あなたは{who}。次の会話の最後の要求に、正確で役立つ回答を日本語で簡潔に述べる。"
        "前置きや名乗りは不要、回答本体のみ。" + NO_TOOLS_NOTE + "\n\n"
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
        '形式: {"mode":"single"|"debate","lead":"claude"|"gemini","search":true|false,"action":true|false}\n'
        "- 原則 single（1モデルで即答）。debate は"
        "『重大な判断・設計・事実の突き合わせが本当に必要』な時だけ（省エネ重視）。\n"
        "- コード・論理・技術寄り → lead=claude ／ 最新情報・画像・幅広い発想 → lead=gemini\n"
        "- 最新情報・時事・製品/価格・実在の事実確認が要る → search=true、雑談や一般常識 → search=false\n"
        "- ファイル編集・コード変更・コマンド実行など『実際に手を動かす作業』を頼まれている"
        "（例:『〜を直して/追加して/作って/実行して/削除して』）→ action=true。"
        "質問・相談・説明だけ → action=false\n\n"
        f"会話:\n{build_transcript(history)}\n\nJSON:"
    )
    try:
        raw = await run_claude_cli(prompt)
        m = re.search(r"\{.*\}", raw, re.S)
        d = json.loads(m.group(0)) if m else {}
        mode = d.get("mode") if d.get("mode") in ("single", "debate") else "single"
        lead = d.get("lead") if d.get("lead") in ("claude", "gemini") else "claude"
        search = bool(d.get("search"))
        action = bool(d.get("action"))
        return mode, lead, search, action
    except Exception:  # noqa: BLE001
        return "single", "claude", False, False


async def _synthesize(claude_ans, gemini_ans, history, extra=""):
    """③ 司令塔が統合。合意点を軸に、対立点があれば触れて単一回答へ。"""
    prompt = (
        "あなたは司令塔（オーケストレーター）。ClaudeとGeminiの回答を統合し、単一の最終回答を作る。"
        "両者の【合意点】を軸に据え、【対立点】があれば簡潔に触れて最も妥当な結論を示す。"
        f"実況や『Claudeが〜』等は書かず、回答本体のみ。日本語で{REPLY_CHARS}字以内、結論を先に。"
        + NO_TOOLS_NOTE + "\n\n"
        + (extra + "\n\n" if extra else "")
        + f"会話:\n{build_transcript(history)}\n\n"
        f"Claudeの回答:\n{claude_ans}\n\n"
        f"Geminiの回答:\n{gemini_ans}\n\n最終回答:"
    )
    return await run_claude_cli(prompt)


def _latest_user_msg(history):
    return history[-1][1] if history else ""


async def ask_orchestrator(history):
    mode, lead, search, _action = await _route(history)
    return await _orchestrate(mode, lead, search, history)


async def _orchestrate(mode, lead, search, history):
    # 必要ならWeb検索して文脈を用意（ボット自身が検索＝権限プロンプト不要）
    ctx = await web_search_context(_latest_user_msg(history)) if search else ""

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


async def _handle_orchestrator(message, cid):
    """オーケストレーター宛て。実行/編集の指示なら承認フロー、それ以外は通常回答。"""
    history = get_history(cid)
    mode, lead, search, action = await _route(history)
    if action:
        await message.channel.send(
            "🛠 実行/編集の指示と判断しました。プランを作ります…"
            "（[✅許可]で実行 / [❌拒否]で中止）"
        )
        asyncio.create_task(
            _run_agent_task(cid, message.content.strip(), message.author.id)
        )
        return
    async with message.channel.typing():
        try:
            answer = await _orchestrate(mode, lead, search, history)
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
    if not HF_AVAILABLE:
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
    await send_as(orch, cid, "🎨 絵コンテ画像を生成中…（Higgsfield）")
    images = []
    for i, sc in enumerate(p["scenes"], 1):
        if not _alive(cid, p):  # !stop で中止された
            return
        try:
            url = await hf_wrapper.generate_image(sc)
            images.append(url)
            await send_as(orch, cid, f"シーン{i}: {sc}\n{url}")
        except Exception as e:  # noqa: BLE001
            if _is_credit_error(e):
                await send_as(orch, cid, CREDIT_MSG)
                return
            await send_as(orch, cid, f"⚠️ シーン{i} の画像生成に失敗: {e}")
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


@orch.event
async def on_ready():
    print(f"オーケストレーター起動: {orch.user}")


@orch.event
async def on_message(message):
    if message.author.bot:
        return
    content = message.content.strip()
    if not content:
        return
    cid = message.channel.id

    if content == "!stop":
        state["stop"] = True
        if projects.pop(cid, None):
            await message.channel.send(
                "⏹️ 進行中のプロジェクトを停止しました。以降は通常の会話に戻ります。"
            )
        else:
            await message.channel.send("⏹️ 停止します")
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

    add_history(cid, message.author.display_name, content)
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
