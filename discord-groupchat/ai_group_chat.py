"""
Discord グループチャット（人間 × Claude × Gemini）
==================================================
Discord Bot 3体（オーケストレーター / Claude / Gemini）を1プロセスで動かす。
人間がチャンネルで発言すると Claude と Gemini が会話に参加する。名前で宛先指定も可能。

使い方（チャンネルで）:
  ・普通に発言           → Claude と Gemini の両方が反応
  ・「クロード ○○」など   → Claude だけ反応（@メンションでもOK）
  ・「gemini ○○」など    → Gemini だけ反応
  ・!talk お題           → Claude と Gemini だけで自動トーク（最大 MAX_TURNS 発言）
  ・!stop                → 自動トークを停止

Claude担当は `claude` CLI（Claude Code のサブスク）を使うので Anthropic API 課金は不要。
Gemini担当は Gemini API の無料枠キー（GEMINI_API_KEY）を使う。

セットアップ手順は README.md を参照。
※ MESSAGE CONTENT INTENT は「オーケストレーター」用Botだけ ON にすればよい
  （メッセージを読むのはオーケストレーターだけ。Claude/Gemini Botは送信専用）。
"""

import asyncio
import os

import discord
from dotenv import load_dotenv
from google import genai

load_dotenv()

# ---------- 設定 ----------
CLAUDE_BIN = os.getenv("CLAUDE_BIN", "claude")     # Claude Code CLI（サブスク利用）
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MAX_TURNS = int(os.getenv("MAX_TURNS", "6"))       # !talk での合計発言数（暴走防止）
REPLY_CHARS = 300                                  # 1発言の目安文字数
SEND_DELAY = 2                                     # 自動トークの発言間の待ち秒数
HISTORY_LIMIT = 24                                 # チャンネルごとに保持する会話数
CLAUDE_TIMEOUT = 120                               # claude CLI のタイムアウト秒

gemini_client = genai.Client()  # 環境変数 GEMINI_API_KEY を自動参照

# ---------- Discordクライアント3体 ----------
orch_intents = discord.Intents.default()
orch_intents.message_content = True  # メッセージ本文の読取りに必要（Portal側もON必須）
orch = discord.Client(intents=orch_intents)

claude_bot = discord.Client(intents=discord.Intents.default())  # 送信専用
gemini_bot = discord.Client(intents=discord.Intents.default())  # 送信専用

state = {"running": False, "stop": False}
histories = {}  # channel_id -> list[(speaker, text)]


def get_history(cid):
    return histories.setdefault(cid, [])


def add_history(cid, speaker, text):
    h = get_history(cid)
    h.append((speaker, text))
    del h[:-HISTORY_LIMIT]  # 直近だけ残す


def build_transcript(history):
    return "\n".join(f"{name}: {text}" for name, text in history) or "(まだ会話なし)"


def persona(me, partner):
    return (
        f"あなたは{me}。人間たちと{partner}が参加するDiscordのグループチャットにいる。"
        f"{partner}も人間もあなたと対等な仲間で、上下関係はない。"
        f"日本語で{REPLY_CHARS}字以内、1発言だけで会話に自然に参加する。前置きや名乗りは不要。"
        "直前の流れを踏まえ、相手に話しかけたり、同意だけでなく自分の視点も述べること。"
    )


def make_prompt(me, partner, history):
    return (
        persona(me, partner) + "\n\nこれまでの会話ログ:\n"
        f"{build_transcript(history)}\n\n次の {me} の発言:"
    )


async def ask_claude(history):
    """Claude Code CLI をヘッドレスで呼ぶ（サブスク利用・API課金なし）。"""
    prompt = make_prompt("Claude", "Gemini", history)
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


async def ask_gemini(history):
    prompt = make_prompt("Gemini", "Claude", history)

    def _call():
        resp = gemini_client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt
        )
        return resp.text

    return await asyncio.to_thread(_call)


async def send_as(bot, channel_id, text):
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    await channel.send((text or "(空の応答)")[:1900])  # Discordの2000字制限対策


async def respond(cid, name, bot, ask):
    """あるAIに履歴を渡して発言させ、履歴に追記してチャンネルに送る。"""
    text = await ask(get_history(cid))
    add_history(cid, name, text)
    await send_as(bot, cid, text)


def decide_targets(message, content):
    """メッセージの宛先から、反応するAIを決める。宛先なしなら両方。"""
    low = content.lower()
    want_claude = (
        "claude" in low or "クロード" in content
        or (claude_bot.user and claude_bot.user in message.mentions)
    )
    want_gemini = (
        "gemini" in low or "ジェミニ" in content or "ジェミナイ" in content
        or (gemini_bot.user and gemini_bot.user in message.mentions)
    )
    if not want_claude and not want_gemini:
        want_claude = want_gemini = True  # 宛先指定なし → 両方参加
    targets = []
    if want_claude:
        targets.append(("Claude", claude_bot, ask_claude))
    if want_gemini:
        targets.append(("Gemini", gemini_bot, ask_gemini))
    return targets


async def run_auto(cid, topic):
    """Claude と Gemini だけで自動的に会話（!talk 用）。"""
    state["running"], state["stop"] = True, False
    speakers = [
        ("Claude", claude_bot, ask_claude),
        ("Gemini", gemini_bot, ask_gemini),
    ]
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


@orch.event
async def on_ready():
    print(f"オーケストレーター起動: {orch.user}")


@orch.event
async def on_message(message):
    if message.author.bot:
        return  # 人間の発言だけに反応（Bot同士の無限ループ防止）
    content = message.content.strip()
    if not content:
        return
    cid = message.channel.id

    if content == "!stop":
        state["stop"] = True
        await message.channel.send("⏹️ 停止します")
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
        return  # 未知のコマンドは無視

    # --- 通常のグループ会話 ---
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
