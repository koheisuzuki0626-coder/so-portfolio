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

# 実物の会話ハンドラ（install_stubs で記録用に差し替わる前に退避）。
# 「判定＋返事の1回化」など、会話ハンドラ本体の挙動を検証するテストで使う。
_HANDLE_ORCH = bot._handle_orchestrator
_EXTRACT_ATT = bot.extract_attachment_context
_MEDIA_CTX = bot._apply_media_url_context
_REPLY_CTX = bot._reply_context


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
    for ch in _CHANNELS.values():
        ch.sent.clear()   # 前のテストの送信内容が混ざらないように
    bot._self_diagnose = _rec_str("diagnose", "🩺 診断結果（スタブ）")
    bot._run_hf_generate = _rec("hf_generate")
    bot._run_short = _rec("short")
    bot._run_revise = _rec("revise")
    bot._run_ad_make = _rec("ad")
    bot._run_virality = _rec("virality")
    bot._run_style_learn = _rec("style_learn")
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
        ("新作スニーカーの広告作って", "ad", None),
        ("コーヒーショップのCM作って", "ad", None),
        ("バズ度分析して", "virality", None),
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

    # ジョブも直近生成も無い →「できた？」は状態確認に入らず普通の会話へ
    install_stubs()
    r = await drive("モーション動画できた？")
    check("ジョブ無し→自然な会話へ", "orchestrator" in r["fired"],
          f"fired={r['fired']} sent={r['sent']}")
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
    check("生成なし→会話へ", "orchestrator" in r["fired"], f"fired={r['fired']}")
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
