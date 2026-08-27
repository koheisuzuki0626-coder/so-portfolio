"""日本語の言い回しを読む層（純粋関数のみ・状態を持たない）。

ここにあるのは「その発言が依頼の形か」「作り直しの指示か」「質問か」
「何を作るかが書かれているか」といった、【文字列を見るだけ】で決まる判定と、
その材料になる正規表現。ボットの状態（設定・履歴・生成の記録）には
一切触らないので、ここだけ読めば言い回しの判定は追える。

ai_group_chat.py から `from phrasing import *` で取り込む。
アンダースコア始まりの名前も渡すため __all__ を明示している。

※このリポジトリの決まり（CLAUDE.md）に従い、判定は可能な限り
  「言い方の数え上げ」ではなく「文法の形」で書くこと。
"""
import os
import re

__all__ = [
    "DESIGN_SIZES",
    "ICLOUD_ROOT",
    "REVISE_MARK",
    "_ASK_INFO_RE",
    "_BIDI_RE",
    "_BY_CLAUDE_RE",
    "_BY_GEMINI_RE",
    "_BY_HF_RE",
    "_CHANGE_VERB_RE",
    "_DESIGN_TWEAK_RE",
    "_DIAGRAM_RE",
    "_ENGINE_WORD_RE",
    "_EXPLAIN_Q_RE",
    "_IMAGE_WORD_RE",
    "_IOS_CRUMB_RE",
    "_NEGATION_RE",
    "_NOTE_FAILED_RE",
    "_NOTE_INSIGHT_RE",
    "_NOTE_KIND_RE",
    "_NOT_NOW_RE",
    "_NOT_THAT_RE",
    "_ORDER_RE",
    "_QUESTION_END_RE",
    "_QUESTION_RE",
    "_REQ_VERB_RE",
    "_RESULT_COMPLAINT_RE",
    "_REVISE_ADD_RE",
    "_REVISE_STRONG_RE",
    "_REVISE_WEAK_RE",
    "_SELF_DONE_RE",
    "_STRONG_ORDER_RE",
    "_USER_REPORT_RE",
    "_VAGUE_FIX_RE",
    "_VIDEO_WORD_RE",
    "_base_request",
    "_design_size",
    "_has_subject",
    "_ios_files_path",
    "_looks_english_prompt",
    "_looks_like_question",
    "_looks_revise",
    "_mmss_to_sec",
    "_note_kind",
    "_said_media",
    "_stack_revise",
    "_strip_engine_words",
    "_strip_media_context",
    "_wants_action",
]

# どのノートに入れるかの言い方。頭に付ける形だけを見る（本文と混ざらない）。
_NOTE_KIND_RE = re.compile(
    r"^\s*(記録して|記録|メモして|メモ|実験(ログ|メモ)|"
    r"知見(メモ|)|失敗(メモ|の記録)|効かなかった)\s*[:：]?\s*", re.I)
_NOTE_INSIGHT_RE = re.compile(r"^\s*知見")
_NOTE_FAILED_RE = re.compile(r"^\s*(失敗|効かなかった)")
def _note_kind(text):
    """『記録して 〜』の形か。(種別, 本文) か None。
    頭に付いている時だけ拾う。文中に「メモ」が出ただけでは反応しない。"""
    t = (text or "").strip()
    m = _NOTE_KIND_RE.match(t)
    if not m:
        return None
    body = t[m.end():].strip()
    if not body:
        return None                       # 中身が無ければ記録しない
    # 「実験ログ見せて」は読み返し。記録として保存しない
    if re.fullmatch(r"(を)?(見せて|みせて|見たい|出して|教えて|確認|表示)[。！!]*",
                    body):
        return None
    head = m.group(0)
    if _NOTE_INSIGHT_RE.match(head):
        return "insight", body
    if _NOTE_FAILED_RE.match(head):
        return "failed", body
    return "experiment", body
