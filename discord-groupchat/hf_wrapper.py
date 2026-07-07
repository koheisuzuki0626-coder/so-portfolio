"""
Higgsfield 公式SDK（higgsfield_client）の薄いラッパー。
=====================================================
認証は キーID + シークレット。.env の HIGGSFIELD_API_KEY / HIGGSFIELD_API_SECRET を
SDKが見る HF_API_KEY / HF_API_SECRET に橋渡しする。

  generate_image(prompt) -> 画像URL
  generate_video(image_url, prompt) -> 動画URL

app名（生成モデル）は .env で上書き可能。
"""

import os

import higgsfield_client as _hf  # 公式SDK（pip install higgsfield_client）

# Higgsfield の実在モデルID（models一覧より）。.env で上書き可能。
# 画像候補: z_image(高速) / recraft_v4_1 / soul_location / soul_cast
IMAGE_APP = os.getenv("HIGGSFIELD_IMAGE_APP", "z_image")
VIDEO_APP = os.getenv("HIGGSFIELD_VIDEO_APP", "dop-turbo")


def _ensure_env():
    """.env の HIGGSFIELD_* を SDK が参照する HF_* に反映する。"""
    if os.getenv("HIGGSFIELD_API_KEY") and not os.getenv("HF_API_KEY"):
        os.environ["HF_API_KEY"] = os.environ["HIGGSFIELD_API_KEY"]
    if os.getenv("HIGGSFIELD_API_SECRET") and not os.getenv("HF_API_SECRET"):
        os.environ["HF_API_SECRET"] = os.environ["HIGGSFIELD_API_SECRET"]


def _find_url(result, prefer=("url", "image_url", "video_url")):
    """レスポンス(dict/list)を再帰的に探して最初のURL文字列を返す。"""
    if isinstance(result, dict):
        for k in prefer:
            v = result.get(k)
            if isinstance(v, str) and v.startswith("http"):
                return v
        for v in result.values():
            u = _find_url(v, prefer)
            if u:
                return u
    elif isinstance(result, list):
        for v in result:
            u = _find_url(v, prefer)
            if u:
                return u
    return None


async def generate_image(prompt, **arguments):
    _ensure_env()
    # モデル名は第1引数（位置引数）。arguments に prompt などを渡す。
    result = await _hf.subscribe_async(
        IMAGE_APP, arguments={"prompt": prompt, **arguments}
    )
    # 代表的な形: {"images": [{"url": ...}]}
    try:
        return result["images"][0]["url"]
    except (KeyError, IndexError, TypeError):
        pass
    url = _find_url(result)
    if not url:
        raise RuntimeError(f"画像URLが取れません（形が想定と違う）: {str(result)[:600]}")
    return url


async def generate_video(image_url, prompt="", **arguments):
    _ensure_env()
    args = {"image_url": image_url, **arguments}
    if prompt:
        args["prompt"] = prompt
    result = await _hf.subscribe_async(VIDEO_APP, arguments=args)
    try:
        return result["videos"][0]["url"]
    except (KeyError, IndexError, TypeError):
        pass
    url = _find_url(result)
    if not url:
        raise RuntimeError(f"動画URLが取れません（形が想定と違う）: {str(result)[:600]}")
    return url
