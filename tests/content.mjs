/* 文言と実装がズレていないか。
   同じことを複数箇所に書いているので、片方だけ直すと嘘になる。
   ここはその突き合わせ専用。 */
import { check, report, open, BASE, TIERS, LENGTHS, leadWeeks } from './lib.mjs';
import { readFileSync } from 'node:fs';
import pwmod from '/opt/node22/lib/node_modules/playwright/index.js';
const ROOT = '/home/user/so-portfolio';
const browser = await pwmod.chromium.launch();
const page = await open(browser, {});
const idx = readFileSync(`${ROOT}/index.html`, 'utf8');
const about = readFileSync(`${ROOT}/about.html`, 'utf8');
const privacy = readFileSync(`${ROOT}/privacy.html`, 'utf8');

/* ---- 構造化データが画面と一致するか ---- */
const ld = await page.evaluate(() => [...document.querySelectorAll('script[type="application/ld+json"]')]
    .map(s => JSON.parse(s.textContent)));
const faqLd = ld.find(d => d['@type'] === 'FAQPage')?.mainEntity || [];
const faqUi = await page.locator('#faq .faq-item').evaluateAll(els => els.map(e => ({
    q: e.querySelector('summary').textContent.trim(),
    a: e.querySelector('.faq-a').textContent.trim() })));
check('FAQ の件数が画面と構造化データで一致', faqLd.length === faqUi.length, `${faqLd.length}/${faqUi.length}`);
check('FAQ の文言が画面と構造化データで一致',
    faqUi.every((u, i) => faqLd[i]?.name === u.q && faqLd[i]?.acceptedAnswer?.text === u.a),
    JSON.stringify(faqUi.map((u, i) => faqLd[i]?.name === u.q)));
check('閉じていても本文が読める(クローラ対策)', faqUi.every(u => u.a.length > 30));

const org = ld.find(d => d['@graph'])?.['@graph']?.find(x => x['@type'] === 'Organization') || {};
check('構造化データに代表者', org.founder?.name === '鈴木 宏平');
check('構造化データの所在地は市区町村まで',
    org.address?.addressRegion === '愛知県' && org.address?.addressLocality === '名古屋市' && !org.address?.streetAddress);
check('未確定の情報を入れていない', !/foundingDate|postalCode|streetAddress/.test(JSON.stringify(ld)));
check('電話番号を載せていない', !org.telephone && !/090-2968-9616|tel:/.test(idx + about + privacy));
check('SNS が sameAs に入っている',
    (org.sameAs || []).includes('https://www.instagram.com/so_maru_official/')
    && (org.sameAs || []).some(u => u.includes('youtube.com/@hzrinrng')));
check('共有トークンを貼っていない', !/stkn=|utm_source=/.test(idx + about + privacy));

/* ---- 会社概要 ---- */
const rows = await (await open(browser, { page: 'about.html' })).locator('#company .about-facts > div')
    .evaluateAll(els => els.map(e => [e.querySelector('dt').textContent.trim(), e.querySelector('dd').textContent.trim()]));
check('会社概要に項目がある', rows.length >= 5, `${rows.length}項目`);
check('見出しと中身が対になっている', rows.every(([k, v]) => k && v));
check('未記入のまま公開していない', !rows.some(([k, v]) => /[◯○●]{2,}|（氏名）|000-0000|TODO|未定/.test(k + v)));
check('会社概要と構造化データが一致',
    rows.some(([k, v]) => k === '代表者' && v === '鈴木 宏平')
    && rows.some(([k, v]) => k === '所在地' && v === '愛知県名古屋市'));

/* ---- プライバシーポリシーが実装と合っているか ---- */
const pv = await (await open(browser, { page: 'privacy.html' })).locator('main').innerText();
check('計測を入れていないという記述が実態と合う',
    /const ANALYTICS_ID = ''/.test(idx) === /アクセス解析ツールを使用しておらず/.test(pv));
check('入力フォームが無いという記述が実態と合う', !/<form/.test(idx) && /入力フォームを設置していません/.test(pv));
check('外部サービスの学習利用を明記', /AI モデルの学習）に利用される場合があります/.test(pv));
check('相対参照(前項など)を使っていない', !/前項|次項/.test(pv));

/* ---- 納期の文言が計算と合っているか ---- */
const leadProse = `30秒で約${leadWeeks(TIERS[0], 30)}週間`;
check('本文の納期が計算結果と一致(30秒)', idx.includes(leadProse), leadProse);
const r90 = [leadWeeks(TIERS[0], 90), leadWeeks(TIERS[2], 90)];
check('本文の納期が計算結果と一致(90秒)', idx.includes(`90秒で約${r90[0]}〜${r90[1]}週間`), `約${r90[0]}〜${r90[1]}週間`);
const r300 = [leadWeeks(TIERS[0], 300), leadWeeks(TIERS[2], 300)];
check('本文の納期が計算結果と一致(5分)', idx.includes(`5分で約${r300[0]}〜${r300[1]}週間`), `約${r300[0]}〜${r300[1]}週間`);
check('タイトルの「最短2週間」が実態と合う',
    /最短2週間/.test(idx) && Math.min(...TIERS.flatMap(t => LENGTHS.map(s => leadWeeks(t, s)))) === 2);

/* ---- 公開範囲 ---- */
for (const [f, html] of [['index.html', idx], ['about.html', about], ['privacy.html', privacy]]) {
    check(`${f} は検索結果に出さない`, /name="robots" content="noindex/.test(html));
}
const rb = await (await page.request.get(`${BASE}/robots.txt`)).text();
check('robots.txt でクロールは止めていない', /Allow: \//.test(rb) && !/Disallow: \//.test(rb));

/* ---- 配信できるか ---- */
for (const f of ['assets/site.css', 'assets/logo-mark.png', 'assets/favicon.png',
                 'assets/apple-touch-icon.png', 'assets/ogp.png', 'privacy.html', 'funnel.html']) {
    const st = (await page.request.get(`${BASE}/${f}`)).status();
    check(`${f} が配信できる`, st === 200, `HTTP ${st}`);
}
await browser.close();
report();
