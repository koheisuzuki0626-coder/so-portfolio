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
_REAL_REFINE = bot._refine_prompt
_REAL_DESIGN = bot._run_design

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
    # テストの失敗を本物の errors.log に書かない。起動時セルフテストで
    # simulate.py が走るため、実在しないエラーが「直近のエラー」に並び、
    # 不具合調査を誤らせた（実際に「404 model not found」が2件紛れた）。
    import tempfile as _tf0
    import pathlib as _pl0
    _tmp0 = _pl0.Path(_tf0.mkdtemp())
    bot.ERROR_LOG = _tmp0 / "errors.log"
    # テストの会話を本物の履歴に書き込まない。起動時セルフテストで
    # simulate.py が走るため、実在しない会話が history/1234.jsonl に
    # 溜まっていた（1日で4.7MB・26,983行）。
    bot.HISTORY_DIR = _tmp0
    bot.CONFIRM_BEFORE_WORK = False   # 確認は専用テストで検証する
    bot.CLARIFY_ON = False            # 聞き返しも専用テストで検証する
    bot._pending_clarify.clear()
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

    def _gen_img(prompt, ref_bytes=None, ref_mime="image/png", extra_refs=None):
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

    def _gen_img_fail(prompt, ref_bytes=None, ref_mime="image/png", extra_refs=None):
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

    def _gen_img_ref(prompt, ref_bytes=None, ref_mime="image/png", extra_refs=None):
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

    # --- ⑱ 「ログ送って」と言わなくても状況が読めること（本人の希望）---
    #     Claude Codeのチャットで不具合を報告した時に、開発側が最新の状況を
    #     読めるよう、動きがあれば黙って共有し続ける。
    install_stubs()
    _shared = []

    async def _share(cid, limit=80):
        _shared.append(cid)
        return "✅ 共有しました"

    _keep18 = (bot._share_debug_log, bot.AUTOLOG_PERIOD_SEC,
               bot.AUTOLOG_URGENT_SEC)
    try:
        bot._share_debug_log = _share
        bot.AUTOLOG_PERIOD_SEC = 0
        bot.AUTOLOG_URGENT_SEC = 0
        bot._activity.update({"n": 0, "shared_n": 0, "cid": 1234, "urgent": False})
        bot._pending_approvals.pop(1234, None)
        _t18 = asyncio.get_running_loop().create_task(bot._autolog_loop())
        for _ in range(50):
            await asyncio.sleep(0)
        check("動きが無ければ共有しない", not _shared, _shared)
        bot.add_history(1234, "kohei", "変な挙動なんだけど")
        for _ in range(50):
            await asyncio.sleep(0)
        check("動きがあれば頼まれなくても共有する", bool(_shared), _shared)
        _n = len(_shared)
        for _ in range(50):
            await asyncio.sleep(0)
        check("同じ状態で共有を繰り返さない", len(_shared) == _n, _shared)
        _t18.cancel()
    finally:
        (bot._share_debug_log, bot.AUTOLOG_PERIOD_SEC,
         bot.AUTOLOG_URGENT_SEC) = _keep18
        bot._activity.update({"n": 0, "shared_n": 0, "cid": None, "urgent": False})
    check("エラーは急ぎ扱いにする",
          bot.AUTOLOG_URGENT_SEC < bot.AUTOLOG_PERIOD_SEC,
          (bot.AUTOLOG_URGENT_SEC, bot.AUTOLOG_PERIOD_SEC))
    # 例外として記録されない⚠️（生成の投入失敗など）でも急ぎ扱いにする。
    # 「なんかエラーでた」と言われた時点でログが古く、中身を読めなかった。
    for _t, _want in (("⚠️ 生成の投入に失敗", True), ("🚫 上限です", True),
                      ("🛑 やめました", True), ("✅ できました！", False),
                      ("ふつうの返事だよ", False)):
        bot._activity.update({"n": 0, "shared_n": 0, "cid": None,
                              "urgent": False})
        await bot.send_as(bot.orch, 1234, _t)
        check(f"{_t[:8]!r} の急ぎ扱い={_want}",
              bot._activity["urgent"] == _want, bot._activity)
    bot._activity.update({"n": 0, "shared_n": 0, "cid": None, "urgent": False})

    # --- ⑲ 使えないと分かっている手を勧めない（本人の指摘）---
    #     「geminiで画像生成できないのに案内してくる」。
    #     Higgsfieldが上限の時に必ず「Geminiで作って」と勧めていた。
    install_stubs()
    _keep19 = (bot.GEMINI_IMAGE_MODELS[:], dict(bot._gemini_cooldown))
    try:
        bot.GEMINI_IMAGE_MODELS[:] = ["img-a"]
        bot._gemini_cooldown.clear()
        bot._gemini_bad_models.clear()
        check("使えるなら使えると答える", bot._gemini_image_usable(), True)
        _note_ok = bot._gen_fail_note("ERROR: 本日の生成上限に達しています")
        check("使える時はGeminiを勧める", "Geminiで画像生成して" in _note_ok, _note_ok)
        # 全部だめな状態にする
        bot._gemini_bad_models.add("img-a")
        check("だめなら使えないと答える", not bot._gemini_image_usable(), False)
        _note_ng = bot._gen_fail_note("ERROR: 本日の生成上限に達しています")
        check("使えない時はGeminiを勧めない",
              "Geminiで画像生成して" not in _note_ng, _note_ng)
        check("代わりに今できる手を出す", "クロードで作って" in _note_ng, _note_ng)
        check("なぜ使えないかを言う",
              "存在しません" in _note_ng or "分" in _note_ng, _note_ng)
        # 画像の依頼そのものも、整形まで走らせずに先に断る
        _ch19 = _CHANNELS.setdefault(1234, _FakeChannel(1234))
        _ch19.sent.clear()
        _refined = []

        async def _refine19(req, mtype, style="", has_ref=False):
            _refined.append(req)
            return req
        _keep_ref19 = bot._refine_prompt
        bot._gemini_img_discovered["done"] = True
        try:
            bot._refine_prompt = _refine19
            await _REAL_IMAGE_REQ(1234, "猫の画像作って")
        finally:
            bot._refine_prompt = _keep_ref19
        check("作れない時はプロンプト整形まで走らせない", not _refined, _refined)
        check("作れないことを先に伝える",
              any("いまGeminiでは画像を作れません" in t for t in _ch19.sent),
              _ch19.sent[-1:])
    finally:
        bot.GEMINI_IMAGE_MODELS[:] = _keep19[0]
        bot._gemini_cooldown.clear()
        bot._gemini_cooldown.update(_keep19[1])
        bot._gemini_bad_models.clear()
        bot._gemini_img_discovered["done"] = False

    # --- ⑳ デザインの書き出し失敗（00:37の実例）---
    #     理由が英語のまま「You've hit your session limit」と出て、
    #     コードの不具合なのかプラン側の上限なのか分からなかった。
    _lim = bot._claude_fail_note(
        "デザインの書き出し",
        "⚠️ 実行に失敗: You've hit your session limit · resets 4am (Asia/Tokyo)")
    check("クロードの利用上限だと分かる言い方にする",
          "利用上限" in _lim and "不具合ではなく" in _lim, _lim)
    check("いつ戻るかを伝える", "4am" in _lim, _lim)
    check("上限以外はそのまま理由を出す",
          "書き出し用のフォントがありません" in bot._claude_fail_note(
              "デザインの書き出し", "ERROR: 書き出し用のフォントがありません"),
          bot._claude_fail_note("デザインの書き出し",
                                "ERROR: 書き出し用のフォントがありません"))
    # 作り手の名指しは、直前がデザインでも勝つ（00:41の実例）
    check("『geminiで』はデザインの続きより優先",
          bot.classify_route("geminiで背景を室内にして",
                             has_last_gen=True, last_was_design=True) == "image",
          bot.classify_route("geminiで背景を室内にして",
                             has_last_gen=True, last_was_design=True))
    check("名指しが無ければデザインの続きのまま",
          bot.classify_route("背景を室内にして",
                             has_last_gen=True, last_was_design=True) == "design",
          bot.classify_route("背景を室内にして",
                             has_last_gen=True, last_was_design=True))
    # 英訳できなかった時は黙って日本語を投入しない（04:02の実例）
    install_stubs()
    _ch20 = _CHANNELS.setdefault(1234, _FakeChannel(1234))
    _ch20.sent.clear()
    _keep20 = (bot._refine_prompt, bot._gemini_generate_image_sync)
    try:
        async def _refine_ng(req, mtype, style="", has_ref=False):
            return req                      # 英訳できなかった（上限など）

        def _gen_ok(prompt, ref_bytes=None, ref_mime="image/png", extra_refs=None):
            return b"PNG"
        bot._refine_prompt = _refine_ng
        bot._gemini_generate_image_sync = _gen_ok
        await _REAL_IMAGE_REQ(1234, "背景を室内にして")
        check("英訳できなかったことを知らせる",
              any("英語プロンプトに直せませんでした" in t for t in _ch20.sent),
              _ch20.sent[:2])
        check("なぜ直せなかったかを添える",
              any("理由" in t for t in _ch20.sent
                  if "英語プロンプトに直せませんでした" in t), _ch20.sent[:2])
    finally:
        (bot._refine_prompt, bot._gemini_generate_image_sync) = _keep20

    # --- ㉑ 「今日はじめてなのに枠が無い」（本人の指摘）---
    #     使い切ったのではなく、そのモデルの無料枠の割り当てが0だった。
    #     待っても戻らないので、「30分で戻ります」は嘘になる。
    _z = ("429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': "
          "'You exceeded your current quota, please check your plan and "
          "billing details', 'details': [{'@type': 'QuotaFailure', "
          "'violations': [{'quotaId': "
          "'GenerateRequestsPerDayPerProjectPerModel-FreeTier', "
          "'quotaValue': '0'}]}]}}")
    check("割り当て0を見分ける", bot._is_zero_quota_error(_z), _z[:60])
    check("使い切りと混同しない",
          not bot._is_zero_quota_error(
              "429 RESOURCE_EXHAUSTED quotaValue: '100' PerDay"),
          True)
    install_stubs()
    _keep21 = (bot.GEMINI_IMAGE_MODELS[:], dict(bot._gemini_cooldown),
               bot.gemini_client)

    class _FakeZero:
        def generate_content(self, model=None, contents=None):
            raise RuntimeError(_z)

    try:
        bot.GEMINI_IMAGE_MODELS[:] = ["img-p"]
        bot._gemini_cooldown.clear()
        bot._gemini_bad_models.clear()
        bot._gemini_img_stats.clear()
        bot._gemini_img_discovered["done"] = True
        bot.gemini_client = types.SimpleNamespace(models=_FakeZero())
        _err21 = None
        try:
            _REAL_GEN_IMG("猫")
        except Exception as _e:  # noqa: BLE001
            _err21 = _e
        check("割り当て0は待たずに外す", "img-p" in bot._gemini_bad_models,
              bot._gemini_bad_models)
        check("クールダウンで待たせない",
              bot._gemini_cooldown.get("img-p", 0) <= _tm4.time(),
              bot._gemini_cooldown)
        check("「使い切った」と言わない",
              "使い切ったのではなく" in str(_err21), str(_err21)[:120])
        check("モデルごとの理由を残す", "内訳" in str(_err21), str(_err21)[:200])
        check("案内でも正しい理由を言う",
              "割り当てが0" in bot._gemini_image_why_not(),
              bot._gemini_image_why_not())
    finally:
        (bot.GEMINI_IMAGE_MODELS[:], _cd21, bot.gemini_client) = _keep21
        bot._gemini_cooldown.clear()
        bot._gemini_cooldown.update(_cd21)
        bot._gemini_bad_models.clear()
        bot._gemini_img_stats.clear()
        bot._gemini_img_discovered["done"] = False

    # 英訳できなかった時は、作り手の指定を落としただけでも見逃さない
    install_stubs()
    _ch21 = _CHANNELS.setdefault(1234, _FakeChannel(1234))
    _ch21.sent.clear()
    _keep21b = (bot._refine_prompt, bot._gemini_generate_image_sync)
    try:
        async def _refine_strip(req, mtype, style="", has_ref=False):
            return bot._drop_tool_words(req)      # 英訳できず、語を落としただけ

        def _gen_ok2(prompt, ref_bytes=None, ref_mime="image/png", extra_refs=None):
            return b"PNG"
        bot._refine_prompt = _refine_strip
        bot._gemini_generate_image_sync = _gen_ok2
        await _REAL_IMAGE_REQ(1234, "geminiで背景を室内に変えて")
        check("語を落としただけを英訳成功と誤認しない",
              any("英語プロンプトに直せませんでした" in t for t in _ch21.sent),
              _ch21.sent[:3])
    finally:
        (bot._refine_prompt, bot._gemini_generate_image_sync) = _keep21b

    # --- ㉒ 「英語プロンプトに直せませんでした」の理由が分からなかった ---
    #     上限なのか、返答が英語でなかったのかを切り分けられず、
    #     推測で「クロード側の利用上限の可能性」と書いていた。
    _keep22 = bot._ai_text_bg
    try:
        async def _ai_ng(prompt, tag=""):
            raise RuntimeError("Claude Code サブスクの利用上限に達しています")
        bot._ai_text_bg = _ai_ng
        bot._refine_fail["why"] = ""
        _out = await _REAL_REFINE("背景を室内にして", "image")
        check("英訳できなければ原文を返す", _out == "背景を室内にして", _out)
        check("理由を控えておく", "利用上限" in bot._refine_fail["why"],
              bot._refine_fail["why"])
        check("知らせに理由を入れる", "利用上限" in bot._refine_fail_note(),
              bot._refine_fail_note())

        async def _ai_ja(prompt, tag=""):
            return "背景を室内にした画像です"      # 英語になっていない
        bot._ai_text_bg = _ai_ja
        bot._refine_fail["why"] = ""
        await _REAL_REFINE("背景を室内にして", "image")
        check("英語でない返答も理由として区別する",
              "英語のプロンプトではありません" in bot._refine_fail["why"],
              bot._refine_fail["why"])
    finally:
        bot._ai_text_bg = _keep22
        bot._refine_fail["why"] = ""
    # 作り手の指定を落とした後に「、」が残っていた
    check("落としたあとの句読点を残さない",
          bot._drop_tool_words("背景を自然な室内に変えて、ヒッグスフィールドで")
          == "背景を自然な室内に変えて",
          bot._drop_tool_words("背景を自然な室内に変えて、ヒッグスフィールドで"))

    # --- ㉓ 上限の言い方が変わって、専用の知らせが出ていなかった（08:17の実例）---
    #     「本日の生成上限に達しています」は拾えたが、
    #     「日次生成制限に達しています」は素通りして生の英語まじりが出ていた。
    for _e in ("ERROR: 日次生成制限に達しています。制限のリセット後または"
               "プランを更新してください",
               "ERROR: 日次生成制限に達しました（グレース期間）。",
               "ERROR: 本日の生成上限（グレース期間分）に達しています",
               "ERROR: daily limit exceeded"):
        bot._hf_limit.update({"t": 0.0, "why": ""})
        check(f"上限だと分かる: {_e[:16]}…",
              "Higgsfield側の上限" in bot._gen_fail_note(_e),
              bot._gen_fail_note(_e)[:60])
    bot._hf_limit.update({"t": 0.0, "why": ""})
    check("上限以外はそのまま出す",
          "タイムアウト" in bot._gen_fail_note("claude CLI実行失敗: タイムアウト"),
          bot._gen_fail_note("claude CLI実行失敗: タイムアウト"))
    check("上限以外で上限の記憶を作らない", not bot._hf_limit["t"], bot._hf_limit)
    # 一度当たったら、次に頼まれた時点で先に知らせる
    bot._gen_fail_note("ERROR: 日次生成制限に達しています")
    check("今日すでに上限なら先に知らせる",
          "日次上限で失敗しています" in bot._hf_limit_note(), bot._hf_limit_note())
    check("いつ失敗したかを言う", ":" in bot._hf_limit_note(), bot._hf_limit_note())
    bot._hf_limit.update({"t": 0.0, "why": ""})
    check("当たっていなければ何も言わない", bot._hf_limit_note() == "",
          bot._hf_limit_note())

    print("■ E2E: 分からないことは始める前に聞き返す")
    # 本人の希望：「いちいち細かく与件を伝えられないから、不明点はあっちから聞いて」。
    # ただし聞きすぎると逆に手間なので、中身が書かれていない依頼の時だけ聞く。
    bot.CLARIFY_ON = True             # 判定そのものを見るので有効にする
    check("中身が薄ければ聞く", bool(bot._missing_slots("video", "動画作って")),
          bot._missing_slots("video", "動画作って"))
    check("仕上がりに効く項目を一通り聞く",
          len(bot._missing_slots("video", "動画作って")) >= 6,
          [n for n, _ in bot._missing_slots("video", "動画作って")])
    # 詳しく書くほど質問は減る（すでに書かれている項目は聞かない）
    _bare = bot._missing_slots("image", "画像作って")
    _rich = bot._missing_slots(
        "image", "YouTubeサムネ用に、夕暮れの海辺を歩く猫を実写風・16:9・"
                 "暖色で、顔のアップ、文字なしで作って")
    check("詳しく書けば質問が減る", len(_rich) < len(_bare) - 3,
          (len(_bare), len(_rich), [n for n, _ in _rich]))
    for _n in ("用途", "比率・サイズ", "作風", "色味", "文字", "被写体の見せ方"):
        check(f"書いてある項目は聞かない: {_n}",
              all(_n != n for n, _ in _rich), [n for n, _ in _rich])
    check("すでに書いてある項目は聞かない",
          all("用途" not in n for n, _q in
              bot._missing_slots("video", "YouTubeショート用の動画作って")),
          bot._missing_slots("video", "YouTubeショート用の動画作って"))
    check("おまかせと言われたら聞かない",
          not bot._missing_slots("video", "おまかせで動画作って"),
          bot._missing_slots("video", "おまかせで動画作って"))

    # 実際の会話：聞く → 答える → その内容で作る
    install_stubs()
    bot.CLARIFY_ON = True
    bot._pending_clarify.clear()
    try:
        _t = asyncio.get_running_loop().create_task(drive("動画作って"))
        for _ in range(80):
            await asyncio.sleep(0)
            if 1234 in bot._pending_clarify:
                break
        check("始める前に聞いてくる", 1234 in bot._pending_clarify,
              "聞いてこない")
        _ch = _CHANNELS.setdefault(1234, _FakeChannel(1234))
        check("何を知りたいのかを書く",
              any("仕上がりを決める項目" in t for t in _ch.sent), _ch.sent[:1])
        check("書かなくてもいいと伝える",
              any("書かなかった項目はこちらで決めます" in t for t in _ch.sent),
              _ch.sent[:1])
        check("答えなくても抜けられると伝える",
              any("おまかせ" in t for t in _ch.sent), _ch.sent[:1])
        await drive("YouTubeショート用、縦で15秒")
        await asyncio.wait_for(_t, timeout=5)
        _c = last_call("hf_generate")
        check("答えた内容が依頼に入る",
              _c is not None and "縦で15秒" in _c[0][1],
              _c[0][1] if _c else f"fired={FIRED}")
        check("答えを『指定』として渡す",
              _c is not None and "【指定】" in _c[0][1],
              _c[0][1] if _c else "")
    finally:
        bot.CLARIFY_ON = False
        bot._pending_clarify.clear()

    # デザインでも聞き返す。ただし作り直し（前回を引き継ぐ形）では聞かない
    check("デザインにも項目がある", bool(bot.CLARIFY_SLOTS.get("design")),
          list(bot.CLARIFY_SLOTS))
    install_stubs()
    bot.CLARIFY_ON = True
    bot._pending_clarify.clear()
    try:
        bot._load_last_gen = lambda cid: {
            "prompt": "前のサムネ", "media_type": "image",
            "label": "デザイン（YouTubeサムネイル）", "t": _tm4.time(),
            "url": "https://example.com/prev.png"}
        _t4 = asyncio.get_running_loop().create_task(drive("文字を大きくして"))
        for _ in range(80):
            await asyncio.sleep(0)
            if "design" in FIRED or 1234 in bot._pending_clarify:
                break
        check("作り直しでは聞き返さない", 1234 not in bot._pending_clarify,
              "作り直しなのに聞いてきた")
        try:
            await asyncio.wait_for(_t4, timeout=5)
        except asyncio.TimeoutError:
            _t4.cancel()
    finally:
        bot.CLARIFY_ON = False
        bot._pending_clarify.clear()

    # 「おまかせ」で必ず抜けられる（聞き返しが行き止まりにならない）
    install_stubs()
    bot.CLARIFY_ON = True
    try:
        _t2 = asyncio.get_running_loop().create_task(drive("動画作って"))
        for _ in range(80):
            await asyncio.sleep(0)
            if 1234 in bot._pending_clarify:
                break
        await drive("おまかせ")
        await asyncio.wait_for(_t2, timeout=5)
        check("おまかせでも作業は進む", "hf_generate" in FIRED, f"fired={FIRED}")
        _c2 = last_call("hf_generate")
        check("おまかせを依頼文に混ぜない",
              _c2 is not None and "おまかせ" not in _c2[0][1],
              _c2[0][1] if _c2 else "")
    finally:
        bot.CLARIFY_ON = False
        bot._pending_clarify.clear()

    # 「やめて」で止まる
    install_stubs()
    bot.CLARIFY_ON = True
    try:
        _t3 = asyncio.get_running_loop().create_task(drive("動画作って"))
        for _ in range(80):
            await asyncio.sleep(0)
            if 1234 in bot._pending_clarify:
                break
        await drive("やめて")
        await asyncio.wait_for(_t3, timeout=5)
        check("やめてで作業を始めない", "hf_generate" not in FIRED, f"fired={FIRED}")
    finally:
        bot.CLARIFY_ON = False
        bot._pending_clarify.clear()

    # --- ㉔ 添付しているのに「添付が見当たりません」（09:09の実例）---
    #     デザインの経路が参照画像を1枚も渡しておらず、
    #     しかもプロンプトで外部画像を禁じていたので、写真は絶対に入らなかった。
    install_stubs()
    bot._last_ref.clear()
    _att = types.SimpleNamespace(filename="IMG_1.jpg",
                                 url="https://example.com/IMG_1.jpg", size=1000)
    _m = _FakeMessage("このサムネイルに、この男性の写真を入れて欲しい", [_att])
    _refs = bot._design_refs(_m, "このサムネイルに、この男性の写真を入れて欲しい")
    check("添付した画像を素材として拾う",
          "https://example.com/IMG_1.jpg" in _refs, _refs)
    bot._last_ref.clear()
    bot._remember_ref(1234, "https://example.com/prev.png")
    _m2 = _FakeMessage("この写真を入れて")
    check("直前に送った画像も拾う",
          "https://example.com/prev.png" in bot._design_refs(_m2, "この写真を入れて"),
          bot._design_refs(_m2, "この写真を入れて"))
    bot._last_ref.clear()
    # 素材が無いのに写真を頼まれたら、2分かけて失敗する前に聞く
    _keep24 = bot._run_claude_exec
    _called24 = []

    async def _exec24(task, timeout=600, model=None):
        _called24.append(task)
        return "URL: https://example.com/out.png"
    try:
        bot._run_claude_exec = _exec24
        bot._load_last_gen = lambda cid: None
        _ch24 = _CHANNELS.setdefault(1234, _FakeChannel(1234))
        _ch24.sent.clear()
        await _REAL_DESIGN(_FakeMessage("この男性の写真を入れて"),
                           "この男性の写真を入れて")
        check("素材が無ければ作る前に聞く",
              any("使う写真が見つかりません" in t for t in _ch24.sent), _ch24.sent)
        check("素材が無いまま書き出しに行かない", not _called24, _called24)
        # 素材があれば、その画像を使うよう指示に入れる
        _called24.clear()
        _ch24.sent.clear()
        bot._remember_ref(1234, "https://example.com/IMG_1.jpg")
        await _REAL_DESIGN(_FakeMessage("この男性の写真を入れて"),
                           "この男性の写真を入れて")
        check("素材があれば書き出しに進む", bool(_called24), _called24)
        check("画像のURLを指示に入れる",
              _called24 and "https://example.com/IMG_1.jpg" in _called24[0],
              (_called24[0][:200] if _called24 else ""))
        check("先に落としてから使わせる",
              _called24 and "curl" in _called24[0] and "【使う画像】" in _called24[0],
              "")
    finally:
        bot._run_claude_exec = _keep24
        bot._last_ref.clear()

    # --- ㉕ 依頼文が作り直しのたびに積み上がって壊れた（09:23の実例）---
    #     「この2枚の写真を組み合わせて」の直後に「クロードでやって」と言ったら、
    #     ずっと前の「背景を室内に変えて」が引きずり出され、
    #     依頼文は「背景を室内に変えて 【今回の修正指示】クロードで
    #     【今回の修正指示】クロードでや」になっていた。
    _stacked = ("背景を室内に変えて\n【今回の修正指示】クロードで"
                "\n【今回の修正指示】クロードでや")
    check("元の依頼だけを取り出せる",
          bot._base_request(_stacked) == "背景を室内に変えて",
          bot._base_request(_stacked))
    check("積み上げは1段までに抑える",
          bot._stack_revise(_stacked, "文字を大きく").count("【今回の修正指示】") == 1,
          bot._stack_revise(_stacked, "文字を大きく"))
    check("元の依頼は消さない",
          "背景を室内に変えて" in bot._stack_revise(_stacked, "文字を大きく"),
          bot._stack_revise(_stacked, "文字を大きく"))
    # 「クロードでやって」は直前の【依頼】をやり直す（直前の生成物ではなく）
    install_stubs()
    bot.CLARIFY_ON = False
    bot._load_last_gen = lambda cid: {
        "prompt": "背景を室内に変えて", "media_type": "image",
        "label": "デザイン（YouTubeサムネイル）", "t": _tm4.time(),
        "url": "https://example.com/prev.png"}
    await drive("この2枚の写真を、いい感じに組み合わせて")
    install_stubs()
    bot._load_last_gen = lambda cid: {
        "prompt": "背景を室内に変えて", "media_type": "image",
        "label": "デザイン（YouTubeサムネイル）", "t": _tm4.time(),
        "url": "https://example.com/prev.png"}
    await drive("クロードでやって")
    _c25 = last_call("design")
    check("直前の依頼をやり直す（古い生成物を引きずらない）",
          _c25 is not None and "組み合わせて" in _c25[0][1],
          _c25[0][1] if _c25 else f"fired={FIRED}")
    check("古い依頼を混ぜない",
          _c25 is not None and "背景を室内" not in _c25[0][1],
          _c25[0][1] if _c25 else "")

    # --- ㉖ 写真を添付した加工の依頼が会話に落ち、2枚目が無視されていた ---
    for _t in ("この2枚の写真を、いい感じに組み合わせて",
               "この写真の背景を消して", "この写真を明るくして"):
        check(f"{_t[:12]!r}… は画像の経路へ",
              bot.classify_route(_t, has_image_att=True,
                                 has_attachments=True) == "image",
              bot.classify_route(_t, has_image_att=True, has_attachments=True))
    check("作り手の名指しがあればそちらが勝つ",
          bot.classify_route("クロードでこの写真を組み合わせて",
                             has_image_att=True, has_attachments=True) == "design",
          bot.classify_route("クロードでこの写真を組み合わせて",
                             has_image_att=True, has_attachments=True))
    check("写真があっても、依頼でなければ会話のまま",
          bot.classify_route("いい感じだね", has_image_att=True,
                             has_attachments=True) is None,
          bot.classify_route("いい感じだね", has_image_att=True,
                             has_attachments=True))
    # 添付が2枚なら2枚とも渡す
    install_stubs()
    _got = {}

    def _gen2(prompt, ref_bytes=None, ref_mime="image/png", extra_refs=None):
        _got["n"] = len(extra_refs or []) + (1 if ref_bytes else 0)
        return b"PNG"

    async def _fetch2(url, limit=None):
        return b"BYTES", "image/jpeg"

    _keep26 = (bot._gemini_generate_image_sync, bot._fetch_image_bytes,
               bot._refine_prompt)
    try:
        bot._gemini_generate_image_sync = _gen2
        bot._fetch_image_bytes = _fetch2

        async def _refine26(req, mtype, style="", has_ref=False):
            _got["has_ref"] = has_ref
            return "two photos combined naturally, soft light, photorealistic"
        bot._refine_prompt = _refine26
        _ch26 = _CHANNELS.setdefault(1234, _FakeChannel(1234))
        _ch26.sent.clear()
        await _REAL_IMAGE_REQ(
            1234, "この2枚の写真をいい感じに組み合わせて",
            refs=["https://example.com/a.jpg", "https://example.com/b.jpg"])
        check("2枚とも素材として渡す", _got.get("n") == 2, _got)
        check("参照ありとして英訳させる", _got.get("has_ref") is True, _got)
        check("何枚使ったかを伝える",
              any("2枚を素材として使います" in t for t in _ch26.sent),
              _ch26.sent[:2])
    finally:
        (bot._gemini_generate_image_sync, bot._fetch_image_bytes,
         bot._refine_prompt) = _keep26

    # --- ㉗ 何も動いていないのに「作り直しますね」と言っていた（09:30の実例）---
    #     丁寧形が守り手の網から漏れていた。語を並べる方式をやめ、形で受ける。
    install_stubs()
    bot._pending_approvals.pop(1234, None)
    for _t in ("元のサムネイルに男性の写真を組み込む形で作り直しますね。"
               "少々お待ちください。",
               "了解、進めますね。",
               "いま対応します。少しお待ちください。"):
        _out = bot._drop_false_progress(_t, 1234)
        check(f"動いていないのに宣言しない: {_t[:12]}…",
              "まだ実際には動かしていない" in _out or "まだ何も動かしていない" in _out,
              _out[:60])
    check("普通の返事は落とさない",
          bot._drop_false_progress("いい天気だね。散歩でもする？", 1234)
          == "いい天気だね。散歩でもする？",
          bot._drop_false_progress("いい天気だね。散歩でもする？", 1234))
    # 「〜して欲しかった」は直しの指示（09:29の実例）
    check("『組み込んで欲しかった』は直しの指示",
          bot.classify_route("全然指示と違う、サムネイルに写真を組み込んで欲しかった",
                             has_last_gen=True, last_was_design=True) == "design",
          bot.classify_route("全然指示と違う、サムネイルに写真を組み込んで欲しかった",
                             has_last_gen=True, last_was_design=True))
    check("お礼を直しの指示にしない",
          bot.classify_route("ありがとう助かった", has_last_gen=True,
                             last_was_design=True) is None,
          bot.classify_route("ありがとう助かった", has_last_gen=True,
                             last_was_design=True))

    # --- ㉘ 言い方ではなく【状態】で「動いていない」を明記する ---
    #     本人の指摘：「何回も修正してる／二度と抜け漏れないようにしろ」。
    #     これまでは返事の言い方を正規表現で拾っていたので、
    #     丁寧形・言い換えが出るたびに漏れた。状態だけで判定すれば漏れない。
    install_stubs()
    bot._pending_approvals.pop(1234, None)
    bot._pending_do.clear()
    _keep28 = bot._busy_tasks
    try:
        bot._busy_tasks = lambda cid: []          # 何も動いていない
        _n = bot._reality_note(1234, "このサムネイルに写真を組み込んで欲しい")
        check("頼まれたのに動いていなければ明記する",
              "まだ何も動いていません" in _n, _n[:60])
        check("次に何をすればいいかを書く", "やって" in _n, _n[:60])
        check("雑談には付けない", bot._reality_note(1234, "今日つかれた") == "", "")
        check("お礼には付けない",
              bot._reality_note(1234, "ありがとう助かった") == "", "")
        check("制作以外の依頼には付けない",
              bot._reality_note(1234, "ログ送って") == "", "")
        bot._busy_tasks = lambda cid: ["動画生成"]  # 本当に動いている
        check("本当に動いている時は付けない",
              bot._reality_note(1234, "動画作って") == "", "")
    finally:
        bot._busy_tasks = _keep28
        bot._pending_do.clear()

    # --- ㉙ 聞き返しの答えが、次の依頼として拾われていた（10:16の実例）---
    #     「クロードで作って」の題材が「16:9 実写 顔のアップ 自然光」になり、
    #     本来の依頼（この人の画像で肩書きは嫉妬ガエル…）が消えていた。
    install_stubs()
    bot._history.clear() if hasattr(bot, "_history") else None
    bot.add_history(1234, "kohei", "この人の画像使って、肩書きは嫉妬ガエルにして")
    bot.add_history(1234, "kohei", bot.CLARIFY_MARK + "16:9 実写 顔のアップ 自然光")
    check("聞き返しの答えは依頼として拾わない",
          "嫉妬ガエル" in bot._last_request_text(1234),
          bot._last_request_text(1234)[:60])
    check("答えの中身を題材にしない",
          "16:9" not in bot._last_request_text(1234),
          bot._last_request_text(1234)[:60])
    check("補うときも本来の依頼を使う",
          "嫉妬ガエル" in bot._request_with_context("クロードで作って", 1234),
          bot._request_with_context("クロードで作って", 1234)[:60])

    print("■ Agent と Model Registry（段階1）")
    # 「どちらのAIが今使えるか」の判断を1か所に集めた。
    # これまでは上限・枠切れの対処があちこちにあり、食い違っていた。
    check("共通の形を持っている",
          all(hasattr(a, "generate") and hasattr(a, "health_check")
              and hasattr(a, "get_capabilities") for a in bot.AGENTS),
          [a.name for a in bot.AGENTS])
    check("得意分野を宣言している",
          "reasoning" in bot.CLAUDE_AGENT.get_capabilities()
          and "web_research" in bot.GEMINI_AGENT.get_capabilities(),
          True)
    _keep_cl = dict(bot._claude_limit)
    _keep_cd2 = dict(bot._gemini_cooldown)
    try:
        bot._claude_limit.update({"t": 0.0, "why": ""})
        bot._gemini_cooldown.clear()
        check("どちらも使えるなら希望どおりの順番",
              [a.provider for a in bot._agent_order("claude")][0], "claude")
        # Claudeが上限 → Geminiを先に試す（無駄に待たされない）
        bot._claude_limit.update({"t": _tm4.time(), "why": "利用上限に達しています"})
        check("上限のAgentは後回しにする",
              [a.provider for a in bot._agent_order("claude")][0], "gemini")
        check("理由を答えられる",
              "上限" in bot.CLAUDE_AGENT.health_check()[1],
              bot.CLAUDE_AGENT.health_check())
        # 上限の記録は、ユーザーに見せる知らせを作る時にも入る
        bot._claude_limit.update({"t": 0.0, "why": ""})
        bot._claude_fail_note(
            "デザインの書き出し",
            "You've hit your session limit · resets 4am (Asia/Tokyo)")
        check("上限は1か所に記録される", bool(bot._claude_limit["why"]),
              bot._claude_limit)
    finally:
        bot._claude_limit.clear()
        bot._claude_limit.update(_keep_cl)
        bot._gemini_cooldown.clear()
        bot._gemini_cooldown.update(_keep_cd2)

    # Registry：使えるか／理由／順番を1か所で答える
    _keep_m = bot.GEMINI_IMAGE_MODELS[:]
    _keep_cd3 = dict(bot._gemini_cooldown)
    try:
        bot.GEMINI_IMAGE_MODELS[:] = ["m1", "m2"]
        bot._gemini_cooldown.clear()
        bot._gemini_bad_models.clear()
        bot._gemini_img_stats.clear()
        check("使えると答える", bot.REGISTRY.usable(bot.PURPOSE_IMAGE), True)
        bot.REGISTRY.mark_quota("m1")
        check("枠切れは理由つきで止める",
              "枠切れ" in bot.REGISTRY.blocked("m1"), bot.REGISTRY.blocked("m1"))
        check("残りがあれば使える", bot.REGISTRY.usable(bot.PURPOSE_IMAGE), True)
        bot.REGISTRY.mark_dead("m2", "このプランでは使えない（無料枠の割り当てが0）")
        bot.REGISTRY.mark_dead("m1", "このプランでは使えない（無料枠の割り当てが0）")
        check("全部だめなら使えないと答える",
              not bot.REGISTRY.usable(bot.PURPOSE_IMAGE), True)
        check("なぜ使えないかを答える",
              "割り当てが0" in bot.REGISTRY.why_not(bot.PURPOSE_IMAGE),
              bot.REGISTRY.why_not(bot.PURPOSE_IMAGE))
        check("案内文も同じ判断を使う",
              not bot._gemini_image_usable(), True)
        bot.REGISTRY.mark_ok("m1")
        check("通ったら枠切れの記録は消える",
              bot.REGISTRY.blocked("m1"), "使えないID・プラン")
    finally:
        bot.GEMINI_IMAGE_MODELS[:] = _keep_m
        bot._gemini_cooldown.clear()
        bot._gemini_cooldown.update(_keep_cd3)
        bot._gemini_bad_models.clear()
        bot._gemini_img_stats.clear()

    print("■ 無駄をなくす（履歴の膨張・テストの汚染）")
    # 写真1枚で2,000〜4,000字の分析文が履歴に入り、直近40発言として
    # 【毎回のAI呼び出しに全部乗って】いた。遅く・高く・不正確になる。
    _sample = ("この2枚の写真をいい感じに組み合わせて\n\n"
               "【メッセージに添付されたファイル】\n【画像: IMG_1.jpg】\n"
               + "この画像には人物が写っています。構図は中央集中型で…" * 60)
    _short = bot._history_text(_sample)
    check("添付の分析文は履歴では短くする", len(_short) < len(_sample) // 2,
          (len(_sample), len(_short)))
    check("依頼の本文は必ず残す", "組み合わせて" in _short, _short[:40])
    check("省略したことが分かるようにする", "以下省略" in _short, _short[-60:])
    check("短い発言はそのまま",
          bot._history_text("動画作って") == "動画作って",
          bot._history_text("動画作って"))
    # テストが本物の履歴に書き込まないこと（起動時セルフテストで毎回走るため）
    import pathlib as _pl9
    check("テストの会話を本物の履歴に混ぜない",
          str(bot.HISTORY_DIR) != str(
              _pl9.Path(bot.__file__).parent / "history"),
          str(bot.HISTORY_DIR))

    print("■ 雑談では校閲しない（速さのため）")
    # 校閲はGeminiを1回、直しが要ればクロードをもう1回呼ぶ。
    # 毎回やると雑談の返事が数秒遅くなるので、間違えると困る話題だけにする。
    _long = "あ" * 200
    for _said in ("今日つかれた", "蕎麦美味しかった？", "いい休日だった",
                  "ありがとう助かった"):
        check(f"雑談は校閲しない: {_said}",
              not bot._needs_review(_long, [("kohei", _said)]), True)
    for _said, _why in (("サムネ作って", "制作"),
                        ("アップルウォッチの相場は？", "実データ"),
                        ("ボットが反応しない", "不具合"),
                        ("クロード3の役割は？", "運用")):
        check(f"{_why}は校閲する: {_said}",
              bot._needs_review(_long, [("kohei", _said)]), True)
    check("長い返事は雑談でも校閲する",
          bot._needs_review("あ" * 600, [("kohei", "今日つかれた")]), True)
    check("短い返事は校閲しない",
          not bot._needs_review("うん", [("kohei", "サムネ作って")]), True)
    # 実際に呼ばれないこと（Geminiを消費しない）
    install_stubs()
    _called = []
    _keep_rv = bot._gemini_call

    async def _gem_rv(prompt, tag=""):
        _called.append(tag)
        return "問題なし"
    _keep_cool = bot._gemini_all_cooling
    try:
        bot._gemini_call = _gem_rv
        bot._gemini_all_cooling = lambda: False   # 枠切れの影響を受けないように
        await bot._review_reply(_long, [("kohei", "今日つかれた")])
        check("雑談ではGeminiを呼ばない", not _called, _called)
        await bot._review_reply(_long, [("kohei", "サムネ作って")])
        check("制作の話では校閲が走る", bool(_called), _called)
    finally:
        bot._gemini_call = _keep_rv
        bot._gemini_all_cooling = _keep_cool

    print("■ Discordでコードを触る作業（本人の指摘）")
    # 事故：ボットが「fixturesに3つファイルを追加する仕組み」を提案した直後、
    # 「それクロードでやってくれる？やり方わからん」がどの機能にも流れず会話で終了。
    bot._last_bot_say.clear()
    bot._remember_bot_say(
        7, "fixtures/ に3つのファイルを追加する仕組み：youtube_insights.md ─ "
           "TOP100リサーチの知見を整理／prompt_experiments.md ─ 試行ログを記録")
    for _t in ("それクロードでやってくれる？やり方わからん", "やって", "お願い",
               "それやって"):
        check(f"提案の直後の「{_t[:10]}」は実行へ",
              bot.classify_route(_t, cid=7) == "selffix",
              (bot.classify_route(_t, cid=7), bot._route_hit["name"]))
    check("提案の中身が作業内容に入る",
          "youtube_insights" in bot._selffix_task(7, "やって"),
          bot._selffix_task(7, "やって")[:80])
    for _t in ("ありがとう", "明日の天気は？", "今日つかれた"):
        check(f"関係ない発言は実行しない: {_t}",
              bot.classify_route(_t, cid=7) != "selffix",
              bot.classify_route(_t, cid=7))
    bot._last_bot_say.clear()
    check("提案が無ければ実行しない",
          bot.classify_route("それやって", cid=7) != "selffix",
          bot.classify_route("それやって", cid=7))
    bot._remember_bot_say(7, "今日は蕎麦食べたんだね。いい休日になってよかった。ゆっくり休んで。")
    check("雑談の直後は実行しない",
          bot.classify_route("やって", cid=7) != "selffix",
          bot.classify_route("やって", cid=7))
    bot._last_bot_say.clear()

    # モデルの切り替え：打ち間違いでも通す／使えない名前は嘘をつかない
    for _t in ("ハイクににして", "ハイクにして", "haikuで", "オーパスでお願い"):
        check(f"モデル切替が通る: {_t}",
              bot._match_claude_model(_t) is not None, bot._match_claude_model(_t))
    for _t in ("モデルをフェイブル5にして", "クロードをGPT5にして"):
        check(f"使えない名前だと分かる: {_t}",
              bot._match_claude_model(_t) is None
              and bool(bot._UNKNOWN_MODEL_RE.search(_t)), _t)
    check("生成モデルの指定は巻き込まない",
          bool(bot._match_gen_model("veo3で動画作って")), True)

    print("■ 学びを溜める（スマホ1通で追記・入院中でも残せる）")
    import tempfile as _tfn, pathlib as _pln
    _tmpn = _pln.Path(_tfn.mkdtemp())
    _keepn = dict(bot.NOTES)
    _keep_push = bot._push_paths
    try:
        bot.NOTES = {
            "experiment": (_tmpn / "prompt_experiments.md", "プロンプト実験ログ"),
            "insight": (_tmpn / "youtube_insights.md", "YouTube知見"),
            "failed": (_tmpn / "failed_patterns.md", "効かなかった表現"),
        }

        async def _no_push(paths, msg):
            return True, ""
        bot._push_paths = _no_push
        # 何を書くかの仕分け
        for _t, _want in (
            ("記録して Veoで高デンシティ試した→成功", "experiment"),
            ("メモ 明日から入院", "experiment"),
            ("知見メモ カメラ寄り引き6:4が効く", "insight"),
            ("失敗メモ cinematic lighting は効かなかった", "failed"),
        ):
            _k = bot._note_kind(_t)
            check(f"{_t[:12]!r} の行き先", _k and _k[0] == _want, _k)
        for _t in ("この動画メモしておきたいんだけど", "記録して", "動画作って",
                   "ログ送って"):
            check(f"{_t[:14]!r} は記録にしない", bot._note_kind(_t) is None,
                  bot._note_kind(_t))
        # 読み返しは記録と取り違えない
        for _t, _want in (("実験ログ見せて", "experiment"), ("知見見せて", "insight"),
                          ("失敗メモ見せて", "failed")):
            check(f"{_t!r} は読み返し", bot._note_show_kind(_t) == _want,
                  bot._note_show_kind(_t))
            check(f"{_t!r} を記録として保存しない", bot._note_kind(_t) is None,
                  bot._note_kind(_t))
        # 実際に書いて、読み返せる
        _r = await bot._run_note(1234, "experiment",
                                 "Veoで高デンシティ試した→成功。カット3つ")
        check("記録したと伝える", "記録しました" in _r, _r[:40])
        check("ファイルに残る",
              "高デンシティ" in bot.NOTES["experiment"][0].read_text(encoding="utf-8"),
              True)
        await bot._run_note(1234, "experiment", "2件目の実験")
        _out = bot._read_note("experiment", 5)
        check("新しい順に読み返せる",
              _out.index("2件目") < _out.index("高デンシティ"), _out[:80])
        check("何件あるかも分かる", "全2件" in _out, _out[:80])
        check("空のときは空と言う",
              "まだ空です" in bot._read_note("failed", 5),
              bot._read_note("failed", 5))
    finally:
        bot.NOTES = _keepn
        bot._push_paths = _keep_push

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
