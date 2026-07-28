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
