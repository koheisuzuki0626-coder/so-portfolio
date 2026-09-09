/* 元の絵(assets/src/logo-original.jpg)から、サイトで使うロゴ画像一式を作る。
 *
 *   node scripts/build-logo.mjs      (要 sharp: npm i sharp)
 *
 * 元は白地の JPEG なので、外周から塗りつぶして「地に繋がっている白」だけを
 * 透明にしている。単純な白キーだと花の内側の淡い色まで穴が開くため。
 * しきい値 HI/LO は暗い地に置いたときモヤが出ない値として決めたもの。 */
import sharp from 'sharp';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
const ROOT = join(dirname(fileURLToPath(import.meta.url)), '..');
const SRC = join(ROOT, 'assets/src/logo-original.jpg');
const A = join(ROOT, 'assets');
const { data, info } = await sharp(SRC).raw().toBuffer({ resolveWithObject: true });
const { width: W, height: H, channels: C } = info;
const BG = [248, 248, 248], HI = 48, LO = 26;
const diff = (i) => Math.max(Math.abs(data[i]-BG[0]), Math.abs(data[i+1]-BG[1]), Math.abs(data[i+2]-BG[2]));

/* 元画像は白地の JPEG。外周から塗りつぶして「地に繋がっている白」だけを抜く。
   単純な白キーだと花の内側の淡い色まで穴が開く */
const isBg = new Uint8Array(W * H), stack = [];
const push = (x, y) => {
    if (x < 0 || y < 0 || x >= W || y >= H) return;
    const p = y * W + x;
    if (isBg[p] || diff(p * C) >= HI) return;
    isBg[p] = 1; stack.push(p);
};
for (let x = 0; x < W; x++) { push(x, 0); push(x, H - 1); }
for (let y = 0; y < H; y++) { push(0, y); push(W - 1, y); }
while (stack.length) { const p = stack.pop(), x = p % W, y = (p - x) / W;
    push(x+1,y); push(x-1,y); push(x,y+1); push(x,y-1); }

const rgba = Buffer.alloc(W * H * 4);
for (let p = 0; p < W * H; p++) {
    const i = p * C, o = p * 4;
    let a = 255;
    if (isBg[p]) {
        const d = diff(i);
        a = d <= LO ? 0 : Math.round(255 * (d - LO) / (HI - LO));
        if (a > 0) {                       // 輪郭に接している画素だけ縁として残す
            const x = p % W, y = (p - x) / W; let near = false;
            for (let dy = -2; dy <= 2 && !near; dy++) for (let dx = -2; dx <= 2; dx++) {
                const yy = y+dy, xx = x+dx;
                if (yy<0||yy>=H||xx<0||xx>=W) continue;
                if (!isBg[yy*W+xx]) { near = true; break; }
            }
            if (!near) a = 0;
        }
    }
    if (a === 0) continue;
    const f = a / 255;                     // 白と混ざったぶんを戻す(暗い地で白フチが出ないように)
    for (let c = 0; c < 3; c++)
        rgba[o+c] = Math.max(0, Math.min(255, Math.round((data[i+c] - (1-f)*BG[c]) / f)));
    rgba[o+3] = a;
}
const full = sharp(rgba, { raw: { width: W, height: H, channels: 4 } });
const cut = async (l, t, w, h) => sharp(await full.clone().extract({ left: l, top: t, width: w, height: h })
    .png().toBuffer()).trim({ threshold: 1 }).png({ compressionLevel: 9 }).toBuffer();

const mark = await cut(548, 738, 414, 408);        // 花のみ
const stem = await cut(548, 738, 414, 534);        // 茎つき
/* サイトで読むぶんは 256px に落として減色する。原寸のままだと 240KB あり、
   ヘッダーで毎回読むには重い。実際に出る 30〜60px では見分けがつかない */
await sharp(mark).resize(256)
    .png({ compressionLevel: 9, palette: true, quality: 100, effort: 10 })
    .toFile(`${A}/logo-mark.png`);
await sharp(stem).toFile(`${A}/logo-full.png`);

/* ファビコン: 透過のまま。タブが白でも黒でも色が残る */
await sharp(mark).resize(64, 64, { fit: 'contain', background: { r:0,g:0,b:0,alpha:0 } })
    .png({ compressionLevel: 9 }).toFile(`${A}/favicon.png`);

/* iOS のホーム画面は透過を黒で埋めるので、地を敷いて余白を取る */
await sharp({ create: { width: 180, height: 180, channels: 4, background: '#ffffff' } })
    .composite([{ input: await sharp(mark).resize(148, 148, { fit: 'contain',
        background: { r:0,g:0,b:0,alpha:0 } }).toBuffer(), gravity: 'centre' }])
    .png({ compressionLevel: 9 }).toFile(`${A}/apple-touch-icon.png`);

for (const f of ['logo-mark.png','logo-full.png','favicon.png','apple-touch-icon.png']) {
    const m = await sharp(`${A}/${f}`).metadata();
    console.log(f, m.width + 'x' + m.height);
}