def _mmss_to_sec(v):
    """"mm:ss" / "hh:mm:ss" / 数値 を秒に。読めなければ None。"""
    if isinstance(v, (int, float)):
        return float(v)
    parts = str(v or "").strip().split(":")
    try:
        nums = [float(x) for x in parts]
    except ValueError:
        return None
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None
# iPhoneの「ファイル」アプリから貼られる道順を、Mac上の実際のパスに変える。
# 実例:「⁨iCloud Drive⁩ ▸ ⁨マルサヂ⁩ ▸ ⁨AI⁩ ▸ ⁨動画⁩の中の武士道….mp4」
# iCloud DriveはMacにも同期されているので、Mac側のパスを組み立てれば扱える。
# これが無いと「iCloudの動画は取りに行けない」と断って終わっていた。
ICLOUD_ROOT = "~/Library/Mobile Documents/com~apple~CloudDocs"
_BIDI_RE = re.compile("[\u2066-\u2069\u200e\u200f]")
_IOS_CRUMB_RE = re.compile(
    r"(iCloud\s*Drive|このiPhone内|iPhone内)((?:\s*▸\s*[^▸]+)+)", re.I)
def _ios_files_path(text):
    """iPhoneのファイルアプリの道順 → Macのパス。読めなければ空。
    最後の区切りのあとは「動画の中の◯◯.mp4」のように
    フォルダ名とファイル名が助詞でつながっているので、そこで分ける。"""
    t = _BIDI_RE.sub("", text or "")
    # iPhoneから貼ると折り返しの改行が入る。「動画\nの中の◯◯.mp4」のまま
    # 読むとフォルダ名が落ち、実際に一階層ぶん違うパスを組み立てていた。
    t = re.sub(r"\s*\n\s*", "", t)
    t = t.replace("▸", "▸").replace("→", "▸").replace(">", "▸")
    m = _IOS_CRUMB_RE.search(t)
    if not m:
        return ""
    segs = [x.strip() for x in m.group(2).split("▸") if x.strip()]
    if not segs:
        return ""
    tail = segs.pop()
    # 「動画の中の武士道….mp4」→ フォルダ「動画」＋ ファイル「武士道….mp4」
    sp = re.search(r"(.*?)(?:の中の|の中に|内の|フォルダの)(.+)$", tail)
    if sp:
        folder, name = sp.group(1).strip(), sp.group(2).strip()
        if folder:
            segs.append(folder)
    else:
        name = tail
    # ファイル名の後ろに続く依頼文（「をショート動画にして」等）を落とす
    m2 = re.search(r"^(.+?\.(?:mp4|mov|m4v|webm|mkv))", name, re.I)
    if not m2:
        return ""
    name = m2.group(1)
    base = ICLOUD_ROOT if re.search("icloud", m.group(1), re.I) else "~"
    return os.path.expanduser("/".join([base, *segs, name]))
