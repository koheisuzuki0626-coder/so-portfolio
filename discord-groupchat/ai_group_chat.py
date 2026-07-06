"""
Discord上で Claude と Gemini が会話するグループチャット
=====================================================
構成: Discord Bot 3体（オーケストレーター / Claude / Gemini）を
1つのPythonプロセスで動かす。進行はオーケストレーターが制御し、
Claude と Gemini は「対等な話し相手」として交互に発言する。

Claude担当は `claude` CLI（Claude Code のサブスク）を使うため、
Anthropic APIの課金は不要。Gemini担当は Gemini API の無料枠キーを使う。

--- セットアップ ---
1) Discord Developer Portal でアプリを3つ作成（Orchestrator / Claude / Gemini）
   → 各「Bot」タブでトークン取得
   ※ Orchestrator用Botのみ「MESSAGE CONTENT INTENT」をONにすること（必須）
2) OAuth2 > URL Generator で scope=bot / 権限=Send Messages を選び、
   3体とも同じサーバー（チャンネル）に招待
3) `claude` CLI がログイン済みであること（`claude -p "hi"` が返事すればOK）
4) python3 -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
5) .env を作成（.env.example 参照）:
   DISCORD_ORCH_TOKEN / DISCORD_CLAUDE_TOKEN / DISCORD_GEMINI_TOKEN / GEMINI_API_KEY
6) python ai_group_chat.py
7) Discordのチャンネルで:  !talk 好きなお題   /  !stop
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
MAX_TURNS = int(os.getenv("MAX_TURNS", "6"))       # 1回の!talkでの合計発言数（暴走防止）
REPLY_CHARS = 300                                  # 1発言の目安文字数
SEND_DELAY = 2                                     # 発言間の待ち秒数
CLAUDE_TIMEOUT = 120                               # claude CLI のタイムアウト秒

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


def make_prompt(me, partner, topic, history):
    return (
        persona(me, partner) + "\n\n"
        f"お題: {topic}\n\nこれまでの会話:\n"
        f"{build_transcript(history) or '(まだ無し)'}\n\n次のあなたの発言:"
    )


async def ask_claude(topic, history):
    """Claude Code CLI をヘッドレスで呼ぶ（サブスク利用・API課金なし）。"""
    prompt = make_prompt("Claude", "Gemini", topic, history)
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


async def ask_gemini(topic, history):
    prompt = make_prompt("Gemini", "Claude", topic, history)

    def _call():
        resp = gemini_client.models.generate_content(
            model=GEMINI_MODEL, contents=prompt
        )
        return resp.text

    return await asyncio.to_thread(_call)


async def send_as(bot, channel_id, text):
    channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
    await channel.send((text or "(空の応答)")[:1900])  # Discordの2000字制限対策


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
                await send_as(orch, channel_id, f"⚠️ {name} の呼び出し失敗: {e}")
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
