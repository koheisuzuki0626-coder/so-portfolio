"""
Higgsfield 動作確認スクリプト（公式SDK版・モデル自動判定）。
使い方:  python hf_test.py "a cute cat sitting on a chair, cinematic"
候補モデルを順に試し、最初に成功したモデル名と画像URLを表示する。
"""

import asyncio
import os
import sys

from dotenv import load_dotenv

load_dotenv()

import hf_wrapper  # noqa: E402

# 実在する text-to-image モデル候補（公式カタログより）
CANDIDATES = [
    "nano_banana",
    "text2image_soul_v2",
    "flux_2",
    "gpt_image_2",
]


async def main():
    prompt = " ".join(sys.argv[1:]).strip() or "a cute cat sitting on a chair, cinematic lighting"
    print(f"[Higgsfield] prompt = {prompt!r}")
    print(f"[Higgsfield] key設定={'あり' if os.getenv('HIGGSFIELD_API_KEY') else 'なし'}"
          f" / secret設定={'あり' if os.getenv('HIGGSFIELD_API_SECRET') else 'なし'}\n")

    for model in CANDIDATES:
        print(f"--- モデル {model} を試す…")
        try:
            url = await hf_wrapper.generate_image(prompt, model=model)
            print(f"\n✅ 成功！ 使えるモデル = {model}")
            print(f"画像URL: {url}")
            print(f"\n→ .env に  HIGGSFIELD_IMAGE_APP={model}  を入れれば固定できます")
            return
        except Exception as e:
            print(f"    ✗ {model}: {e!r}")

    print("\n❌ どのモデルも失敗しました。上のエラー全部を貼ってください。")


if __name__ == "__main__":
    asyncio.run(main())
