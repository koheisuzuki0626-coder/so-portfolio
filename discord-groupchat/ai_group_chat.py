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
import os

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
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
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
async def run_claude_cli(prompt):
    """Claude Code CLI をヘッドレスで呼ぶ（サブスク利用・API課金なし）。"""
    proc = await asyncio.create_subprocess_exec(
        CLAUDE_BIN, "-p", prompt,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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


async def ask_gemini(history):
    prompt = peer_prompt("Gemini", "Claude", history)

    def _call():
        return gemini_client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt
        ).text

    return await asyncio.to_thread(_call)


async def ask_orchestrator(history):
    """司令塔。Geminiの意見を（取れれば）取り込み、Claudeで1つの回答にまとめる。"""
    gemini_view = ""
    try:
        gemini_view = await ask_gemini(history)
    except Exception:
        gemini_view = ""  # Geminiが使えなくてもClaude単独で続行

    prompt = (
        "あなたは司令塔（オーケストレーター）。人間とのグループチャットで、"
        "部下だが対等な仲間である Claude と Gemini の力を借りて、"
        f"最良の【単一の回答】をまとめて返す。日本語で{REPLY_CHARS}字以内、結論を先に簡潔に。"
        "『Claudeが〜』『Geminiが〜』などの実況や名乗りは不要。回答本体だけを書く。\n\n"
        f"これまでの会話ログ:\n{build_transcript(history)}\n\n"
        + (
            f"参考：Geminiの意見（鵜呑みにせず取捨選択して統合すること）:\n{gemini_view}\n\n"
            if gemini_view.strip()
            else ""
        )
        + "上を踏まえた、あなた（オーケストレーター）の最終回答:"
    )
    return await run_claude_cli(prompt)


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
    async with message.channel.typing():
        for name, bot, ask in decide_targets(message, content):
            try:
                await respond(cid, name, bot, ask)
            except Exception as e:
                await send_as(orch, cid, f"⚠️ {name} の呼び出し失敗: {e}")
            await asyncio.sleep(1)


async def main():
    await asyncio.gather(
        orch.start(os.environ["DISCORD_ORCH_TOKEN"]),
        claude_bot.start(os.environ["DISCORD_CLAUDE_TOKEN"]),
        gemini_bot.start(os.environ["DISCORD_GEMINI_TOKEN"]),
    )


if __name__ == "__main__":
    asyncio.run(main())
