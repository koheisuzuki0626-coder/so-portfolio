#!/usr/bin/env node
// YouTube チャンネルの新着動画を取得し data/videos.json に書き出す。
//
// 【方式について】
// 当初は RSS(https://www.youtube.com/feeds/videos.xml)を使っていたが、
// GitHub Actions のランナーからは UA の有無に関わらず常に 404 が返る
// (2026-07 時点、実測で確認)。そのためチャンネルの videos タブに
// 埋め込まれた ytInitialData を読む方式に切り替えた。
//
// この方式は YouTube のフロントエンド実装に依存するため壊れやすい。
// 壊れたときに「静かに空データを公開する」ことがないよう、
// 1件も取れなければ例外を投げ、既存の JSON は書き換えない。
//
// より安定させたい場合は YouTube Data API v3 (要APIキー) への移行を推奨。
// ランナーから googleapis.com には到達できることを確認済み。
//
// 使い方: node scripts/fetch-youtube.mjs

import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const CONFIG_PATH = resolve(ROOT, 'youtube.config.json');
const OUTPUT_PATH = resolve(ROOT, 'data/videos.json');

const UA =
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ' +
    '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

const CHANNEL_ID_RE = /^UC[\w-]{22}$/;
const VIDEO_ID_RE = /^[\w-]{11}$/;

/* ---------- 相対日時のパース ---------- */

const MS = {
    second: 1000,
    minute: 60 * 1000,
    hour: 60 * 60 * 1000,
    day: 24 * 60 * 60 * 1000,
    week: 7 * 24 * 60 * 60 * 1000,
    month: 30 * 24 * 60 * 60 * 1000,
    year: 365 * 24 * 60 * 60 * 1000,
};

const JA_UNITS = [
    [/秒/, 'second'], [/分/, 'minute'], [/時間/, 'hour'],
    [/週間/, 'week'], [/日/, 'day'],
    [/(?:か|ヶ|カ|ケ)月/, 'month'], [/年/, 'year'],
];

/**
 * 「3 日前」「2 か月前」「5 years ago」などを概算の絶対日時に変換する。
 * チャンネルページは相対表記しか持たないため、結果は概算値。
 * 解釈できない場合は null を返す(呼び出し側で年の表示を省く)。
 */
export function parseRelativeDate(text, now = Date.now()) {
    if (!text) return null;
    const s = String(text).trim();

    const num = s.match(/(\d+)/);
    if (!num) return null;
    const n = Number(num[1]);

    // 週間 は 日 より先に判定する(「週間」に「日」は含まれないが順序を明示)
    let unit = null;
    for (const [re, u] of JA_UNITS) {
        if (re.test(s)) { unit = u; break; }
    }
    if (!unit) {
        const en = s.toLowerCase().match(/\b(second|minute|hour|day|week|month|year)s?\b/);
        if (en) unit = en[1];
    }
    if (!unit) return null;

    return new Date(now - n * MS[unit]).toISOString();
}

/* ---------- ページ取得 ---------- */

async function get(url) {
    const res = await fetch(url, {
        headers: { 'user-agent': UA, 'accept-language': 'ja' },
    });
    if (!res.ok) throw new Error(`GET ${url} -> ${res.status} ${res.statusText}`);
    return res.text();
}

/**
 * 設定値から videos タブの URL を組み立てる。
 */
export function videosUrl(channel) {
    const value = String(channel ?? '').trim();
    if (!value || value.includes('REPLACE_ME')) {
        throw new Error('youtube.config.json の "channel" が未設定です(@ハンドル か UC... を指定してください)');
    }
    if (CHANNEL_ID_RE.test(value)) return `https://www.youtube.com/channel/${value}/videos`;

    const fromUrl = value.match(/\/channel\/(UC[\w-]{22})/);
    if (fromUrl) return `https://www.youtube.com/channel/${fromUrl[1]}/videos`;

    const handle = value.match(/@([\w.-]+)/);
    if (handle) return `https://www.youtube.com/@${handle[1]}/videos`;

    return `https://www.youtube.com/@${value}/videos`;
}

