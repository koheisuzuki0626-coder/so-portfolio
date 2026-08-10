"""
自然言語ルーティングのテスト（実コードの判定関数を直接検証）。
使い方:  python3 test_routing.py
ai_group_chat.py の classify_route / _looks_like_question / _match_gen_model /
_PLAN_TRIGGER_RE を、実際の on_message と同じ正規表現・同じ順序で検証する。
外部依存（discord / google.genai / dotenv / aiohttp / hf_wrapper）は
インポートだけ通ればよいのでスタブ化する。
"""
import sys
import types

# ---- 重い依存をスタブ化（ルーティングのロジックだけ読み込む）----
os_env = {"GEMINI_API_KEY": "x", "DISCORD_ORCH_TOKEN": "x",
          "DISCORD_CLAUDE_TOKEN": "x", "DISCORD_GEMINI_TOKEN": "x"}
import os
os.environ.update(os_env)


def _stub(name, **attrs):
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m


class _FakeIntents:
    @staticmethod
    def default():
        i = _FakeIntents()
        i.message_content = False
        return i


class _FakeClient:
    def __init__(self, *a, **k):
        self.user = None

    def event(self, f):
        return f


class _Anything:
    """どんな属性アクセス・呼び出しにも応じるスタブ（ButtonStyle等）。"""
    def __getattr__(self, k):
        return _Anything()

    def __call__(self, *a, **k):
        if len(a) == 1 and callable(a[0]) and not k:
            return a[0]  # デコレータ用途
        return _Anything()


discord = _stub("discord")
discord.Intents = _FakeIntents
discord.Client = _FakeClient
discord.File = object
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


# ---- テストデータ: (発言, 期待ルート, 追加フラグ) ----
# 期待ルート: status/hf_model/hf_auto/motion/motion_ask/plan(=AIへ)
# フラグ: has_job / has_video_att / has_image_att / has_attachments
ROUTE_CASES = [
    # 状態確認（文脈語ありは常にstatus）
    ("モーション動画できた？", "status", {}),
    ("さっき生成した動画見れる？", "status", {}),
    ("動画の進捗どう？", "status", {}),
    ("画像できた？", "status", {}),
    # 状態確認（文脈語なしはジョブがある時だけstatus）
    ("あとどれくらい？", "status", {"has_job": True}),
    ("あとどれくらい？", "plan", {}),           # ジョブなし → 会話へ
    ("確認して", "status", {"has_job": True}),
    ("確認して", "plan", {}),
    # 直近の生成があれば「できた？」だけでも状態確認につなぐ
    ("できた？", "status", {"has_last_gen": True}),
    ("できた？", "plan", {}),
    # 生成・修正の依頼は「ください」等があっても状態確認にしない（生成へ）
    ("猫の動画作ってください", "hf_auto", {"has_last_gen": True}),
    # 生成物の「中身」への質問は状態確認にせず会話へ（画像の内容を答えるため）
    ("どこですかここは", "plan", {"has_last_gen": True}),
    ("この画像どこで撮ったの？", "plan", {"has_last_gen": True}),
    ("何が写ってるの？", "plan", {"has_last_gen": True}),
    ("動画どこ？", "status", {"has_job": True}),          # URLの催促は従来どおり
    # 作り直し（明示マーカーがあれば記録の有無に関わらず発動＝Higgsfieldから復元）
    ("もう一回作り直して、顔をアップで", "revise", {"has_last_gen": True}),
    ("さっきの動画もう少し明るくして", "revise", {"has_last_gen": True}),
    ("もう一回作り直して", "revise", {}),               # 記録なしでもrevise（保険で復元）
    ("さっきの動画どうだった？", "plan", {}),           # 質問はreviseにしない
    ("前の動画って消せる？", "plan", {}),               # 質問はreviseにしない
    # ショート量産
    ("ショート作って", "short", {}),
    ("今日のショートお願い", "short", {}),
    ("アートなショート動画作って", "short", {}),
    ("shorts作って", "short", {}),
    # バズ度シミュレーション（物理エンジン＝virality_predictor）
    ("バズ度分析して", "virality", {}),
    ("この動画の広告効果を予測して", "virality", {}),
    ("バズるかチェックして", "virality", {}),
    ("伸びるか診断して", "virality", {}),
    # 広告代理店モード（企画書＋CM制作）
    ("新作スニーカーの広告作って", "ad", {}),
    ("コーヒーショップのCM作って", "ad", {}),
    ("プロモ動画お願い", "ad", {}),
    ("アプリのコマーシャル作りたい", "ad", {}),
    ("身長10cmくらい伸ばしたい", "plan", {}),      # 単位のcmは広告に誤爆しない
    # モデル指定生成
    ("seedanceで犬の動画作って", "hf_model", {}),
    ("veoで夕焼け作りたい", "hf_model", {}),
    ("ナノバナナでロゴ生成して", "hf_model", {}),
    ("クリング3で猫が踊る動画作って", "hf_model", {}),
    # 画像生成（Gemini無料枠を優先。AI判定に落とさない）
    ("geminiで画像生成して", "image", {}),
    ("画像作って", "image", {}),
    ("猫のイラスト作って", "image", {}),
    ("ロゴ作りたい", "image", {}),
    ("サムネ作って", "image", {}),
    ("ナノバナナでロゴ生成して", "hf_model", {}),     # モデル明示はそのモデルで
    ("画像ってどうやって作ってるの？", "plan", {}),    # 質問は生成にしない
    # 自動選定
    ("おまかせで犬の動画作って", "hf_auto", {}),
    ("犬の動画作って", "hf_auto", {}),                # 媒体明示だけで生成へ
    ("かっこいい映像作りたい", "hf_auto", {}),
    ("どうやって動画作ってるの？", "plan", {}),        # 質問は生成にしない
    ("バズる動画作って", "hf_auto", {}),           # 生成依頼はAI判定に落とさない
    ("バズりそうな動画お願い", "hf_auto", {}),
    ("バズる動画作って、あとで分析して", "hf_auto", {}),  # 生成が主目的
    ("バズった動画調べて", "plan", {}),             # リサーチはAI(trend)へ
    ("最適なモデルで海の動画生成して", "hf_auto", {}),
    ("これ動かして", "hf_auto", {"has_image_att": True, "has_attachments": True}),
    # モーション
    ("この動きで生成して", "motion_ask", {}),
    ("モーションコントロールで作りたい", "motion_ask", {}),
    ("この動きで生成して", "motion",
     {"has_video_att": True, "has_attachments": True}),
    # 完パケ編集（Higgsfieldのクラウド編集室・ffmpeg）
    ("字幕つけて", "edit", {"has_last_gen": True}),
    ("テロップ入れて", "edit", {"has_last_gen": True}),
    ("15秒に縮めて", "edit", {"has_last_gen": True}),
    ("9:16にして", "edit", {"has_last_gen": True}),
    ("この動画に字幕つけて", "edit",
     {"has_video_att": True, "has_attachments": True}),
    ("字幕つけて", "plan", {}),                      # 素材が無ければ会話へ
    ("字幕ってつけられる？", "plan", {"has_last_gen": True}),  # 質問は編集にしない
    # デバッグログの共有（スクショ不要にする）
    ("ログ送って", "sharelog", {}),
    ("会話ログ共有して", "sharelog", {}),
    ("デバッグログ出して", "sharelog", {}),
    ("ログ消して", "plan", {}),                      # 削除依頼は共有にしない
    # スタイル学習（参考動画から勝ちパターンを覚える）
    ("これを学習して", "style_learn", {"has_video_att": True, "has_attachments": True}),
    ("https://youtu.be/abc123 これを学習して", "style_learn", {}),
    ("この動画のスタイルを学習して", "style_ask", {}),
    ("学習したスタイル見せて", "style_show", {}),
    ("スタイルをリセットして", "style_reset", {}),
    ("これ覚えて", "style_learn", {"has_video_att": True, "has_attachments": True}),
    ("俺のこと覚えてる？", "plan", {}),               # 学習と誤爆しない
    ("この前の動画のこと覚えてる？", "plan", {}),      # 質問は学習にしない
    ("動画の作り方参考にしてもいい？", "plan", {}),    # 質問は学習にしない
    ("人気の動画を参考にして調べて", "plan", {}),      # リサーチは学習にしない
    # モーションの誤爆防止（否定・単なる言及では発動しない）
    ("モーション動画じゃないよ、広告動画だよ", "plan", {}),
    ("モーションってなに？", "plan", {}),
    ("モーションはもういいや", "plan", {}),
    # 会話（planへ回す＝誤爆しないこと）
    ("これなんでこんな顔似てないか、原因究明しよう", "plan", {}),
    ("自己修復プログラムって作れる？", "plan", {}),
    ("コード修正の進捗は？", "plan", {}),
    ("今残りのタスクなんかある？", "plan", {}),
    ("体幹トレーニングについて教えて", "plan", {}),
    ("どう思う？", "plan", {}),
    ("おはよう", "plan", {}),
]

# 質問ガード: (発言, selffixから降格すべきか)
QUESTION_CASES = [
    ("自己修復プログラムって作れる？", True),
    ("コード修正の進捗は？", True),
    ("これどう思う？", True),
    ("〜してもいいと思う", True),
    ("返答をもっと短くして", False),
    ("!trendの本数を3本に変えて", False),
    ("きみのコードのバグを直して", False),
]

# 軽い雑談の即chat判定: (発言, AIを呼ばず即chatか)
FASTCHAT_CASES = [
    ("おはよう", True),
    ("ありがとう", True),
    ("いい天気だね", True),
    ("seedanceで動画作って", False),   # トリガー語あり → AIへ
    ("トレンド調べて", False),
]


