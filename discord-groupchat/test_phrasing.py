"""言い回しテスト：同じ意図を「人が実際に打つ色々な言い方」で通じるか検証する。
使い方:  python3 test_phrasing.py

test_routing.py が「仕様どおりか」を見るのに対し、こちらは
「ユーザーが言いそうな表現を取りこぼしていないか」を見る。
不具合報告のたびに直す（後手）のをやめ、先回りして穴を潰すためのファイル。
新しい言い方で通じなかったことがあれば、必ずここに1行足すこと。
"""
import sys

import test_routing as _stubs  # noqa: F401  （依存のスタブ設置＋botの読み込み）
from test_routing import bot

LAST = {"has_last_gen": True}
JOB = {"has_job": True}
VIDEO_ATT = {"has_video_att": True, "has_attachments": True}
IMAGE_ATT = {"has_image_att": True, "has_attachments": True}

# (発言, 期待ルート, フラグ)
CASES = [
    # ---- 動画を作ってほしい ----
    ("犬が走ってる動画作って", "hf_auto", {}),
    ("かっこいい映像が欲しい", "hf_auto", {}),
    ("夕暮れの海の動画お願い", "hf_auto", {}),
    ("なんか動画つくって", "hf_auto", {}),
    ("30秒くらいの映像作りたい", "hf_auto", {}),
    ("動画1本お願いします", "hf_auto", {}),
    ("バズりそうな動画作って", "hf_auto", {}),
    ("おまかせで動画作成して", "hf_auto", {}),
    # ---- 画像を作ってほしい ----
    ("猫の写真作って", "image", {}),
    ("ロゴをデザインして", "image", {}),
    ("イラスト描いて", "image", {}),
    ("アイコン作ってほしい", "image", {}),
    ("サムネイル作って", "image", {}),
    ("画像が欲しい", "image", {}),
    # ---- 作り直し（内容を変えて再生成）----
    ("もうちょい明るくして", "revise", LAST),
    ("さっきのやつ作り直して", "revise", LAST),
    ("いまいちだからやり直して", "revise", LAST),
    ("もう一回お願い", "revise", LAST),
    ("もう少し可愛い感じにして", "revise", LAST),
    # ---- 編集（できた動画への後工程）----
    ("字幕入れて", "edit", LAST),
    ("もっと短くして", "edit", LAST),
    ("テロップ足して", "edit", LAST),
    ("縦にして", "edit", LAST),
    ("15秒にして", "edit", LAST),
    ("音量下げて", "edit", LAST),
    ("この動画つなげて", "edit", VIDEO_ATT),
    # ---- 状況を知りたい ----
    ("できてる？", "status", LAST),
    ("まだかな", "status", JOB),
    ("進捗どう", "status", JOB),
    ("動画できた？", "status", {}),
    # ---- ログを共有してほしい ----
    ("ログちょうだい", "sharelog", {}),
    ("さっきのログ送っといて", "sharelog", {}),
    ("デバッグログ共有して", "sharelog", {}),
    # ---- 参考動画を覚えてほしい ----
    ("これ参考にして", "style_learn", VIDEO_ATT),
    ("これを学習して", "style_learn", VIDEO_ATT),
    # ---- 広告 ----
    ("うちの店の広告作って", "ad", {}),
    ("新商品のCM作りたい", "ad", {}),
    # ---- 自分のチャンネルの実績 ----
    ("チャンネル登録して https://www.youtube.com/@mych", "ch_set", {}),
    ("実績分析して", "ch_stats", {}),
    ("うちのチャンネルの再生数どうなってる", "ch_stats", {}),
    ("投稿した動画の成績レポート出して", "ch_stats", {}),
    ("バズる動画作って、あとで分析して", "hf_auto", {}),   # 制作が主目的
    ("バズ度分析して", "virality", {}),                    # 単体動画の予測
    # ---- 複数視点（クロード1＝情報収集／クロード3＝多角的視点）----
    ("クロード1に聞いて", "multiview", {}),
    ("クロード3はどう思う", "multiview", {}),
    ("多角的に見て", "multiview", {}),
    ("いろんな視点でこの企画見て", "multiview", {}),
    ("クロード1と3で検討して", "multiview", {}),
    ("両面から考えて", "multiview", {}),
    ("リサーチャーに聞いて", "multiview", {}),
    ("アドバイザーはどう思う", "multiview", {}),
    ("トレンドをリサーチして", "plan", {}),      # 調査依頼は役の呼び出しではない
    # ---- ただの会話（作業を誤発動してはいけない）----
    ("クロードどう思う", "plan", {}),          # 役の名指しでなければ通常会話
    ("3本作って", "plan", {}),                # 数字の3は役と無関係
    ("今日つかれた", "plan", {}),
    ("動画編集って難しいの？", "plan", {}),
    ("画像生成の仕組み教えて", "plan", {}),
    ("どんな動画が伸びると思う？", "plan", {}),
    ("ありがとう助かった", "plan", {}),
    ("愛知県ってどんなとこ？", "plan", {}),
    ("字幕ってどうやってつけるの？", "plan", LAST),
    ("さっきの動画よかったよ", "plan", LAST),
    ("動画の作り方が知りたい", "plan", {}),
    # 料金・条件を聞いているだけ（作業を誤発動してはいけない）
    ("veo3.1生成するときクレジットいくらくらいか聞きたいだけ", "plan", {}),
    ("veo3で動画作ると何クレジット？", "plan", {}),
    ("画像生成の料金っていくら", "plan", {}),
    ("動画作るのにいくらかかるか知りたい", "plan", {}),
    ("sora2って有料なの", "plan", {}),
    ("veo3で犬の動画作って", "hf_model", {}),      # 依頼ならモデル指定は効く
    ("合谷が痛い", "plan", {}),          # 体の話：ログ共有を誤発動しない
    ("何か原因はある？", "plan", {}),
    ("寝不足かな", "plan", {}),
]