# AI判定が必要そうなキーワード（作業指示・生成依頼・検索・過去記憶）。
# これに全く該当しない短い発言は、AIを呼ばず雑談として即処理（Gemini無料枠の節約）。
# 「実際にやって」と頼んでいる形か。会話のテンションの発言を作業にしないための門。
# 事故：「クロードコードって便利だよね」のような雑談から、コード修正の
# 実行プラン（承認ダイアログ）が立ち上がっていた。話題に出しただけの語を
# 依頼と取り違えないよう、依頼の【形】を要求する。
# 依頼の「文法」。動詞を並べて当てる方式では、言い方を1つ増やすたびに
# 取りこぼす。日本語で人にものを頼む形そのものを見る。
_ORDER_RE = re.compile(
    # 〜て / 〜てください / 〜てほしい / 〜てくれる？ / 〜といて
    # 〜て（文末でも、読点や続きがあっても依頼は依頼）
    "[ぁ-んァ-ヶ一-龥ー]て(ください|下さい|ほしい|欲しい|くれ|くんない|"
    "もらえ|いただけ|ね|よ|)[。、,!！?？]*($|[^いるたなかもの])|"
    "[ぁ-んァ-ヶ一-龥ー]て(ください|下さい|ほしい|欲しい|くれ|)[。、!！?？]*$|"
    "て(ください|下さい|ほしい|欲しい|くれる|くれない|もらえる|もらえない)|"
    "といて|とい(て|た)|ておいて|"
    # 〜たい / 〜が欲しい（日本語では依頼として通じる）
    "たい(な|んだけど|んだよね|)[。、!！?？]*$|たいんだけど|"
    "(が|を|)(欲しい|ほしい)|"
    # 言い切りの依頼
    "お願い|頼む|頼める|やって|やろう|"
    # 意向形（〜ろう/〜よう＝「一緒にやろう」）。「だろう」「どうしよう」の
    # ような推量・不安の表現まで拾わないよう、動詞を絞って列挙する。
    # 事故：「まず1枚目を作ろう」が依頼として拾われず、会話に落ちて
    # 実際にはファイルを書けない経路で処理された（2026-08-20）。
    "作ろう|組もう|始めよう|進めよう|描こう|書こう|"
    # 「〜っか」（やろっか＝やろうか）と「〜ましょう」。
    # 事故（2026-08-21）：「一枚目やろっか」が依頼と認識されず会話に落ちた。
    "やろっか|作ろっか|つくろっか|やりましょう|作りましょう|始めましょう|"
    # 開始の指示（体言止めの命令）。「制作開始」「作成開始」は依頼そのもの。
    "制作開始|作成開始|生成開始|着手して|"
    # 命令形
    "(作|直|変|消|足|入|出|送|見せ|調べ|止め|上げ|下げ)れ[。、!！]*$"
)
# はっきり頼んでいる形。これがあれば、説明を求める言い方や
# 「〜って」を含んでいても依頼として通す。
# 事故：「相関図の作り方を踏まえて相関図作って」が、文中の「の作り方」だけを見て
# 説明の質問と判定され、制作が始まらなかった。
_STRONG_ORDER_RE = re.compile(
    "ください|下さい|ほしい|欲しい|お願い|頼む|頼める|"
    "て(くれ|くんない|もらえ|いただけ|ちょうだい)|"
    "といて|ておいて|やって|やっとい|"
    # 文末の「〜て」（「相関図作って」「コード直して」）
    "[ぁ-んァ-ヶ一-龥ー]て[。、!！]*$"
)
# 質問の終わり方。頼んでいるのではなく聞いている。
_QUESTION_END_RE = re.compile("[?？]$|の[?？]?$|ですか[。?？]*$|ますか[。?？]*$")
# 何を直すのかが書かれていない修正の希望。道具の名前しか書かれていない
# 「クロードコードで直したい」で、対象不明のまま確認画面を出さないため。
_VAGUE_FIX_RE = re.compile(
    "^(直し|なおし|修正し|変更し|変え|改善し|いじり|触り)たい[。！!]*$")
def _wants_action(text):
    """『いま実際にやって』と頼まれている形か。
    プロンプトで「迷ったらchat」と書いても守られなかったので、
    最終判断はコード側で持つ（このリポジトリの決まり）。"""
    t = _strip_media_context(text or "").strip()
    if not t:
        return False
    # 「顔が違うので、鼻だけ変えてください」は打ち消しではなく直しの依頼。
    # 「違う」を打ち消しとして先に弾くと、直しの指示が会話に落ちる。
    if _RESULT_COMPLAINT_RE.search(t) and _CHANGE_VERB_RE.search(t):
        return True
    if (_NEGATION_RE.search(t) or _USER_REPORT_RE.search(t)
            or _NOT_NOW_RE.search(t)):
        return False                      # 打ち消し・お礼・継続のお願い
    # はっきり頼んでいない時だけ、話題として触れただけの言い方を落とす。
    # 「〜って便利だよね」「〜って使ってる？」の「って」は引用の助詞であって
    # 依頼の「〜て」ではない。独り言（〜わ／〜かな）も、質問も依頼ではない。
    if not _STRONG_ORDER_RE.search(t):
        if _EXPLAIN_Q_RE.search(t):
            return False
        if re.search("(よね|だよね|かな|のかな|わ|っけ)[。、!！?？]*$", t):
            return False
        if _QUESTION_END_RE.search(t):
            return False
        if _VAGUE_FIX_RE.search(_strip_engine_words(t)):
            return False
    # 「ヒッグスフィールドで」「クロードで」だけの言い直しは、
    # 直前の依頼の作り手を変える指示。文法上は依頼の形をしていない。
    if not _has_subject(t) and (_BY_HF_RE.search(t) or _BY_CLAUDE_RE.search(t)
                                or _BY_GEMINI_RE.search(t)):
        return True
    return bool(_ORDER_RE.search(t))
