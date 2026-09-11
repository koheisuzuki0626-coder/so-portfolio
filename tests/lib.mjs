/* テストの土台。結果の集計と、よく使う操作をまとめてある。
   これらは scratchpad ではなくリポジトリに置くこと。
   一時ディレクトリに置いていたぶんは消えて失われた。 */
export const state = { fail: 0, total: 0 };
export function check(name, ok, note = '') {
    state.total += 1;
    if (!ok) state.fail += 1;
    console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${note ? '  ' + note : ''}`);
}
export function report() {
    console.log(state.fail ? `\nRESULT: ${state.fail} / ${state.total} 件 FAILED`
                           : `\nRESULT: ALL PASS (${state.total}件)`);
    process.exit(state.fail ? 1 : 0);
}
export const BASE = process.env.BASE || 'http://127.0.0.1:8899';
export const PW = '/opt/node22/lib/node_modules/playwright/index.js';

/* 料金と工数のモデル。index.html のコメントと同じもの。
   ここを書き換えるときは index.html も必ず合わせること */
export const PRICE = { base: 90000, perExtra: 65000 };
export const TIERS = [
    { id: 'ume',   label: '梅', perSec: 3500, hours: 1.0 },
    { id: 'take',  label: '竹', perSec: 4900, hours: 1.4 },
    { id: 'matsu', label: '松', perSec: 6650, hours: 1.9 },
];
export const LENGTHS = [15, 30, 45, 60, 90, 120, 180, 300];
export const countCap = (sec) => (sec <= 30 ? 2 : sec <= 90 ? 4 : 6);
export const price = (t, sec, n) => PRICE.base + t.perSec * sec + PRICE.perExtra * (n - 1);
/* 工数には絵コンテぶんを上乗せしてある(固定 +1h・本数 +0.5h) */
export const hours = (t, sec, n) => 5.8 + 0.24 * t.hours * sec + 4.5 * (n - 1);
export const leadWeeks = (t, sec) => Math.max(2, Math.round((4.8 + 0.24 * t.hours * sec) / 25 + 1));
export const RATE = 14900;

export async function open(pw, { width = 1280, height = 900, mobile = false, page: file = 'index.html' } = {}) {
    const { mockFonts, mockYtimg } = await import('./route.mjs');
    const ctx = { viewport: { width, height }, deviceScaleFactor: mobile ? 3 : 1 };
    if (mobile) { ctx.isMobile = true; ctx.hasTouch = true; }
    const p = await pw.newPage(ctx);
    const errors = [];
    p.on('pageerror', (e) => errors.push(String(e)));
    await mockFonts(p); await mockYtimg(p);
    await p.goto(`${BASE}/${file}`, { waitUntil: 'networkidle' });
    await p.evaluate(() => document.fonts.ready);
    await p.waitForTimeout(500);
    p.__errors = errors;
    return p;
}
export const pick = async (p, tier, sec, n) => {
    await p.locator(`#calc-tier .calc-opt[data-tier="${tier}"]`).click();
    await p.locator(`#calc-len .calc-opt[data-sec="${sec}"]`).click();
    if (n) await p.locator(`#calc-cnt .calc-opt[data-count="${n}"]`).click();
    await p.waitForTimeout(60);
    return Number((await p.locator('#calc-total').innerText()).replace(/[^\d]/g, ''));
};
