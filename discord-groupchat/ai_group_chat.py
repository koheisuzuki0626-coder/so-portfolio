"""
Discord上で Claude と Gemini が会話するグループチャット
=====================================================
構成: Discord Bot 3体（オーケストレーター / Claude / Gemini）を
1つのPythonプロセスで動かす。会話の進行はオーケストレーターが制御。
Claude と Gemini は「対等な話し相手」として交互に発言する。

--- セットアップ ---
1) Discord Developer Portal (https://discord.com/developers/applications) で
   アプリを3つ作成（Orchestrator / Claude / Gemini）→ 各「Bot」タブでトークン取得
   ※ Orchestrator用Botのみ「MESSAGE CONTENT INTENT」をONにすること（必須）
2) 各アプリの OAuth2 > URL Generator で scope=bot / 権限=Send Messages を選び、
   生成URLから3体とも同じサーバー（チャンネル）に招待
3) python3 -m venv venv && source venv/bin/activate
   pip install -U discord.py anthropic google-genai python-dotenv
   （Python 3.10以上推奨）
4) このファイルと同じ場所に .env を作成（.env.example 参照）:
   DISCORD_ORCH_TOKEN=xxx
   DISCORD_CLAUDE_TOKEN=xxx
   DISCORD_GEMINI_TOKEN=xxx
   ANTHROPIC_API_KEY=xxx
   GEMINI_API_KEY=xxx
5) python ai_group_chat.py
6) Discordのチャンネルで:
   !talk 好きなお題   → 会話開始
   !stop             → 途中停止
"""

import asyncio
import os

import discord
from anthropic import Anthropic
from dotenv import load_dotenv
from google import genai

load_dotenv()

# ---------- 設定 ----------
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
MAX_TURNS = int(os.getenv("MAX_TURNS", "6"))  # 1回の!talkでの合計発言数（暴走防止）
REPLY_CHARS = 300                             # 1発言の目安文字数
SEND_DELAY = 2                                # 発言間の待ち秒数

anthropic_client = Anthropic()  # 環境変数 ANTHROPIC_API_KEY を自動参照
gemini_client = genai.Client()  # 環境変数 GEMINI_API_KEY を自動参照

# ---------- Discordクライアント3体 ----------
orch_intents = discord.Intents.default()
orch_intents.message_content = True  # コマンド読取りに必要（Portal側の設定もON必須）
orch = discord.Client(intents=orch_intents)

claude_bot = discord.Client(intents=discord.Intents.default())
gemini_bot = discord.Client(intents=discord.Intents.default())

state = {"running": False, "stop": False}


def build_transcript(history):
    return "\n".join(f"{name}: {text}" for name, text in history)


def persona(me, partner):
    return (
        f"あなたは{me}。Discordのグループチャットで{partner}と対話している。"
        f"{partner}は対等な話し相手であり、上下関係はない。"
        f"日本語で、{REPLY_CHARS}字以内で1発言だけ返す。前置きや名乗りは不要。"
        "相手の直前の発言を踏まえ、同意だけでなく自分の視点も加えること。"
    )


async def ask_claude(topic, history):
    prompt = (
        f"お題: {topic}\n\nこれまでの会話:\n"
        f"{build_transcript(history) or '(まだ無し)'}\n\n次のあなたの発言:"
    )

    def _call():
        resp = anthropic_client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=500,
            system=persona("Claude", "Gemini"),
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text

    return await asyncio.to_thread(_call)


async def ask_gemini(topic, history):
    prompt = (
        persona("Gemini", "Claude") + "\n\n"
        f"お題: {topic}\n\nこれまでの会話:\n"
        f"{build_transcript(history) or '(まだ無し)'}\n\n次のあなたの発言:"
    )

    def _call():
        resp = gemini_client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt
        )
        return resp.text

    return await asyncio.to_thread(_call)


async def send_as(bot, channel_id, text):
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    await channel.send(text[:1900])  # Discordの2000字制限対策


async def run_conversation(channel_id, topic):
    state["running"], state["stop"] = True, False
    history = []
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
                text = await ask(topic, history)
            except Exception as e:
                await send_as(orch, channel_id, f"⚠️ {name} のAPI呼び出し失敗: {e}")
                break
            history.append((name, text))
            await send_as(bot, channel_id, text)
            await asyncio.sleep(SEND_DELAY)
        await send_as(orch, channel_id, "🏁 会話終了（!talk お題 で再開できます）")
    finally:
        state["running"] = False


@orch.event
async def on_ready():
    print(f"オーケストレーター起動: {orch.user}")


@orch.event
async def on_message(message):
    if message.author.bot:
        return  # 人間のコマンドだけ受け付ける（Bot同士の無限ループ防止）
    content = message.content.strip()
    if content.startswith("!talk"):
        if state["running"]:
            await message.channel.send("会話が進行中です。!stop で止められます。")
            return
        topic = content[len("!talk"):].strip() or "自由なテーマで雑談"
        await message.channel.send(
            f"🎙️ お題「{topic}」で最大 {MAX_TURNS} 発言の会話を開始します"
        )
        asyncio.create_task(run_conversation(message.channel.id, topic))
    elif content == "!stop":
        state["stop"] = True
        await message.channel.send("⏹️ 停止します")


async def main():
    await asyncio.gather(
        orch.start(os.environ["DISCORD_ORCH_TOKEN"]),
        claude_bot.start(os.environ["DISCORD_CLAUDE_TOKEN"]),
        gemini_bot.start(os.environ["DISCORD_GEMINI_TOKEN"]),
    )


if __name__ == "__main__":
    asyncio.run(main())