# ユーザー自身が「できた／うまくいった」と報告・お礼を言っている発言。
# 進捗の質問ではないので、完成物を出し直してはいけない。
# 実際に「できた！ありがとー」に対して直近のデザインを貼り直す事故が起きた。
_USER_REPORT_RE = re.compile(
    "ありがと|あんがと|助かった|たすかった|嬉しい|うれしい|よかった|良かった|"
    "できた[！!♪〜ー。]|できたよ|できたわ|できました|うまくいった|うまく行った|"
    "いけた|やった[！!]|完成した[！!]|投稿できる|使えそう"
)
# ユーザー自身の作業が終わったことの報告。お礼（_USER_REPORT_RE）とは別で、
# 「作成まで終わった」「登録した」のような手順の完了を伝える言い方。
# スクショを添えて報告することが多いが、その添付は素材ではないので、
# これに当たる発言を「これで何か作って」と読んではいけない。
_SELF_DONE_RE = re.compile(
    r"(まで)?\s*(終わ(った|りました|り)|終了|完了|済(んだ|みました|んでる)|"
    r"できた|出来た|できました|登録した|設定した|作成した|作った|"
    r"入れた|入力した|やってみた|やった)\s*[。、!！]?$"
)
# 否定の言い回し。「〜してないよ」を指示と読まないための共通ガード。
_NEGATION_RE = re.compile("してない|してません|じゃな|ではな|違う|ちがう|ないよ|ないです")
# デザインの手直しで実際に触る「部位・見た目」の語。
# これが無い発言を手直しとみなすと、「親が入院しろって言ってくる」の
# 「しろ」まで変更指示に見えてしまう（実際に制作が始まる事故が起きた）。
_DESIGN_TWEAK_RE = re.compile(
    "文字|フォント|書体|字|色|カラー|背景|レイアウト|余白|間隔|行間|字間|"
    "サイズ|大きく|小さく|太く|細く|明るく|暗く|濃く|薄く|派手|地味|"
    "位置|配置|中央|真ん中|左|右|上|下|寄せ|揃え|"
    "線|枠|箱|矢印|囲み|影|グラデ|タイトル|見出し|ラベル|凡例|"
    "全体|バランス|雰囲気|トーン|デザイン|画像|図"
)
# 「クロードでサムネ作って」「geminiでサムネ作って」のように作り手を名指しする言い方。
# クロード＝HTMLで組む（文字が正確）／Gemini＝画像生成（絵が得意）。
# 同じ「サムネ」でも、どちらに投げたいかは本人にしか決められないので明示を優先する。
_BY_CLAUDE_RE = re.compile(
    r"(クロード|claude|くろーど|html)\s*(?:で|に|を使って|使って|側で)", re.I)
_BY_GEMINI_RE = re.compile(
    r"(gemini|ジェミニ|じぇみに)\s*(?:で|に|を使って|使って|側で)", re.I)
_BY_HF_RE = re.compile(
    r"(ヒッグスフィールド|ヒッグス|higgsfield|hf)\s*(?:で|に|を使って|使って|側で)",
    re.I)
# 作り手の指定だけを取り除くための語（依頼の中身と混ぜない）。
# 事故：「ヒッグスフィールドで作って」が題材として読まれ、
# "Higgs boson field visualized as an endless dark cosmic void..." という
# 素粒子物理の画像が生成された。指定語は題材ではない。
_ENGINE_WORD_RE = re.compile(
    # 「クロードコード」は道具の名前。「クロード」だけ落とすと「コード」が
    # 依頼の中身として残り、「クロードコードで直したい」が
    # 『コードを直す依頼』に見えてしまう。長い名前から先に落とす。
    r"(ヒッグスフィールド|ヒッグス|higgsfield|クロードコード|claude\s*code|"
    r"クロード|claude|くろーど|"
    r"gemini|ジェミニ|じぇみに|html)\s*(?:で|に|を使って|使って|側で)?",
    re.I)
