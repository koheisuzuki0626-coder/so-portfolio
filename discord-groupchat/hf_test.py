"""
Higgsfield API 動作確認スクリプト（公式SDK版）。
使い方:  python hf_test.py "a cute cat sitting on a chair, cinematic"
画像URLが返れば成功。エラーなら、その内容を貼って調整する。
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import hf_wrapper  # noqa: E402


async def main():
    prompt = " ".join(sys.argv[1:]).strip() or "a cute cat sitting on a chair, cinematic lighting"
    print(f"[Higgsfield] 画像生成中… prompt = {prompt!r}")
    print(f"[Higgsfield] app={hf_wrapper.IMAGE_APP}")
    print(f"[Higgsfield] key設定={'あり' if os.getenv('HIGGSFIELD_API_KEY') else 'なし'}"
          f" / secret設定={'あり' if os.getenv('HIGGSFIELD_API_SECRET') else 'なし'}")
    try:
        url = await hf_wrapper.generate_image(prompt)
        print("\n✅ 成功！ 画像URL:")
        print(url)
    except Exception as e:
        print("\n❌ エラー:")
        print(repr(e))
        print("\n↑ この内容を貼ってくれれば、API仕様に合わせて調整します。")


if __name__ == "__main__":
    asyncio.run(main())