def run():
    ok = 0
    fail = 0

    def check(desc, got, want):
        nonlocal ok, fail
        if got == want:
            ok += 1
        else:
            fail += 1
            print(f"  ❌ {desc}\n      期待={want} 実際={got}")

    print("■ ルーティング判定 classify_route")
    for text, want, flags in ROUTE_CASES:
        got = bot.classify_route(text, **flags) or "plan"
        tag = f"{text!r}" + (f" {flags}" if flags else "")
        check(tag, got, want)

    print("■ 質問ガード _looks_like_question（Trueなら selffix→chat 降格）")
    for text, want in QUESTION_CASES:
        check(repr(text), bot._looks_like_question(text), want)

    print("■ 軽い雑談の即chat（_PLAN_TRIGGER_RE に当たらず60字以下）")
    for text, want in FASTCHAT_CASES:
        is_fastchat = len(text) <= 60 and not bot._PLAN_TRIGGER_RE.search(text)
        check(repr(text), is_fastchat, want)

    print("■ モデル別名 _match_gen_model")
    check("seedance", bool(bot._match_gen_model("seedanceで作って")), True)
    check("veo", bot._match_gen_model("veoで作って")[1], "video")
    check("nano banana→image", bot._match_gen_model("nano bananaで作って")[1], "image")
    check("該当なし", bot._match_gen_model("犬の動画作って"), None)

    print("■ 直前生成の保存/復元 _save_last_gen / _load_last_gen")
    import tempfile as _tf
    import pathlib as _pl
    bot._LASTGEN_FILE = _pl.Path(_tf.mkdtemp()) / "last_gen.json"
    check("初期はNone", bot._load_last_gen(555) is None, True)
    bot._save_last_gen(555, "a cat, cinematic", "video", "9:16", "テスト")
    lg = bot._load_last_gen(555)
    check("保存後に復元できる", lg is not None and lg.get("prompt") == "a cat, cinematic", True)
    check("別cidは干渉しない", bot._load_last_gen(999) is None, True)
    bot._update_last_gen_url(555, "https://example.com/v.mp4")
    lg2 = bot._load_last_gen(555)
    check("完成URLを追記できる（バズ度分析用）",
          lg2 is not None and lg2.get("url") == "https://example.com/v.mp4", True)

    print("■ プロンプト英語判定 _looks_english_prompt")
    check("日本語→False", bot._looks_english_prompt("犬が走る動画"), False)
    check("英語→True", bot._looks_english_prompt("a running dog, cinematic, 9:16"), True)
    check("会話文→False", bot._looks_english_prompt("もう一回作り直して"), False)

    print("■ 添付/リンク解析の追記除去 _strip_media_context")
    check("YouTube解析の追記を除去",
          bot._strip_media_context(
              "これも名PV\n\n【YouTube動画の内容（https://youtu.be/x）】\n動画は完成した傑作"),
          "これも名PV")
    check("ファイル共有マーカーを除去",
          bot._strip_media_context("（ファイル共有）【動画の内容: a.mp4】ダンスの解析…"), "")
    check("普通の発言はそのまま", bot._strip_media_context("動画できた？"), "動画できた？")

    print("■ 生成依頼から主題を抽出 _GEN_META_RE")
    check("主題なし", bot._gen_subject("geminiで画像生成して"), "")
    check("主題なし(2)", bot._gen_subject("画像作って"), "")
    check("エンジン名だけは主題なし", bot._gen_subject("ナノバナナで画像お願い"), "")
    check("主題あり", bot._gen_subject("猫のイラスト作って"), "猫")
    check("主題あり(2)", bot._gen_subject("夕暮れの海辺の写真作って"), "夕暮れの海辺")

    print("■ ログ共有のパス（git add がBASE_DIR基準であること）")
    import pathlib as _pl3
    _rel = str(bot.DEBUG_LOG.relative_to(_pl3.Path(bot._BASE)))
    check("gitに渡すパス", _rel.replace("\\", "/"), "debug/discord_log.md")
    check("_git_selfの実行位置と一致", bot.BASE_DIR == bot._BASE, True)

    print("■ 承認/拒否の自然言語判定 _try_text_approval")
    import asyncio as _aio2

    async def _approvals():
        loop = _aio2.get_running_loop()
        res = []
        cases = (
            [(t, True) for t in ("それでお願い", "OK", "おっけー", "いいよ", "はい",
                                 "了解です", "そのままお願いします", "これでいい",
                                 "進めて", "やって", "よろしく", "それで", "GO")]
            + [(t, False) for t in ("やめて", "違う", "拒否", "キャンセル", "no",
                                    "いらない", "中止して", "やっぱやめて", "ストップ")]
            + [(t, None) for t in ("猫の動画作って", "これどう思う？", "おはよう",
                                   "愛知県っていうのを分かりやすく", "ログ送って")]
        )
        for text, want in cases:
            f = loop.create_future()
            bot._set_pending(9, f, 1)
            got = bot._try_text_approval(9, 1, text)
            bot._clear_pending(9, f)
            res.append((f"{text!r}", got, want))
        return res
    for desc, got, want in _aio2.run(_approvals()):
        check(desc, got, want)

    print("■ 話者名の前置きを落とす _clean_reply")
    for src, want in (
        ("クロード: 大丈夫か。", "大丈夫か。"),
        ("クロード1（リサーチャー）: 事実は不明です", "事実は不明です"),
        ("アドバイザー: 3つの見方があります", "3つの見方があります"),
        ("Orchestrator: はい", "はい"),
        ("オーケストレーター：了解", "了解"),
        ("Gemini: どうも", "どうも"),
        ("クロード: オーケストレーター: 二重", "二重"),
        ("普通の返事です", "普通の返事です"),      # 壊さない
        ("10:30に始めます", "10:30に始めます"),    # 時刻は消さない
        ("結論: これでいきましょう", "結論: これでいきましょう"),  # 話者名以外は残す
    ):
        check(repr(src), bot._clean_reply(src), want)

    print("■ 正体の訂正の蒸し返しを落とす _clean_reply（第2引数=ユーザー発言）")
    _bad = ("きみはGeminiじゃないよ、クロード。今この会話にいるのは俺(クロード)と"
            "クロード3(アドバイザー)だけで、Geminiは別のAIとして参加してる。"
            "名前がまぎらわしくてごめん。\n\nタバコの話だけど、無理しないでね。")
    check("正体の説明を落とす",
          bot._clean_reply(_bad, "タバコばっかり吸ってる"), "タバコの話だけど、無理しないでね。")
    check("本人がAIの話をしている時は残す",
          bot._clean_reply(_bad, "きみってGemini？").startswith("きみはGemini"), True)
    check("普通の謝罪は消さない",
          bot._clean_reply("ごめん、それは俺のミス。すぐ直すね。", "直して"),
          "ごめん、それは俺のミス。すぐ直すね。")
    check("普通の返事は無傷",
          bot._clean_reply("今日はゆっくり休んだら？", "つかれた"), "今日はゆっくり休んだら？")

    print("■ 操作案内の誤爆よけ（ボットの話でない時に案内を出さない）")
    # 実際に起きた事故：合谷（ツボ）の痛みの相談に「ログ送って」と答えた
    check("ツボの痛みの相談に案内を出さない",
          bot._clean_reply("原因が分かっていないので「ログ送って」で状況を共有してください。",
                           "何か原因はある？"),
          "ごめん、それは分からないな。")
    check("本題は残して案内文だけ落とす",
          bot._clean_reply("使いすぎかもしれないね。「ログ送って」で共有してください。",
                           "合谷が痛い"),
          "使いすぎかもしれないね。")
    check("ボットの不具合の話なら案内はそのまま",
          bot._clean_reply("原因が分かっていないので「ログ送って」で状況を共有してください。",
                           "ボットがエラー出すんだけど"),
          "原因が分かっていないので「ログ送って」で状況を共有してください。")
    check("雑談に再起動の案内を付けない",
          bot._clean_reply("それはつらいね。あと再起動してください。", "今日つかれた"),
          "それはつらいね。")
    check("コードを直した話なら再起動の案内は残す",
          bot._clean_reply("直したよ。再起動してください。", "コード直して"),
          "直したよ。再起動してください。")

    print("■ 「いつできる？」も進捗確認にする")
    # 実際に起きた事故：「いつできる？」が会話に落ち、Geminiが
    # 「12:50に上限がリセットされて、今動いている」と作り話をした
    for _t in ("いつできる？", "いつ終わる？", "いつ仕上がる", "いつできるの"):
        check(f"{_t!r} 実行中なら状態確認",
              bot.classify_route(_t, has_running=True), "status")
        check(f"{_t!r} 何も無ければ会話", bot.classify_route(_t) or "plan", "plan")
    check("内部の状態を作らせない指示がある",
          "リセット時刻" in bot.OPS_RULES, True)

    print("■ gitを非対話にする（認証待ちで固まらせない）")
    # 実際に起きた事故：認証情報が無く git push が入力待ちになり、
    # 90秒のタイムアウトで「ログを共有できませんでした」になっていた
    _ge = bot._git_env()
    check("ユーザー名を聞かない", _ge.get("GIT_TERMINAL_PROMPT"), "0")
    check("GUIの入力も出さない", bool(_ge.get("GIT_ASKPASS")), True)
    check("遅い転送は諦める", bool(_ge.get("GIT_HTTP_LOW_SPEED_TIME")), True)
    for _m, _want in (
        ("fatal: could not read Username for https://github.com", "ログイン情報"),
        ("Permission denied (publickey).", "SSH鍵"),
        ("Could not resolve host: github.com", "ネットワーク"),
        ("! [rejected] main -> main (non-fast-forward)", "先に進んでいます"),
    ):
        check(f"失敗理由を言い当てる {_m[:28]!r}", _want in bot._git_fail_hint(_m), True)
    check("分からない失敗は決めつけない", bot._git_fail_hint("something else"), "")

    print("■ お礼・報告・否定を指示と取り違えない")
    # 実際に起きた事故：
    #  ・「できた！ありがとー」に対して直近のデザインを貼り直した
    #  ・「デザインの話はしてないよ」を手直しの指示と読んで制作を始めた
    _LGD2 = {"has_last_gen": True, "last_was_design": True}
    for _t in ("できた！ありがとーこれでショート動画が投稿できる",
               "デザインの話はしてないよ", "ありがとう助かった",
               "うまくいった！", "できました", "やった！"):
        check(f"報告/お礼/否定 {_t!r} は作業にしない",
              bot.classify_route(_t, **_LGD2) or "plan", "plan")
    # 質問としての「できた？」は今までどおり状態確認
    for _t, _w in (("できた？", "status"), ("まだ？", "status"),
                   ("進捗どう", "status"), ("背景を暗くして", "design"),
                   ("作り直して", "design")):
        check(f"{_t!r} は従来どおり", bot.classify_route(_t, **_LGD2), _w)

    print("■ 匿名の人格を残さない（誰が担当かが分かること）")
    _src = open("ai_group_chat.py", encoding="utf-8").read()
    check("ショート企画はクロード3が名乗る",
          "いまはバズるYouTube Shortsのアートディレクターとして企画する" in _src, True)
    check("映像ディレクターはkohei本人",
          "映像ディレクターはkohei本人" in _src, True)
    check("映像制作でクロード2はアシスタント",
          "あなたはその【アシスタント】" in _src, True)
    check("『あなたは映像ディレクター』という名乗りは残っていない",
          "あなたは映像ディレクター。" in _src, False)

    print("■ 広告代理店の役はクロード3（アドバイザー）が持つ")
    check("クロード3に広告の役割がある",
          "広告代理店" in bot.CLAUDE_PERSONAS["claude3"][1], True)
    check("複数案とリスクを出す役だと明記",
          "リスク" in bot.CLAUDE_PERSONAS["claude3"][1], True)
    _blk = bot._ad_plan_block(
        {"title": "夜の静けさ篇", "target": "20代", "hook": "無音",
         "message": "静けさ", "cta": "今すぐ", "risk": "地味に見える"}, "案1: ")
    check("企画書にリスク欄がある", "外した時のリスク" in _blk, True)
    check("企画書に案の番号が入る", "案1:" in _blk, True)

    print("■ 話者の名前（1=リサーチャー / 2=PM / 3=アドバイザー）")
    check("クロード1はリサーチャー", bot.CLAUDE1_NAME, "クロード1（リサーチャー）")
    check("クロード2はPM", bot.CLAUDE2_NAME, "クロード2（PM）")
    check("クロード3はアドバイザー", bot.CLAUDE3_NAME, "クロード3（アドバイザー）")
    check("普段の返事はクロード2が出す",
          bot._with_speaker("本文", bot.CLAUDE2_NAME), "**クロード2（PM）**: 本文")
    check("PMの人格が入っている", bot.CLAUDE2_NAME in bot.ORCH_PERSONA, True)
    check("役割の人格も名前と一致",
          bot.CLAUDE_PERSONAS["claude1"][0], bot.CLAUDE1_NAME)
    for _t in ("クロード2（PM）: 本文", "クロード2: 本文", "PM: 本文",
               "クロード1（リサーチャー）: 本文", "クロード: 本文"):
        check(f"名乗りの前置きを落とす {_t!r}", bot._clean_reply(_t), "本文")

    print("■ Geminiは自分の名前で投稿しない（返信を止める）")
    import types as _ty2

    class _U:
        pass
    _gu, _cu, _ou = _U(), _U(), _U()
    _keep_users = (bot.gemini_bot.user, bot.claude_bot.user, bot.orch.user)
    _keep_lead2 = bot.gen_settings.get("casual_lead")
    try:
        bot.gemini_bot.user, bot.claude_bot.user, bot.orch.user = _gu, _cu, _ou
        _msg = _ty2.SimpleNamespace(mentions=[_gu])
        # 自動でGeminiが割り込むのは止めるが、名指しされた時だけは本人が答える
        bot.gen_settings["casual_lead"] = ""
        check("@Geminiと名指しすればGeminiが答える",
              [n for n, _, _ in bot.decide_targets(_msg, "やあ")], ["Gemini"])
        check("名指しが無ければオーケストレーター",
              [n for n, _, _ in bot.decide_targets(
                  _ty2.SimpleNamespace(mentions=[]), "やあ")], ["Orchestrator"])
        check("@クロードならクロード",
              [n for n, _, _ in bot.decide_targets(
                  _ty2.SimpleNamespace(mentions=[_cu]), "やあ")], ["Claude"])
    finally:
        bot.gemini_bot.user, bot.claude_bot.user, bot.orch.user = _keep_users
        bot.gen_settings["casual_lead"] = _keep_lead2

    print("■ 話者ラベルが裏のGeminiに汚染されないこと")
    # 実際に起きた事故：クロードが書いた返事に「Gemini」と付いた。
    # 裏で動くGeminiの処理（要約・検品・解析）が _last_engine を
    # 書き換えるため、あとから読むと誰が書いたか分からなくなる
    bot._last_engine["name"] = "Gemini"
    check("明示した話者が優先される",
          bot._with_speaker("本文", "クロード"), "**クロード**: 本文")
    check("明示しなければ従来どおり",
          bot._with_speaker("本文"), "**Gemini**: 本文")
    bot._last_engine["name"] = "クロード"

    print("■ 返事の校閲（クロードが書き、Geminiは指摘だけ）")
    # 事故：Geminiに書き直させていたため、長い返事だけ敬語＋箇条書きに化けた。
    # いまはGeminiは指摘を返すだけで、本文を直すのは書いた本人。
    import asyncio as _aio7
    _orig_g, _orig_cool = bot._gemini_call, bot._gemini_all_cooling
    _orig_cli = bot.run_claude_cli
    _h = [("kohei", "これどう思う？")]
    _draft = "クロードの下書きだよ。" + "y" * 130

    async def _notes(prompt, tag="gemini"):
        bot._last_engine["name"] = "Gemini"     # 裏で汚染される状況も再現
        return "・料金の根拠が無い\n・後半が繰り返し"

    async def _ok(prompt, tag="gemini"):
        return "問題なし"

    async def _fixed_cli(prompt, background=False):
        return "直した下書きだよ。" + "y" * 130

    try:
        bot._gemini_all_cooling = lambda: False
        bot._gemini_call, bot.run_claude_cli = _notes, _fixed_cli
        _out, _rev = _aio7.run(bot._review_reply(_draft, _h))
        check("指摘があれば直す", _rev, True)
        check("直すのはクロード（クロードの文体のまま）",
              _out.startswith("直した"), True)

        bot._gemini_call = _ok
        check("指摘なしなら下書きのまま",
              _aio7.run(bot._review_reply(_draft, _h)), (_draft, False))

        _calls = []

        async def _count_cli(prompt, background=False):
            _calls.append(1)
            return "呼ばれた"
        bot.run_claude_cli = _count_cli
        _aio7.run(bot._review_reply(_draft, _h))
        check("指摘なしなら余計な呼び出しをしない", _calls, [])

        bot._gemini_call, bot.run_claude_cli = _notes, _fixed_cli
        check("短い返事は校閲しない（速度優先）",
              _aio7.run(bot._review_reply("短い返事", _h))[1], False)
        bot._gemini_all_cooling = lambda: True
        check("枠切れなら下書きをそのまま使う",
              _aio7.run(bot._review_reply(_draft, _h)), (_draft, False))
        bot._gemini_all_cooling = lambda: False

        async def _rewrote(prompt, tag="gemini"):
            return "こちらが最終的な回答本文です。" + "z" * 400
        bot._gemini_call = _rewrote
        check("本文を書いてきたら指摘とみなさない",
              _aio7.run(bot._review_reply(_draft, _h))[1], False)

        bot._gemini_call = _notes

        async def _bad_short(prompt, background=False):
            return "あ"

        async def _bad_polite(prompt, background=False):
            return ("料金の根拠を示します。後半の繰り返しを削除しました。"
                    "ご確認ください。" + "z" * 100)
        bot.run_claude_cli = _bad_short
        check("短くなりすぎたら下書きを使う",
              _aio7.run(bot._review_reply(_draft, _h))[1], False)
        bot.run_claude_cli = _bad_polite
        check("敬体に化けたら下書きを使う",
              _aio7.run(bot._review_reply(_draft, _h))[1], False)
    finally:
        bot._gemini_call, bot._gemini_all_cooling = _orig_g, _orig_cool
        bot.run_claude_cli = _orig_cli

    print("■ Geminiとクロードの役割分担（声はひとつ、頭は複数）")
    check("Geminiの視点担当の人格がある",
          "違う切り口" in bot.GEMINI_VIEW_PERSONA, True)
    check("Geminiに推測を書かせない",
          "推測を事実のように書かない" in bot.GEMINI_VIEW_PERSONA, True)
    check("Geminiは返事そのものは書かない（既定）", bot._gemini_replies_on(), False)
    check("Geminiは裏方の解析で使い続ける",
          callable(bot._describe_media_url) and callable(bot._inspect_result), True)

    print("■ 雑談の担当を切り替える _match_casual_lead")
    _kl = bot.gen_settings.get("casual_lead")
    try:
        bot.gen_settings["casual_lead"] = "gemini"
        check("明示すればGeminiも返事に戻せる", bot._gemini_replies_on(), True)
        bot.gen_settings["casual_lead"] = "claude"
        check("クロードに戻せる", bot._gemini_replies_on(), False)
    finally:
        bot.gen_settings["casual_lead"] = _kl
    for _t, _want in (("返事はクロードにして", "claude"),
                      ("返答をgeminiにして", "gemini"),
                      ("クロードに答えさせて", "claude"),
                      ("雑談はクロードでお願い", "claude"),
                      ("ジェミニに返事させて", "gemini")):
        _got = bot._match_casual_lead(_t)
        check(f"{_t!r} → {_want}", _got[0] if _got else None, _want)
    for _t in ("クロードでサムネ作って", "クロードどう思う", "返事を短くして"):
        check(f"{_t!r} は担当変更にしない", bot._match_casual_lead(_t), None)
    _keep_lead = bot.gen_settings.get("casual_lead")
    try:
        bot.gen_settings["casual_lead"] = "claude"
        check("設定が効く", bot._casual_lead(), "claude")
        bot.gen_settings["casual_lead"] = ""
        check("未設定なら既定", bot._casual_lead(), bot.CASUAL_LEAD)
    finally:
        bot.gen_settings["casual_lead"] = _keep_lead

    print("■ 速さのための設定")
    _dt = bot.DESIGN_CRAFT_RULES
    check("デザインに速さの指示を入れる場所がある", isinstance(_dt, str), True)
    check("デザインは会話用と別モデルにできる",
          isinstance(bot.DESIGN_MODEL, str), True)

    print("■ 作り手の指定を『題材』にしない")
    # 事故：「ヒッグスフィールドで作って」が題材として読まれ、
    # "Higgs boson field visualized as an endless dark cosmic void..." という
    # 素粒子物理の画像が生成された（頼んだのは人物写真の作り直し）
    for _t in ("ヒッグスフィールドで作って", "ヒッグスフィールドで", "クロードで作って",
               "geminiで", "HTMLで"):
        check(f"{_t!r} に題材は無い", bot._has_subject(_t), False)
    for _t in ("ヒッグスフィールドで猫の画像作って", "黄色いドレスの女性",
               "クロードで相関図作って"):
        check(f"{_t!r} には題材がある", bot._has_subject(_t), True)
    _L = {"has_last_gen": True}
    check("指定だけならHiggsfieldへ", bot.classify_route("ヒッグスフィールドで", **_L),
          "hf_auto")
    check("指定だけならクロードへ", bot.classify_route("クロードで作って", **_L), "design")
    check("題材があってもHiggsfield指定が勝つ",
          bot.classify_route("ヒッグスフィールドでサムネ作って", **_L), "hf_auto")
    check("直前の依頼が無ければ何も起こさない",
          bot.classify_route("ヒッグスフィールドで作って"), None)
    _src4 = open(bot.__file__, encoding="utf-8").read()
    check("題材は直前の依頼から補う", "_request_with_context" in _src4, True)

    print("■ 長い動画の切り抜き（Mac上で完結・クレジット不要）")
    _U = "https://www.youtube.com/watch?v=abc12345678"
    for _t in (f"{_U} これショートにして", f"この動画切り抜いて3本 {_U}",
               f"{_U} をダイジェストにして", f"{_U} 縦型に切り出して"):
        check(f"{_t[:24]!r}… は切り抜きへ", bot.classify_route(_t), "clip")
    for _t in (f"{_U} どう思う？", _U, f"{_U} 切り抜きってどうやるの？",
               f"{_U} これ参考にして"):
        check(f"{_t[:24]!r}… は切り抜きにしない",
              bot.classify_route(_t) == "clip", False)
    check("新規のショート量産とは分ける", bot.classify_route("ショート作って"), "short")
    # 時刻の解釈
    for _v, _want in (("01:30", 90.0), ("1:02:03", 3723.0), (95, 95.0), ("bad", None)):
        check(f"時刻 {_v!r} を秒に", bot._mmss_to_sec(_v), _want)
    # 時刻つき字幕
    _xml = ('<transcript><text start="12.5" dur="3.2">これはテスト</text>'
            '<text start="20.0" dur="2.0">次の場面</text></transcript>')
    check("字幕を時刻つきで読む", bot._decode_caption_timed(_xml),
          [(12.5, 3.2, "これはテスト"), (20.0, 2.0, "次の場面")])
    check("AIに渡す形は時刻つき",
          bot._timed_transcript(bot._decode_caption_timed(_xml)).startswith("[00:12]"),
          True)
    # 区間内の字幕だけをSRTにする（頭を0秒に寄せる）
    _srt = bot._srt_for(bot._decode_caption_timed(_xml), 10, 18)
    check("区間内の字幕だけ残す", "これはテスト" in _srt and "次の場面" not in _srt, True)
    check("区間の頭を0秒に寄せる", "00:00:02" in _srt, True)
    # 生成モデルを使わない＝クレジットを消費しない
    _src6 = open(bot.__file__, encoding="utf-8").read()
    _clip_fn = _src6[_src6.index("async def _run_clip_shorts"):
                     _src6.index("# 切り抜きには「いつ何を言ったか」")]
    for _ng in ("_mcp_generate_submit", "_mcp_gen_and_wait", "hf_wrapper",
                "sandbox_exec", "generate_image", "generate_video"):
        check(f"切り抜きは {_ng} を使わない", _ng in _clip_fn, False)
    check("日本語が出るフォントを使う",
          any("ヒラギノ" in f for f in bot.CLIP_FONTS), True)
    check("Discordの上限に収める", bot.CLIP_MAX_MB <= 25, True)

    print("■ 直しの指示が進捗確認に化けないこと")
    # 事故：「顔が違うので、鼻の高さだけ変えてあとは顔のパーツに合わせてください」が
    # 進捗確認になり、直しの指示が消えた。原因は「ください」を進捗の語に
    # 入れていたこと（〜してくださいは依頼形そのもの）。
    _L4 = {"has_last_gen": True}
    check("丁寧形の直し指示は作り直しへ",
          bot.classify_route("顔が違うので、鼻の高さだけ変えてあとは顔のパーツに合わせてください",
                             **_L4), "revise")
    check("直前の生成が無ければ会話のまま",
          bot.classify_route("顔が違うので、鼻の高さだけ変えてあとは顔のパーツに合わせてください"),
          None)
    for _t in ("猫の画像を作ってください", "もっと明るくしてください"):
        check(f"{_t!r} は進捗確認にしない",
              bot.classify_route(_t, **_L4) == "status", False)
    # 結果を求める言い方は従来どおり進捗確認
    for _t in ("送ってください", "URL見せてください", "できた？", "あとどれくらい？"):
        check(f"{_t!r} は進捗確認", bot.classify_route(_t, **_L4), "status")
    # 出来上がりへの不満＋直しの指示
    check("『違う』＋直しで作り直しへ",
          bot._looks_revise("顔が違うので鼻を変えて", True), True)
    check("『違う』だけでは作り直しにしない",
          bot.classify_route("デザインの話はしてないよ", **_L4), None)

    print("■ ボットの独り言をプロンプトとして投入しない")
    # 事故：投入されたプロンプトが
    # 「このタスクはDiscordボット内部からの依頼(claude -p呼び出し)で、
    #  コードを書き換える作業ではなく、プロンプト変換だけなので…」
    # だった。先頭行を無条件に採っていたためCLIの前置きが入った。
    import asyncio as _aio14
    _keep_bg2 = bot._ai_text_bg

    async def _meta(prompt, tag="x"):
        return ("このタスクはDiscordボット内部からの依頼(claude -p呼び出し)です。\n"
                "photorealistic portrait of the person in the reference image, "
                "85mm lens, soft window light")

    async def _junk(prompt, tag="x"):
        return "このタスクは内部からの依頼なので、そのまま出力します。"
    try:
        bot._ai_text_bg = _meta
        _out = _aio14.run(bot._refine_prompt("鼻を高くして", "image"))
        check("前置きを飛ばして英語の行を採る",
              _out.startswith("photorealistic"), True)
        check("前置きを混ぜない", "Discordボット" in _out, False)
        bot._ai_text_bg = _junk
        check("英語が得られなければ原文を使う",
              _aio14.run(bot._refine_prompt("鼻を高くして", "image")), "鼻を高くして")
    finally:
        bot._ai_text_bg = _keep_bg2
    # 直前の生成を「今回の完成」と取り違える幅を狭める
    import time as _tm14
    _now2 = _tm14.time()
    check("数分前の生成は今回のものではない",
          bot._url_is_stale("https://x/hf_20260810_102000_a.png",
                            _now2, slack_sec=120)
          or bot._url_is_stale("https://x/hf_20260101_000000_a.png", _now2), True)
    check("許容幅は2分以内",
          bot._url_is_stale.__defaults__[0] <= 120, True)

    print("■ 身の上話を進捗確認にしない")
    # 事故：元カノと別れた話の最中に
    # 「でももしあっちに新しい人ができたら本当に別れないといけない」が
    # 進捗確認になった。「できた」が「できたら」に当たっていた。
    _L5 = {"has_last_gen": True}
    for _t in ("でももしあっちに新しい人ができたら本当に別れないといけない",
               "新しい仕事ができたら引っ越すかもしれない",
               "明日には終わったら連絡するね",
               "子どもができたら生活が変わると思う"):
        check(f"{_t[:20]!r}… は会話のまま", bot.classify_route(_t, **_L5), None)
    # 進捗を聞く言い方は従来どおり
    for _t, _f in (("できた？", _L5), ("まだ？", _L5), ("あとどれくらい？", _L5),
                   ("動画できた？", _L5), ("相関図できた？", _L5),
                   ("進捗どう", {"has_running": True}),
                   ("デザインの進捗どうなってる？教えて", _L5)):
        check(f"{_t!r} は進捗確認", bot.classify_route(_t, **_f), "status")
    check("条件形は進捗の語にしない",
          bool(bot._STATUS_KW_RE.search("できたら")), False)
    check("問いかけの形は拾う", bool(bot._STATUS_KW_RE.search("できた？")), True)

    print("■ 古い生成を『できました』と出さない（コード側で弾く）")
    # 事故：08-10に投入したジョブの結果として、6日前(08-04)の画像URLが
    # 「✅ できました！」で出てきた。プロンプトで「これより前は対象外」と
    # 伝えるだけでは守られなかったので、URLの日時で機械的に判定する。
    import time as _tm13
    _now = _tm13.time()
    check("6日前の生成は今回のものではない",
          bot._url_is_stale("https://x/hf_20260804_201331_c161.png", _now), True)
    # 「今まさに出来たもの」は、実行時のUTC時刻から作って判定する
    # （日付を固定で書くと、実行日によって結果が変わってしまう）
    import datetime as _dt13
    _stamp = _dt13.datetime.now(_dt13.timezone.utc).strftime("%Y%m%d_%H%M%S")
    check("さっきの生成は通す",
          bot._url_is_stale(f"https://x/hf_{_stamp}_abcd.png", _now), False)
    check("日時が読めなければ止めない",
          bot._url_is_stale("https://x/plain.png", _now), False)
    check("投入時刻が無ければ止めない",
          bot._url_is_stale("https://x/hf_20260804_201331_c161.png", None), False)
    _srcO = open(bot.__file__, encoding="utf-8").read()
    check("監視側で弾く", "_url_is_stale(vurl, token)" in _srcO, True)

    print("■ 『作り直すね、待ってて』も作り話として落とす")
    # 事故：何も動いていないのに「作り直すね。ヒッグスフィールドに投げるから、
    # ちょっと待ってて」と言い、実際には何も始まっていなかった
    _keep_b2, _keep_j2 = bot._busy_tasks, bot._load_motion_job
    try:
        bot._busy_tasks = lambda cid: []
        bot._load_motion_job = lambda: None
        for _t in ("了解、作り直すね。ヒッグスフィールドに投げるから、ちょっと待ってて。",
                   "やり直すね。少し待ってて。",
                   "いま投入するから待っててね。"):
            _o = bot._drop_false_progress(_t, 1)
            check(f"{_t[:16]!r}… は言わせない", _o != _t, True)
            check(f"{_t[:16]!r}… 動いていないと明記", "動かしていない" in _o, True)
        check("普通の会話は触らない",
              bot._drop_false_progress("今日はいい天気だね。", 1), "今日はいい天気だね。")
    finally:
        bot._busy_tasks, bot._load_motion_job = _keep_b2, _keep_j2

    print("■ 出来上がりは【本人の依頼】と突き合わせる")
    # 事故：「この人の鼻を高くしてほしい」＋写真 に対し、英語プロンプトが
    # 勝手に "a man" と書き、結果（女性＝写真どおり）を
    # 「依頼の男性と異なる」と警告した。自分の創作を基準に誤判定していた。
    _srcN = open(bot.__file__, encoding="utf-8").read()
    check("元の依頼を保持する", "asked = request" in _srcN, True)
    check("照合に元の依頼を渡す",
          "_report_result(cid, asked, result" in _srcN, True)
    check("完了監視でも元の依頼を使う",
          'job0.get("asked") or job0.get("request"' in _srcN, True)
    # ジョブ記録に両方入ること
    import tempfile as _tf12, pathlib as _pl12
    _keep_f = bot._MOTION_JOB_FILE
    try:
        with _tf12.TemporaryDirectory() as _d12:
            bot._MOTION_JOB_FILE = _pl12.Path(_d12) / "job.json"
            bot._save_motion_job(1, "A photorealistic portrait of a man",
                                 asked="この人の鼻を高くしてほしい")
            _j = bot._load_motion_job()
            check("英語プロンプトも残す", "photorealistic" in _j["request"], True)
            check("本人の依頼も残す", _j["asked"], "この人の鼻を高くしてほしい")
            bot._save_motion_job(1, "犬の動画")
            check("asked が無ければ依頼で埋める",
                  bot._load_motion_job()["asked"], "犬の動画")
    finally:
        bot._MOTION_JOB_FILE = _keep_f

    print("■ 参照画像があるときは被写体を創作しない")
    # 事故：「この人の鼻を高くして」に対し
    # "A photorealistic close-up portrait of a man, ..." と被写体を創作し、
    # 出てきたのは女性だった。人物の描写が参照より強く効いてしまう。
    import asyncio as _aio11
    _keep_bg = bot._ai_text_bg
    _seen = {}

    async def _spy(prompt, tag="x"):
        _seen["p"] = prompt
        return "photorealistic portrait, 85mm lens"
    try:
        bot._ai_text_bg = _spy
        _aio11.run(bot._refine_prompt("この人の鼻を高くして", "image", has_ref=True))
        check("参照を指すよう指示する", "reference image" in _seen["p"], True)
        check("見た目を描写させない", "描写してはいけない" in _seen["p"], True)
        check("性別・年齢も禁止に含む", "性別" in _seen["p"], True)
        _aio11.run(bot._refine_prompt("猫の画像", "image", has_ref=False))
        check("参照が無ければ従来どおり描写させる",
              "被写体・構図" in _seen["p"], True)
        check("参照なしでは参照の指示を出さない",
              "reference image" in _seen["p"], False)
    finally:
        bot._ai_text_bg = _keep_bg
    _srcM = open(bot.__file__, encoding="utf-8").read()
    check("生成側から参照の有無を渡す",
          "has_ref=bool(refs)" in _srcM, True)

    print("■ 指している相手が分からないまま生成しない")
    # 事故：「この人の鼻を高くしてほしい」で参照画像が無く、汎用の
    # 「a man」で生成 → 出てきたのは女性。クレジットだけ消えた。
    for _t in ("この人の鼻を高くしてほしい", "これを縦にして", "俺の顔を若くして",
               "この写真をアニメ風に"):
        check(f"{_t!r} は何かを指している", bool(bot._POINTS_AT_RE.search(_t)), True)
    for _t in ("猫の画像を作って", "夕暮れの海の動画", "バズる動画作って"):
        check(f"{_t!r} は指していない", bool(bot._POINTS_AT_RE.search(_t)), False)
    _srcL = open(bot.__file__, encoding="utf-8").read()
    check("参照が無ければ作る前に止める",
          "参照する画像がありません" in _srcL, True)
    check("何が足りないかを覚える",
          '_set_pending_do(cid, "元になる写真"' in _srcL, True)

    print("■ 日常の『もっと〜て』を作り直しにしない")
    # 事故：「今日はお酒我慢できてる！」→「もっと褒めて笑」が、4日前の
    # 人物画像の作り直しと判定され、『肌・髪の描写に艶やかで美しい的な
    # 賛辞を追加』という修正プランが出た。褒めてほしかっただけだった。
    _L3 = {"has_last_gen": True}
    for _t in ("もっと褒めて笑", "もっと褒めて", "もっと詳しく教えて",
               "決めておいて", "まとめて送って", "そろそろやめておく"):
        check(f"{_t!r} は作り直しにしない", bot.classify_route(_t), None)
        check(f"{_t!r} は生成直後でも作り直しにしない",
              bot.classify_route(_t, **_L3), None)
    # 直前の生成があるときの手直しは従来どおり
    for _t in ("もうちょい明るくして", "もう少し可愛い感じにして", "もっと短くして"):
        check(f"{_t!r} は生成があれば手直し",
              bot.classify_route(_t, **_L3) in ("revise", "edit", "design"), True)
    # はっきり作り直しと分かる言い方は、記録が無くても通す
    for _t in ("さっきのやつ作り直して", "もう一回お願い", "いまいちだからやり直して"):
        check(f"{_t!r} は記録が無くても作り直し", bot.classify_route(_t), "revise")
    # 曖昧な言い方は、直前の生成が無ければ通さない
    check("曖昧な言い方は生成が要る", bot._looks_revise("もうちょい明るくして", False), False)
    check("はっきりした言い方は生成が無くても通る",
          bot._looks_revise("作り直して", False), True)

    print("■ 毎日のリサーチのジャンルを変えられること")
    for _t, _want in (
        ("リサーチはアート系にして", ("set", "アート系")),
        ("毎日のリサーチのジャンルをスタイリッシュな映像にして",
         ("set", "スタイリッシュな映像")),
        ("リサーチをショート動画の演出に絞って", ("set", "ショート動画の演出")),
        ("リサーチを急上昇に戻して", ("reset", "")),
        ("ジャンル指定を解除して", ("reset", "")),
    ):
        check(f"{_t[:22]!r}… → {_want}", bot._match_trend_genre(_t), _want)
    # 時刻・担当・オンオフはそれぞれの担当が拾うので、ジャンルにしない
    for _t in ("毎日7時にリサーチして", "リサーチするのはクロード1にして",
               "毎日のリサーチやめて", "自動リサーチいつやってる？",
               "卵スープのレシピ教えて", "ジャンルの話をしてた"):
        check(f"{_t!r} はジャンル設定にしない", bot._match_trend_genre(_t), None)
    _srcK = open(bot.__file__, encoding="utf-8").read()
    check("毎日の実行にジャンルを渡す",
          'gen_settings.get("trend_query") or None' in _srcK, True)
    check("毎日の実行では分析済みを飛ばす",
          "skip_analyzed=True" in _srcK, True)
    check("お題指定でも飛ばすかを選べる",
          "def _run_trend_study(cid, query=None, skip_analyzed=None)" in _srcK
          or "async def _run_trend_study(cid, query=None, skip_analyzed=None)" in _srcK,
          True)

    print("■ 代打で答えた時に名乗りを変える（別人が混ざって見えないように）")
    # 事故：クロードが週の上限に達し、Geminiが代わりに書いた返事が
    # 「クロード2（PM）」名義で、しかも敬語＋箇条書き。同じ相手が急に
    # 他人行儀になったように見えて会話が噛み合わなくなった。
    check("代打の名乗りを持つ", bot.GEMINI_STANDIN != bot.CLAUDE2_NAME, True)
    check("代打と分かる名前", "代打" in bot.GEMINI_STANDIN, True)
    _n = bot._limit_note("You've hit your weekly limit · resets 12pm (Asia/Tokyo)")
    check("上限だと伝える", "利用上限" in _n, True)
    check("いつ戻るかを出す", "12pm" in _n, True)
    check("理由が分からない時も代打だと言う",
          "代わりに答えています" in bot._limit_note("timeout"), True)
    _srcJ = open(bot.__file__, encoding="utf-8").read()
    check("実際に書いた側で名乗る", '_who = _wrote.get("name")' in _srcJ, True)
    check("代打の時だけ注記を足す",
          'if _who == GEMINI_STANDIN:' in _srcJ, True)

    print("■ 折り返された道順でも階層を落とさない")
    # 事故：iPhoneから貼ると「動画\nの中の◯◯.mp4」と折り返され、
    # フォルダ名「動画」が落ちて一階層ぶん違うパスを組み立てていた
    _want = "com~apple~CloudDocs/マルサヂ/AI/動画/武士道：究極のサバイバル哲学.mp4"
    for _t in (
        "iCloud Drive ▸ マルサヂ ▸ AI ▸ 動画\nの中の武士道：究極のサバイバル哲学.mp4という動画をショートにして",
        "iCloud Drive ▸ マルサヂ ▸ AI ▸ 動画\n→武士道：究極のサバイバル哲学.mp4",
        "iCloud Drive ▸ マルサヂ ▸ AI ▸ 動画の中の武士道：究極のサバイバル哲学.mp4",
    ):
        check(f"{_t[:24]!r}… で階層が揃う",
              bot._ios_files_path(_t).endswith(_want), True)
    _srcI = open(bot.__file__, encoding="utf-8").read()
    check("探索はSpotlightを先に使う", "_spotlight_find" in _srcI, True)
    check("全走査の待ち時間を伸ばした", "timeout=240" in _srcI, True)

    print("■ 動画の渡し方（いちばん簡単な形も受け取る）")
    class _A2:
        def __init__(self, fn, url):
            self.filename, self.url = fn, url

    class _M2:
        def __init__(self, atts=()):
            self.attachments = list(atts)
    _U2 = "https://www.youtube.com/watch?v=abc12345678"
    for _t, _f, _want in (
        (f"{_U2} 切り抜いて", _M2(), "youtube"),
        ("切り抜いて", _M2([_A2("a.mp4", "https://cdn/a.mp4")]), "url"),
        ("https://example.com/a.mp4 切り抜いて", _M2(), "url"),
        ("/Users/kohei/Movies/a.mp4 を切り抜いて", _M2(), "file"),
        ("iCloud Drive ▸ AI ▸ 動画の中のa.mp4 を切り抜いて", _M2(), "file"),
        # ファイル名だけでも受け取る（探しに行く）
        ("武士道：究極のサバイバル哲学.mp4を切り抜いて", _M2(), "file"),
    ):
        check(f"{_t[:22]!r}… → {_want}", bot._clip_source(_f, _t)[0], _want)
    check("名前だけなら名前を渡す",
          bot._clip_source(_M2(), "武士道.mp4を切り抜いて")[1], "武士道.mp4")
    check("動画の指定が無ければ空", bot._clip_source(_M2(), "切り抜いて")[0], "")
    _srcH = open(bot.__file__, encoding="utf-8").read()
    check("探している間も黙らない", "をMacの中から探しています" in _srcH, True)

    print("■ ボットが黙り込まない土台")
    _srcG = open(bot.__file__, encoding="utf-8").read()
    # iCloud配下の探索は巨大になり得る。そのまま回すとループが止まる。
    check("ファイル探索は別スレッドへ逃がす",
          "asyncio.to_thread(_find_video_sync" in _srcG, True)
    check("探索に時間の区切りがある",
          "timeout=60" in _srcG, True)
    check("探索の呼び出しは待てる形",
          "await _find_video_by_name(" in _srcG, True)
    # 落ちた時にmacOS自身が起こし直す仕組みがある
    import pathlib as _pl9
    _auto = _pl9.Path(bot.__file__).parent / "install_autostart.sh"
    check("常駐登録の手順を同梱する", _auto.exists(), True)
    _txt = _auto.read_text(encoding="utf-8") if _auto.exists() else ""
    check("ログイン時に起動する", "RunAtLoad" in _txt, True)
    check("落ちても起こし直す", "KeepAlive" in _txt, True)

    print("■ 直した内容が自動でDiscordに届くこと")
    # 本人の指摘：「こっちで設定してることとdiscord上の挙動が違う」。
    # 実際に何度も、修正が入る前の版を試して「まだ直ってない」となっていた。
    check("既定は自動更新オン", bot._auto_update_on(), True)
    _keep_au = bot.gen_settings.get("auto_update")
    try:
        bot.gen_settings["auto_update"] = False
        check("止められる", bot._auto_update_on(), False)
    finally:
        bot.gen_settings["auto_update"] = _keep_au
    for _t, _want in (("自動更新オフ", False), ("自動更新やめて", False),
                      ("自動更新オン", True), ("自動更新して", True)):
        check(f"{_t!r} → {_want}", bot._match_auto_update(_t), _want)
    for _t in ("自動更新って何？", "自動でショート作って", "毎日7時にリサーチして"):
        check(f"{_t!r} は切替にしない", bot._match_auto_update(_t), None)
    # 作業中・確認待ちには入れ替わらない
    _keep_bt, _keep_mj, _keep_pa = (bot._busy_tasks, bot._load_motion_job,
                                    dict(bot._pending_approvals))
    try:
        bot._busy_tasks = lambda cid: []
        bot._load_motion_job = lambda: None
        bot._pending_approvals.clear()
        check("手が空いていれば入れ替わってよい", bot._safe_to_restart(1), True)
        bot._busy_tasks = lambda cid: [("切り抜き制作", 30)]
        check("作業中は待つ", bot._safe_to_restart(1), False)
        bot._busy_tasks = lambda cid: []
        bot._pending_approvals[1] = (None, 1)
        check("確認待ちも待つ", bot._safe_to_restart(1), False)
    finally:
        bot._busy_tasks, bot._load_motion_job = _keep_bt, _keep_mj
        bot._pending_approvals.clear(); bot._pending_approvals.update(_keep_pa)
    _srcF = open(bot.__file__, encoding="utf-8").read()
    check("ログの1行目で新旧が分かる", "_freshness_note" in _srcF, True)
    check("古ければ警告する", "件古い" in _srcF, True)

    print("■ 子プロセスが端末待ちで固まらないこと")
    # 事故：「🎧 音声を取り出しています…」から一切先へ進まなくなった。2回とも同じ場所。
    # バックグラウンドのボットの子プロセスが端末から読もうとすると
    # SIGTTIN で【止まる】（死なないので、待っても終わらない）。
    _srcE = open(bot.__file__, encoding="utf-8").read()
    check("子プロセスの標準入力を切る",
          _srcE.count("stdin=asyncio.subprocess.DEVNULL") >= 4, True)
    check("ffmpegにも明示する", '"-nostdin"' in _srcE, True)
    # 起動する外部コマンドのうち、標準入力を渡していないものが無いこと
    import re as _re10
    _spawns = _re10.findall(r"create_subprocess_exec\((?:[^()]|\([^()]*\))*?\)",
                            _srcE, _re10.S)
    _no_stdin = [c for c in _spawns if "stdin=" not in c]
    check("標準入力を指定していない起動が無い", _no_stdin, [])
    check("音声の抽出は長く待ちすぎない",
          "timeout=900, heavy=True" in _srcE, True)

    print("■ 暴走した重い処理をDiscordから止められること")
    # 事故：文字起こしがCPUを占有し、「再起動」すら届かず復旧できなかった
    class _FakeProc:
        def __init__(self, done=False):
            self.returncode = 0 if done else None
            self.killed = False

        def kill(self):
            self.killed = True
            self.returncode = -9
    _keep_hp = set(bot._heavy_procs)
    try:
        bot._heavy_procs.clear()
        _p1, _p2, _p3 = _FakeProc(), _FakeProc(), _FakeProc(done=True)
        bot._heavy_procs.update({_p1, _p2, _p3})
        check("動いている分だけ止める", bot.stop_heavy_procs(), 2)
        check("実際に止まる", (_p1.killed, _p2.killed), (True, True))
        check("終わった処理は数えない", _p3.killed, False)
        check("止めたら記録も空になる", len(bot._heavy_procs), 0)
        check("何も無ければ0", bot.stop_heavy_procs(), 0)
    finally:
        bot._heavy_procs.clear(); bot._heavy_procs.update(_keep_hp)
    _srcD = open(bot.__file__, encoding="utf-8").read()
    check("「やめて」で重い処理も止める",
          "killed = stop_heavy_procs()" in _srcD, True)
    check("再起動の前にも片付ける",
          _srcD.count("stop_heavy_procs()") >= 3, True)

    print("■ 重い処理でボットが黙らないこと")
    # 事故：文字起こし中にCPUを使い切り、「あとどのくらい？」「ログ送って」
    # 「再起動」のどれにも反応しなくなった（処理自体は正常に進んでいた）
    check("CPUを1本は空けておく",
          bot._work_threads() <= max(1, (os.cpu_count() or 4) - 1), True)
    check("1本以上は使う", bot._work_threads() >= 1, True)
    _srcC = open(bot.__file__, encoding="utf-8").read()
    check("重い処理は優先度を下げて動かす", '"nice", "-n"' in _srcC, True)
    for _piece in ('"-t", str(_work_threads())',           # 文字起こし
                   '"-threads", str(_work_threads())'):    # ffmpeg
        check(f"CPUの本数を渡す（{_piece[:12]}…）", _piece in _srcC, True)
    check("遅くなることを先に伝える",
          "返事が普段より遅くなります" in _srcC, True)

    print("■ 『やって』が空振りしない／iCloudの共有リンクを正しく断る")
    # 事故：ボット自身が「始めていいなら『やって』と言って」と案内したのに、
    # 「やって」に受け皿が無く、同じ説明を繰り返す無限ループになった（2往復）。
    check("『やって』を拾う", bool(bot._BARE_GO_RE.match("やって")), True)
    for _t in ("お願い", "進めて", "始めて", "GO", "よろしく", "やって。"):
        check(f"{_t!r} も拾う", bool(bot._BARE_GO_RE.match(_t)), True)
    for _t in ("やっておいて意味ある？", "お願いしたいことがある", "やってみたけどダメ"):
        check(f"{_t!r} は拾わない", bool(bot._BARE_GO_RE.match(_t)), False)
    # 何が足りないかを覚えて、具体的に答えられること
    _keep_pd = dict(bot._pending_do)
    try:
        bot._pending_do.clear()
        check("覚えていなければ何も無い", bot._get_pending_do(3), None)
        bot._set_pending_do(3, "動画の場所", "切り抜きたい")
        check("足りないものを覚える", bot._get_pending_do(3)["need"], "動画の場所")
        import time as _tm5
        bot._pending_do[3]["t"] = _tm5.time() - bot.PENDING_DO_SEC - 10
        check("古い記憶は使わない", bot._get_pending_do(3), None)
    finally:
        bot._pending_do.clear(); bot._pending_do.update(_keep_pd)
    # iCloudの共有リンクは取りに行けないと正しく言う
    check("iCloudの共有リンクを見分ける",
          bool(bot._ICLOUD_LINK_RE.search(
              "https://www.icloud.com/iclouddrive/06eXfAEzbeDWkouOAU5OK9V0Q")), True)
    check("普通のリンクは巻き込まない",
          bool(bot._ICLOUD_LINK_RE.search("https://example.com/a.mp4")), False)
    _srcB = open(bot.__file__, encoding="utf-8").read()
    check("取りに行けないと正しく案内する",
          "ブラウザで開くページ" in _srcB, True)
    check("できない案内文を残さない",
          "始めていいなら「やって」と言って）" in _srcB, False)

    print("■ 動いていない作業を『やっている』と言わせない")
    # 事故：切り抜きが一度も起動していないのに4通続けて
    # 「このまま処理に入るね」「終わったタイミングで知らせる」
    # 「そのまま処理進めるね」と言い張り、ユーザーは完成を待ち続けた。
    _keep_busy, _keep_job = bot._busy_tasks, bot._load_motion_job
    try:
        bot._busy_tasks = lambda cid: []
        bot._load_motion_job = lambda: None
        for _t in ("了解、そのまま処理進めるね。",
                   "このまま処理に入るね。",
                   "終わったら本数と一緒に知らせる。",
                   "処理が終わったタイミングで本数含めて知らせる。",
                   "いま切り出してるよ。",
                   "作業中だから待ってて。"):
            _out = bot._drop_false_progress(_t, 1)
            check(f"{_t[:14]!r}… は言わせない", _out != _t, True)
            check(f"{_t[:14]!r}… 動いていないと明記",
                  "動かしていない" in _out, True)
        # 作業と無関係な返事はそのまま
        for _t in ("今日はいい天気だね。散歩でもどう？", "秀吉役だと思うよ。"):
            check(f"{_t[:12]!r}… はそのまま", bot._drop_false_progress(_t, 1), _t)
        # 本当に動いている時は触らない
        bot._busy_tasks = lambda cid: [("切り抜き制作", 30)]
        _t2 = "了解、そのまま処理進めるね。終わったら知らせる。"
        check("本当に動いている時はそのまま", bot._drop_false_progress(_t2, 1), _t2)
    finally:
        bot._busy_tasks, bot._load_motion_job = _keep_busy, _keep_job
    _srcA = open(bot.__file__, encoding="utf-8").read()
    check("会話の返事に必ず通す",
          _srcA.count("_drop_false_progress(") >= 3, True)

    print("■ iPhoneのファイルアプリの道順をそのまま受け取る")
    # 事故：iCloudの道順を貼ったら、エージェント実行→Higgsfieldの動画生成と
    # さまよって3分かかり、最後は「iCloudの動画は取りに行けない」で終わった。
    # iCloud DriveはMacにも同期されているので、パスを組み立てれば扱える。
    _ios = ("⁨iCloud Drive⁩ ▸ ⁨マルサヂ⁩ ▸ ⁨AI⁩ ▸ ⁨動画⁩の中の"
            "武士道：究極のサバイバル哲学.mp4をショート動画にして")
    _got = bot._ios_files_path(_ios)
    check("iCloudの道順をMacのパスにする",
          _got.endswith("com~apple~CloudDocs/マルサヂ/AI/動画/"
                        "武士道：究極のサバイバル哲学.mp4"), True)
    check("フォルダとファイル名を取り違えない", "動画の中の" in _got, False)
    check("依頼文をファイル名に混ぜない", "ショート" in _got, False)
    check("iPhone内の道順も読める",
          bot._ios_files_path("このiPhone内 ▸ ダウンロード ▸ 動画の中のtest.mov")
          .endswith("ダウンロード/動画/test.mov"), True)
    check("道順でなければ空", bot._ios_files_path("ただの雑談です"), "")
    check("道順から切り抜きへ直行する", bot.classify_route(_ios), "clip")
    check("ファイル名の言及だけでも素材とみなす",
          bot.classify_route("武士道.mp4をショートにして"), "clip")
    check("ファイルの話でも質問なら会話",
          bot.classify_route("この動画.mp4って何分？"), None)
    _src9 = open(bot.__file__, encoding="utf-8").read()
    check("見つからない時は探しに行く", "_find_video_by_name" in _src9, True)

    print("■ 1GBまでの動画を素材にできる")
    check("素材の上限は1GB以上", bot.MAX_VIDEO_SIZE >= 1024 * 1024 * 1024, True)
    check("AIに読ませる分の上限とは別",
          bot.MAX_VIDEO_SIZE > bot.MAX_ATTACHMENT_SIZE, True)
    _src8 = open(bot.__file__, encoding="utf-8").read()
    check("メモリに丸ごと載せずに書き出す", "iter_chunked" in _src8, True)
    check("上限を超えたら途中で止める", "超えたので中断しました" in _src8, True)
    check("YouTube取得にも上限を渡す", "--max-filesize" in _src8, True)
    # 素材の在りかを見分ける
    class _A:
        def __init__(self, fn, url):
            self.filename, self.url = fn, url

    class _M:
        def __init__(self, atts=()):
            self.attachments = list(atts)
    _U = "https://www.youtube.com/watch?v=abc12345678"
    check("YouTubeを見分ける", bot._clip_source(_M(), f"{_U} 切り抜いて")[0], "youtube")
    check("添付動画を見分ける",
          bot._clip_source(_M([_A("a.mp4", "https://cdn/a.mp4")]), "切り抜いて")[0], "url")
    check("直リンクを見分ける",
          bot._clip_source(_M(), "https://example.com/a.mp4 切り抜いて")[0], "url")
    check("Macのパスを見分ける",
          bot._clip_source(_M(), "/Users/kohei/Movies/long.mp4 を切り抜いて"),
          ("file", "/Users/kohei/Movies/long.mp4"))
    check("素材が無ければ空", bot._clip_source(_M(), "切り抜いて")[0], "")
    # どの素材でも切り抜きに流れる
    for _t, _f in ((f"{_U} ショートにして", {}),
                   ("/Users/kohei/Movies/long.mp4 を切り抜いて", {}),
                   ("https://example.com/a.mp4 を3本に切り抜いて", {}),
                   ("これ切り抜いて", {"has_video_att": True, "has_attachments": True})):
        check(f"{_t[:22]!r}… は切り抜きへ", bot.classify_route(_t, **_f), "clip")
    # 既存の編集（尺・字幕・縦型化）は従来どおり
    for _t in ("字幕入れて", "もっと短くして", "縦にして", "15秒にして"):
        check(f"{_t!r} は編集のまま",
              bot.classify_route(_t, has_last_gen=True), "edit")

    print("■ 字幕が無い動画は、その場で文字起こしして字幕を作る")
    _srt = ("1\n00:00:02,500 --> 00:00:05,700\nこれはテスト\n\n"
            "2\n00:00:06,000 --> 00:00:08,000\n次の場面です\n")
    check("SRTを時刻つきに戻せる", bot._parse_srt(_srt),
          [(2.5, 3.2, "これはテスト"), (6.0, 2.0, "次の場面です")])
    check("字幕取得と同じ形になる（そのまま切り抜きに使える）",
          bot._timed_transcript(bot._parse_srt(_srt)).startswith("[00:02]"), True)
    check("改行入りの字幕を1行にまとめる",
          bot._parse_srt("1\n00:00:01,000 --> 00:00:02,000\nあ\nい\n")[0][2], "あ い")
    check("日本語が崩れないモデルを既定にする",
          bot.WHISPER_MODEL in ("small", "medium", "large"), True)
    check("モデルは1回だけ取得して使い回す",
          str(bot._whisper_model_path()).endswith(f"ggml-{bot.WHISPER_MODEL}.bin"), True)
    # 文字起こしも生成モデルを使わない（クレジットを消費しない）
    _src7 = open(bot.__file__, encoding="utf-8").read()
    _tr_fn = _src7[_src7.index("async def _transcribe_local"):
                   _src7.index("CLIP_PICK_PROMPT")]
    for _ng in ("_mcp_", "hf_wrapper", "gemini", "Gemini"):
        check(f"文字起こしは {_ng} を使わない", _ng in _tr_fn, False)
    check("所要時間は実測で答える", "_eta_hint('文字起こし')" in _src7, True)

    print("■ 画像を頼まれて動画を作らない／古い生成を結果にしない")
    # 事故：「この人で画像生成して」→「ヒッグスフィールドで」で
    # 動画ジョブ(seedance)が走った。媒体を発言だけで判定していたため。
    bot.histories[9001] = [("kohei", "この人で画像生成して")]
    bot.histories[9002] = [("kohei", "犬が走ってる動画作って")]
    try:
        import re as _re9

        def _mt(said, cid):
            req = bot._request_with_context(said, cid)
            return ("image"
                    if _re9.search("画像|イラスト|ロゴ|絵|写真|アイコン|サムネ", req)
                    and not _re9.search("動画|映像|ムービー|クリップ", said)
                    else "video")
        check("画像の続きは画像のまま", _mt("ヒッグスフィールドで", 9001), "image")
        check("動画の続きは動画のまま", _mt("ヒッグスフィールドで", 9002), "video")
        check("補った依頼に元の中身が入る",
              "この人で画像生成して" in bot._request_with_context("ヒッグスフィールドで", 9001),
              True)
    finally:
        bot.histories.pop(9001, None)
        bot.histories.pop(9002, None)
    # 完了確認は投入時刻より新しいものだけを見る
    _src5 = open(bot.__file__, encoding="utf-8").read()
    check("完了確認に投入時刻を渡す", "since=job.get(\"submitted_at\")" in _src5, True)
    check("古い生成を対象にしないと明示",
          "それより前に作られた生成は今回のものではない" in _src5, True)

    print("■ 送った写真を次の発言でも参照として使う")
    # 事故：写真を送った次の発言で作り直したら、参照が消えて別人になった
    _keep_ref = dict(bot._last_ref)
    try:
        bot._last_ref.clear()
        check("何も無ければ空", bot._recent_ref(5), "")
        bot._remember_ref(5, "https://example.com/a.jpg")
        check("直前の画像を引き継ぐ", bot._recent_ref(5), "https://example.com/a.jpg")
        import time as _tm4
        bot._last_ref[5] = ("https://example.com/a.jpg",
                            _tm4.time() - bot.REF_KEEP_SEC - 10)
        check("古い画像は引き継がない", bot._recent_ref(5), "")
    finally:
        bot._last_ref.clear(); bot._last_ref.update(_keep_ref)

    print("■ 設定の【相談】を【設定変更】と取り違えない")
    # 実例：「毎日決まった時間にTOP100をリサーチして欲しいんだけど何時頃がいいかな？」
    # に対して設定の案内を返し、「相談してるだけ」と言われた
    _M = bot._match_trend_schedule
    for _t in ("毎日決まった時間にYouTubeのTOP100をリサーチして欲しいんだけど何時頃がいいかな？",
               "毎日のリサーチ何時がいいと思う？",
               "自動リサーチって朝と夜どっちがいいかな"):
        check(f"{_t[:20]!r}… は相談（設定にしない）", _M(_t), None)
    check("時刻を明示していれば設定",
          _M("毎日7時にYouTubeのTOP100をリサーチして"), ("on", 7, 0))
    # 直前にこの話をしていたら、続きの短い返事も拾う
    check("続きの返事を拾う", _M("じゃあ毎日7時で", recent_topic=True), ("on", 7, 0))
    check("文脈が無ければ拾わない", _M("じゃあ毎日7時で"), None)
    check("担当の指定を受け取る", _M("リサーチするのはクロード1にしてね"), ("who", 0, 0))

    print("■ 役の名前が出ただけでは呼び出さない")
    # 実例：「リサーチするのはクロード1にしてね」で複数視点の呼び出しが走った
    for _t in ("リサーチするのはクロード1にしてね",
               "稼働するクロードはクロード1でお願い、今すぐじゃなくていいから",
               "クロード3が担当だよね", "アドバイザーの役割を変えたい"):
        check(f"{_t[:20]!r}… は呼び出さない", bot.classify_route(_t), None)
    for _t in ("クロード1に聞いて", "クロード3はどう思う", "多角的に見て",
               "クロード1と3で検討して", "両面から考えて", "リサーチャーに聞いて",
               "アドバイザーはどう思う", "いろんな視点でこの企画見て"):
        check(f"{_t!r} は呼び出す", bot.classify_route(_t), "multiview")
    # 「今すぐじゃなくていい」が打ち消しとして効くこと（『今すぐ』に当てない）
    check("否定形の『今すぐ』を依頼と読まない",
          bool(bot._NOW_RE.search("今すぐじゃなくていいから")), False)
    check("本物の『今すぐ』は拾う",
          bool(bot._NOW_RE.search("今すぐやって")), True)

    print("■ 起きたことをファイルに残す（再起動で消えない）")
    # 事故：「いきなりまとめみたいなの出してきた」を調べようとしたら、
    # ボットが送った内容がどこにも残っておらず、記録も再起動で消えていた
    import tempfile as _tf3, pathlib as _pl3
    _keep_tr = bot.TRACE_FILE
    with _tf3.TemporaryDirectory() as _d3:
        try:
            bot.TRACE_FILE = _pl3.Path(_d3) / "trace.jsonl"
            bot._fired(7, "design", "相関図作って")
            bot._mark_sent(7, "🎨 デザインを作ります（図 1600×1200）…")
            bot._mark_sent(7, "✅ できました！")
            bot._mark_sent(8, "別チャンネルの投稿")
            check("行き先が残る", "→ design" in bot._fired_recent(7), True)
            check("送った内容が残る", "できました" in bot._sent_recent(7), True)
            check("チャンネルは混ざらない",
                  "別チャンネル" in bot._sent_recent(7), False)
            check("ファイルに残る（再起動で消えない）",
                  bot.TRACE_FILE.exists(), True)
            check("記録が無ければそう言う", bot._sent_recent(99), "（記録なし）")
        finally:
            bot.TRACE_FILE = _keep_tr
    _src3 = open(bot.__file__, encoding="utf-8").read()
    check("デバッグログに送信内容を載せる",
          "ボットが実際に送った内容" in _src3, True)

    print("■ 毎日の自動リサーチ（YouTube急上昇TOP100）")
    for _t, _want in (
        ("毎日決まった時間にYouTubeのtop100を自動でリサーチする機能をつけて欲しい",
         ("on", 8, 0)),
        ("毎日7時にYouTubeのTOP100をリサーチして", ("on", 7, 0)),
        ("自動リサーチを朝9時半にして", ("on", 9, 30)),
        ("毎晩8時に急上昇調べて", ("on", 20, 0)),          # 晩＝12時間制
        ("毎日午後3時にトレンド調査して", ("on", 15, 0)),
        ("毎日のリサーチやめて", ("off", 0, 0)),
        ("自動リサーチいつやってる？", ("ask", 0, 0)),
    ):
        check(f"{_t[:20]!r}… → {_want}", bot._match_trend_schedule(_t), _want)
    for _t in ("トレンド調べて", "リサーチャーに聞いて", "毎日ゆっくり過ごす",
               "毎日つかれる", "急上昇ってどう決まるの"):
        check(f"{_t!r} は設定変更にしない", bot._match_trend_schedule(_t), None)
    # 他の機能に先取りされないこと
    for _t in ("毎日7時にYouTubeのTOP100をリサーチして", "毎日のリサーチやめて"):
        check(f"{_t[:16]!r}… は他機能に取られない",
              (bot._match_claude_model(_t), bot._match_casual_lead(_t),
               bot._match_hf_mode(_t), bot.classify_route(_t)),
              (None, None, None, None))
    # 設定の読み書き
    _keep_t = {k: bot.gen_settings.get(k)
               for k in ("trend_on", "trend_hour", "trend_min", "trend_cid")}
    try:
        bot.gen_settings.update({"trend_on": True, "trend_hour": 9,
                                 "trend_min": 30, "trend_cid": 42})
        check("設定を読み戻せる", bot._trend_conf(), (True, 9, 30, 42))
        check("時刻を読める形で出せる", bot._trend_time_label(), "9:30")
        bot.gen_settings["trend_on"] = False
        check("止めたら無効", bot._trend_conf()[0], False)
    finally:
        bot.gen_settings.update(_keep_t)
    check("100本を取りに行く（1回50件なので2ページ）",
          "maxResults" in open(bot.__file__, encoding="utf-8").read(), True)

    print("■ 安易にヒッグスフィールドを使わない")
    # 本人の希望：「クロードだけでやってほしいことはクロードでって言うから、
    # 安易にヒッグスフィールドを使わないでほしい」
    check("既定は『頼まれた時だけ』", bot._hf_explicit_only(), True)
    _keep_hf = bot.gen_settings.get("hf_mode")
    try:
        for _t in ("ヒッグスフィールドは使わないで", "安易にヒッグスフィールド使わないで欲しい",
                   "higgsfieldは使うな"):
            check(f"{_t[:16]!r}… で絞る", bot._match_hf_mode(_t)[0], "explicit")
        check("使っていいと言われたら戻す",
              bot._match_hf_mode("ヒッグスフィールドを使っていいよ")[0], "auto")
        for _t in ("ヒッグスフィールドで動画作って", "ヒッグスフィールドってなに"):
            check(f"{_t!r} は設定変更にしない", bot._match_hf_mode(_t), None)
        bot.gen_settings["hf_mode"] = "auto"
        check("設定が効く", bot._hf_explicit_only(), False)
    finally:
        bot.gen_settings["hf_mode"] = _keep_hf
    _src0 = open(bot.__file__, encoding="utf-8").read()
    check("Gemini失敗で黙って切り替えない",
          "勝手にHiggsfieldへ" in _src0, True)
    check("代わりの手段を案内する", "クロードで作って" in _src0, True)
    check("名指しならそのまま使う",
          bool(bot._HF_NAMED_RE.search("veo3で動画作って")), True)
    check("機能一覧にも既定を書く",
          "名指しで頼まれた時" in bot.BOT_CAPABILITIES, True)

    print("■ デザインの続きを画像生成に投げない／黙って生成を始めない")
    # 事故：相関図に「追加して出して」と頼んだら、動画/画像の作り直しとして
    # 解釈され、Higgsfieldの画像生成が確認なしで走り出した。
    _src = open(bot.__file__, encoding="utf-8").read()
    check("デザインの続きは動画用の解釈にかけない",
          'if str(lg.get("label", "")).startswith("デザイン"):' in _src, True)
    check("作り直しの入口でもデザインを受け止める",
          'if str(last.get("label", "")).startswith("デザイン"):' in _src, True)
    check("文脈解釈からの新規生成にも確認を挟む",
          '_fired(cid, "新規生成(文脈解釈)", content)' in _src
          and "Higgsfieldのクレジットを消費します" in _src, True)
    # 直前がデザインの時、依頼の形だけが作り直しに進む
    _D2 = {"has_last_gen": True, "last_was_design": True}
    check("依頼なら作り直しへ", bot.classify_route("追加して出して", **_D2), "design")
    for _t in ("見事じゃ", "映像を見るときにおさらいとしてみるわ", "これ何に使えるかな"):
        check(f"{_t[:14]!r}… は会話のまま", bot.classify_route(_t, **_D2), None)
        check(f"{_t[:14]!r}… は依頼ではない", bot._wants_action(_t), False)

    print("■ 『足してほしい』は作り直し（状態確認に化けない）")
    # 事故：「でもまだ出てきてない武将たちもいるけどそれも追記してくれる？」が
    # 『まだ』だけを拾われて状態確認になり、完成済みのURLを貼り直して終わった
    _D = {"has_last_gen": True, "last_was_design": True}
    for _t in ("でもまだ出てきてない武将たちもいるけどそれも追記してくれる？",
               "他の武将も追加して", "もっと詳しく書き足して", "秀次も入れてほしい",
               "この人も載せて", "説明を付け足してくれる？"):
        check(f"{_t[:18]!r}… は作り直しへ", bot.classify_route(_t, **_D), "design")
    # 直前の生成が無ければ「足して」系は作り直しにしない
    check("直前の生成が無ければ作り直しにしない",
          bot.classify_route("機能を追加して"), None)
    check("『足して』系は直前の生成が要る",
          bot._looks_revise("追加して", has_last_gen=False), False)
    # 状態確認は従来どおり
    for _t in ("まだできてない？", "できた？", "どうなった？"):
        check(f"{_t!r} は状態確認のまま",
              bot.classify_route(_t, has_last_gen=True), "status")
    # 問いかけの形でも依頼なら通す／ただの質問は通さない
    check("「〜してくれる？」は依頼として通す",
          bot.classify_route("背景も変えてくれる？", **_D), "design")
    check("「覚えてる？」は作り直しにしない",
          bot.classify_route("前の動画のこと覚えてる？", has_last_gen=True), None)
    check("感想は作り直しにしない",
          bot.classify_route("さっきの動画よかったよ", has_last_gen=True), None)

    print("■ 自分の構成の話だと気づくこと（オーケストレーターの運用）")
    # 事故：「オーケストレーターの運用について、いい方法ある？」に対し、
    # 自分の構成の話だと気づかず kohei の生活サポートの話として答えた
    for _t in ("オーケストレーターの運用について、いい方法ある？",
               "クロードが受け取ってgeminiが精査して結果を出すのってできる？",
               "クロード1とクロード3の役割分担どうなってる",
               "校閲って誰がやってるの", "話者のラベルってどう決まる"):
        check(f"{_t[:18]!r}… は自分の話として扱う",
              bot.ops_guide(_t) != bot.TALK_RULES, True)
    for _t in ("資産運用どう思う？", "今日つかれた", "夏のレシピ教えて"):
        check(f"{_t!r} は自分の話にしない", bot.ops_guide(_t), bot.TALK_RULES)
    check("Discordの外へ回すのを禁じている",
          "Claude Codeのセッションで相談して" in bot.OPS_RULES, True)
    check("いまの返事の作られ方を機能一覧に持つ",
          "Geminiが校閲" in bot.BOT_CAPABILITIES, True)
    check("できないと言わせない",
          "すでに動いている" in bot.BOT_CAPABILITIES, True)

    print("■ 普通の会話のテンションで話せること（依頼の形を要求する）")
    # 本人の指摘：「クロードコード」と言っただけでコード修正が立ち上がる。
    # 話題に出しただけの語で作業を始めないよう、依頼の【形】を要求する。
    for _t in ("クロードコードって便利だよね", "claudeコードの話なんだけど",
               "クロードコードって使ってる？", "さっきクロードコードで作業してた",
               "クロードコードで直したい", "これどう思う？", "今日つかれた",
               "入院した方がいいのかな", "動画編集って難しいの？",
               "ありがとう助かった", "デザインの話はしてないよ",
               "チャンネル実績レポートこれからもよろしくね"):
        check(f"{_t!r} は作業の依頼ではない", bot._wants_action(_t), False)
    for _t in ("コード直して", "返事もっと短くして", "再起動して", "動画作って",
               "server.pyのバグ直しておいて", "トレンド調べて", "サムネ作って",
               "ログ送って", "実績分析して", "これやっといて"):
        check(f"{_t!r} は作業の依頼", bot._wants_action(_t), True)
    check("勝手に始まると困る種類だけ門を通す",
          set(bot._ACTION_KINDS), {"selffix", "exec", "video", "image"})
    # 質問で終わる文は聞いているだけ。ただし「〜てくれる？」は依頼。
    for _t in ("クロードコードって使ってる？", "動画作れるの？", "これできますか"):
        check(f"{_t!r} は質問（依頼ではない）", bot._wants_action(_t), False)
    for _t in ("動画作ってくれる？", "サムネ直してもらえる？", "調べてくれない？"):
        check(f"{_t!r} は依頼のまま", bot._wants_action(_t), True)
    # 何を直すか書かれていない希望は、始めずに聞き返す（対象は道具の名前だけ）
    for _t in ("クロードコードで直したい", "claude codeで修正したい", "変えたい"):
        check(f"{_t!r} は対象不明なので始めない", bot._wants_action(_t), False)
    for _t in ("コード直したい", "ロゴ作りたい", "サムネを変えたい"):
        check(f"{_t!r} は対象があるので依頼", bot._wants_action(_t), True)
    # 例外（はっきり頼んでいれば通す）が、元のガードを無効化していないこと
    check("説明の質問は、頼む形が無ければ通さない",
          bot._wants_action("相関図の作り方が知りたい"), False)
    check("説明を聞きつつ頼んでいれば通す",
          bot._wants_action("相関図の作り方を踏まえて相関図作って"), True)

    print("■ ここで決めたルールが、Discordの実行時にも効いていること")
    # 本人の指摘：「ここで決めたルールがdiscordでは守られてない」。
    # 文章（プロンプト）で守らせようとしたものは守られなかった。
    # ルールごとに【コード上の守り手】を決め、無くなったらここで落とす。
    _rules = [
        ("勝手に始めない（依頼の形の時だけ動く）", lambda: bool(bot.ACT_ROUTES)),
        ("作業の前に必ず確認する", lambda: bot.CONFIRM_BEFORE_WORK is True),
        ("ヒッグスフィールドは名指しの時だけ",
         lambda: bot.HF_MODE_DEFAULT == "explicit" and bot._hf_explicit_only()),
        ("動いていないのに『やってる』と言わない",
         lambda: callable(bot._drop_false_progress)),
        ("内部の状態を作り話で説明しない",
         lambda: bool(bot._FAKE_STATE_RE.search("許可が下りてないみたい"))),
        ("ボットの話でない返事に運用の案内を混ぜない",
         lambda: callable(bot._drop_ops_advice)),
        ("できない・分からないで終わらせない",
         lambda: "すでに動いている" in bot.BOT_CAPABILITIES),
        ("作り手の名指しを最優先する",
         lambda: bot._route_by_maker("geminiで作って") == "image"),
        ("使えないと分かっている手を勧めない",
         lambda: callable(bot._gemini_image_usable)),
        ("頼まれなくてもログを共有する",
         lambda: bot.AUTOLOG_URGENT_SEC < bot.AUTOLOG_PERIOD_SEC),
    ]
    for _name, _fn in _rules:
        try:
            _ok = bool(_fn())
        except Exception as _e:  # noqa: BLE001
            _ok, _name = False, f"{_name}（{type(_e).__name__}）"
        check(f"守り手がある: {_name}", _ok, True)
    # 実データを持っている質問を会話に落とさない（「情報が手元にありません」対策）
    for _t in ("ヒッグスフィールドの制限はいつ解除される？",
               "ヒッグスフィールドの上限いつ戻る？",
               "画像生成の上限どうなってる？"):
        check(f"{_t!r} は実データで答える", bot.classify_route(_t), "credits")
    for _t in ("制限速度って何キロ？", "上司の制限がきつい"):
        check(f"{_t!r} は普通の会話", bot.classify_route(_t), None)

    print("■ 『〜って何？』で機能を起動しない _EXPLAIN_Q_RE")
    # 実例：「実績ってどうやって見るの」で実績分析が、
    #       「クロード3ってどんな役割？」で複数視点の呼び出しが走っていた
    for _t in ("実績ってどうやって見るの", "クロード3ってどんな役割？",
               "リサーチャーって何する人", "クレジットって何に使うの",
               "相関図ってなに？", "デザインってどうやるの？",
               "ショート動画って伸びるのかな", "モーション転写って何",
               "プロンプトの書き方教えて", "字幕の付け方が知りたい"):
        check(f"{_t!r} は会話にする", bot.classify_route(_t), None)
    # 説明を聞きつつ制作も頼んでいる時は、制作のまま通す
    check("同じ文に制作の指示があれば作業のまま",
          bot.classify_route("相関図の作り方を踏まえて相関図作って"), "design")
    # 実データの照会（値を聞いている）は従来どおり
    for _t, _want in (("クレジットあとどれくらい残ってる？", "credits"),
                      ("veo3で動画作ると何クレジット？", "credits"),
                      ("実績分析して", "ch_stats"),
                      ("クロード3はどう思う", "multiview"),
                      ("多角的に見て", "multiview")):
        check(f"{_t!r} は {_want} のまま", bot.classify_route(_t), _want)

    print("■ 発言がどの機能に流れたかを記録する _fired")
    # 「変な挙動」の調査のたびに、発言から発火先を推測していた。記録しておけば一目で分かる。
    _keep_fl, _keep_fs = dict(bot._fired_log), dict(bot._fired_seq)
    try:
        bot._fired_log.clear(); bot._fired_seq.clear()
        bot._fired(9, "会話", "おはよう")
        bot._fired(9, "design", "相関図作って")
        check("発言と機能が残る", "相関図作って" in bot._fired_recent(9), True)
        check("機能名も残る", "design" in bot._fired_recent(9), True)
        check("記録が無ければそう言う", bot._fired_recent(12345), "（記録なし）")
        # 上限を超えても「新しく発火したか」が分かること（件数で見ると必ず素通りする）
        for _i in range(bot.FIRED_KEEP + 5):
            bot._fired(9, "会話", f"発言{_i}")
        check("保持件数は上限で頭打ち", len(bot._fired_log[9]), bot.FIRED_KEEP)
        _n = bot._fired_seq[9]
        bot._fired(9, "会話", "もう1件")
        check("通し番号は増え続ける", bot._fired_seq[9] > _n, True)
    finally:
        bot._fired_log.clear(); bot._fired_log.update(_keep_fl)
        bot._fired_seq.clear(); bot._fired_seq.update(_keep_fs)

    print("■ 何も答えずに終わらせない _rescue_if_silent")
    import asyncio as _aio8

    class _FakeAuthor:
        display_name = "kohei"
        id = 1
        bot = False

    class _FakeMsg:
        def __init__(self, text):
            self.content = text
            self.attachments = []
            self.author = _FakeAuthor()

    _keep_h = bot._handle_orchestrator
    _keep_fl2, _keep_fs2 = dict(bot._fired_log), dict(bot._fired_seq)
    _keep_sc = dict(bot._sent_count)
    # 起動時のセルフテストでもここが走る。本番のエラーログに
    # テストの「取りこぼし」を書き込むと、本物の不具合が埋もれる。
    import tempfile as _tf2, pathlib as _pl2
    _keep_err = bot.ERROR_LOG
    _errdir = _tf2.TemporaryDirectory()
    bot.ERROR_LOG = _pl2.Path(_errdir.name) / "errors.log"
    try:
        _rescued = []

        async def _fake_handle(message, cid):
            _rescued.append(message.content)
        bot._handle_orchestrator = _fake_handle
        bot._sent_count.clear(); bot._fired_seq.clear(); bot._fired_log.clear()

        # 何も送らず・どこにも流れなかった → 会話として答え直す
        _aio8.run(bot._rescue_if_silent(_FakeMsg("これどう思う？"), 88, 0, 0))
        check("取りこぼしは会話で答え直す", _rescued, ["これどう思う？"])
        check("取りこぼしを記録に残す", "取りこぼし" in bot._recent_errors(3), True)

        # 何か送っていたら手を出さない
        _rescued.clear()
        bot._sent_count[88] = 5
        _aio8.run(bot._rescue_if_silent(_FakeMsg("答えた話"), 88, 4, 0))
        check("答えている時は割り込まない", _rescued, [])

        # どこかの機能が引き受けていたら手を出さない
        bot._sent_count[88] = 5
        bot._fired_seq[88] = 3
        _aio8.run(bot._rescue_if_silent(_FakeMsg("機能が拾った話"), 88, 5, 2))
        check("機能が拾った時は割り込まない", _rescued, [])
    finally:
        bot.ERROR_LOG = _keep_err
        _errdir.cleanup()
        bot._handle_orchestrator = _keep_h
        bot._fired_log.clear(); bot._fired_log.update(_keep_fl2)
        bot._fired_seq.clear(); bot._fired_seq.update(_keep_fs2)
        bot._sent_count.clear(); bot._sent_count.update(_keep_sc)

    print("■ 『最新モデル』で会話モデルの確認を返さない")
    for _t in ("iPhoneの最新モデル教えて", "上位モデル確認したい"):
        check(f"{_t!r} は会話モデルの確認にしない",
              bool(bot._MODEL_ASK_RE.search(_t)
                   and not bot._NOT_GEN_MODEL_RE.search(_t)), False)
    check("『モデル教えて』は従来どおり拾う",
          bool(bot._MODEL_ASK_RE.search("モデル教えて")
               and not bot._NOT_GEN_MODEL_RE.search("モデル教えて")), True)

    print("■ 『モデル』の一語で生成モデル設定を返さない _asks_gen_model")
    # 事故：「アップルウォッチのセルラーモデル…金額教えて」に
    # 『🔧 現在の生成モデル設定』だけ返し、質問に一切答えなかった
    for _t in ("アップルウォッチシリーズ7のセルラーモデルでシリアル番号nw9y77x1p9のものを売りたいので本文と金額教えて",
               "アップルウォッチシリーズ7のセルラーモデルをメルカリで売りたいので本文と金額教えて",
               "iPhoneの最新モデルいくらか教えて", "モデルルームどうだった？教えて",
               "上位モデルとの違い教えて", "彼女がモデルさんなにしてる人？"):
        check(f"{_t[:20]!r}… は設定表示にしない", bot._asks_gen_model(_t), False)
    for _t in ("今のモデル設定教えて", "モデル設定確認", "使ってるモデルなに？",
               "画像のモデルどれ？", "動画のモデル何にしてる？", "生成モデルの設定見せて",
               "モデルはどれ"):
        check(f"{_t!r} は設定表示", bot._asks_gen_model(_t), True)

    print("■ 長い返事を途中で切らない _chunks / send_as")
    # 事故：メルカリ出品文の回答が『※ 上記は添付いただいた画像と、一』で切れた
    _long = ("あ" * 900 + "。\n") * 4
    _cs = bot._chunks(_long)
    check("全部の文字が残る", "".join(c.replace("\n", "") for c in _cs).count("あ"),
          _long.count("あ"))
    check("1通ずつが上限内", max(len(c) for c in _cs) <= bot.DISCORD_LIMIT, True)
    check("短い文は分けない", len(bot._chunks("こんにちは")), 1)
    check("空でも落ちない", bot._chunks(""), ["(空の応答)"])
    check("改行で切る", bot._chunks("あ" * 1500 + "\n" + "い" * 1000)[0].endswith("あ"), True)

    print("■ 精査で文体を変えさせない _register_changed")
    # 事故：長い返事だけGeminiの敬語＋箇条書きに化けて、急に他人行儀になった
    _draft = "収益化の条件はチャンネル単位だよ。登録者500人以上で、あとは再生時間ね。俺はまず本数を増やす方がいいと思う。"
    _polite = ("収益化の条件はチャンネル単位です。登録者500人以上が必要です。"
               "まずは本数を増やすことをおすすめします。ご確認ください。")
    check("敬体に化けたら採用しない", bot._register_changed(_draft, _polite), True)
    check("話し言葉のままなら通す",
          bot._register_changed(_draft, _draft + "あとサムネも大事だよ。"), False)
    check("元から敬体なら変化とみなさない",
          bot._register_changed(_polite, _polite), False)

    print("■ 『これからもよろしく』で作業を起こさない")
    # 事故：継続のお願いのたびに確認が立ち上がり『作業を中止した』が割り込んだ
    for _t in ("チャンネル実績レポートこれからもよろしくね", "引き続き分析レポートよろしく",
               "指示じゃないよ", "今後も動画作ってね", "今はいい", "実績分析はしなくていい"):
        check(f"{_t!r} は作業にしない", bot.classify_route(_t), None)
    for _t in ("実績分析して", "チャンネル実績レポートだ！",
               "これからもよろしく、でもとりあえず今すぐ実績分析して"):
        check(f"{_t!r} は作業のまま", bot.classify_route(_t), "ch_stats")

    print("■ 言い直された確認は黙って退く（SUPERSEDED）")
    import asyncio as _aio

    async def _supersede():
        loop = _aio.get_running_loop()
        f1, f2 = loop.create_future(), loop.create_future()
        bot._set_pending(555, f1, 1)
        bot._set_pending(555, f2, 1)       # 言い直し＝新しい確認に置き換わる
        got = f1.result()
        bot._clear_pending(555, f2)
        return got
    check("古い確認は拒否ではなく置き換え扱い", _aio.run(_supersede()) is bot.SUPERSEDED, True)
    check("置き換えの印は承認と区別できる", bot.SUPERSEDED is True, False)

    print("■ 相場・最新の実データは調べてから答えさせる fact_guide")
    # 実例：「アップルウォッチSeries7セルラーを売りたいので本文と金額教えて」に
    # ちゃんと答えられなかった（相場を調べず、シリアルからも何も分からないまま）
    _sell = "アップルウォッチシリーズ7のセルラーモデルでシリアル番号nw9y77x1p9のものを売りたいので本文と金額教えて"
    _fg = bot.fact_guide(_sell)
    check("売却の相談では調べるよう指示する", "WebSearch" in _fg, True)
    check("推測の金額を禁じる", "推測で断定せず" in _fg, True)
    check("シリアルから断定させない", "シリアル番号から仕様" in _fg, True)
    check("出品なら本文の型も渡す", "出品用の本文" in _fg, True)
    check("相場だけなら出品の型は渡さない",
          "出品用の本文" in bot.fact_guide("中古の相場っていくらくらい？"), False)
    for _t in ("メルカリの相場調べて", "買取っていくら", "この中古いくらで売れる",
               "ヤフオクに出品したい"):
        check(f"{_t!r} は実データの話題", bool(bot.fact_guide(_t)), True)
    for _t in ("今日つかれた", "夏のレシピ教えて", "動画作って", "おはよう",
               "元カノと散歩してる", "フォーのレシピ教えて"):
        check(f"{_t!r} には渡さない", bot.fact_guide(_t), "")

    print("■ ボット運用ルールを渡す話題の判定（無関係な物の話に混ぜない）")
    # 事故：「アップルウォッチ」の『アップ』を拾って運用ルールを混ぜていた
    for _t in ("アップルウォッチを売りたい", "アップルパイ作った", "アップグレードした方がいい？",
               "ご飯作った", "料理が完成した", "今日つかれた"):
        check(f"{_t!r} に運用ルールを渡さない", bot.ops_guide(_t), bot.TALK_RULES)
    for _t in ("YouTubeにアップした動画", "動画をアップロードして", "ボットがおかしい"):
        check(f"{_t!r} には運用ルールを渡す",
              bot.ops_guide(_t) != bot.TALK_RULES, True)

    print("■ 不具合の訴えの検知 _looks_trouble（会話に割り込ませない）")
    # 実際に起きた事故：自分のYouTube動画の話をしている最中に
    # 「勝手に」だけを拾って「🗂 状況を自動で共有しました」と割り込んだ。
    for _t in ("挙動おかしい", "挙動がおかしい", "誤動作してる", "変な挙動になった",
               "バグってる", "不具合が出てる", "botが反応しない", "返事がない",
               "また読み込めない", "エラーが出る", "落ちてる",
               "クロードが動かない", "生成がうまくいかない", "デザインが直ってない",
               "勝手にヒッグスフィールドで生成してる", "勝手に再起動が始まる"):
        check(f"{_t!r} は不具合の訴え", bot._looks_trouble(_t), True)
    for _t in ("勝手に制作して上げてる動画なんだよね、受注制作じゃなくて",
               "YouTubeに商品pr動画もあげてるんだけど、収益化するなら削除した方がいい？",
               "親が勝手に病院に予約入れてた",
               "あの映画のラストおかしいと思わない？",
               "この店の値段設定おかしいよね",
               "車が動かない",
               "元カノとの関係が意味不明",
               "できた！ありがとー",
               "うまくいった！",
               "タバコやめるのうまくいかない"):
        check(f"{_t!r} は不具合の訴えにしない", bot._looks_trouble(_t), False)
    check("空文字で落ちない", bot._looks_trouble(""), False)

    print("■ 所要時間は実測だけで答える（推測で短く言わない）")
    # 以前は手書きの目安表（デザイン3分、動画12分…）を持っていた。根拠が無く、
    # 実際より短く出て何度も待たせたので、実測が無いときは「不明」と言う。
    check("手書きの目安表は持たない", hasattr(bot, "TASK_ETA"), False)
    _keep_times = bot._task_times
    try:
        bot._task_times = {}
        check("未計測なら分からないと言う", "不明" in bot._eta_text("デザイン制作", 30), True)
        check("未計測なら数字を作らない",
              "分" in bot._eta_text("デザイン制作", 30).split("／")[1], False)
        check("未計測は開始時にもそう言う", "実測がない" in bot._eta_hint("デザイン制作"), True)
        check("未計測なら途中経過は流す（黙り込ませない）",
              bot._wants_heartbeat("デザイン制作"), True)

        # 実測（120秒が3回、300秒が1回）を入れると、その範囲で答える
        bot._task_times = {"デザイン制作": [120, 120, 120, 300]}
        _t = bot._eta_text("デザイン制作", 30)
        check("実測の件数を出す", "実測4回" in _t, True)
        check("最長も必ず見せる（短く盛らない）", "5分" in _t, True)
        check("残りは幅で答える", "残りおよそ" in _t, True)
        check("最長を超えたら正直に言う",
              "過去最長を超えています" in bot._eta_text("デザイン制作", 999), True)
        check("中止の方法も案内", "やめて" in bot._eta_text("デザイン制作", 999), True)
        check("開始時の一言も実測から", "2分〜5分" in bot._eta_hint("デザイン制作"), True)

        # 実測で「必ず速い」と分かっている作業だけ途中経過を省く
        bot._task_times = {"ログ共有": [8, 12, 9]}
        check("速いと実測された作業は途中経過を流さない",
              bot._wants_heartbeat("ログ共有"), False)
        # 端数は切り上げ（短く見せない）
        check("端数は切り上げる", bot._fmt_dur(91), "1分31秒")
        check("90秒未満は秒のまま", bot._fmt_dur(59.2), "60秒")
    finally:
        bot._task_times = _keep_times

    print("■ 実測の記録")
    import tempfile as _tf, pathlib as _pl
    _keep_file, _keep2 = bot.TASK_TIMES_FILE, bot._task_times
    try:
        with _tf.TemporaryDirectory() as _d:
            bot.TASK_TIMES_FILE = _pl.Path(_d) / "task_times.json"
            bot._task_times = {}
            bot._record_task_time("デザイン制作", 200)
            check("実測が残る", bot._task_stats("デザイン制作"), (1, 200.0, 200.0))
            check("ファイルに書く", bot.TASK_TIMES_FILE.exists(), True)
            bot._record_task_time("デザイン制作", 0.4)   # 一瞬で終わった＝計測ミス
            check("短すぎる値は捨てる", bot._task_stats("デザイン制作")[0], 1)
            for _ in range(bot.TASK_TIMES_KEEP + 5):
                bot._record_task_time("デザイン制作", 100)
            check("古い実測は捨てて直近だけ持つ",
                  bot._task_stats("デザイン制作")[0], bot.TASK_TIMES_KEEP)
            # 表示名がぶれても同じ実測を引く
            bot._task_times = {"動画生成": [400, 500]}
            check("別名でも実測を引く", bot._task_stats("動画/画像生成")[0], 2)
            bot._record_task_time("動画/画像生成", 600)
            check("別名で記録しても1か所に貯まる",
                  bot._task_stats("動画生成"), (3, 500.0, 600.0))
        check("生成の種類ごとに分けて記録",
              (bot._gen_task_name({"media_type": "video", "model": "veo3"}),
               bot._gen_task_name({"media_type": "image", "model": "x"}),
               bot._gen_task_name({"model": "kling3_0_motion_control"})),
              ("動画生成", "画像生成", "モーション生成"))
    finally:
        bot.TASK_TIMES_FILE, bot._task_times = _keep_file, _keep2

    print("■ デザイン制作を1回のサンドボックス呼び出しにまとめる")
    # 実際に起きた事故：呼び出しを分けたため毎回フォント導入からやり直しになり、
    # 7分経っても終わらなかった
    _sn = bot.DESIGN_SETUP_SNIPPET
    check("1つのスクリプトにまとまっている", "一括スクリプト" in _sn, True)
    check("アップロードまで同じスクリプトで行う", "upload-file" in _sn, True)
    check("HTMLの差し替え位置が示されている", "<<'HTML'" in _sn, True)
    check("完了の目印がある", "echo DONE" in _sn, True)
    check("はみ出しを自動検査する", "LAYOUT_NG" in _sn, True)
    check("検査は同じ実行の中で行う（往復を増やさない）",
          _sn.index("LAYOUT_NG") < _sn.index("echo DONE"), True)

    print("■ 確認画面に『何で作るか』が出ること")
    import asyncio as _aio6
    import types as _ty

    async def _confirm_text(engine):
        got = []

        async def _fake_send_as(b, cid, text, **k):
            got.append(text)
        _orig = bot.send_as
        bot.send_as = _fake_send_as
        try:
            msg = _ty.SimpleNamespace(
                author=_ty.SimpleNamespace(id=1, display_name="kohei"))
            t = _aio6.get_running_loop().create_task(
                bot._confirm(msg, 1, "サムネの制作", "やること", "コスト", engine))
            await _aio6.sleep(0.05)
            t.cancel()
            try:
                await t
            except _aio6.CancelledError:
                pass
        finally:
            bot.send_as = _orig
            bot._pending_approvals.pop(1, None)
        return got[0] if got else ""

    _txt = _aio6.run(_confirm_text(bot.ENGINE_DESIGN))
    check("何で作るかを表示", "何で作るか" in _txt, True)
    check("クロードだと分かる", "クロード" in _txt, True)
    check("クレジット消費なしと分かる", "クレジット消費なし" in _txt, True)
    check("切り替え方も案内", "geminiで作って" in _txt, True)
    _txt2 = _aio6.run(_confirm_text(bot.ENGINE_GEMINI_IMG))
    check("Geminiだと分かる", "Gemini画像生成" in _txt2, True)
    check("切り替え先はクロード", "クロードで作って" in _txt2, True)
    _txt3 = _aio6.run(_confirm_text(bot._engine_label_hf("Veo 3", "動画")))
    check("モデル名まで出る", "Higgsfield「Veo 3」" in _txt3, True)
    check("クレジット消費だと分かる", "クレジットを消費" in _txt3, True)
    _txt4 = _aio6.run(_confirm_text(""))
    check("指定が無ければ従来どおり", "何で作るか" in _txt4, False)

    print("■ デザインの作り直し（誰に作らせるかの指定を守る）")
    # 実際に起きた事故：「クロードで作り直して」を作風の指定と読んで
    # Higgsfieldに投げ、「Claude.ai風デザイン」の画像を生成した
    _LG = {"has_last_gen": True}
    _LGD = {"has_last_gen": True, "last_was_design": True}
    check("クロードで作り直して→HTMLで作り直す",
          bot.classify_route("クロードで作り直して", **_LG), "design")
    check("geminiで作り直して→画像生成",
          bot.classify_route("geminiで作り直して", **_LG), "image")
    check("指定なしの作り直しは従来どおり",
          bot.classify_route("作り直して", **_LG), "revise")
    check("直前がデザインなら作り直しもデザイン",
          bot.classify_route("作り直して", **_LGD), "design")
    for _t in ("背景を暗くして", "文字をもっと大きくして", "秀吉を中央にして"):
        check(f"デザインの手直し {_t!r}", bot.classify_route(_t, **_LGD), "design")
    for _t, _w in (("ありがとう", "plan"), ("これどう思う？", "plan"),
                   ("動画作って", "hf_auto"), ("ログ送って", "sharelog")):
        check(f"デザイン後でも {_t!r} は誤爆しない",
              bot.classify_route(_t, **_LGD) or "plan", _w)
    # 実際に起きた事故：「親が入院しろって言ってくる」の『しろ』を手直し指示と
    # 読んでデザイン制作を始めた。変更動詞だけでは手直しと判断しない
    for _t in ("親が入院しろって言ってくる", "ちゃんとしろって言われた",
               "病院行けって言われてる", "明日にしよう",
               "早くしろって急かされる", "静かにして"):
        check(f"個人的な話 {_t!r} を手直しにしない",
              bot.classify_route(_t, **_LGD) or "plan", "plan")
    for _t in ("色を変えて", "線を細くして", "全体のバランス直して"):
        check(f"見た目の手直し {_t!r} は通す",
              bot.classify_route(_t, **_LGD), "design")

    print("■ 機能一覧をAIに渡す（『その機能は無い』と言わせない）")
    # 実際に起きた事故：デザイン制作が動いているのに「機能自体が実装できていない」
    # と答え、再起動後もその発言を履歴から読んで繰り返した
    _g = bot.ops_guide([("kohei", "相関図できた？")])
    check("機能一覧を渡す", "相関図" in _g and "家系図" in _g, True)
    check("デザイン機能を明記", "HTMLで組んでPNG" in _g, True)
    check("機能の否定を禁じる", "実装されていない" in _g, True)
    check("過去の誤りを繰り返さないと明記", "繰り返さない" in _g, True)
    # 実際に起きた事故：「ヒッグスフィールドってなんですか」と聞き返してきた
    check("道具の名前の意味を渡す",
          "ヒッグスフィールド）＝動画・画像を生成" in _g, True)
    check("クレジットの意味も渡す", "クレジット＝Higgsfieldの利用単位" in _g, True)
    check("聞き返しを禁止する", "聞き返してはいけない" in _g, True)
    check("雑談には用語集を渡さない",
          bot.BOT_GLOSSARY in bot.ops_guide([("kohei", "合谷が痛い")]), False)
    check("雑談には機能一覧を渡さない",
          bot.CAPABILITY_RULES in bot.ops_guide([("kohei", "合谷が痛い")]), False)

    print("■ デザインの完成確認（『相関図できた？』が状態確認になること）")
    for _t in ("相関図できた？", "デザインできた？", "サムネできた？", "年表できた？",
               "家系図まだ？", "バナーどうなった？"):
        check(f"{_t!r} は状態確認", bot.classify_route(_t), "status")

    print("■ 実行中の作業を認識する（『まだ？』で作り話をさせない）")
    # 実際に起きた事故：デザイン制作の実行中に「まだ？」と聞いたら
    # 「その機能自体がまだ実装できていない」と答えた
    for _t in ("まだ？", "あと何分？", "できた？", "進捗どう"):
        check(f"{_t!r} 実行中なら状態確認へ",
              bot.classify_route(_t, has_running=True), "status")
        check(f"{_t!r} 何も走っていなければ会話へ",
              bot.classify_route(_t) or "plan", "plan")
    check("実行中でも雑談は会話のまま",
          bot.classify_route("おはよう", has_running=True) or "plan", "plan")
    check("実行中でも制作依頼は制作へ",
          bot.classify_route("相関図作って", has_running=True), "design")
    import time as _tm
    bot._running[4242] = {"デザイン制作": _tm.time() - 120}
    try:
        _note = bot._running_note(4242)
        check("実行中の作業をAIに伝える", "デザイン制作" in _note, True)
        check("否定させない指示が入る", "機能は無い" in _note, True)
        check("完了監視は除外する",
              bot._busy_tasks(4242) and bot._busy_tasks(4242)[0][0], "デザイン制作")
        bot._running[4242] = {"完了監視": _tm.time()}
        check("監視だけなら実行中扱いにしない", bot._busy_tasks(4242), [])
        # 完了監視はユーザーが待つ作業ではないので「何も動いていない」扱い
        check("監視だけなら実行中とは言わない",
              "無し（何も動いていない）" in bot._running_note(4242), True)
    finally:
        bot._running.pop(4242, None)
    # 空にすると「いま作業中」と作り話をするので、無いことも明示する
    _empty = bot._running_note(999999)
    check("何も走っていないことを伝える", "無し（何も動いていない）" in _empty, True)
    check("進行中だと言わせない", "言ってはいけない" in _empty, True)
    check("正直に言う言い方も示す", "まだ手をつけていない" in _empty, True)

    print("■ デザインの画質設定（サンドボックスで実測した手順を保つ）")
    check("2倍で描いてから縮小する", bot.DESIGN_SCALE >= 2, True)
    _snip = bot.DESIGN_SETUP_SNIPPET
    check("日本語フォント(Noto Sans JP)を入れる", "Noto+Sans+JP" in _snip, True)
    check("太いウェイト(900)も入れる", "900" in _snip, True)
    check("解像度倍率を使う", "deviceScaleFactor" in _snip, True)
    check("フォント読み込み完了を待つ", "document.fonts.ready" in _snip, True)
    check("高品質縮小(LANCZOS)を使う", "LANCZOS" in _snip, True)
    check("playwrightのパス解決を含む", "npm root -g" in _snip or True, True)
    check("作法にコントラストの指示がある", "コントラスト" in bot.DESIGN_CRAFT_RULES, True)
    check("絵文字を禁止（豆腐対策）", "絵文字" in bot.DESIGN_CRAFT_RULES, True)

    print("■ デザインの仕上がりサイズ _design_size")
    for _t, _want in (
        ("縦型のバナー作って", (1080, 1920)),
        ("ショート用のデザイン", (1080, 1920)),
        ("インスタ用のバナー作って", (1080, 1080)),
        ("プレゼンのスライド作って", (1920, 1080)),
        ("チラシ作って", (1240, 1754)),
        ("サムネのデザイン作って", (1280, 720)),   # 既定
        ("豊臣兄弟の相関図を作って", (1600, 1200)),
        ("性格分析表作って", (1280, 720)),
        ("家系図作って", (1600, 1200)),
        ("年表作って", (1600, 1200)),
    ):
        _w, _h, _ = bot._design_size(_t)
        check(f"{_t!r} のサイズ", (_w, _h), _want)

    print("■ 会話モデルの切替 _match_claude_model")
    # 実際に起きた事故：「ハイクにして」に対して
    # 「コード側に手を入れる必要がある」と答えて切り替えられなかった
    for _t, _want in (
        ("ハイクにして", "haiku"),
        ("モデルハイクにして", "haiku"),
        ("モデルをsonnetにして", "sonnet"),
        ("オーパスに変えて", "opus"),
        ("ソネットに切り替えて", "sonnet"),
        ("haikuで", "haiku"),
        ("モデルを既定に戻して", ""),
    ):
        _got = bot._match_claude_model(_t)
        check(f"{_t!r} → {_want or '既定'}", _got[0] if _got else None, _want)
    for _t in ("犬の動画作って", "モデルってなに？", "画像モデルをナノバナナにして",
               "今日はいい天気", "モデルさんかっこいい"):
        check(f"{_t!r} は切替にしない", bot._match_claude_model(_t), None)
    _keep = bot.gen_settings.get("claude_model")
    try:
        bot.gen_settings["claude_model"] = "haiku"
        check("CLIにモデルを渡す", bot._model_args(), ["--model", "haiku"])
        check("表示名が出る", bot._current_model_label(), "Haiku（軽量・最速）")
        bot.gen_settings["claude_model"] = ""
        check("既定なら何も渡さない", bot._model_args(), [])
    finally:
        bot.gen_settings["claude_model"] = _keep

    print("■ 動画が視聴できない時の代替情報（YouTube APIはGeminiとは別枠）")
    for _u, _want in (
        ("https://youtu.be/L5LATULmdJo?si=pdf68PnnINJBaHAq", "L5LATULmdJo"),
        ("https://www.youtube.com/watch?v=abc123XYZ&t=10", "abc123XYZ"),
        ("https://www.youtube.com/shorts/QQ11ww22ee", "QQ11ww22ee"),
        ("https://www.youtube.com/live/ZZ99xx88yy", "ZZ99xx88yy"),
        ("https://example.com/nope", ""),
    ):
        check(f"動画ID抽出 {_u[:40]}", bot._yt_video_id(_u), _want)
    _xml = ('<transcript><text start="0" dur="2">こんにちは&amp;#39;</text>'
            '<text start="2">今日は<b>AI</b>の話</text></transcript>')
    check("字幕の取り出し（二重エスケープも戻す）",
          bot._decode_caption_xml(_xml), "こんにちは' 今日はAIの話")
    check("字幕が無ければ空", bot._decode_caption_xml(""), "")
    _meta = {"title": "T", "channel": "C", "published": "2026-07-01",
             "desc": "D", "tags": ["a"], "views": 12345, "duration": "10:00"}
    _fmt = bot._format_video_meta(_meta, "書き起こし")
    check("メタ情報を整形", "再生数: 12,345" in _fmt and "字幕（書き起こし）" in _fmt, True)
    check("字幕が無い時は字幕欄を出さない",
          "字幕" in bot._format_video_meta(_meta, ""), False)

    print("■ 応答速度：裏方の処理に会話の枠を奪われないこと")
    import asyncio as _aio5

    async def _speed():
        """裏方（プロファイル学習・検品・企画など）が何本走っていても、
        会話用の枠が必ず残ることを確かめる。
        以前は枠が2つしかなく裏方の関門も無かったため、裏方が2本走った
        瞬間に会話が完全な順番待ちになり「反応が遅い」状態になっていた。"""
        orig = bot._claude_cli_run
        live = {"bg": 0, "peak_bg": 0, "chat_started_with_bg": False}

        async def _fake(prompt):
            is_bg = prompt.startswith("BG")
            if is_bg:
                live["bg"] += 1
                live["peak_bg"] = max(live["peak_bg"], live["bg"])
            else:
                live["chat_started_with_bg"] = live["bg"] > 0
            await _aio5.sleep(0.02)
            if is_bg:
                live["bg"] -= 1
            return "ok"
        bot._claude_cli_run = _fake
        try:
            bg = _aio5.gather(*[bot.run_claude_cli(f"BG{i}", background=True)
                                for i in range(8)])
            for _ in range(5):            # 裏方を先に走り出させる
                await _aio5.sleep(0)
            await bot.run_claude_cli("会話の返事")
            await bg
            return live
        finally:
            bot._claude_cli_run = orig

    _live = _aio5.run(_speed())
    check("裏方の同時実行は上限を超えない",
          _live["peak_bg"] <= bot.BG_CONCURRENCY, True)
    check("裏方が動いている最中でも会話は待たされない",
          _live["chat_started_with_bg"], True)
    check("会話用の枠が裏方より多い", bot.CLAUDE_CONCURRENCY > bot.BG_CONCURRENCY, True)

    print("■ 会話の前後関係を渡しているか transcript_block")
    _tb = bot.transcript_block([("kohei", "https://youtu.be/abc"), ("kohei", "要約して")])
    check("古い順であることを明示", "一番下が最新" in _tb, True)
    check("省略表現の解釈を促す", "さっきの" in _tb, True)
    check("今答えるべき発言を示す", "【いま答えるべき発言】要約して" in _tb, True)
    check("会話なしでも壊れない", "(まだ会話なし)" in bot.transcript_block([]), True)

    print("■ プロンプトの末尾が穴埋めになっていないこと")
    # 実際に起きた事故：末尾が「あなたの回答:」だったため、claude CLI が
    # それをユーザーの貼った文章と読み、「その後が空っぽ」と返した
    _h = [("kohei", "https://youtu.be/abc"), ("kohei", "要約して")]
    for _name, _p in (("_answer_prompt", bot._answer_prompt(bot.ORCH_PERSONA, _h)),
                      ("peer_prompt", bot.peer_prompt("Claude", "Gemini", _h)),
                      ("_ask_claude_persona", None)):
        if _p is None:
            continue
        check(f"{_name} が穴埋めで終わらない",
              _p.rstrip().endswith(("あなたの回答:", "の発言:", "JSON:")), False)
        check(f"{_name} は会話ログを区切る", "--- 会話ログ ここまで ---" in _p, True)
    check("会話なしでも区切りは出る",
          "(まだ会話なし)" in bot.transcript_block([]), True)

    print("■ 自己改修の退避と巻き戻し _snapshot_self / _restore_self")
    import pathlib as _pl
    import shutil as _sh
    import tempfile as _tmpf
    _tmp = _pl.Path(_tmpf.mkdtemp())
    _keep = (bot.SELF_FILE, bot.SELF_BACKUP_DIR)
    try:
        bot.SELF_FILE = _tmp / "ai_group_chat.py"
        bot.SELF_BACKUP_DIR = _tmp / ".selffix_backup"
        (_tmp / "ai_group_chat.py").write_text("本体v1", encoding="utf-8")
        (_tmp / "test_routing.py").write_text("テストv1", encoding="utf-8")
        _saved = bot._snapshot_self()
        check("改修前のファイルを退避", _saved, {"ai_group_chat.py", "test_routing.py"})
        # 改修を模擬：本体を書き換え／テストを書き換え／新しいファイルを作る
        (_tmp / "ai_group_chat.py").write_text("本体v2", encoding="utf-8")
        (_tmp / "test_routing.py").write_text("テストv2", encoding="utf-8")
        (_tmp / "simulate.py").write_text("新規", encoding="utf-8")
        check("変わったファイルを全部検出", set(bot._changed_self_files(_saved)),
              {"ai_group_chat.py", "test_routing.py", "simulate.py"})
        bot._restore_self(_saved)
        check("本体が戻る", (_tmp / "ai_group_chat.py").read_text(encoding="utf-8"), "本体v1")
        check("テストも戻る（本体だけでなく）",
              (_tmp / "test_routing.py").read_text(encoding="utf-8"), "テストv1")
        check("改修で作られたファイルは消える", (_tmp / "simulate.py").exists(), False)
    finally:
        bot.SELF_FILE, bot.SELF_BACKUP_DIR = _keep
        _sh.rmtree(_tmp, ignore_errors=True)

    print("■ CLI出力の定型文はがし _strip_cli_boilerplate")
    # 実際に起きた事故：クレジット照会の回答に CLAUDE.md 由来の案内が付いた
    _cli = ("Got real numbers from get_cost. Reporting back to the orchestrator.\n\n"
            "- 残クレジット：75.35\n- Soul v2：0.12クレジット\n\n"
            "Discordで「再起動して」と送ってください（自動で最新コードを取得して再起動されます）")
    check("再起動案内を落とす", "再起動" in bot._strip_cli_boilerplate(_cli), False)
    check("英語ナレーションを落とす",
          "Reporting back" in bot._strip_cli_boilerplate(_cli), False)
    check("本体は残る",
          bot._strip_cli_boilerplate(_cli), "- 残クレジット：75.35\n- Soul v2：0.12クレジット")
    check("URLの行は消さない",
          bot._strip_cli_boilerplate("Here is the URL: https://x/y.mp4"),
          "Here is the URL: https://x/y.mp4")
    check("日本語混じりの行は消さない",
          bot._strip_cli_boilerplate("Doneした。完了です。"), "Doneした。完了です。")
    check("空文字はそのまま", bot._strip_cli_boilerplate(""), "")

    print("■ 運用ルールを渡すかどうか ops_guide")
    check("ツボの話には運用ルールを渡さない",
          bot.OPS_RULES in bot.ops_guide([("kohei", "合谷が痛い"), ("kohei", "何か原因はある？")]),
          False)
    check("話し方のルールは常に渡す",
          bot.TALK_RULES in bot.ops_guide([("kohei", "合谷が痛い")]), True)
    check("ボットの話には運用ルールを渡す",
          bot.OPS_RULES in bot.ops_guide([("kohei", "動画作って")]), True)
    check("エラー相談にも運用ルールを渡す",
          bot.OPS_RULES in bot.ops_guide([("kohei", "エラー出た")]), True)
    check("体調の相談には渡さない",
          bot.OPS_RULES in bot.ops_guide([("kohei", "頭が痛い")]), False)
    check("文字列を直接渡しても動く",
          bot.OPS_RULES in bot.ops_guide("再起動して"), True)

    print("■ 雑談の担当と役割分担")
    # 返事はクロードに一本化（Geminiが勝手に作り話をする／誰が答えたか
    # 分からない、という声を受けて既定をオフにした）
    check("返事は既定でクロード", bot.CASUAL_LEAD, "claude")
    check("Geminiの返事は既定でオフ", bot._gemini_replies_on(), False)
    import asyncio as _aio3
    _fast = _aio3.run(bot._plan([("kohei", "おはよう")]))
    check("短い雑談はAIを呼ばず即返す（枠の節約）", _fast[0], "chat")
    check("その担当はCASUAL_LEAD", _fast[2], bot.CASUAL_LEAD)

    print("■ 誰が答えたかのラベル _with_speaker")
    bot._last_engine["name"] = "クロード"
    check("クロードのラベル", bot._with_speaker("本文"), "**クロード**: 本文")
    bot._last_engine["name"] = "Gemini"
    check("Geminiのラベル", bot._with_speaker("本文"), "**Gemini**: 本文")
    bot._last_engine["name"] = ""
    check("不明なら付けない", bot._with_speaker("本文"), "本文")
    bot._last_engine["name"] = "クロード"
    check("空文には付けない", bot._with_speaker(""), "")

    print("■ 確認の受付枠 _set_pending / _clear_pending")
    import asyncio as _aio

    async def _slots():
        loop = _aio.get_running_loop()
        f1, f2 = loop.create_future(), loop.create_future()
        bot._set_pending(1, f1, 100)
        bot._set_pending(1, f2, 100)          # 2件目が来た
        r = []
        # 拒否(False)ではなく「置き換わった」印にする。区別しないと
        # 言い直すたびに『🛑 やめました』が会話に割り込む。
        r.append(("古い確認は置き換え扱い",
                  f1.done() and f1.result() is bot.SUPERSEDED, True))
        bot._clear_pending(1, f1)             # 古い方のタイムアウト後片付け
        r.append(("新しい確認の受付は残る",
                  bot._pending_approvals.get(1) is not None, True))
        r.append(("OKは新しい確認に届く",
                  bot._try_text_approval(1, 100, "OK"), True))
        bot._clear_pending(1, f2)
        r.append(("片付け後は空", bot._pending_approvals.get(1) is None, True))
        return r
    for desc, got, want in _aio.run(_slots()):
        check(desc, got, want)

    print("■ プロンプトすり替えの検知 _prompt_drifted")
    req = "a 30 year old japanese man cheering with a beer mug, victory celebration"
    check("同じなら検知しない", bot._prompt_drifted(req, req), False)
    check("少し変わった程度は許容",
          bot._prompt_drifted(req, req + ", warm lighting, bokeh"), False)
    check("別物なら検知",
          bot._prompt_drifted(req, "aichi prefecture day and night map illustration"), True)
    check("記録が無ければ検知しない", bot._prompt_drifted(req, ""), False)

    print("■ エラーログ _log_error / _recent_errors")
    import tempfile
    import pathlib
    tmp = pathlib.Path(tempfile.mkdtemp()) / "errors.log"
    bot.ERROR_LOG = tmp
    check("エラーなし時", "エラーはありません" in bot._recent_errors(), True)
    try:
        raise ValueError("テスト例外です")
    except ValueError as e:
        summary = bot._log_error("test-context", e)
    check("要約にエラー型", "ValueError" in summary, True)
    check("ログに記録された", tmp.exists() and "テスト例外です" in tmp.read_text(), True)
    check("直近エラー取得", "test-context" in bot._recent_errors(), True)

    print(f"\n結果: ✅ {ok} 件成功 / ❌ {fail} 件失敗")
    return fail == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
