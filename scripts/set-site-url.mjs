#!/usr/bin/env node
// 公開URLを差し替える。
//
// canonical / og:url / og:image / 構造化データ / sitemap.xml / robots.txt には
// 絶対URLを書く必要があり、いま17か所に散っている。URLを変えるとき
// 1か所でも直し忘れると検索エンジンに古いURLを伝え続けることになるので、
// まとめて書き換えるためのスクリプト。
//
// 使い方:
//   node scripts/set-site-url.mjs https://so-film.jp/          (独自ドメイン)
//   node scripts/set-site-url.mjs https://xxx.github.io/       (ユーザーサイト)
//   node scripts/set-site-url.mjs --show                       (現在の設定を表示)
//
// 独自ドメインを指定すると CNAME も書き出す。github.io なら CNAME は消す。

import { readFile, writeFile, unlink } from 'node:fs/promises';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const FILES = ['index.html', 'about.html', 'sitemap.xml', 'robots.txt'];
const URL_RE = /https:\/\/[a-z0-9.-]+(?:\/[A-Za-z0-9._~\-/]*)?/g;

/** いま書かれているベースURL(最頻値)を調べる */
async function currentBase() {
    const counts = new Map();
    for (const f of FILES) {
        let text;
        try { text = await readFile(resolve(ROOT, f), 'utf8'); } catch { continue; }
        for (const m of text.match(URL_RE) ?? []) {
            if (/fonts\.(googleapis|gstatic)\.com|youtube\.com|ytimg\.com|schema\.org|sitemaps\.org|w3\.org/.test(m)) continue;
            // ページやアセットの部分を落として、サイトのルートだけを残す
            const base = m.replace(/(index\.html|about\.html|sitemap\.xml|assets\/[^/]+|#[\w-]+)$/, '');
            counts.set(base, (counts.get(base) ?? 0) + 1);
        }
    }
    if (counts.size === 0) return null;
    return [...counts.entries()].sort((a, b) => b[1] - a[1])[0][0];
}

function normalize(input) {
    let u;
    try { u = new URL(input); } catch { throw new Error(`URL として読めません: ${input}`); }
    if (u.protocol !== 'https:') throw new Error('https:// で指定してください');
    return u.origin + (u.pathname.endsWith('/') ? u.pathname : u.pathname + '/');
}

async function main() {
    const arg = process.argv[2];
    const from = await currentBase();

    if (!arg || arg === '--show') {
        console.log(`現在の公開URL: ${from ?? '(見つかりません)'}`);
        return;
    }

    const to = normalize(arg);
    if (!from) throw new Error('置き換え元のURLを特定できませんでした');
    if (from === to) { console.log(`すでに ${to} です。変更はありません。`); return; }

    let total = 0;
    for (const f of FILES) {
        const path = resolve(ROOT, f);
        let text;
        try { text = await readFile(path, 'utf8'); } catch { continue; }
        const hits = text.split(from).length - 1;
        if (!hits) continue;
        await writeFile(path, text.split(from).join(to), 'utf8');
        console.log(`  ${f}: ${hits}か所`);
        total += hits;
    }

    // 独自ドメインなら CNAME が要る。github.io なら邪魔になるので消す
    const host = new URL(to).hostname;
    const cname = resolve(ROOT, 'CNAME');
    if (host.endsWith('.github.io')) {
        try { await unlink(cname); console.log('  CNAME: 削除'); } catch { /* もともと無い */ }
    } else {
        await writeFile(cname, host + '\n', 'utf8');
        console.log(`  CNAME: ${host}`);
    }

    console.log(`\n${from}\n  → ${to}\n合計 ${total}か所を書き換えました。`);
    if (!host.endsWith('.github.io')) {
        console.log('\nこのあと GitHub 側での作業:');
        console.log('  1. ドメインの DNS に GitHub Pages のレコードを設定');
        console.log('  2. リポジトリの Settings → Pages → Custom domain に ' + host);
        console.log('  3. Enforce HTTPS にチェック(証明書の発行に最大24時間)');
    }
}

main().catch((err) => { console.error(`[set-site-url] ${err.message}`); process.exit(1); });
