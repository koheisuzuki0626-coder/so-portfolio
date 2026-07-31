#!/usr/bin/env node
// YouTube チャンネルの新着動画を RSS(Atom)から取得し data/videos.json に書き出す。
// API キー不要。GitHub Actions から定期実行される。
//
// 使い方: node scripts/fetch-youtube.mjs
// 設定:   youtube.config.json の "channel" に @ハンドル / チャンネルURL / UC... を指定

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

/* ---------- XML helpers ---------- */

const ENTITIES = {
    amp: '&', lt: '<', gt: '>', quot: '"', apos: "'", nbsp: ' ',
};

export function decodeEntities(text) {
    return String(text).replace(/&(#x?[0-9a-fA-F]+|[a-zA-Z]+);/g, (match, body) => {
        if (body[0] === '#') {
            const code = body[1] === 'x' || body[1] === 'X'
                ? parseInt(body.slice(2), 16)
                : parseInt(body.slice(1), 10);
            return Number.isFinite(code) ? String.fromCodePoint(code) : match;
        }
        return Object.prototype.hasOwnProperty.call(ENTITIES, body) ? ENTITIES[body] : match;
    });
}

function tagText(xml, tag) {
    // CDATA と通常テキストの両方に対応
    const re = new RegExp(`<${tag}(?:\\s[^>]*)?>([\\s\\S]*?)</${tag}>`);
    const m = xml.match(re);
    if (!m) return null;
    const raw = m[1].replace(/^<!\[CDATA\[([\s\S]*?)\]\]>$/, '$1');
    return decodeEntities(raw).trim();
}

function attr(xml, tag, name) {
    const re = new RegExp(`<${tag}\\b[^>]*\\b${name}="([^"]*)"`);
    const m = xml.match(re);
    return m ? decodeEntities(m[1]) : null;
}

/**
 * YouTube の Atom フィードを動画配列にパースする。
 * @param {string} xml raw feed body
 */
export function parseFeed(xml) {
    const entries = xml.match(/<entry\b[\s\S]*?<\/entry>/g) ?? [];

    const videos = entries.map((entry) => {
        const id = tagText(entry, 'yt:videoId') ?? tagText(entry, 'videoId');
        if (!id) return null;

        const published = tagText(entry, 'published');
        return {
            id,
            title: tagText(entry, 'title') ?? '',
            description: tagText(entry, 'media:description') ?? '',
            publishedAt: published,
            // 年だけは Works のメタ表記に使うので先に出しておく
            year: published ? published.slice(0, 4) : null,
            url: attr(entry, 'link', 'href') ?? `https://www.youtube.com/watch?v=${id}`,
            thumbnail: attr(entry, 'media:thumbnail', 'url')
                ?? `https://i.ytimg.com/vi/${id}/hqdefault.jpg`,
        };
    }).filter(Boolean);

    return {
        channelId: tagText(xml, 'yt:channelId'),
        channelTitle: tagText(xml, 'title'),
        videos,
    };
}

/* ---------- network ---------- */

async function get(url) {
    const res = await fetch(url, { headers: { 'user-agent': UA, 'accept-language': 'ja,en;q=0.8' } });
    if (!res.ok) throw new Error(`GET ${url} -> ${res.status} ${res.statusText}`);
    return res.text();
}

/**
 * @ハンドル / チャンネルURL / UC... を channelId に解決する。
 */
export async function resolveChannelId(channel) {
    const value = String(channel ?? '').trim();
    if (!value || value.includes('REPLACE_ME')) {
        throw new Error('youtube.config.json の "channel" が未設定です(@ハンドル か UC... を指定してください)');
    }

    if (CHANNEL_ID_RE.test(value)) return value;

    const direct = value.match(/\/channel\/(UC[\w-]{22})/);
    if (direct) return direct[1];

    const pageUrl = value.startsWith('http')
        ? value
        : `https://www.youtube.com/${value.startsWith('@') ? value : '@' + value}`;

    const html = await get(pageUrl);
    const found = html.match(/"(?:channelId|externalId)":"(UC[\w-]{22})"/)
        ?? html.match(/<meta itemprop="(?:channelId|identifier)" content="(UC[\w-]{22})">/)
        ?? html.match(/channel\/(UC[\w-]{22})/);

    if (!found) throw new Error(`チャンネルIDを解決できませんでした: ${pageUrl}`);
    return found[1];
}

export async function fetchFeed(channelId) {
    return get(`https://www.youtube.com/feeds/videos.xml?channel_id=${encodeURIComponent(channelId)}`);
}

/* ---------- main ---------- */

async function main() {
    const config = JSON.parse(await readFile(CONFIG_PATH, 'utf8'));
    const maxVideos = Number.isInteger(config.maxVideos) && config.maxVideos > 0
        ? config.maxVideos
        : 6;

    const channelId = await resolveChannelId(config.channel);
    console.log(`channelId: ${channelId}`);

    const parsed = parseFeed(await fetchFeed(channelId));
    if (parsed.videos.length === 0) {
        throw new Error('フィードから動画を1件も取得できませんでした(既存の videos.json は変更しません)');
    }

    const payload = {
        updatedAt: new Date().toISOString(),
        channelId: parsed.channelId ?? channelId,
        channelTitle: parsed.channelTitle,
        videos: parsed.videos.slice(0, maxVideos),
    };

    await mkdir(dirname(OUTPUT_PATH), { recursive: true });
    await writeFile(OUTPUT_PATH, JSON.stringify(payload, null, 2) + '\n', 'utf8');
    console.log(`wrote ${payload.videos.length} videos -> data/videos.json`);
}

// 直接実行されたときだけ main を走らせる(パーサ単体テストから import できるように)
if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))) {
    main().catch((err) => {
        console.error(`[fetch-youtube] ${err.message}`);
        process.exit(1);
    });
}
