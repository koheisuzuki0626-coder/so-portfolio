#!/usr/bin/env python3
"""自己完結HTMLをPNGに書き出す（Macローカルのヘッドレスブラウザ、Higgsfield不要）。

なぜ要るか:
    デザイン制作（HTMLで組んで画像化）は以前、Higgsfieldのクラウドサンドボックス
    （sandbox_exec）に頼っていた。だが Discord ボットのような非対話セッション
    （claude -p）はMCP接続を使えず、認証済みでも常に失敗する
    （2026-08-20 に判明。詳しくは discord-groupchat/CLAUDE.md 参照）。
    Mac上にヘッドレスブラウザを直接インストールすれば、Higgsfieldを介さず
    ローカルで完結する。macOSは日本語フォント（Hiragino Sans）を標準で
    持っているので、Higgsfieldのサンドボックスと違ってフォント導入も不要。

使い方:
    venv/bin/python3 tools/html_to_png.py <html_path> <out_png_path> <width> <height> [scale]

出力:
    最後の行に LAYOUT_OK か LAYOUT_NG（はみ出しの詳細つき）を1行で出す。
"""
import sys

from playwright.sync_api import sync_playwright

_OVERFLOW_JS = """() => {
    const d = document.documentElement, out = [];
    const vw = window.innerWidth, vh = window.innerHeight;
    if (d.scrollWidth > vw + 2) out.push('横に' + (d.scrollWidth - vw) + 'pxはみ出し');
    if (d.scrollHeight > vh + 2) out.push('縦に' + (d.scrollHeight - vh) + 'pxはみ出し');
    for (const el of document.querySelectorAll('body *')) {
        const r = el.getBoundingClientRect();
        if (r.width && (r.right > vw + 2 || r.bottom > vh + 2 || r.left < -2 || r.top < -2))
            out.push('はみ出し: ' + el.tagName + '.' + (el.className || ''));
    }
    return out.slice(0, 5);
}"""


def main():
    if len(sys.argv) < 5:
        sys.exit(f"使い方: {sys.argv[0]} <html_path> <out_png_path> <width> <height> [scale]")
    html_path, out_path, w, h = sys.argv[1:5]
    w, h = int(w), int(h)
    scale = float(sys.argv[5]) if len(sys.argv) > 5 else 2.0

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(
                viewport={"width": w, "height": h}, device_scale_factor=scale
            )
            page.goto(f"file://{html_path}")
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(300)
            bad = page.evaluate(_OVERFLOW_JS)
            # scale倍で撮ってから等倍へ高品質縮小＝文字の輪郭がなめらかになる
            # （等倍のまま撮ると日本語の細部がつぶれて安っぽく見える）。
            raw_path = out_path + ".raw.png"
            page.screenshot(path=raw_path)
        finally:
            browser.close()

    from PIL import Image

    with Image.open(raw_path) as im:
        im.resize((w, h), Image.LANCZOS).save(out_path)
    import os
    os.remove(raw_path)

    print("LAYOUT_NG: " + " / ".join(bad) if bad else "LAYOUT_OK")


if __name__ == "__main__":
    main()