# 確認への返事（自然な言い方で通じること）
REPLY_CASES = (
    [(t, True) for t in (
        "OK", "おけ", "おっけー", "はい", "うん", "いいよ", "いいね", "了解",
        "了解です", "それで", "それでお願い", "そのままお願いします", "これでいい",
        "進めて", "やって", "やろう", "お願いします", "よろしく", "許可", "GO",
    )]
    + [(t, False) for t in (
        "やめて", "やめ", "違う", "ちがう", "拒否", "キャンセル", "no", "いらない",
        "中止して", "やっぱやめて", "だめ", "ストップ", "結構です",
    )]
    + [(t, None) for t in (
        "猫の動画作って", "これどう思う？", "おはよう", "ログ送って",
        "愛知県っていうのを分かりやすく",
    )]
)


def run():
    ok = fail = 0

    def check(desc, got, want):
        nonlocal ok, fail
        if got == want:
            ok += 1
        else:
            fail += 1
            print(f"  ❌ {desc}\n      期待={want} 実際={got}")

    print("■ 言い回し → ルーティング")
    for text, want, flags in CASES:
        check(f"{text!r}", bot.classify_route(text, **flags) or "plan", want)

    print("■ 確認への返事（承認/拒否/それ以外）")
    import asyncio

    async def _replies():
        loop = asyncio.get_running_loop()
        out = []
        for text, want in REPLY_CASES:
            fut = loop.create_future()
            bot._set_pending(777, fut, 1)
            out.append((f"{text!r}", bot._try_text_approval(777, 1, text), want))
            bot._clear_pending(777, fut)
        return out
    for desc, got, want in asyncio.run(_replies()):
        check(desc, got, want)

    print(f"\n結果: ✅ {ok} 件成功 / ❌ {fail} 件失敗")
    return fail == 0


if __name__ == "__main__":
    sys.exit(0 if run() else 1)
