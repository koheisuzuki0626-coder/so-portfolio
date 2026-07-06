"""
Higgsfield API の薄いラッパー。
=============================
公式APIの細部（エンドポイント名・モデル名・レスポンス形）は変わりうるので、
主要な値は環境変数（HIGGSFIELD_*）で上書きできるようにしてある。
テストで実際のエラー/レスポンスを見て、必要なら .env で調整する。

想定API（api.higgsfield.ai）:
  POST {BASE}/v1/generations        … 生成ジョブ作成 → {"id": ...}
  GET  {BASE}/v1/generations/{id}   … 状態確認（completed になったら出力URL）
  認証: Authorization: Bearer <API_KEY>
"""

import os
import time

import requests

BASE = os.getenv("HIGGSFIELD_BASE_URL", "https://api.higgsfield.ai").rstrip("/")
KEY = os.getenv("HIGGSFIELD_API_KEY", "")
SECRET = os.getenv("HIGGSFIELD_API_SECRET", "")  # キー+シークレット方式のAPI用（任意）
GEN_PATH = os.getenv("HIGGSFIELD_GEN_PATH", "/v1/generations")
IMAGE_MODEL = os.getenv("HIGGSFIELD_IMAGE_MODEL", "flux")
VIDEO_MODEL = os.getenv("HIGGSFIELD_VIDEO_MODEL", "dop-turbo")
POLL_TIMEOUT = int(os.getenv("HIGGSFIELD_TIMEOUT", "600"))  # 秒
POLL_INTERVAL = int(os.getenv("HIGGSFIELD_POLL_INTERVAL", "5"))


class HiggsfieldError(RuntimeError):
    pass


def _headers():
    h = {"Content-Type": "application/json", "Authorization": f"Bearer {KEY}"}
    if SECRET:
        h["hf-secret"] = SECRET  # 一部APIはキー+シークレット
    return h


def _create(body):
    if not KEY:
        raise HiggsfieldError("HIGGSFIELD_API_KEY が設定されていません（.env を確認）")
    r = requests.post(BASE + GEN_PATH, json=body, headers=_headers(), timeout=60)
    if r.status_code >= 300:
        raise HiggsfieldError(f"作成失敗 HTTP {r.status_code}: {r.text[:600]}")
    data = r.json()
    gid = (
        data.get("id")
        or data.get("generation_id")
        or (data.get("data") or {}).get("id")
    )
    if not gid:
        raise HiggsfieldError(f"生成IDが取得できません: {str(data)[:600]}")
    return gid


def _extract_output(d):
    """いくつかの一般的なレスポンス形から出力URLを取り出す。"""
    candidates = (
        ("output_url",),
        ("result", "url"),
        ("output", "url"),
        ("url",),
        ("assets", 0, "url"),
        ("data", "url"),
        ("results", 0, "url"),
    )
    for path in candidates:
        cur = d
        ok = True
        for k in path:
            try:
                cur = cur[k]
            except (KeyError, IndexError, TypeError):
                ok = False
                break
        if ok and isinstance(cur, str) and cur.startswith("http"):
            return cur
    raise HiggsfieldError(f"出力URLが見つかりません（形が想定と違う）: {str(d)[:600]}")


def _poll(gid):
    url = f"{BASE}{GEN_PATH}/{gid}"
    deadline = time.time() + POLL_TIMEOUT
    last = ""
    while time.time() < deadline:
        r = requests.get(url, headers=_headers(), timeout=60)
        if r.status_code >= 300:
            raise HiggsfieldError(f"状態取得失敗 HTTP {r.status_code}: {r.text[:600]}")
        d = r.json()
        status = str(d.get("status") or d.get("state") or "").lower()
        last = status
        if status in ("completed", "succeeded", "success", "done", "finished"):
            return _extract_output(d)
        if status in ("failed", "error", "canceled", "cancelled"):
            raise HiggsfieldError(f"生成が失敗しました: {str(d)[:600]}")
        time.sleep(POLL_INTERVAL)
    raise HiggsfieldError(f"タイムアウト（最後の状態: {last or '不明'}）")


# ---------- 公開関数 ----------
def generate_image(prompt, width=1024, height=768):
    """テキストから画像を生成し、完成した画像URLを返す。"""
    gid = _create({
        "task": "text-to-image",
        "model": IMAGE_MODEL,
        "prompt": prompt,
        "width": width,
        "height": height,
    })
    return _poll(gid)


def generate_video(input_image, prompt="", duration=5):
    """画像URLから動画を生成し、完成した動画URLを返す。"""
    gid = _create({
        "task": "image-to-video",
        "model": VIDEO_MODEL,
        "input_image": input_image,
        "prompt": prompt,
        "duration": duration,
    })
    return _poll(gid)
