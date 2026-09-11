/* 読みやすさ。全テキストのコントラストと、日本語の折り返し。 */
import { check, report, open } from './lib.mjs';
import pwmod from '/opt/node22/lib/node_modules/playwright/index.js';
const browser = await pwmod.chromium.launch();
for (const file of ['index.html', 'about.html', 'privacy.html', 'funnel.html', 'roadmap.html']) {
    const page = await open(browser, { page: file });
    const bad = await page.evaluate(() => {
        const L = (c) => { const v = c.map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); });
            return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]; };
        const px = (c) => { const m = c.match(/[\d.]+/g); return m ? m.slice(0, 3).map(Number) : null; };
        const bgOf = (el) => { let n = el;
            while (n) { const c = getComputedStyle(n).backgroundColor;
                const m = c.match(/[\d.]+/g);
                if (m && (m.length < 4 || Number(m[3]) > 0.85)) return px(c);
                n = n.parentElement; } return [255, 255, 255]; };
        const out = [];
        for (const el of document.querySelectorAll('body *')) {
            const txt = [...el.childNodes].filter(n => n.nodeType === 3 && n.textContent.trim()).map(n => n.textContent.trim()).join('');
            if (!txt) continue;
            const s = getComputedStyle(el);
            if (s.visibility === 'hidden' || s.display === 'none' || Number(s.opacity) < 0.5) continue;
            const r = el.getBoundingClientRect();
            if (r.width === 0 || r.height === 0) continue;
            const fg = px(s.color); if (!fg) continue;
            const bg = bgOf(el);
            const l1 = L(fg) + 0.05, l2 = L(bg) + 0.05;
            const ratio = Math.max(l1, l2) / Math.min(l1, l2);
            const size = parseFloat(s.fontSize), bold = Number(s.fontWeight) >= 700;
            const need = (size >= 24 || (size >= 18.66 && bold)) ? 3 : 4.5;
            if (ratio < need) out.push({ t: txt.slice(0, 24), ratio: +ratio.toFixed(2), need, size });
        }
        return out;
    });
    check(`${file} 全テキストが AA を満たす`, bad.length === 0, JSON.stringify(bad.slice(0, 3)));
    const wrap = await page.evaluate(() => getComputedStyle(document.body).wordBreak);
    check(`${file} 日本語を文節で折り返す`, wrap === 'auto-phrase', wrap);
    // 最終行に1〜3文字だけ残っていないか(日本語の泣き別れ)
    const orphan = await page.evaluate(() => {
        const out = [];
        for (const el of document.querySelectorAll('p, li, dd, h1, h2, h3')) {
            const t = el.textContent.trim();
            if (t.length < 20) continue;
            const r = el.getClientRects();
            if (r.length < 2) continue;
            const last = r[r.length - 1], first = r[0];
            if (last.width > 0 && last.width < first.width * 0.06) out.push(t.slice(0, 20));
        }
        return out;
    });
    check(`${file} 最終行に1〜3文字だけ残る箇所がない`, orphan.length === 0, JSON.stringify(orphan.slice(0, 2)));
    check(`${file} JS エラーなし`, page.__errors.length === 0, JSON.stringify(page.__errors));
    await page.close();
}
await browser.close();
report();
