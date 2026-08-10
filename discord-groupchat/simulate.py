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
        return _channel(cid)


class _FakeUser:
    def __init__(self, uid, name):
        self.id = uid
        self.display_name = name
        self.bot = False


discord = _stub("discord")
discord.Intents = _FakeIntents
discord.Client = _FakeClient
discord.File = lambda *a, **k: None
class _FakeView:                       # discord.ui.View 相当（timeout等を受ける）
    def __init__(self, *a, **k):
        pass


discord.ui = types.SimpleNamespace(
    View=_FakeView, Button=object, button=lambda **k: (lambda f: f)
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

# 本物の実装を控えておく（スタブに差し替えたあとでも中身を検証するため）
_REAL_GEN_IMG = bot._gemini_generate_image_sync

# 実物の会話ハンドラ（install_stubs で記録用に差し替わる前に退避）。
# 「判定＋返事の1回化」など、会話ハンドラ本体の挙動を検証するテストで使う。
_HANDLE_ORCH = bot._handle_orchestrator
_EXTRACT_ATT = bot.extract_attachment_context
_MEDIA_CTX = bot._apply_media_url_context
_YT_CTX = bot._apply_youtube_context
_REPLY_CTX = bot._reply_context
_IMAGE_REQ = bot._handle_image_request
_LOAD_LAST_GEN = bot._load_last_gen
_INSPECT = bot._inspect_result
_REPORT = bot._report_result
_DESCRIBE_MEDIA = bot._describe_media_url
_AI_TEXT_BG = bot._ai_text_bg


# ---- ダミーのDiscordオブジェクト ----
class _FakeTyping:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


_CHANNELS = {}   # cid -> _FakeChannel（send_as経由の送信も同じ箱に集める）


def _channel(cid):
    """cidごとに同じチャンネルを返す。実物の send_as は
    orch.fetch_channel(cid) から送るため、毎回別オブジェクトを返すと
    テストで送信内容を取りこぼす。"""
    if cid not in _CHANNELS:
        _CHANNELS[cid] = _FakeChannel(cid)
    return _CHANNELS[cid]


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
    def __init__(self, content, attachments=None, uid=1, name="kohei",
                 reference=None):
        self.content = content
        self.attachments = attachments or []
        self.author = _FakeUser(uid, name)
        self.channel = _channel(1234)
        self.mentions = []
        if reference is not None:
            self.reference = reference


def _reply_to(text, attachments=None, name="Orchestrator"):
    """「この発言に返信した」状態を作る（reference.resolved 相当）。"""
    src = types.SimpleNamespace(
        content=text, attachments=attachments or [], author=_FakeUser(999, name))
    return types.SimpleNamespace(resolved=src, message_id=42)


# ---- 記録用にハンドラを差し替え ----
# 何が呼ばれたか（FIRED）だけでなく、【どんな引数で】呼ばれたかも残す。
# ルートだけ見て合格にしていたため、「題材は直したが媒体は動画のまま」
# という半端な修正を通してしまった。仕事の中身まで検証できるようにする。
FIRED = []
CALLS = []


def _rec(label):
    async def _f(*a, **k):
        FIRED.append(label)
        CALLS.append((label, a, k))
    return _f


def _rec_str(label, ret):
    async def _f(*a, **k):
        FIRED.append(label)
        CALLS.append((label, a, k))
        return ret
    return _f


def last_call(label):
    """最後にその処理が呼ばれたときの (args, kwargs)。無ければ None。"""
    for lb, a, k in reversed(CALLS):
        if lb == label:
            return a, k
    return None


def install_stubs(mcp_url=None):
    FIRED.clear()
    CALLS.clear()
    bot.CONFIRM_BEFORE_WORK = False   # 確認は専用テストで検証する
    for ch in _CHANNELS.values():
        ch.sent.clear()   # 前のテストの送信内容が混ざらないように
    bot._self_diagnose = _rec_str("diagnose", "🩺 診断結果（スタブ）")
    bot._run_hf_generate = _rec("hf_generate")
    bot._run_short = _rec("short")
    bot._run_revise = _rec("revise")
    bot._run_ad_make = _rec("ad")
    bot._run_virality = _rec("virality")
    bot._run_style_learn = _rec("style_learn")
    globals().setdefault("_REAL_IMAGE_REQ", bot._handle_image_request)
    bot._handle_image_request = _rec("image_gen")
    bot._share_debug_log = _rec_str("sharelog", "✅ 共有しました")
    bot._run_credits = _rec_str("credits", "💳 残クレジット: 1,200（スタブ）")
    bot._run_video_edit = _rec("edit")
    bot._run_design = _rec("design")
    bot._analyze_my_channel = _rec("ch_stats")
    bot._run_multi_view = _rec("multiview")

    async def _insp(req, url, mt="image"):
        return True, ""
    bot._inspect_result = _insp        # 既定は「合格」（他テストに影響させない）
    # 個別テストで差し替えたものを毎回戻す（スタブの漏れで後続が誤って落ちるのを防ぐ）
    bot._describe_media_url = _DESCRIBE_MEDIA
    bot._ai_text_bg = _AI_TEXT_BG
    import tempfile as _tf
    import pathlib as _pl
    bot.STYLE_PROFILE_FILE = _pl.Path(_tf.mkdtemp()) / "style_profile.md"
    bot._load_last_gen = lambda cid: None  # 既定は直前生成なし

    async def _vturn(cid, latest, last):
        return ("chat", latest)   # 既定はchat（会話へ流す）
    bot._interpret_video_turn = _vturn
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

    async def _media(msg, content, cid):
        return content
    bot._apply_media_url_context = _media

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


async def _dl_ok(url):
    """添付ダウンロードのスタブ（中身は使わない）。"""
    return b"data"


ok = 0
fail = 0


def check(desc, cond, detail=""):
    global ok, fail
    if cond:
        ok += 1
    else:
        fail += 1
        print(f"  ❌ {desc} {detail}")


import time as _tm4


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
        ("バズる動画作って", "hf_generate", None),
        ("ショート作って", "short", None),
        ("猫のイラスト作って", "image_gen", None),
        ("サムネのデザイン作って", "design", None),
        ("バナー作って", "design", None),
        ("価格表のデザインして", "design", None),
        ("ロゴをデザインして", "image_gen", None),
        ("クロードでサムネ作って", "design", None),
        ("クロードで豊臣兄弟の相関図を作って", "design", None),
        ("家系図作って", "design", None),
        ("geminiで猫のサムネ作って", "image_gen", None),
        ("犬の動画作って", "hf_generate", None),
        ("新作スニーカーの広告作って", "ad", None),
        ("コーヒーショップのCM作って", "ad", None),
        ("バズ度分析して", "virality", None),
        ("ログ送って", "sharelog", None),
        ("veo3で動画作ると何クレジット？", "credits", None),
        ("クレジットあとどれくらい残ってる？", "credits", None),
        ("実績分析して", "ch_stats", None),
        ("多角的に見て", "multiview", None),
        ("この動画の広告効果を予測して", "virality", None),
        ("!short 夜の都市", "short", None),
        ("今日のショートお願い", "short", None),
        ("この動きで生成して", None, None),           # motion_ask → sent案内のみ
        ("モーション動画じゃないよ、広告動画だよ", "orchestrator", None),  # 訂正は会話へ
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

    # 料金を聞いただけで生成が始まらないこと（実際に起きた事故の再発防止）
    for _q in ("veo3.1生成するときクレジットいくらくらいか聞きたいだけ",
               "画像生成の料金っていくら",
               "動画作るのにいくらかかるか知りたい"):
        install_stubs()
        r = await drive(_q)
        check(f"{_q!r} で生成しない",
              not ({"hf_generate", "image_gen", "short"} & set(r["fired"])),
              f"実際={r['fired']}")

    # 「クロードで作り直して」がHiggsfieldの画像生成に流れないこと
    install_stubs()
    bot._load_last_gen = lambda cid: {
        "prompt": "豊臣兄弟の相関図", "media_type": "image",
        "aspect_ratio": "1600:1200", "label": "デザイン（図（相関図・年表など））",
        "url": "https://example.com/soukanzu.png", "t": bot.time.time()}
    r = await drive("クロードで作り直して")
    check("クロードで作り直して→デザイン", "design" in r["fired"], f"実際={r['fired']}")
    check("クロードで作り直して→画像生成しない",
          "hf_generate" not in r["fired"] and "revise" not in r["fired"],
          f"実際={r['fired']}")
    install_stubs()
    bot._load_last_gen = lambda cid: {
        "prompt": "豊臣兄弟の相関図", "media_type": "image",
        "aspect_ratio": "1600:1200", "label": "デザイン（図（相関図・年表など））",
        "url": "https://example.com/soukanzu.png", "t": bot.time.time()}
    r = await drive("背景を暗くして")
    check("デザインの手直しもデザインで", "design" in r["fired"], f"実際={r['fired']}")

    # 再起動後に「相関図できた？」と聞かれても、機能の存在を否定しない
    install_stubs()
    bot._load_last_gen = lambda cid: None
    r = await drive("相関図できた？")
    check("相関図できた？→事実を返す",
          any("直近の完成物もありません" in s for s in r["sent"]), f"sent={r['sent']}")
    check("相関図できた？→AIに流さない",
          "orchestrator" not in r["fired"], f"fired={r['fired']}")

    # 実行中に「まだ？」と聞かれたら、作り話をせず実行中だと答える
    # （実際に「その機能自体がまだ実装できていない」と答えた事故の再発防止）
    import time as _tm2
    install_stubs()
    bot._running[1234] = {"デザイン制作": _tm2.time() - 90}
    try:
        r = await drive("まだ？")
        check("実行中を報告する",
              any("実行中" in s2 for s2 in r["sent"]), f"sent={r['sent']}")
        check("実行中の作業名を出す",
              any("デザイン制作" in s2 for s2 in r["sent"]), f"sent={r['sent']}")
        check("AI応答に流さない（作り話をさせない）",
              "orchestrator" not in r["fired"], f"実際={r['fired']}")
    finally:
        bot._running.pop(1234, None)

    # 不具合を訴えたら、頼まれる前にログを自動共有すること
    install_stubs()
    bot._last_autolog.clear()
    r = await drive("挙動がおかしい")
    await asyncio.sleep(0)
    check("不具合の訴えでログを自動共有", "sharelog" in r["fired"], f"実際={r['fired']}")
    # 連投では送り直さない
    install_stubs()
    r = await drive("まだおかしい")
    await asyncio.sleep(0)
    check("短時間の連投では送り直さない",
          "sharelog" not in r["fired"], f"実際={r['fired']}")
    install_stubs()
    bot._last_autolog.clear()
    r = await drive("おはよう")
    await asyncio.sleep(0)
    check("普通の発言では共有しない", "sharelog" not in r["fired"], f"実際={r['fired']}")
    bot._last_autolog.clear()

    # 既定ではGeminiが返事を書かないこと
    install_stubs()
    _kl2 = bot.gen_settings.get("casual_lead")
    bot.gen_settings["casual_lead"] = ""
    try:
        _plan_out = await bot._plan([("kohei", "おはよう")])
        check("雑談の担当はクロード", _plan_out[2], "claude")
        check("Geminiの返事はオフ", bot._gemini_replies_on() is False, True)
    finally:
        bot.gen_settings["casual_lead"] = _kl2

    # 雑談の担当をDiscordから切り替えられること
    _keep_lead = bot.gen_settings.get("casual_lead")
    try:
        install_stubs()
        r = await drive("返事はクロードにして")
        check("雑談の担当が変わる", bot.gen_settings.get("casual_lead") == "claude",
              f"実際={bot.gen_settings.get('casual_lead')}")
        check("担当変更を伝える",
              any("クロード" in s2 for s2 in r["sent"]), f"sent={r['sent']}")
        check("担当変更はAI応答に流さない",
              "orchestrator" not in r["fired"], f"実際={r['fired']}")
    finally:
        bot.gen_settings["casual_lead"] = _keep_lead

    # 実行中の「いつできる？」も状態確認になること
    import time as _tm3
    install_stubs()
    bot._running[1234] = {"デザイン制作": _tm3.time() - 200}
    try:
        r = await drive("いつできる？")
        check("いつできる？→実行中を報告",
              any("実行中" in s2 for s2 in r["sent"]), f"sent={r['sent']}")
        check("いつできる？→AIに流さない",
              "orchestrator" not in r["fired"], f"実際={r['fired']}")
    finally:
        bot._running.pop(1234, None)

    # 会話モデルの切替がDiscordだけで完結すること
    _keep_model = bot.gen_settings.get("claude_model")
    try:
        for _say, _val, _label in (("ハイクにして", "haiku", "Haiku"),
                                   ("モデルをsonnetにして", "sonnet", "Sonnet"),
                                   ("モデルを既定に戻して", "", "既定")):
            install_stubs()
            r = await drive(_say)
            check(f"{_say!r} でモデルが変わる",
                  bot.gen_settings.get("claude_model") == _val,
                  f"実際={bot.gen_settings.get('claude_model')}")
            check(f"{_say!r} の結果を伝える",
                  any(_label in s2 for s2 in r["sent"]), f"sent={r['sent']}")
            check(f"{_say!r} でAI応答に流さない",
                  "orchestrator" not in r["fired"], f"実際={r['fired']}")
        install_stubs()
        r = await drive("いまのモデルは？")
        check("現在のモデルを答える",
              any("いまの会話モデル" in s2 for s2 in r["sent"]), f"sent={r['sent']}")
    finally:
        bot.gen_settings["claude_model"] = _keep_model

    # 料金照会の直後は、続きの短い質問も権限のある経路で答える
    install_stubs()
    await drive("veo3で動画作ると何クレジット？")
    install_stubs()          # FIRED はクリアされるが _last_credits は残る
    r = await drive("画像生成はどうなの？")
    check("照会の続きも credits へ", "credits" in r["fired"], f"実際={r['fired']}")
    bot._last_credits.clear()

    # 会話パスが権限エラーを言い出したら、権限のある経路で調べ直す
    install_stubs()
    _orig_plan = bot._plan

    async def _denied_plan(history):
        return "chat", "single", "claude", False, False, \
               "権限が下りなくてツールが動かない状況。"
    bot._plan = _denied_plan
    try:
        r = await drive("画像生成のクレジットどうなってる")
        check("権限エラーは見せずに調べ直す", "credits" in r["fired"], f"実際={r['fired']}")
        check("権限エラーの言い訳を送らない",
              not any("権限が下り" in s for s in r["sent"]), f"sent={r['sent']}")
    finally:
        bot._plan = _orig_plan
        bot._last_credits.clear()

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

    # スタイル学習（動画添付→学習 / 添付なし→案内 / 未学習→表示は案内）
    install_stubs()
    r = await drive("これを学習して", [_FakeAttachment("ref.mp4")])
    check("動画添付＋学習して→style_learn", "style_learn" in r["fired"], f"{r['fired']}")

    install_stubs()
    r = await drive("この動画のスタイルを学習して")
    check("添付なし→添付の案内", any("学習して" in s for s in r["sent"]),
          f"sent={r['sent']}")

    install_stubs()
    r = await drive("学習したスタイル見せて")
    check("スタイル表示(未学習)", any("まだスタイル" in s for s in r["sent"]),
          f"sent={r['sent']}")
    check("スタイル表示 例外なし", r["err"] is None)

    # 直前生成あり＋作り直し → revise
    install_stubs()
    bot._load_last_gen = lambda cid: {"prompt": "a cat", "media_type": "video",
                                      "aspect_ratio": None, "label": "x", "t": bot.time.time()}
    r = await drive("もう一回作り直して、顔をアップで")
    check("作り直し→revise", "revise" in r["fired"], f"{r['fired']}")

    # 動画制作モード：あいまい発言→AI解釈（intentに応じて分岐）
    print("■ E2E: 動画制作モード（文脈解釈）")
    for intent, want_fired in [("revise", "revise"), ("new", "hf_generate"), ("chat", "orchestrator")]:
        install_stubs()
        bot._load_last_gen = lambda cid: {"prompt": "a cat", "media_type": "video",
                                          "aspect_ratio": "9:16", "label": "x",
                                          "t": bot.time.time()}
        async def _vt(cid, latest, last, _i=intent):
            return (_i, latest)
        bot._interpret_video_turn = _vt
        r = await drive("イマイチだから変えて")
        check(f"制作モード intent={intent}", want_fired in r["fired"], f"{r['fired']}")

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

    # ジョブも直近生成も無い →「事実だけ」を返す。会話に流すとAIが
    # 「その機能は実装されていない」と理由を作り話する事故が実際に起きた
    install_stubs()
    r = await drive("モーション動画できた？")
    check("ジョブ無し→事実を返す",
          any("動いている作業も、直近の完成物もありません" in s for s in r["sent"]),
          f"fired={r['fired']} sent={r['sent']}")
    check("ジョブ無し→AIに作り話をさせない",
          "orchestrator" not in r["fired"], f"fired={r['fired']}")
    check("ジョブ無し 例外なし", r["err"] is None)

    # ジョブ無し・直近生成が完成済み（URLあり）→ 即答で再案内
    install_stubs()
    bot._load_last_gen = lambda cid: {"prompt": "a cat", "media_type": "video",
                                      "aspect_ratio": "9:16", "label": "テストCM",
                                      "url": "https://example.com/last.mp4",
                                      "t": bot.time.time()}
    r = await drive("動画できた？")
    check("完成済み→即URL再案内", any("last.mp4" in s for s in r["sent"]),
          f"sent={r['sent']}")
    check("完成済み→会話が続く案内", any("バズ度" in s for s in r["sent"]),
          f"sent={r['sent']}")

    # 直近の生成が新しければ「できた？」だけでも状態確認につながる
    install_stubs()
    bot._load_last_gen = lambda cid: {"prompt": "a cat", "media_type": "video",
                                      "aspect_ratio": "9:16", "label": "広告CM",
                                      "url": "https://example.com/ad.mp4",
                                      "t": bot.time.time()}
    r = await drive("できた？")
    check("できた？→直近生成を即答", any("ad.mp4" in s for s in r["sent"]),
          f"fired={r['fired']} sent={r['sent']}")

    # 生成が何も無い時の「動画できた？」は会話へ流れ、履歴が二重登録されないこと
    install_stubs()
    bot._load_last_gen = lambda cid: None
    before = len(bot.get_history(1234))
    r = await drive("動画できた？")
    added = len(bot.get_history(1234)) - before
    check("生成なし→事実を返す（AIに理由を作らせない）",
          any("直近の完成物もありません" in s for s in r["sent"]), f"sent={r['sent']}")
    check("履歴の二重登録なし（発言1件＋応答1件）", added <= 2, f"追加={added}件")

    # ===== ②-b 高速化：判定と返事を1回のAI呼び出しで済ませる =====
    print("■ E2E: 判定＋返事の1回化（速度）")
    calls = []

    async def _ai_merged(prompt, tag="x"):
        calls.append(tag)
        return ('{"kind":"chat","mode":"single","lead":"claude",'
                '"search":false,"recall":false}\n'
                "---REPLY---\nおはよう！今日は何する？")
    install_stubs()
    bot._handle_orchestrator = _HANDLE_ORCH      # 実物の会話ハンドラを使う
    bot._ai_text = _ai_merged
    r = await drive("この動画の作り方どう思う？")
    check("返事が届く", any("今日は何する" in s for s in r["sent"]), f"sent={r['sent']}")
    check("AI呼び出しは1回だけ", len(calls) == 1, f"実際={len(calls)}回")
    check("例外なし", r["err"] is None, f"{r['err']}")

    # 区切りが無い（従来形式）なら回答フェーズにフォールバックすること
    calls.clear()

    async def _ai_plan_only(prompt, tag="x"):
        calls.append(tag)
        return '{"kind":"chat","mode":"single","lead":"claude"}'
    install_stubs()
    bot._handle_orchestrator = _HANDLE_ORCH
    bot._ai_text = _ai_plan_only
    r = await drive("この動画の作り方どう思う？")
    check("返事なし→従来の回答フェーズで応答",
          any("claude回答" in s for s in r["sent"]), f"sent={r['sent']}")
    check("フォールバックでも例外なし", r["err"] is None, f"{r['err']}")

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
    # 主題が無い画像依頼は生成せず聞き返す（無駄な生成を避ける）
    install_stubs()
    r = await drive("geminiで画像生成して")
    check("主題なし→聞き返す", any("どんな画像" in x for x in r["sent"]), f"{r['sent']}")
    check("主題なし→生成しない", "image_gen" not in r["fired"], f"{r['fired']}")

    # ===== ②-c 作業前の反復確認（承認するまで実行しない）=====
    print("■ E2E: 作業前の反復確認")
    install_stubs()
    bot.CONFIRM_BEFORE_WORK = True
    r = await drive("犬の動画作って")
    check("確認を出す", any("確認させてください" in x for x in r["sent"]), f"{r['sent']}")
    check("理解した内容を示す", any("ご依頼の理解" in x for x in r["sent"]), f"{r['sent']}")
    check("コストを示す", any("クレジット" in x for x in r["sent"]), f"{r['sent']}")
    check("承認前は実行しない", "hf_generate" not in r["fired"], f"{r['fired']}")

    # 「OK」で実行される
    r2 = await drive("OK")
    for _ in range(5):
        await asyncio.sleep(0)
    check("OKで実行される", "hf_generate" in r2["fired"], f"{r2['fired']}")

    # 「やめて」で中止され、実行されない
    install_stubs()
    bot.CONFIRM_BEFORE_WORK = True
    r = await drive("猫のイラスト作って")
    check("画像でも確認を出す", any("確認させてください" in x for x in r["sent"]), f"{r['sent']}")
    r2 = await drive("やめて")
    for _ in range(5):
        await asyncio.sleep(0)
    check("拒否で実行しない", "image_gen" not in r2["fired"], f"{r2['fired']}")
    check("中止を伝える", any("やめました" in x for x in r2["sent"]), f"{r2['sent']}")

    # 会話・状態確認には確認を挟まない（余計な往復を増やさない）
    install_stubs()
    bot.CONFIRM_BEFORE_WORK = True
    r = await drive("おはよう")
    check("雑談には確認を出さない",
          not any("確認させてください" in x for x in r["sent"]), f"{r['sent']}")
    bot.CONFIRM_BEFORE_WORK = False

    # ===== ②-d 画像生成は日本語のまま渡さず英語プロンプトに変換する =====
    print("■ E2E: 画像プロンプトの英訳")
    used = {}

    def _gen_img(prompt, ref_bytes=None, ref_mime="image/png"):
        used["prompt"] = prompt
        return b"PNG"
    install_stubs()
    bot._handle_image_request = _IMAGE_REQ
    bot._gemini_generate_image_sync = _gen_img

    async def _refine(req, mtype, style="", has_ref=False):
        return "a 30 year old japanese man cheering with a beer mug, victory"
    bot._refine_prompt = _refine

    async def _send_img(cid, text, data, name):
        _channel(1234).sent.append(text)
    bot.send_image_bytes = _send_img
    import tempfile as _tf2
    import pathlib as _pl2
    bot._LASTGEN_FILE = _pl2.Path(_tf2.mkdtemp()) / "last_gen.json"
    await _IMAGE_REQ(1234, "30歳が酒飲んで優勝してる画像作って")
    check("日本語のまま渡さない", "30歳" not in used.get("prompt", ""), used)
    check("英語プロンプトで生成", "beer mug" in used.get("prompt", ""), used)
    check("使ったプロンプトを見せる",
          any("🖋" in x for x in _channel(1234).sent), _channel(1234).sent[:2])
    check("作り直せると案内する",
          any("作り直して" in x for x in _channel(1234).sent), _channel(1234).sent[-1:])
    lg = _LOAD_LAST_GEN(1234)
    check("画像も作り直しの対象として記録", bool(lg and lg.get("prompt")), lg)

    # 完パケ編集（素材がある時だけ動く）
    install_stubs()
    bot._load_last_gen = lambda cid: {"prompt": "a cat", "media_type": "video",
                                      "label": "自動選定", "url": "https://ex.com/a.mp4",
                                      "t": bot.time.time()}
    r = await drive("字幕つけて")
    check("字幕→編集が動く", "edit" in r["fired"], f"{r['fired']}")
    install_stubs()
    r = await drive("字幕つけて")
    check("素材が無ければ編集しない", "edit" not in r["fired"], f"{r['fired']}")

    # ===== ②-e 生成物の自動検品（依頼と食い違ったら先に知らせる）=====
    print("■ E2E: 生成物の自動検品")
    install_stubs()
    bot._inspect_result = _INSPECT

    async def _desc_ok(url, channel=None):
        return "ビールを掲げて喜ぶ30代の男性の写真"
    bot._describe_media_url = _desc_ok

    async def _judge(prompt, tag="x"):
        return '{"ok": true}'
    bot._ai_text_bg = _judge
    res_ok, res_why = await _INSPECT("30歳が酒飲んで優勝してる画像",
                                     "https://ex.com/a.png")
    check("一致なら合格", res_ok is True, f"{res_ok} {res_why}")

    async def _judge_ng(prompt, tag="x"):
        return '{"ok": false, "reason": "三面図になっており人物が喜んでいない"}'
    bot._ai_text_bg = _judge_ng
    res_ok, res_why = await _INSPECT("30歳が酒飲んで優勝してる画像",
                                     "https://ex.com/a.png")
    check("不一致を検出", res_ok is False, f"{res_ok}")
    check("理由が付く", "三面図" in res_why, res_why)

    # 不一致のときはユーザーに先に知らせる
    await _REPORT(1234, "30歳が酒飲んで優勝してる画像",
                  "https://ex.com/a.png", "image", "✅ できました！")
    sent = _channel(1234).sent
    check("警告を先に出す", any("依頼と違うもの" in x for x in sent), f"{sent[-1:]}")
    check("作り直しを案内", any("作り直して" in x for x in sent), f"{sent[-1:]}")

    # 画像が見られない場合は止めない（誤検知で作業を止めない）
    async def _desc_ng(url, channel=None):
        return ""
    bot._describe_media_url = _desc_ng
    res_ok, _ = await _INSPECT("依頼", "https://ex.com/a.png")
    check("見られない時は素通し", res_ok is True, f"{res_ok}")

    # ===== ②-f 「まだ？」は今やっている作業の進捗を答える =====
    print("■ E2E: 実行中の作業の進捗")
    install_stubs()
    bot._load_last_gen = lambda cid: {"prompt": "a cat", "media_type": "image",
                                      "label": "画像", "url": "https://ex.com/old.png",
                                      "t": bot.time.time()}
    bot._running[1234] = {"ログ共有": bot.time.time() - 42}
    r = await drive("まだ？")
    check("実行中の作業を答える",
          any("ログ共有" in x and "実行中" in x for x in r["sent"]), f"{r['sent']}")
    check("無関係な直近画像を出さない",
          not any("old.png" in x for x in r["sent"]), f"{r['sent']}")
    bot._running.pop(1234, None)

    # 何も実行中でなければ従来どおり直近の完成物を案内する
    install_stubs()
    bot._load_last_gen = lambda cid: {"prompt": "a cat", "media_type": "image",
                                      "label": "画像", "url": "https://ex.com/old.png",
                                      "t": bot.time.time()}
    r = await drive("まだ？")
    check("非実行中は直近の完成物", any("old.png" in x for x in r["sent"]), f"{r['sent']}")

    # 作業の登録と解除が自動で行われる
    install_stubs()

    async def _slow():
        await asyncio.sleep(0)
    t = bot._spawn(_slow(), 1234, "テスト作業")
    check("開始で登録される", any(n == "テスト作業" for n, _ in bot._running_for(1234)),
          f"{bot._running_for(1234)}")
    await t
    check("終了で解除される", bot._running_for(1234) == [], f"{bot._running_for(1234)}")

    # ===== ③-b 添付ファイルの読み取り（種類ごとの仕様テーブル）=====
    print("■ E2E: 添付ファイルの内容理解")
    seen = []

    def _fake_analyze(data, mime, prompt, tag):
        seen.append((mime, tag))
        return f"解析結果({tag})"

    install_stubs()
    bot._download_file = _dl_ok
    bot._gemini_analyze_media_sync = _fake_analyze
    msg = _FakeMessage("", [_FakeAttachment("a.png"), _FakeAttachment("b.mp3"),
                            _FakeAttachment("c.mp4"), _FakeAttachment("d.pdf")])
    ctx = await _EXTRACT_ATT(msg)
    check("画像を解析", "解析結果(gemini_analyze_image)" in ctx, ctx[:120])
    check("音声を書き起こし", "音声の書き起こし: b.mp3" in ctx, ctx[:200])
    check("動画を解析", "動画の内容: c.mp4" in ctx, ctx[:200])
    check("PDFを解析", "PDFの内容: d.pdf" in ctx, ctx[:200])
    check("MIMEが種類ごとに正しい",
          [m for m, _ in seen][1:] == ["audio/mp3", "video/mp4", "application/pdf"],
          f"{seen}")
    check("書き起こしはチャンネルにも投稿",
          any("書き起こし" in x for x in msg.channel.sent), f"{msg.channel.sent}")

    # サイズ超過・ダウンロード失敗・未対応拡張子
    install_stubs()
    bot._download_file = _dl_ok
    bot._gemini_analyze_media_sync = _fake_analyze
    big = _FakeAttachment("big.mp4")
    big.size = 30 * 1024 * 1024
    ctx = await _EXTRACT_ATT(_FakeMessage("", [big]))
    check("20MB超は案内を返す", "20MBを超える" in ctx, ctx[:150])

    install_stubs()
    bot._gemini_analyze_media_sync = _fake_analyze

    async def _dl_ng(url):
        return None
    bot._download_file = _dl_ng
    ctx = await _EXTRACT_ATT(_FakeMessage("", [_FakeAttachment("x.pdf")]))
    check("DL失敗を報告", "ダウンロード失敗" in ctx, ctx[:150])

    install_stubs()
    bot._download_file = _dl_ok
    ctx = await _EXTRACT_ATT(_FakeMessage("", [_FakeAttachment("x.zip")]))
    check("未対応拡張子でも落ちない", "x.zip" in ctx, ctx[:150])

    # 枠切れ時は握りつぶさず案内する
    install_stubs()
    bot._download_file = _dl_ok

    def _quota(*a, **k):
        raise bot.GeminiQuotaExceeded("枠切れです")
    bot._gemini_analyze_media_sync = _quota
    ctx = await _EXTRACT_ATT(_FakeMessage("", [_FakeAttachment("y.pdf")]))
    check("枠切れを案内", "枠切れです" in ctx, ctx[:150])

    # ===== ③-b2 YouTubeが視聴できない時、メタ情報＋字幕で答えられること =====
    print("■ E2E: 動画が視聴できない時の代替情報")
    YT = "https://youtu.be/L5LATULmdJo"

    def _watch_ng(*a, **k):
        raise bot.GeminiQuotaExceeded("枠切れです")

    async def _meta_ok(vid):
        return {"title": "中国AI KIMI K3の衝撃", "channel": "TBS CROSS DIG",
                "published": "2026-07-29", "desc": "Kimi K3の性能について",
                "tags": ["AI"], "views": 98765, "duration": "24:00"}

    async def _cap_ok(vid, limit=6000):
        return "今日はKimi K3の性能について話します。" * 10

    async def _cap_ng(vid, limit=6000):
        return ""

    for _cap, _label, _want in ((_cap_ok, "字幕あり", "字幕（書き起こし）"),
                                (_cap_ng, "字幕なし", "タイトル: 中国AI")):
        install_stubs()
        bot._gemini_watch_youtube_sync = _watch_ng
        bot._fetch_video_meta, bot._fetch_captions = _meta_ok, _cap
        msg = _FakeMessage(YT)
        out, bare = await _YT_CTX(msg, YT)
        check(f"{_label}: 代替情報を文脈に入れる", _want in out, out[:200])
        check(f"{_label}: 読めていない扱いにしない",
              "まだ中身を読めていない" not in out, out[:200])

    # メタ情報も取れない時だけ「読めていない」と正直に言う
    async def _meta_ng(vid):
        return None
    install_stubs()
    bot._gemini_watch_youtube_sync = _watch_ng
    bot._fetch_video_meta, bot._fetch_captions = _meta_ng, _cap_ng
    out, _ = await _YT_CTX(_FakeMessage(YT), YT)
    check("何も取れなければ正直に伝える", "まだ中身を読めていない" in out, out[:200])

    # ===== ③-c 画像/動画URLの内容理解（生成物について質問できること）=====
    print("■ E2E: 画像/動画URLの内容理解")
    IMG = "https://example.com/a/hf_1234.png"

    def _fake_analyze2(data, mime, prompt, tag):
        return "石造りの大きな門と塔。熱帯の街並み、ハノイの門に似た建築。"

    install_stubs()
    bot._apply_media_url_context = _MEDIA_CTX          # 実物に戻す
    bot._download_file = _dl_ok
    bot._gemini_analyze_media_sync = _fake_analyze2
    bot._media_ctx_cache.clear()
    bot._load_last_gen = lambda cid: {"prompt": "gate", "media_type": "image",
                                      "label": "自動選定", "url": IMG,
                                      "t": bot.time.time()}
    msg = _FakeMessage("どこですかここは")
    out = await _MEDIA_CTX(msg, "どこですかここは", 1234)
    check("直前の生成画像を見に行く", "石造りの大きな門" in out, out[:120])

    # 発言に画像URLが直接貼られた場合も読む
    out = await _MEDIA_CTX(_FakeMessage(IMG), f"これ何？ {IMG}", 1234)
    check("貼られたURLも読む", "石造りの大きな門" in out, out[:120])

    # 関係ない雑談では見に行かない（Gemini無料枠の節約）
    out = await _MEDIA_CTX(_FakeMessage("今日は疲れた"), "今日は疲れた", 1234)
    check("無関係な発言では読まない", "石造り" not in out, out[:120])

    # 画像が無いときは何もしない
    install_stubs()
    bot._apply_media_url_context = _MEDIA_CTX
    bot._download_file = _dl_ok
    bot._gemini_analyze_media_sync = _fake_analyze2
    bot._load_last_gen = lambda cid: None
    out = await _MEDIA_CTX(_FakeMessage("どこですかここは"), "どこですかここは", 1234)
    check("生成物が無ければそのまま", out == "どこですかここは", out[:80])

    # ===== ③-d 返信（リプライ）を文脈として読む =====
    print("■ E2E: リプライの文脈")
    install_stubs()
    ref = _reply_to("✅ できました！\n" + IMG)
    ctx = await _REPLY_CTX(_FakeMessage("どこですか？", reference=ref))
    check("返信先の本文を取り込む", "できました" in ctx and IMG in ctx, ctx[:120])
    check("返信先の発言者がわかる", "Orchestrator" in ctx, ctx[:120])

    ctx = await _REPLY_CTX(_FakeMessage("ふつうの発言"))
    check("返信でなければ空", ctx == "", repr(ctx))

    # 返信先に添付がある場合はURLも拾う
    ref2 = _reply_to("これどう？", [_FakeAttachment("photo.png")])
    ctx = await _REPLY_CTX(_FakeMessage("どこ？", reference=ref2))
    check("返信先の添付URLも拾う", "photo.png" in ctx, ctx[:120])

    # 返信先の画像URL → 中身まで読める（今回の不具合の本命経路）
    install_stubs()
    bot._apply_media_url_context = _MEDIA_CTX
    bot._download_file = _dl_ok
    bot._gemini_analyze_media_sync = _fake_analyze2
    bot._media_ctx_cache.clear()
    bot._load_last_gen = lambda cid: None      # 直前生成の記録が無くても効くこと
    body = "どこですか？" + await _REPLY_CTX(_FakeMessage("どこですか？", reference=ref))
    out = await _MEDIA_CTX(_FakeMessage("どこですか？", reference=ref), body, 1234)
    check("返信先の画像を読む", "石造りの大きな門" in out, out[:150])

    # 「この画像」と明示すれば古い生成物でも読む（曖昧な指定は直近のみ）
    install_stubs()
    bot._apply_media_url_context = _MEDIA_CTX
    bot._download_file = _dl_ok
    bot._gemini_analyze_media_sync = _fake_analyze2
    bot._media_ctx_cache.clear()
    old = {"prompt": "gate", "media_type": "image", "label": "自動選定",
           "url": IMG, "t": bot.time.time() - 10800}   # 3時間前
    bot._load_last_gen = lambda cid: old
    out = await _MEDIA_CTX(_FakeMessage("この画像どこ？"), "この画像どこ？", 1234)
    check("明示なら3時間前でも読む", "石造りの大きな門" in out, out[:150])
    out = await _MEDIA_CTX(_FakeMessage("ここどこ？"), "ここどこ？", 1234)
    check("曖昧な指定＋古い生成物は読まない", "石造り" not in out, out[:150])

    print("■ E2E: 実際に壊れた会話の再現（ルートだけでなく仕事の中身まで見る）")
    # ここは「直したつもりで半分だけ直っていた」を防ぐための場所。
    # 1件でも実害が出た会話は、必ずここに丸ごと再現を足すこと。

    # --- ① 画像を頼んだのに動画が走った（05:26 の実例）---
    install_stubs()
    # 直前に画像を作っている状態（実際の流れと同じ）
    bot._load_last_gen = lambda cid: {
        "prompt": "この人で画像生成して", "media_type": "image", "label": "画像",
        "t": _tm4.time()}
    await drive("この人で画像生成して")
    await drive("ヒッグスフィールドで")
    _c = last_call("hf_generate")
    check("『ヒッグスフィールドで』が生成につながる", _c is not None, f"CALLS={FIRED}")
    if _c:
        _args = _c[0]
        _req, _media = _args[1], _args[3]
        check("題材は直前の依頼から補う（指定語を題材にしない）",
              "画像生成して" in _req and "Higgs" not in _req, _req[:80])
        check("画像を頼まれたら画像のまま（動画にしない）", _media == "image", _media)

    # --- ② 動画の続きは動画のまま（①の直しで巻き添えにしない）---
    install_stubs()
    bot._load_last_gen = lambda cid: {
        "prompt": "犬が走ってる動画作って", "media_type": "video", "label": "動画",
        "t": _tm4.time()}
    await drive("犬が走ってる動画作って")
    await drive("ヒッグスフィールドで")
    _c2 = last_call("hf_generate")
    if _c2:
        check("動画の続きは動画のまま", _c2[0][3] == "video", _c2[0][3])

    # --- ③ デザインの続きを画像生成に投げない（14:53 の実例）---
    install_stubs()
    bot._load_last_gen = lambda cid: {
        "prompt": "豊臣兄弟の相関図", "media_type": "image", "label": "デザイン（図）",
        "t": _tm4.time(), "url": "https://example.com/x.png"}
    await drive("追加して出して")
    check("デザインの続きはクロード（HTML）で作り直す",
          "design" in FIRED and "hf_generate" not in FIRED, f"fired={FIRED}")

    # --- ④ 相談を設定変更にしない（04:41 の実例）---
    install_stubs()
    bot._load_last_gen = lambda cid: None
    _r4 = await drive(
        "毎日決まった時間にYouTubeのTOP100をリサーチして欲しいんだけど何時頃がいいかな？")
    check("相談に設定の案内を返さない",
          not any("毎日の自動リサーチ" in s and "設定" in s for s in _r4["sent"]),
          f"sent={_r4['sent'][:1]}")

    # --- ⑤ 役の名前が出ただけで呼び出さない（04:43 の実例）---
    install_stubs()
    bot._load_last_gen = lambda cid: None
    await drive("リサーチするのはクロード1にしてね")
    check("担当を決める話で役を呼び出さない", "multiview" not in FIRED, f"fired={FIRED}")

    # --- ⑥ 長い動画の切り抜きが、Higgsfieldに流れないこと ---
    install_stubs()
    bot._load_last_gen = lambda cid: None
    _REAL_CLIP = bot._run_clip_shorts        # ⑦で本物を呼ぶので控えておく
    bot._run_clip_shorts = _rec("clip")
    _r6 = await drive(
        "https://www.youtube.com/watch?v=abc12345678 これ3本ショートにして")
    check("切り抜きは専用の処理へ", "clip" in FIRED, f"fired={FIRED}")
    check("切り抜きで生成モデルを呼ばない",
          "hf_generate" not in FIRED and "image_gen" not in FIRED, f"fired={FIRED}")
    _c6 = last_call("clip")
    if _c6:
        check("本数の指定が伝わる", _c6[0][2] == 3, _c6[0])
        check("動画のURLが伝わる", "abc12345678" in _c6[0][1], _c6[0][1])

    # --- ⑦ 字幕が無い動画は、その場で文字起こしして続行する ---
    install_stubs()
    _called = []

    async def _no_caps(vid):
        _called.append("captions")
        return []

    async def _dl(cid, url, dest, kind="youtube"):
        _called.append("download")
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
        return True

    async def _tr(cid, src, workdir, lang="ja"):
        _called.append("transcribe")
        return [(0.0, 3.0, "こんにちは"), (3.0, 3.0, "本題に入ります"),
                (6.0, 3.0, "結論はこうです")]

    async def _pick(transcript, n):
        _called.append("pick")
        return [{"start": 0.0, "end": 20.0, "title": "本題",
                 "hook": "結論から", "why": "単体で成立"}]

    async def _cut(src, rows, clip, idx, workdir):
        _called.append("cut")
        return None, "（テストなので実際には切らない）"

    _keep = (bot._fetch_captions_timed, bot._download_video,
             bot._transcribe_local, bot._pick_clip_ranges, bot._cut_one_clip,
             bot._missing_clip_tools)
    try:
        bot._fetch_captions_timed = _no_caps
        bot._download_video = _dl
        bot._transcribe_local = _tr
        bot._pick_clip_ranges = _pick
        bot._cut_one_clip = _cut
        bot._missing_clip_tools = lambda: []
        _msg7 = _FakeMessage("dummy")
        try:
            await _REAL_CLIP(_msg7, "https://youtu.be/abc12345678", 1)
        except Exception as _e7:      # 途中で落ちても、どこまで進んだかを見る
            print(f"    （切り抜きの実行で例外: {str(_e7)[:120]}）")
        check("字幕が無ければ文字起こしに進む", "transcribe" in _called, _called)
        check("文字起こしの前に動画を取得する",
              "download" in _called and "transcribe" in _called
              and _called.index("download") < _called.index("transcribe"), _called)
        check("文字起こしの結果で切りどころを選ぶ", "pick" in _called, _called)
        check("同じ動画を二度落としに行かない",
              _called.count("download") == 1, _called)
    finally:
        (bot._fetch_captions_timed, bot._download_video, bot._transcribe_local,
         bot._pick_clip_ranges, bot._cut_one_clip,
         bot._missing_clip_tools) = _keep

    # --- ⑧ 「やって」が空振りしないこと（無限ループの再現）---
    install_stubs()
    bot._load_last_gen = lambda cid: None
    bot._pending_do.clear()
    _r8a = await drive("https://www.icloud.com/iclouddrive/06eXfAEzbeDWkouOAU5OK9V0Q")
    check("iCloudの共有リンクは取りに行けないと言う",
          any("ブラウザで開くページ" in t for t in _r8a["sent"]), _r8a["sent"][:1])
    check("代わりの渡し方を示す",
          any("ファイルアプリの道順" in t for t in _r8a["sent"]), _r8a["sent"][:1])
    _before8 = len(_r8a["sent"])          # 送信は積み上がるので、増えた分だけ見る
    _r8b = await drive("やって")
    _new8 = _r8b["sent"][_before8:]
    check("『やって』に具体的に答える",
          any("分からないので動かせない" in t for t in _new8), _new8)
    check("『やって』で同じ説明を繰り返さない",
          not any("ブラウザで開くページ" in t for t in _new8), _new8)
    bot._pending_do.clear()

    # --- ⑨ 確認待ちの「やって」を横取りしないこと ---
    #     「やって」「お願い」「よろしく」は承認の返事でもある。ここを
    #     横取りすると、承認しても作業が始まらず「返事がない」になる。
    install_stubs()
    bot.CONFIRM_BEFORE_WORK = True
    bot._load_last_gen = lambda cid: None
    bot._pending_do.clear()
    bot._set_pending_do(1234, "動画の場所", "切り抜きたい")   # 未解決の依頼がある状態
    try:
        _t9 = asyncio.get_running_loop().create_task(drive("犬が走ってる動画作って"))
        for _ in range(60):
            await asyncio.sleep(0)
            if 1234 in bot._pending_approvals:
                break
        check("確認が出ている", 1234 in bot._pending_approvals, "確認が出ていない")
        _r9 = await drive("やって")
        check("承認として受け付ける",
              any("承認を受け付け" in t for t in _r9["sent"]), _r9["sent"][-2:])
        check("『動かせない』で横取りしない",
              not any("分からないので動かせない" in t for t in _r9["sent"]),
              _r9["sent"][-2:])
        await asyncio.wait_for(_t9, timeout=5)
        check("承認後に作業が走る", "hf_generate" in FIRED, f"fired={FIRED}")
    finally:
        bot.CONFIRM_BEFORE_WORK = False
        bot._pending_do.clear()
        bot._pending_approvals.pop(1234, None)

    # --- ⑩ 話題に出しただけで作業が始まらないこと ---
    #     本人の指摘：「ボットが意図せず動くのをやめたい」。
    #     語が当たったら動く方式だと、雑談の中の「クロードコード」「動画」で
    #     修正や生成が立ち上がっていた。依頼の【形】をしている時だけ通す。
    install_stubs()
    bot.CONFIRM_BEFORE_WORK = True
    bot._load_last_gen = lambda cid: None
    bot._pending_do.clear()
    try:
        for _say in ("クロードコードって便利だよね",
                     "クロードコードって使ってる？",
                     "さっきクロードコードで作業してた",
                     "動画編集って難しいの？",
                     "ロゴのデザインの話なんだけど"):
            install_stubs()
            _r10 = await drive(_say)
            check(f"{_say!r} で作業が始まらない",
                  not any(_f in FIRED for _f in ("hf_generate", "image_gen",
                                                 "design", "selffix",
                                                 "exec", "clip")),
                  f"fired={FIRED}")
            check(f"{_say!r} で確認画面を出さない",
                  1234 not in bot._pending_approvals,
                  bot._pending_approvals.get(1234))
            check(f"{_say!r} は会話として返す",
                  "orchestrator" in FIRED, f"fired={FIRED} sent={_r10['sent']}")
        # 同じ語でも、頼まれた形なら通ること（守りが効きすぎていないか）
        install_stubs()
        await drive("かっこいい猫のロゴ画像作って")
        check("頼まれた形なら通る",
              any(_f in FIRED for _f in ("hf_generate", "image_gen")),
              f"fired={FIRED}")
    finally:
        bot.CONFIRM_BEFORE_WORK = False
        bot._pending_approvals.pop(1234, None)
        bot._pending_do.clear()

    # --- ⑪ 「ヒッグスフィールドで」が絵の題材になっていた（21:50の実例）---
    #     「ヒッグスフィールドで画像生成して」が Higgs field（物理）と英訳され、
    #     背景が宇宙・エネルギー粒子の画像になった。本人は何度も
    #     「背景が宇宙になってる」と直しを頼むことになった。
    install_stubs()
    bot._load_last_gen = lambda cid: None
    check("作り手の指定は題材から落とす",
          "ヒッグス" not in bot._drop_tool_words(
              "これの背景を自然な室内にして、ヒッグスフィールドで画像生成で"),
          bot._drop_tool_words("これの背景を自然な室内にして、ヒッグスフィールドで画像生成で"))
    check("依頼の中身は残す",
          "室内" in bot._drop_tool_words(
              "これの背景を自然な室内にして、ヒッグスフィールドで画像生成で"))
    _dirty = ("the person in the reference image, cosmic energy particles, "
              "higgs field ripples of light, soft daylight, photorealistic")
    check("英訳に紛れ込んだ物理の描写を落とす",
          not any(_w in bot._clean_tool_words(_dirty, "背景を自然にして").lower()
                  for _w in ("higgs", "cosmic")),
          bot._clean_tool_words(_dirty, "背景を自然にして"))
    check("本当に宇宙を頼まれた時は落とさない",
          "cosmic" in bot._clean_tool_words(_dirty, "宇宙の背景で作って"),
          bot._clean_tool_words(_dirty, "宇宙の背景で作って"))

    # --- ⑫ 画像を頼んだのに動画が出た（19:25 と 21:52 の実例）---
    #     「画像で生成して」→「ヒッグスフィールドでやって」と続けると、
    #     後の発言に媒体の語が無いので既定の動画になり .mp4 が出ていた。
    check("「動画じゃなくて画像」は画像",
          bot._said_media("動画の生成じゃなくて画像の生成にして") == "image",
          bot._said_media("動画の生成じゃなくて画像の生成にして"))
    check("「画像じゃなくて動画」は動画",
          bot._said_media("画像じゃなくて動画にして") == "video",
          bot._said_media("画像じゃなくて動画にして"))
    check("媒体を言っていなければ決めつけない",
          bot._said_media("ヒッグスフィールドでやって") is None,
          bot._said_media("ヒッグスフィールドでやって"))
    install_stubs()
    bot._last_media.clear()
    # 直前に画像を作った状態（実際の流れと同じ。媒体の語は今の発言に無い）
    bot._load_last_gen = lambda cid: {
        "prompt": "higher nose bridge, portrait", "media_type": "image",
        "label": "画像", "t": _tm4.time()}
    await drive("ヒッグスフィールドでやって")
    _c12 = last_call("hf_generate")
    check("直前が画像なら『ヒッグスフィールドでやって』も画像のまま",
          _c12 is not None and _c12[0][3] == "image",
          _c12[0] if _c12 else f"fired={FIRED}")
    bot._last_media.clear()

    # --- ⑬ 生成物に返信して直しを頼んだのに会話に落ちた（23:13の実例）---
    #     「この画像の背景を室内の背景にして、ヒッグスフィールドで」が
    #     どの機能にも流れず、ボットは「許可が下りてないみたい」と作り話をした。
    install_stubs()
    check("生成物への返信の直し依頼はヒッグスフィールドへ",
          bot.classify_route("この画像の背景を室内の背景にして、ヒッグスフィールドで",
                             has_last_gen=True) == "hf_auto",
          bot.classify_route("この画像の背景を室内の背景にして、ヒッグスフィールドで",
                             has_last_gen=True))
    # 作り手の名指しだけで中身が無い時は、直前の依頼が無ければ何も始めない
    check("中身が無ければ勝手に始めない",
          bot.classify_route("ヒッグスフィールドで作って") is None,
          bot.classify_route("ヒッグスフィールドで作って"))
    # 中身が書かれていれば、直前の生成が無くても名指しされた作り手へ通す
    # （参照が要る依頼なら、生成前に写真を求める別のガードが受け止める）
    check("中身があれば名指しされた作り手へ",
          bot.classify_route("geminiで背景を普通の室内にして") == "image",
          bot.classify_route("geminiで背景を普通の室内にして"))
    # 参照が引き継がれないと、人物の消えた「部屋だけ」の画像になる
    bot._last_ref.clear()
    _msg13 = _FakeMessage("この画像の背景を室内にして")
    _keep13 = bot._describe_media_url
    try:
        async def _no_desc(url, ch=None):
            return ""
        bot._describe_media_url = _no_desc
        await bot._apply_media_url_context(
            _msg13,
            "この画像の背景を室内にして https://example.com/a/hf_20260810_x.png",
            1234)
    finally:
        bot._describe_media_url = _keep13
    check("返信で指された画像を参照として覚える",
          bot._recent_ref(1234) is not None, bot._last_ref.get(1234))
    bot._last_ref.clear()

    # --- ⑭ 上限で生成できない時に、何をすればいいか分かること ---
    _note = bot._gen_fail_note("ERROR: 本日の生成上限（グレース期間分）に達しています")
    check("上限だと分かる言い方にする", "上限" in _note and "不具合ではなく" in _note, _note)
    check("代わりの手（無料枠）を示す", "Gemini" in _note, _note)
    check("上限以外はそのまま理由を出す",
          "タイムアウト" in bot._gen_fail_note("claude CLI実行失敗: タイムアウト"),
          bot._gen_fail_note("claude CLI実行失敗: タイムアウト"))

    # --- ⑮ 内部の状態について作り話をしない（23:14の実例）---
    install_stubs()
    bot._pending_approvals.pop(1234, None)
    # 言い方は毎回変わったので（「下りてない」→「必要みたい」）、形で受ける
    for _fake in ("生成の許可が下りてないみたい。ここから普通に進められるよ。",
                  "生成の実行に許可が必要みたい。この場では動かせないので通常フローで。",
                  "実際の生成ボタンを押すところは通ってなくて、まだ動いてない。"):
        check(f"確認待ちが無いのに内部の状態を作り話しない: {_fake[:14]}…",
              not any(_w in bot._drop_false_progress(_fake, 1234)
                      for _w in ("許可", "生成ボタン", "動かせない")),
              bot._drop_false_progress(_fake, 1234))
    _fake = "生成の許可が下りてないみたい。ここから普通に進められるよ。"
    check("道具の説明としての『許可』は落とさない",
          "許可" in bot._drop_false_progress(
              "ヒッグスフィールドのアカウントは管理者の許可で使えるようになるよ。", 1234),
          bot._drop_false_progress(
              "ヒッグスフィールドのアカウントは管理者の許可で使えるようになるよ。", 1234))
    check("落としたあとも何か返す",
          bool(bot._drop_false_progress(_fake, 1234).strip()),
          bot._drop_false_progress(_fake, 1234))
    bot._pending_do.clear()

    # --- ⑯ 「無料枠は残っているのに作れない」の理由が見えなかった（23:32の実例）---
    #     Geminiの画像生成が1秒で失敗し、理由は標準出力にしか出ていなかった。
    #     モデルIDを1つに固定していたため、そのIDが使えないと詰んでいた。
    install_stubs()
    _tries = []

    def _gen_img_fail(prompt, ref_bytes=None, ref_mime="image/png"):
        raise RuntimeError("404 model not found")

    _keep16 = bot._gemini_generate_image_sync
    try:
        bot._gemini_generate_image_sync = _gen_img_fail
        _ch16 = _CHANNELS.setdefault(1234, _FakeChannel(1234))
        _ch16.sent.clear()
        await _REAL_IMAGE_REQ(1234, "猫の画像作って")
        check("作れなかった理由を本人に見せる",
              any("理由" in t and "404" in t for t in _ch16.sent),
              _ch16.sent[-1:])
        check("勝手にHiggsfieldへ切り替えない",
              not any("Higgsfieldで生成します" in t for t in _ch16.sent),
              _ch16.sent[-1:])
    finally:
        bot._gemini_generate_image_sync = _keep16
    check("画像モデルは複数用意して順に試す",
          len(bot.GEMINI_IMAGE_MODELS) >= 2, bot.GEMINI_IMAGE_MODELS)

    # --- ⑯.5 画像モデルも無料枠切れでローテーションすること ---
    #     テキスト側と同じ方針（枠切れはクールダウン→次のモデル→時間で復帰）。
    install_stubs()
    _asked = []
    _keep_models = bot.GEMINI_IMAGE_MODELS[:]
    _keep_cd = dict(bot._gemini_cooldown)
    _keep_client = bot.gemini_client

    class _FakeModels:
        def generate_content(self, model=None, contents=None):
            _asked.append(model)
            if model == "img-a":          # 1つ目は無料枠切れ
                raise RuntimeError("429 RESOURCE_EXHAUSTED quota PerDay")
            return types.SimpleNamespace(candidates=[types.SimpleNamespace(
                content=types.SimpleNamespace(parts=[types.SimpleNamespace(
                    inline_data=types.SimpleNamespace(data=b"PNG"))]))])

    try:
        bot.GEMINI_IMAGE_MODELS[:] = ["img-a", "img-b"]
        bot._gemini_cooldown.clear()
        bot._gemini_image_ok["model"] = ""
        bot._gemini_img_rr["i"] = 0
        bot.gemini_client = types.SimpleNamespace(models=_FakeModels())
        check("枠切れなら次のモデルで作る",
              _REAL_GEN_IMG("猫") == b"PNG", _asked)
        check("枠切れのモデルはクールダウンに入れる",
              bot._gemini_cooldown.get("img-a", 0) > _tm4.time(),
              bot._gemini_cooldown)
        _asked.clear()
        _REAL_GEN_IMG("犬")
        check("次からは枠切れのモデルを試さない",
              "img-a" not in _asked, _asked)
        # 全部クールダウン → 無料枠切れとして扱う（他のエラーと混ぜない）
        bot._gemini_cooldown["img-b"] = _tm4.time() + 600
        _raised = None
        try:
            _REAL_GEN_IMG("鳥")
        except Exception as _e:  # noqa: BLE001
            _raised = _e
        check("全部だめなら無料枠切れとして扱う",
              isinstance(_raised, bot.GeminiQuotaExceeded), repr(_raised))
        check("いつ戻るかを伝える", "分" in str(_raised), str(_raised))
    finally:
        bot.GEMINI_IMAGE_MODELS[:] = _keep_models
        bot._gemini_cooldown.clear()
        bot._gemini_cooldown.update(_keep_cd)
        bot._gemini_image_ok["model"] = ""
        bot.gemini_client = _keep_client

    # --- ⑯.6 ローテーションの状況が見えること（本人の指摘）---
    #     「geminiがローテーションできてるのかわからない」。
    #     動いていても見る手段が無かった。送れば一覧が出るようにした。
    install_stubs()
    for _t in ("geminiがローテーションできてるのかわからない",
               "geminiのモデルの状態は？", "画像モデルどうなってる",
               "ローテーションできてる？"):
        check(f"{_t!r} は画像モデルの状態を聞いている",
              bot._asks_image_model_status(_t), _t)
    for _t in ("画像生成のクレジットどうなってる", "veo3で動画作ると何クレジット？",
               "geminiで画像作って"):
        check(f"{_t!r} は状態確認にしない",
              not bot._asks_image_model_status(_t), _t)
    _keep_models2 = bot.GEMINI_IMAGE_MODELS[:]
    _keep_cd2 = dict(bot._gemini_cooldown)
    try:
        bot.GEMINI_IMAGE_MODELS[:] = ["img-a", "img-b", "img-c"]
        bot._gemini_cooldown.clear()
        bot._gemini_bad_models.clear()
        bot._gemini_img_stats.clear()
        bot._gemini_bad_models.add("img-c")
        bot._gemini_cooldown["img-b"] = _tm4.time() + 900
        bot._gemini_image_ok["model"] = "img-a"
        _st = bot._gemini_image_status()
        check("使えるモデルが分かる", "✅ 使える" in _st, _st)
        check("枠切れは残り時間が分かる", "分で復帰" in _st, _st)
        check("存在しないIDは使わないと分かる", "存在しないID" in _st, _st)
        check("いま何個使えるかを言う", "いま使えるのは 1個" in _st, _st)
        _r = await drive("ローテーションできてる？")
        check("Discordから見られる",
              any("画像生成モデルの状態" in t for t in _r["sent"]), _r["sent"][-1:])
    finally:
        bot.GEMINI_IMAGE_MODELS[:] = _keep_models2
        bot._gemini_cooldown.clear()
        bot._gemini_cooldown.update(_keep_cd2)
        bot._gemini_bad_models.clear()
        bot._gemini_img_stats.clear()
        bot._gemini_image_ok["model"] = ""

    # --- ⑯.7 存在しないモデルID（404）は待たずに外すこと ---
    #     実際に2つのIDが404で、30分待っても永遠に復活しない相手を
    #     クールダウンで待っていた。
    install_stubs()
    _asked2 = []

    class _Fake404:
        def generate_content(self, model=None, contents=None):
            _asked2.append(model)
            if model == "img-x":
                raise RuntimeError(
                    "404 NOT_FOUND. {'error': {'code': 404, "
                    "'message': 'models/img-x is not found'}}")
            return types.SimpleNamespace(candidates=[types.SimpleNamespace(
                content=types.SimpleNamespace(parts=[types.SimpleNamespace(
                    inline_data=types.SimpleNamespace(data=b"PNG"))]))])

    _keep_client2 = bot.gemini_client
    try:
        bot.GEMINI_IMAGE_MODELS[:] = ["img-x", "img-y"]
        bot._gemini_cooldown.clear()
        bot._gemini_bad_models.clear()
        bot._gemini_image_ok["model"] = ""
        bot._gemini_img_rr["i"] = 0
        bot._gemini_img_discovered["done"] = True   # 一覧の問い合わせはしない
        bot.gemini_client = types.SimpleNamespace(models=_Fake404())
        check("404でも次のモデルで作れる", _REAL_GEN_IMG("猫") == b"PNG", _asked2)
        check("404はクールダウンではなく除外", "img-x" in bot._gemini_bad_models,
              bot._gemini_bad_models)
        check("404を待ち時間として数えない",
              bot._gemini_cooldown.get("img-x", 0) <= _tm4.time(),
              bot._gemini_cooldown)
        _asked2.clear()
        _REAL_GEN_IMG("犬")
        check("次からは404のモデルを呼ばない", "img-x" not in _asked2, _asked2)
    finally:
        bot.GEMINI_IMAGE_MODELS[:] = _keep_models2
        bot._gemini_cooldown.clear()
        bot._gemini_cooldown.update(_keep_cd2)
        bot._gemini_bad_models.clear()
        bot._gemini_img_stats.clear()
        bot._gemini_image_ok["model"] = ""
        bot._gemini_img_discovered["done"] = False
        bot.gemini_client = _keep_client2

    # --- ⑰ 「この画像の背景を室内にして」で別人が出来ていた（23:38の実例）---
    #     参照画像をGeminiに渡していなかったため、依頼者の写真と無関係な
    #     「young man」の画像が作られていた。
    install_stubs()
    _seen = {}

    def _gen_img_ref(prompt, ref_bytes=None, ref_mime="image/png"):
        _seen["ref"] = ref_bytes
        return b"PNG"

    async def _fetch_ok(url, limit=None):
        return b"REFBYTES", "image/jpeg"

    _keep17 = (bot._gemini_generate_image_sync, bot._fetch_image_bytes)
    try:
        bot._gemini_generate_image_sync = _gen_img_ref
        bot._fetch_image_bytes = _fetch_ok
        bot._remember_ref(1234, "https://example.com/a.png")
        await _REAL_IMAGE_REQ(1234, "この画像の背景を自然な室内にして")
        check("元の画像をGeminiに渡す", _seen.get("ref") == b"REFBYTES", _seen)
    finally:
        (bot._gemini_generate_image_sync, bot._fetch_image_bytes) = _keep17
        bot._last_ref.clear()
    install_stubs()
    _seen.clear()
    try:
        bot._gemini_generate_image_sync = _gen_img_ref
        bot._fetch_image_bytes = _fetch_ok
        bot._last_ref.clear()
        await _REAL_IMAGE_REQ(1234, "猫の画像作って")
        check("関係のない新規依頼には参照を付けない",
              _seen.get("ref") is None, _seen)
    finally:
        (bot._gemini_generate_image_sync, bot._fetch_image_bytes) = _keep17
        bot._last_ref.clear()

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
