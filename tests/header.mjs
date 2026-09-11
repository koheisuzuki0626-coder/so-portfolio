/* ヘッダー。
   広い画面では項目をロゴの右に左寄せ、狭い画面では2段目で横に流す。
   黒帯との境目で読めなくならないことも、ここで担保する。 */
import { check, report, open } from './lib.mjs';
import pwmod from '/opt/node22/lib/node_modules/playwright/index.js';
const browser = await pwmod.chromium.launch();

/* ---- 広い画面：ロゴの隣に1行で並ぶ ---- */
for (const w of [1440, 1280, 1000, 900]) {
    const page = await open(browser, { width: w });
    const g = await page.evaluate(() => {
        const its = [...document.querySelectorAll('.nav-main .nav-item')];
        const lg = document.querySelector('#nav .logo').getBoundingClientRect();
        const cta = document.querySelector('.nav-end').getBoundingClientRect();
        const gaps = its.slice(1).map((e, i) =>
            Math.round(e.getBoundingClientRect().left - its[i].getBoundingClientRect().right));
        return { n: its.length, fromLogo: Math.round(its[0].getBoundingClientRect().left - lg.right),
                 toCta: Math.round(cta.left - its[its.length - 1].getBoundingClientRect().right),
                 rows: new Set(its.map(e => Math.round(e.getBoundingClientRect().top))).size, gaps };
    });
    check(`幅${w} 項目が6つ`, g.n === 6);
    // 中央に寄せると広い画面で左右に大きな空きができ、バーではなく「浮いた島」に見える
    check(`幅${w} 項目がロゴの隣`, g.fromLogo > 0 && g.fromLogo < 80, `${g.fromLogo}px`);
    check(`幅${w} 1行に収まる`, g.rows === 1);
    check(`幅${w} 間隔が揃う`, Math.max(...g.gaps) - Math.min(...g.gaps) <= 2, JSON.stringify(g.gaps));
    check(`幅${w} CTA と重ならない`, g.toCta > 0, `${g.toCta}px`);
    await page.close();
}

/* ---- 狭い画面：2段目に並べて横に流す ---- */
{
    const page = await open(browser, { width: 390, height: 780, mobile: true });
    const m = await page.evaluate(() => {
        const nm = document.querySelector('.nav-main');
        const its = [...nm.querySelectorAll('.nav-item')];
        const lg = document.querySelector('#nav .logo').getBoundingClientRect();
        return { n: its.length, below: its[0].getBoundingClientRect().top >= lg.bottom - 1,
                 rows: new Set(its.map(e => Math.round(e.getBoundingClientRect().top))).size,
                 scrollable: nm.scrollWidth > nm.clientWidth + 2,
                 font: parseFloat(getComputedStyle(its[0]).fontSize),
                 navH: Math.round(document.querySelector('#nav').getBoundingClientRect().height),
                 navVar: getComputedStyle(document.documentElement).getPropertyValue('--nav-h').trim(),
                 overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth };
    });
    check('スマホでも項目を隠さない', m.n === 6 && m.below && m.rows === 1, JSON.stringify(m));
    check('入りきらないぶんは横に流せる', m.scrollable);
    check('文字を小さくしすぎていない', m.font >= 12.5, `${m.font}px`);
    check('ヘッダーの高さが --nav-h に入る', m.navVar === `${m.navH}px`, `${m.navH}px`);
    check('スマホで横溢れなし', m.overflow <= 0, `${m.overflow}px`);
    check('ハンバーガーも残っている', await page.locator('#burger').isVisible());
    const edge = await page.evaluate(async () => {
        const nm = document.querySelector('.nav-main');
        const before = nm.classList.contains('nav-main-end');
        nm.scrollLeft = nm.scrollWidth; nm.dispatchEvent(new Event('scroll'));
        await new Promise(r => setTimeout(r, 60));
        return { before, after: nm.classList.contains('nav-main-end') };
    });
    check('流しきると端のぼかしが外れる', edge.before === false && edge.after === true, JSON.stringify(edge));
    // 2段になってもアンカーがヘッダーに潜らないこと
    await page.evaluate(() => window.scrollTo({ top: 0, behavior: 'instant' }));
    await page.locator('.nav-main').evaluate(n => { n.scrollLeft = 0; });
    await page.locator('.nav-main .nav-item[href="#plans"]').scrollIntoViewIfNeeded();
    await page.locator('.nav-main .nav-item[href="#plans"]').tap();
    await page.waitForTimeout(900);
    const land = await page.evaluate(() => ({
        top: Math.round(document.querySelector('#plans').getBoundingClientRect().top),
        navH: Math.round(document.querySelector('#nav').getBoundingClientRect().height) }));
    check('スマホでも着地がヘッダーに潜らない', land.top >= land.navH - 2, `y=${land.top} nav=${land.navH}`);
    await page.close();
}

/* ---- 黒帯との境目で読めなくならないか ----
   配色をアニメーションさせると、途中でバーも文字も中間色になり
   一瞬ヘッダーが消える(実測 1.91:1)。境目を10px刻みで走査する */
{
    const page = await open(browser, { width: 390, height: 780, mobile: true });
    const zones = await page.evaluate(() => [...document.querySelectorAll('.stage-dark, .cta-band')]
        .map(el => ({ top: Math.round(el.getBoundingClientRect().top + scrollY),
                      bottom: Math.round(el.getBoundingClientRect().bottom + scrollY) })));
    let worst = { ratio: 99, y: 0 };
    for (const z of zones) for (const edge of [z.top, z.bottom]) {
        for (let y = Math.max(0, edge - 70); y <= edge + 70; y += 10) {
            await page.evaluate((y) => window.scrollTo({ top: y, behavior: 'instant' }), y);
            await page.waitForTimeout(60);
            const r = await page.evaluate(() => {
                const nav = document.querySelector('#nav'), nr = nav.getBoundingClientRect();
                const mid = nr.top + nr.height / 2;
                nav.style.visibility = 'hidden';
                const el = document.elementFromPoint(30, mid);
                nav.style.visibility = '';
                let bg = 'rgba(0, 0, 0, 0)', n = el;
                while (n && bg === 'rgba(0, 0, 0, 0)') { bg = getComputedStyle(n).backgroundColor; n = n.parentElement; }
                const px = (c) => { const m = c.match(/[\d.]+/g); return m ? m.slice(0, 3).map(Number) : [255, 255, 255]; };
                const a = (getComputedStyle(nav).backgroundColor.match(/[\d.]+/g) || [0, 0, 0, 1]).map(Number);
                const al = a.length > 3 ? a[3] : 1, under = px(bg);
                const comp = [0, 1, 2].map(i => a[i] * al + under[i] * (1 - al));
                const L = (c) => { const v = c.map(x => { x /= 255; return x <= 0.03928 ? x / 12.92 : Math.pow((x + 0.055) / 1.055, 2.4); });
                    return 0.2126 * v[0] + 0.7152 * v[1] + 0.0722 * v[2]; };
                const t = px(getComputedStyle(document.querySelector('#nav .logo')).color);
                const l1 = L(comp) + 0.05, l2 = L(t) + 0.05;
                return { ratio: Math.max(l1, l2) / Math.min(l1, l2), y: Math.round(scrollY) };
            });
            if (r.ratio < worst.ratio) worst = r;
        }
    }
    check('黒帯の境目でもヘッダーが読める', worst.ratio >= 4.5, `最小 ${worst.ratio.toFixed(2)}:1 (y=${worst.y})`);
    await page.close();
}
await browser.close();
report();