_REQ_VERB_RE = re.compile(
    "作って|作成して|生成して|つくって|描いて|お願いします|お願い|ください|"
    "してほしい|して欲しい|やって|して")
def _strip_engine_words(text):
    """『ヒッグスフィールドで作って』から作り手の指定と依頼の動詞を落として、
    依頼の【中身】だけを残す。空になれば中身は書かれていない、と判断できる。"""
    t = _ENGINE_WORD_RE.sub("", text or "")
    t = _REQ_VERB_RE.sub("", t)
    return re.sub(r"[\s、。,.！!？?でにをがはもの]+", "", t)
def _has_subject(text):
    """何を作るのかが、その発言自体に書かれているか。"""
    return len(_strip_engine_words(text)) >= 2
_IMAGE_WORD_RE = re.compile("画像|イラスト|ロゴ|絵|写真|アイコン|サムネ|静止画")
_VIDEO_WORD_RE = re.compile("動画|映像|ムービー|クリップ|ショート")
_NOT_THAT_RE = "(じゃなくて|ではなく|でなく|じゃなく|ではない|じゃない)"
def _said_media(text):
    """発言が媒体をはっきり指しているか。指していなければ None。
    「動画の生成じゃなくて画像の生成にして」は【画像】。打ち消しを先に見る。"""
    t = text or ""
    # 「動画の生成じゃなくて画像の生成にして」のように、語と打ち消しの間に
    # 「の生成」が挟まる。間を許して見る。
    if re.search(f"(動画|映像|ムービー)[^。、]{{0,6}}{_NOT_THAT_RE}", t):
        return "image"
    if re.search(f"(画像|静止画|写真)[^。、]{{0,6}}{_NOT_THAT_RE}", t):
        return "video"
    has_i, has_v = _IMAGE_WORD_RE.search(t), _VIDEO_WORD_RE.search(t)
    if has_i and not has_v:
        return "image"
    if has_v and not has_i:
        return "video"
    return None
REVISE_MARK = "【今回の修正指示】"
def _base_request(prompt):
    """積み上がった依頼文から、元の依頼だけを取り出す。
    事故：作り直しのたびに指示が継ぎ足され、
    「背景を室内に変えて 【今回の修正指示】クロードで 【今回の修正指示】クロードでや」
    のようになって、本来の依頼（2枚の写真を組み合わせて）が消えていた。"""
    return (prompt or "").split(REVISE_MARK)[0].strip()
def _stack_revise(prev, instruction):
    """前回の依頼に今回の直しを足す。積み上げは1段までに抑える。"""
    base = _base_request(prev)
    return f"{base}\n{REVISE_MARK}{instruction}" if base else instruction
