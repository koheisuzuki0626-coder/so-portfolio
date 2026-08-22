"""
Higgsfield プラットフォームに「使えるモデル一覧」を直接問い合わせる。
使い方:  python hf_models.py
候補エンドポイントを順に叩き、200が返ったものの中身（モデル一覧）を表示する。
その中の正しいモデルIDを .env の HIGGSFIELD_IMAGE_APP に設定する。
"""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

KEY = os.getenv("HIGGSFIELD_API_KEY", "")
SECRET = os.getenv("HIGGSFIELD_API_SECRET", "")

# 認証ヘッダの候補（プラットフォームの想定形）
AUTH_HEADERS = [
    {"Authorization": f"Key {KEY}:{SECRET}"},
    {"hf-api-key": KEY, "hf-secret": SECRET},
    {"Authorization": f"Bearer {KEY}:{SECRET}"},
]

BASES = [
    "https://platform.higgsfield.ai",
    "https://api.higgsfield.ai",
]
PATHS = [
    "/v1/models",
    "/models",
    "/v1/apps",
    "/apps",
    "/v1/model-list",
    "/v1/catalog",
]


def main():
    print(f"key={'あり' if KEY else 'なし'} / secret={'あり' if SECRET else 'なし'}\n")
    found = False
    for base in BASES:
        for path in PATHS:
            url = base + path
            for headers in AUTH_HEADERS:
                try:
                    r = httpx.get(url, headers=headers, timeout=30)
                except Exception as e:
                    print(f"{url}  [{list(headers)[0]}]  ERR {e}")
                    continue
                tag = list(headers)[0]
                if r.status_code == 200:
                    print(f"\n✅ 200 OK  {url}  (auth: {tag})")
                    print(r.text[:2000])
                    found = True
                elif r.status_code in (401, 403):
                    print(f"{url}  [{tag}]  {r.status_code} 認証NG")
                else:
                    print(f"{url}  [{tag}]  {r.status_code}")
    if not found:
        print("\n200が返るURLが見つかりませんでした。上の一覧（ステータスコード）を貼ってください。")
    else:
        print("\n↑ ✅ の中身から text-to-image のモデルIDを探して教えてください。")


if __name__ == "__main__":
    main()
