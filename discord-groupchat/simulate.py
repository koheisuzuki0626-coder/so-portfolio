"""
エンドツーエンド・シミュレーション：実際の on_message を通して全経路を検証。
使い方:  python3 simulate.py

classify_route の単体テスト（test_routing.py）より一段深く、実際の
_dispatch_message / on_message を、ダミーのDiscordメッセージで駆動する。
検証項目:
  1. 各入力で「正しいハンドラ」が起動するか（ルーティングの実挙動）
  2. どんな入力でも未捕捉例外が漏れないか（沈黙失敗ゼロ）
  3. 例外注入時にグローバルガードが働き、ユーザー通知＋errors.log記録が起きるか
外部I/O（claude/gemini/MCP/http/discord）は全てスタブ化し、ネットワークを使わない。
"""
import asyncio
import os
import sys
import types

os.environ.update({"GEMINI_API_KEY": "x", "DISCORD_ORCH_TOKEN": "x",
                   "DISCORD_CLAUDE_TOKEN": "x", "DISCORD_GEMINI_TOKEN": "x",
                   "YOUTUBE_API_KEY": "yt-key"})


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _Anything:
    def __getattr__(self, k):
        return _Anything()

    def __call__(self, *a, **k):
        if len(a) == 1 and callable(a[0]) and not k:
            return a[0]
        return _Anything()


class _FakeIntents:
    @staticmethod
    def default():
        i = _FakeIntents()
        i.message_content = False
        return i


class _FakeClient:
    def __init__(self, *a, **k):
        self.user = _FakeUser(999, "Orchestrator")

    def event(self, f):
        return f

    def get_channel(self, cid):
        return None

    async def fetch_channel(self, cid):
        return _FakeChannel(cid)


class _FakeUser:
    def __init__(self, uid, name):
        self.id = uid
        self.display_name = name
        self.bot = False


discord = _stub("discord")
discord.Intents = _FakeIntents
discord.Client = _FakeClient
discord.File = lambda *a, **k: None
discord.ui = types.SimpleNamespace(
    View=object, Button=object, button=lambda **k: (lambda f: f)
)
discord.ButtonStyle = _Anything()
discord.Interaction = object

genai_mod = _stub("google.genai")
genai_mod.Client = lambda *a, **k: types.SimpleNamespace(models=None)
_stub("google", genai=genai_mod)
sys.modules["google.genai"] = genai_mod
_stub("dotenv", load_dotenv=lambda *a, **k: None)
_stub("aiohttp", ClientSession=object, ClientTimeout=lambda **k: None)

import ai_group_chat as bot  # noqa: E402


# ---- ダミーのDiscordオブジェクト ----
class _FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class _FakeChannel:
    def __init__(self, cid):
        self.id = cid
        self.sent = []

    async def send(self, text, **k):
        self.sent.append(text)
        return types.SimpleNamespace(attachments=[])

    def typing(self):
        return _FakeTyping()


class _FakeAttachment:
    def __init__(self, filename):
        self.filename = filename
        self.url = f"https://example.com/{filename}"
        self.size = 1000


class _FakeMessage:
    def __init__(self, content, attachments=None, uid=1, name="kohei"):
        self.content = content
        self.attachments = attachments or []
        self.author = _FakeUser(uid, name)
        self.channel = _FakeChannel(1234)
        self.mentions = []


# ---- 記録用にハンドラを差し替え ----
FIRED = []


def _rec(label):
    async def _f(*a, **k):
        FIRED.append(label)
    return _f


def _rec_str(label, ret):
    async def _f(*a, **k):
        FIRED.append(label)
        return ret
    return _f


def install_stubs(mcp_url=None):
    FIRED.clear()
    bot._self_diagnose = _rec_str("diagnose", "🩺 診断結果（スタブ）")
    bot._run_hf_generate = _rec("hf_generate")
    bot._run_motion_control = _rec("motion_control")
    bot._run_trend_study = _rec("trend")
    bot._run_agent_task = _rec("agent")
    bot.pipeline_start = _rec("project")
    bot.run_auto = _rec("talk")
    bot.pipeline_reply = _rec("pipeline_reply")
    bot._handle_orchestrator = _rec("orchestrator")
    bot._do_stop = _rec("stop")
    bot._restart_self = _rec("restart")
    bot._update_profile = _rec("profile_update")
    bot._backfill_channel_history = _rec("backfill")

    async def _mcp_status(*a, **k):
        return mcp_url
    bot._mcp_gen_status = _mcp_status

    async def _web(*a, **k):
        return "検索結果ctx"
    bot.web_search_context = _web

    async def _claude(*a, **k):
        return "claude回答"
    bot.run_claude_cli = _claude

    async def _extract(*a, **k):
        return ""
    bot.extract_attachment_context = _extract

    async def _yt(msg, content):
        return content, False
    bot._apply_youtube_context = _yt

    async def _respond(cid, name, b, ask):
        FIRED.append(f"respond:{name}")
    bot.respond = _respond

    # 状態は毎回リセット
    bot._pending_motion.clear()
    bot._pending_approvals.clear()
    bot.projects.clear()
    bot.state["running"] = False
    bot._import_started.add(1234)  # backfill抑制
    bot._load_motion_job = lambda: None