/* ---------- ytInitialData のパース ---------- */

export function extractInitialData(html) {
    const m = html.match(/ytInitialData\s*=\s*(\{[\s\S]*?\});\s*<\/script>/);
    if (!m) throw new Error('ytInitialData が見つかりません(YouTube のページ構造が変わった可能性があります)');
    return JSON.parse(m[1]);
}

function collectLockups(root) {
    const out = [];
    (function walk(node) {
        if (!node || typeof node !== 'object') return;
        if (Array.isArray(node)) return node.forEach(walk);
        if (node.lockupViewModel) { out.push(node.lockupViewModel); return; }
        for (const key of Object.keys(node)) walk(node[key]);
    })(root);
    return out;
}

/**
 * videos タブの ytInitialData から動画一覧を取り出す。
 */
export function parseVideos(data, now = Date.now()) {
    const lockups = collectLockups(data);

    const seen = new Set();
    const videos = [];

    for (const lockup of lockups) {
        if (lockup.contentType !== 'LOCKUP_CONTENT_TYPE_VIDEO') continue;

        const id = lockup.contentId;
        if (!id || !VIDEO_ID_RE.test(id) || seen.has(id)) continue;

        const meta = lockup.metadata?.lockupMetadataViewModel;
        const title = meta?.title?.content?.trim();
        if (!title) continue;

        // metadataRows は [視聴回数, 相対日時] の並び。表記ゆれに備えて
        // 「前 / ago」を含むものを日時として拾う。
        const parts = (meta?.metadata?.contentMetadataViewModel?.metadataRows ?? [])
            .flatMap((row) => row.metadataParts ?? [])
            .map((part) => part.text?.content)
            .filter(Boolean);
        const relative = parts.find((t) => /前|ago/.test(t)) ?? null;
        const publishedAt = parseRelativeDate(relative, now);

        seen.add(id);
        videos.push({
            id,
            title,
            description: '',
            publishedAt,
            publishedRelative: relative,
            // チャンネルページは相対表記しか返さないため、年は概算値
            publishedIsApproximate: Boolean(publishedAt),
            year: publishedAt ? publishedAt.slice(0, 4) : null,
            url: `https://www.youtube.com/watch?v=${id}`,
            thumbnail: `https://i.ytimg.com/vi/${id}/hqdefault.jpg`,
        });
    }

    return videos;
}

export function parseChannel(html, data) {
    const idMatch = html.match(/"(?:channelId|externalId)":"(UC[\w-]{22})"/);
    return {
        channelId: idMatch ? idMatch[1] : null,
        channelTitle: data?.metadata?.channelMetadataRenderer?.title ?? null,
    };
}

/* ---------- main ---------- */

async function main() {
    const config = JSON.parse(await readFile(CONFIG_PATH, 'utf8'));
    const maxVideos = Number.isInteger(config.maxVideos) && config.maxVideos > 0
        ? config.maxVideos
        : 6;

    const url = videosUrl(config.channel);
    console.log(`fetching: ${url}`);

    const html = await get(url);
    const data = extractInitialData(html);
    const { channelId, channelTitle } = parseChannel(html, data);
    const videos = parseVideos(data);

    if (videos.length === 0) {
        throw new Error(
            '動画を1件も抽出できませんでした。YouTube のページ構造が変わった可能性があります。' +
            '既存の data/videos.json は変更していません。',
        );
    }

    console.log(`channel: ${channelTitle ?? '(不明)'} / ${channelId ?? '(不明)'}`);
    console.log(`extracted ${videos.length} videos, keeping ${Math.min(videos.length, maxVideos)}`);

    const payload = {
        updatedAt: new Date().toISOString(),
        channelId,
        channelTitle,
        videos: videos.slice(0, maxVideos),
    };

    await mkdir(dirname(OUTPUT_PATH), { recursive: true });
    await writeFile(OUTPUT_PATH, JSON.stringify(payload, null, 2) + '\n', 'utf8');
    console.log('wrote data/videos.json');
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))) {
    main().catch((err) => {
        console.error(`[fetch-youtube] ${err.message}`);
        process.exit(1);
    });
}
