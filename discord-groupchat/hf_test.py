"""
Higgsfield API 動作確認スクリプト。
使い方:  python hf_test.py "a cute cat sitting on a chair, cinematic"
画像URLが返れば成功。エラーなら、その内容（HTTPコード・レスポンス）を貼って調整する。
"""

import sys

from dotenv import load_dotenv

load_dotenv()

import higgsfield_client as hf  # noqa: E402


def main():
    prompt = " ".join(sys.argv[1:]).strip() or "a cute cat sitting on a chair, cinematic lighting"
    print(f"[Higgsfield] 画像生成中… prompt = {prompt!r}")
    print(f"[Higgsfield] BASE={hf.BASE}  IMAGE_MODEL={hf.IMAGE_MODEL}  key設定={'あり' if hf.KEY else 'なし'}")
    try:
        url = hf.generate_image(prompt)
        print("\n✅ 成功！ 画像URL:")
        print(url)
    except Exception as e:
        print("\n❌ エラー:")
        print(e)
        print("\n↑ この内容を貼ってくれれば、API仕様に合わせて調整します。")


if __name__ == "__main__":
    main()
