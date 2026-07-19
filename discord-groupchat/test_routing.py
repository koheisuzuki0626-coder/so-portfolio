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
    # ショート量産
    ("ショート作って", "short", {}),
    ("今日のショートお願い", "short", {}),
    ("アートなショート動画作って", "short", {}),
    ("shorts作って", "short", {}),
    # モデル指定生成
    ("seedanceで犬の動画作って", "hf_model", {}),
    ("veoで夕焼け作りたい", "hf_model", {}),
    ("ナノバナナでロゴ生成して", "hf_model", {}),
    ("クリング3で猫が踊る動画作って", "hf_model", {}),
    # 自動選定
    ("おまかせで犬の動画作って", "hf_auto", {}),
    ("最適なモデルで海の動画生成して", "hf_auto", {}),
    ("これ動かして", "hf_auto", {"has_image_att": True, "has_attachments": True}),
    # モーション
    ("この動きで生成して", "motion_ask", {}),
    ("モーションコントロールで作りたい", "motion_ask", {}),
    ("この動きで生成して", "motion",
     {"has_video_att": True, "has_attachments": True}),
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

    print("■ プロンプト英語判定 _looks_english_prompt")
    check("日本語→False", bot._looks_english_prompt("犬が走る動画"), False)
    check("英語→True", bot._looks_english_prompt("a running dog, cinematic, 9:16"), True)
    check("会話文→False", bot._looks_english_prompt("もう一回作り直して"), False)

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