async def drive(content, attachments=None, expect=None):
    """1メッセージを流し、起動ハンドラと送信メッセージ・例外を返す。"""
    msg = _FakeMessage(content, attachments)
    err = None
    try:
        await bot.on_message(msg)
    except Exception as e:  # noqa: BLE001
        err = e
    # create_taskで走る記録コルーチンを消化
    for _ in range(5):
        await asyncio.sleep(0)
    return {"fired": list(FIRED), "sent": msg.channel.sent, "err": err}


ok = 0
fail = 0


def check(desc, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print(f"  ❌ {desc} {detail}")


async def run():
    # ===== ① コマンド・機能ルーティング（実 on_message）=====
    print("■ E2E: コマンド/機能ルーティング")
    cases = [
        # (入力, 期待して起動するハンドラ label or None, 添付)
        ("システムチェックして", "diagnose", None),
        ("エラー教えて", None, None),                 # sent に🔴、ハンドラ無し
        ("!stop", "stop", None),
        ("止めて", "stop", None),
        ("再起動して", "restart", None),
        ("!restart", "restart", None),
        ("!project 犬のCM", "project", None),
        ("!agent ファイル一覧", "agent", None),
        ("!trend", "trend", None),
        ("!trend 料理", "trend", None),
        ("!talk 猫の話", "talk", None),
        ("seedanceで犬の動画作って", "hf_generate", None),
        ("おまかせで海の動画作って", "hf_generate", None),
        ("この動きで生成して", None, None),           # motion_ask → sent案内のみ
        ("犬について教えて", "orchestrator", None),    # 通常会話→オーケストレーター
        ("おはよう", "orchestrator", None),
    ]
    for content, expect, att in cases:
        install_stubs()
        r = await drive(content, att)
        check(f"{content!r}", r["err"] is None, f"例外={r['err']}")
        if expect:
            check(f"{content!r}→{expect}", expect in r["fired"],
                  f"実際={r['fired']} sent={r['sent'][:1]}")

    # motion_ask は案内文が送られる
    install_stubs()
    r = await drive("モーションコントロールで作りたい")
    check("motion_ask 案内文", any("動きの元になる動画" in s for s in r["sent"]),
          f"sent={r['sent']}")
    check("motion_ask 例外なし", r["err"] is None)

    # 動画添付＋モーション → motion_control
    install_stubs()
    r = await drive("この動きで", [_FakeAttachment("ref.mp4")])
    check("動画添付→motion_control", "motion_control" in r["fired"], f"{r['fired']}")

    # ===== ② 状態確認（ジョブ有無で分岐）=====
    print("■ E2E: 状態確認")
    install_stubs(mcp_url=None)
    bot._load_motion_job = lambda: {"cid": 1234, "submitted_at": bot.time.time(),
                                    "media_type": "video", "label": "テスト動画"}
    r = await drive("あとどれくらい？")
    check("あとどれ→ETA表示", any("経過" in s or "生成中" in s for s in r["sent"]),
          f"sent={r['sent']}")
    check("あとどれ 例外なし", r["err"] is None)

    install_stubs(mcp_url="https://example.com/done.mp4")
    bot._load_motion_job = lambda: {"cid": 1234, "submitted_at": bot.time.time(),
                                    "media_type": "video", "label": "テスト動画"}
    r = await drive("モーション動画できた？")
    check("完成→URL表示", any("done.mp4" in s for s in r["sent"]), f"sent={r['sent']}")

    # ===== ③ ストレス：異常・境界入力で例外が漏れないこと =====
    print("■ E2E: ストレス（例外ゼロ）")
    stress = [
        "", "   ", "。", "？", "!", "!!!", "www", "😀😀😀",
        "あ" * 3000,                       # 超長文
        "http://youtu.be/xxxx",             # YouTubeリンク
        "seedance " * 200,                  # モデル名連発
        "モーション画像動画生成できた作って", # キーワード衝突
        "\n\n\n", "```code```", "@everyone",
        "システムチェックエラー再起動作って",  # 複数トリガー衝突
    ]
    for s in stress:
        install_stubs()
        r = await drive(s)
        check(f"stress {s[:16]!r} 例外なし", r["err"] is None, f"例外={r['err']}")

    # 添付バリエーション
    for fn in ["a.png", "a.mp4", "a.mp3", "a.pdf", "a.txt", "a.xyz"]:
        install_stubs()
        r = await drive("", [_FakeAttachment(fn)])
        check(f"添付 {fn} 例外なし", r["err"] is None, f"例外={r['err']}")

    # ===== ④ グローバル例外ガードの動作確認 =====
    print("■ E2E: 例外ガード（沈黙失敗の防止）")
    install_stubs()
    import tempfile
    import pathlib
    bot.ERROR_LOG = pathlib.Path(tempfile.mkdtemp()) / "errors.log"

    async def _boom(*a, **k):
        raise RuntimeError("わざと壊す")
    bot._handle_orchestrator = _boom
    r = await drive("普通の会話メッセージ")
    check("例外がユーザーに通知される",
          any("エラー" in s for s in r["sent"]), f"sent={r['sent']}")
    check("例外がerrors.logに記録される",
          bot.ERROR_LOG.exists() and "わざと壊す" in bot.ERROR_LOG.read_text())
    check("on_message自体は例外を外に出さない", r["err"] is None, f"例外={r['err']}")

    print(f"\n結果: ✅ {ok} 件成功 / ❌ {fail} 件失敗")
    return fail == 0


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(run()) else 1)