_QUESTION_RE = re.compile(
    "どう思う|なんで|なぜ|どうやって|できる\\?|できる？|作れる|入れられる|"
    "って何|とは|意味|進捗|どうなって|してもいい|でもいい|と思う|かな|"
    "教えて|仕組み|方法|やり方|どうやって|違いは|どっちが"
)
# 「作ってほしい」ではなく「聞きたいだけ」の言い方。
# 実例:「veo3.1生成するときクレジットいくらくらいか聞きたいだけ」で生成が始まった。
# 料金・条件を知りたいだけの質問は、生成の意図でも作業の依頼でもない。
_ASK_INFO_RE = re.compile(
    "聞きたい|聞くだけ|訊きたい|知りたい|確認したいだけ|"
    "いくら(?!でも)|幾ら|何円|なん円|"
    # 「価格表」「料金表」は作るものの名前なので、料金の質問と取り違えない
    "料金(?!表)|価格(?!表)|値段|費用|相場|コスト(?!を?(下げ|抑え|削減))|"
    # 「無料で動画作って」は依頼なので、断定を聞く形のときだけ質問扱いにする
    "(無料|有料)(なの|ですか|かな|か？|か\\?|？|\\?)|"
    "どれくらいかかる|どのくらいかかる|どれぐらいかかる|どのくらい必要|"
    "何クレジット|なんクレジット|クレジット(は|って|を)?(いくら|どれ|どの|何|なん)"
)
# 「〜って何？」「〜ってどうやるの？」＝ものの説明を求める質問。
# 名前が出ただけで機能を起動しない（普通の会話のテンションを守るための門）。
# 実例:「実績ってどうやって見るの」で実績分析が、
#      「クロード3ってどんな役割？」で複数視点の呼び出しが走っていた。
_EXPLAIN_Q_RE = re.compile(
    "って(何|なに|どう|どんな|する人|できる|使う|やる|便利|すごい|いい|難しい|大事)|"
    "とは(何|なに|\?|？|$)|"
    "の(意味|仕組み|やり方|使い方|作り方|直し方|調べ方|コツ|違い|役割)|"
    "どうやって(見る|やる|する|作る|調べる|使う|出す)|"
    "どういう(こと|意味|仕組み|もの|仕事|役割)"
)
# 「今やって」ではない言い方。継続のお願い・打ち消し・前置きなど。
_NOT_NOW_RE = re.compile(
    "これからも|今後も|引き続き|次回から|次からも|そのうち|いずれ|"
    "指示じゃな|依頼じゃな|命令じゃな|お願いじゃな|やらなくていい|"
    "しなくていい|今はいい|今じゃなくて|今すぐじゃな|まだいい"
)
def _looks_like_question(text):
    """質問・相談っぽい発言か（作業命令ではない）。作業系ルートの誤爆を防ぐ。"""
    text = text or ""
    return bool(text.rstrip().endswith(("？", "?"))
                or _QUESTION_RE.search(text)
                or _ASK_INFO_RE.search(text))
def _looks_english_prompt(text):
    """既に英語の生成プロンプトっぽいか（日本語をほぼ含まない）。"""
    jp = len(re.findall(r"[ぁ-んァ-ン一-龯]", text))
    return jp <= 2
# 「前の生成を修正して作り直す」意図の検出（明確なマーカーのみ）
# 作り直しの検出は2段構え。
# 「さっきの動画」「もう少し」だけでは判断できない（『さっきの動画よかったよ』は
# ただの感想）。それ単体で作り直しと分かる言い方と、変更を求める語とセットで
# 初めて作り直しになる言い方を分けている。
_REVISE_STRONG_RE = re.compile(
    "もう一回|もう一度|もっかい|作り直|作りなお|やり直|やりなお|"
    "同じの|別バージョン|別ver|修正して"
)
# 「足してほしい」系。直前の生成がある時だけ作り直しになる
# （「機能を追加して」のような、生成物と関係ない依頼を巻き込まないため）。
# 事故：「まだ出てきてない武将も追記してくれる？」が『まだ』だけを拾われて
# 状態確認になり、完成済みのURLを貼り直して終わった。
_REVISE_ADD_RE = re.compile(
    "追記|付け足|書き足|足してくれ|足して欲しい|足してほしい|"
    "追加して|入れて欲しい|入れてほしい|入れてくれ|載せて|加えて"
)
_REVISE_WEAK_RE = re.compile(
    "さっきの(動画|画像|映像|やつ|の)|前の(動画|画像|映像|やつ)|"
    # 生成物に返信して「この画像の背景を室内にして」と言うのは、
    # どれを直すかの最も明確な指定。これを拾えず会話に落ちていた。
    "この(画像|写真|動画|映像|やつ)|それの|これの|"
    "少し変えて|ちょっと変えて|もうちょい|もうちょっと|もう少し|もっと"
)
# 「めて」は褒めて・決めて・まとめて・やめて…と当たりが広すぎた。
# 「もっと褒めて笑」が画像の作り直しになった原因のひとつ。
_CHANGE_VERB_RE = re.compile(
    "して|しろ|変えて|かえて|直して|なおして|くして|にして|"
    "してほしい|して欲しい|できる\?|できる？|"
    # 実例：「サムネイルに写真を組み込んで欲しかった」が直しの指示として
    # 拾えず、会話に落ちていた。「〜て＋ほしい/ほしかった」も直しの指示。
    # 「組み込んで欲しかった」は「んで」＝濁ったて形。でも拾う。
    "[てで](ほしかった|欲しかった|ほしい|欲しい|もらいたい|くれれば|くれたら)"
)
# 出来上がりが違う、という訴え。直しの指示とセットで作り直しになる。
_RESULT_COMPLAINT_RE = re.compile(
    "違う|ちがう|別人|イメージと|思ってたのと|想像と|そうじゃな|"
    "似てな|なってない|おかしい"
)
def _looks_revise(content, has_last_gen=True):
    """『前の生成を作り直したい』発言か。
    はっきり作り直しと分かる言い方（作り直して・もう一回）だけは、
    記録が無くてもHiggsfieldから前のプロンプトを回収できるので通す。
    それ以外の曖昧な言い方は【直前の生成がある時だけ】。
    事故：「もっと褒めて笑」が4日前の人物画像の作り直しになり、
    『肌・髪の描写に艶やかで美しい的な賛辞を追加』という修正プランが出た。"""
    if _REVISE_STRONG_RE.search(content):
        return True
    if not has_last_gen:
        return False
    if _REVISE_ADD_RE.search(content):
        return True
    # 「顔が違うので、鼻の高さだけ変えて…」のような、出来上がりへの
    # 不満＋直しの指示。以前は進捗確認に化けて指示が消えていた。
    if _RESULT_COMPLAINT_RE.search(content) and _CHANGE_VERB_RE.search(content):
        return True
    if _REVISE_WEAK_RE.search(content) and _CHANGE_VERB_RE.search(content):
        return True
    # 「背景が宇宙になってるから自然な背景にして」のように、
    # 出来上がりの【部位】を名指しして直しを頼む言い方。
    # 直前の生成がある時だけなので、普通の会話には効かない。
    return bool(_DESIGN_TWEAK_RE.search(content)
                and _CHANGE_VERB_RE.search(content)
                and not _looks_like_question(content)
                and not _USER_REPORT_RE.search(content))
DESIGN_SIZES = {
    "thumbnail": (1280, 720, "YouTubeサムネイル"),
    "short": (1080, 1920, "縦型（ショート/ストーリー）"),
    "square": (1080, 1080, "正方形（SNS）"),
    "slide": (1920, 1080, "スライド/資料"),
    "a4": (1240, 1754, "A4チラシ"),
    "diagram": (1600, 1200, "図（相関図・年表など）"),
}
# 線と箱で構造を見せるもの。写真的な絵ではないので描き方の指示を変える。
_DIAGRAM_RE = re.compile(
    "相関図|関係図|家系図|系図|系統図|組織図|構成図|フローチャート|チャート|"
    "図表|図解|ダイアグラム|マインドマップ|ロードマップ|年表|タイムライン")
def _design_size(text):
    """発言から仕上がりサイズを決める。既定はYouTubeサムネイル。"""
    t = text or ""
    if re.search("縦型|縦長|ショート|ストーリー|リール|9:16|tiktok", t, re.I):
        return DESIGN_SIZES["short"]
    if _DIAGRAM_RE.search(t):
        return DESIGN_SIZES["diagram"]
    if re.search("正方形|スクエア|1:1|インスタ|instagram", t, re.I):
        return DESIGN_SIZES["square"]
    if re.search("スライド|資料|プレゼン|16:9の資料|発表", t):
        return DESIGN_SIZES["slide"]
    if re.search("チラシ|フライヤー|ポスター|A4|a4|印刷", t):
        return DESIGN_SIZES["a4"]
    return DESIGN_SIZES["thumbnail"]
def _strip_media_context(text):
    """添付/YouTube解析で発言に追記された【…】ブロックや（ファイル共有）マーカーを
    取り除き、ユーザーが実際に打った部分だけを返す。
    解析まとめの中の「動画」「完成」等の単語で状態確認などの機能が
    誤発動するのを防ぐ（機能トリガーの判定には必ずこちらを使う）。"""
    t = re.split(r"\s*【", text or "")[0]
    return t.replace("（ファイル共有）", "").strip()
