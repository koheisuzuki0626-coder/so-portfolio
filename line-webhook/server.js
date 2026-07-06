'use strict';

/**
 * LINE Messaging API webhook with two-way media support.
 *
 * Text flow:
 *   LINE text -> claude "<text>" (execFile, no shell) -> reply stdout
 *
 * Outbound media (claude generates image/video):
 *   After each claude run we detect newly-created media in the work dir
 *   (mtime scan) AND file paths mentioned in stdout, copy them into a
 *   publicly-served directory, and reply with LINE image/video messages
 *   pointing at PUBLIC_BASE_URL (LINE requires public HTTPS URLs).
 *
 * Inbound media (user sends image/video to LINE):
 *   We download the binary via the Content API, save it, and remember it as
 *   a "pending attachment" for that user. The user's next text message is
 *   sent to claude with the saved file path(s) appended, so claude can read
 *   / edit / animate the file and generate new media back.
 *
 * Security: only allow-listed user IDs are served, and the request
 * signature is verified with the channel secret.
 */

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFile } = require('node:child_process');
const express = require('express');
const { middleware, messagingApi } = require('@line/bot-sdk');

// ---------------------------------------------------------------------------
// Configuration
// ---------------------------------------------------------------------------
const CHANNEL_ACCESS_TOKEN = process.env.LINE_CHANNEL_ACCESS_TOKEN;
const CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET;
const PORT = Number(process.env.PORT) || 3000;

const ALLOWED_USER_IDS = new Set(
  (process.env.ALLOWED_USER_IDS || '')
    .split(',')
    .map((id) => id.trim())
    .filter(Boolean)
);

const CLAUDE_BIN = process.env.CLAUDE_BIN || 'claude';
const COMMAND_TIMEOUT_MS = Number(process.env.COMMAND_TIMEOUT_MS) || 300_000;

// Public HTTPS base URL LINE can reach (e.g. https://xxxx.ngrok.io). Required
// to send media back; without it we fall back to text-only replies.
const PUBLIC_BASE_URL = (process.env.PUBLIC_BASE_URL || '').replace(/\/+$/, '');

// Directory where claude runs and drops generated files. Scanned after every
// run for new media. Kept clean (no node_modules) so scanning is cheap.
const WORK_DIR = path.resolve(process.env.WORK_DIR || path.join(__dirname, 'workspace'));

// Downloaded inbound media lands here. Kept OUTSIDE WORK_DIR so it is never
// mistaken for freshly generated output.
const INBOUND_DIR = path.resolve(process.env.INBOUND_DIR || path.join(__dirname, 'inbound'));

// Files served publicly at MEDIA_ROUTE. Generated media is copied here.
const MEDIA_SERVE_DIR = path.resolve(
  process.env.MEDIA_SERVE_DIR || path.join(__dirname, 'public-media')
);
const MEDIA_ROUTE = '/media';

const LINE_MAX_TEXT_LENGTH = 5000;
const MAX_MEDIA_PER_REPLY = 4; // LINE allows 5 messages/reply; keep 1 for text.
const IMAGE_MAX_BYTES = 10 * 1024 * 1024; // LINE image limit
const VIDEO_MAX_BYTES = 200 * 1024 * 1024; // LINE video limit

const IMAGE_EXTS = new Set(['.png', '.jpg', '.jpeg']);
const VIDEO_EXTS = new Set(['.mp4', '.mov', '.m4v']);
const MEDIA_EXTS = new Set([...IMAGE_EXTS, ...VIDEO_EXTS]);

// Minimal valid PNG (1x1) used as a video preview when ffmpeg is unavailable.
const PLACEHOLDER_PNG_B64 =
  'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==';

if (!CHANNEL_ACCESS_TOKEN || !CHANNEL_SECRET) {
  console.error('Missing LINE_CHANNEL_ACCESS_TOKEN and/or LINE_CHANNEL_SECRET');
  process.exit(1);
}
if (ALLOWED_USER_IDS.size === 0) {
  console.warn('ALLOWED_USER_IDS is empty — nobody can use the bot.');
}
if (!PUBLIC_BASE_URL) {
  console.warn('PUBLIC_BASE_URL not set — outbound media will be skipped (text only).');
}

for (const dir of [WORK_DIR, INBOUND_DIR, MEDIA_SERVE_DIR]) {
  fs.mkdirSync(dir, { recursive: true });
}
const PLACEHOLDER_PATH = path.join(MEDIA_SERVE_DIR, '_preview_placeholder.png');
fs.writeFileSync(PLACEHOLDER_PATH, Buffer.from(PLACEHOLDER_PNG_B64, 'base64'));

const lineClient = new messagingApi.MessagingApiClient({
  channelAccessToken: CHANNEL_ACCESS_TOKEN,
});

// Per-user pending inbound attachments (file paths waiting for a text prompt).
// In-memory only: cleared on restart.
const pendingAttachments = new Map();

// ---------------------------------------------------------------------------
// claude execution
// ---------------------------------------------------------------------------
function runClaude(text) {
  return new Promise((resolve, reject) => {
    execFile(
      CLAUDE_BIN,
      [text],
      {
        cwd: WORK_DIR,
        timeout: COMMAND_TIMEOUT_MS,
        maxBuffer: 20 * 1024 * 1024,
        // No `shell: true`: the message is a single argv element, so shell
        // metacharacters in it can never be interpreted as commands.
      },
      (error, stdout, stderr) => {
        if (error) {
          error.stderr = stderr;
          return reject(error);
        }
        resolve((stdout || '').trim());
      }
    );
  });
}

// ---------------------------------------------------------------------------
// Outbound media detection
// ---------------------------------------------------------------------------
/** Recursively list files under `dir`, skipping heavy/irrelevant folders. */
function walkFiles(dir, out = [], depth = 0) {
  if (depth > 6) return out;
  let entries;
  try {
    entries = fs.readdirSync(dir, { withFileTypes: true });
  } catch {
    return out;
  }
  for (const entry of entries) {
    if (entry.name.startsWith('.')) continue;
    if (entry.name === 'node_modules') continue;
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walkFiles(full, out, depth + 1);
    else if (entry.isFile()) out.push(full);
  }
  return out;
}

/** Media files under WORK_DIR modified at/after `sinceMs`. */
function scanNewMedia(sinceMs) {
  const results = [];
  for (const file of walkFiles(WORK_DIR)) {
    if (!MEDIA_EXTS.has(path.extname(file).toLowerCase())) continue;
    try {
      // -2000ms epsilon to tolerate filesystem timestamp granularity.
      if (fs.statSync(file).mtimeMs >= sinceMs - 2000) results.push(file);
    } catch {
      /* ignore */
    }
  }
  return results;
}

/** Existing media file paths mentioned in claude's stdout. */
function extractMediaPathsFromText(text) {
  const found = [];
  // Match quoted or bare tokens that end in a known media extension.
  const re = /(?:^|[\s"'`(<])([~./\w-][\w./~-]*\.(?:png|jpe?g|mp4|mov|m4v))/gi;
  let m;
  while ((m = re.exec(text)) !== null) {
    let p = m[1];
    if (p.startsWith('~/')) p = path.join(process.env.HOME || '', p.slice(2));
    const abs = path.isAbsolute(p) ? p : path.resolve(WORK_DIR, p);
    try {
      if (fs.statSync(abs).isFile()) found.push(abs);
    } catch {
      /* not a real file */
    }
  }
  return found;
}

function detectGeneratedMedia(sinceMs, stdout) {
  const set = new Map(); // realpath -> path
  for (const p of [...scanNewMedia(sinceMs), ...extractMediaPathsFromText(stdout)]) {
    let key;
    try {
      key = fs.realpathSync(p);
    } catch {
      key = p;
    }
    if (!set.has(key)) set.set(key, p);
  }
  return [...set.values()];
}

// ---------------------------------------------------------------------------
// Publishing media to a public URL + building LINE messages
// ---------------------------------------------------------------------------
function publicUrlFor(fileName) {
  return `${PUBLIC_BASE_URL}${MEDIA_ROUTE}/${encodeURIComponent(fileName)}`;
}

/** Copy `srcPath` into the served dir under a unique name; return the URL. */
function publish(srcPath) {
  const ext = path.extname(srcPath).toLowerCase();
  const name = `${crypto.randomUUID()}${ext}`;
  fs.copyFileSync(srcPath, path.join(MEDIA_SERVE_DIR, name));
  return { name, url: publicUrlFor(name) };
}

/** Best-effort first-frame JPEG preview for a video, else a placeholder. */
function makeVideoPreview(videoServedName) {
  const videoPath = path.join(MEDIA_SERVE_DIR, videoServedName);
  const previewName = `${path.parse(videoServedName).name}_preview.jpg`;
  const previewPath = path.join(MEDIA_SERVE_DIR, previewName);
  try {
    require('node:child_process').execFileSync(
      'ffmpeg',
      ['-y', '-loglevel', 'error', '-i', videoPath, '-frames:v', '1', previewPath],
      { timeout: 30_000 }
    );
    if (fs.existsSync(previewPath)) return publicUrlFor(previewName);
  } catch {
    /* ffmpeg missing or failed */
  }
  return publicUrlFor(path.basename(PLACEHOLDER_PATH));
}

/** Turn detected media file paths into LINE message objects. */
function buildMediaMessages(mediaPaths) {
  if (!PUBLIC_BASE_URL) return [];
  const messages = [];
  for (const src of mediaPaths) {
    if (messages.length >= MAX_MEDIA_PER_REPLY) break;
    const ext = path.extname(src).toLowerCase();
    let size;
    try {
      size = fs.statSync(src).size;
    } catch {
      continue;
    }
    if (IMAGE_EXTS.has(ext)) {
      if (size > IMAGE_MAX_BYTES) continue;
      const { url } = publish(src);
      messages.push({ type: 'image', originalContentUrl: url, previewImageUrl: url });
    } else if (VIDEO_EXTS.has(ext)) {
      if (size > VIDEO_MAX_BYTES) continue;
      const { name, url } = publish(src);
      messages.push({
        type: 'video',
        originalContentUrl: url,
        previewImageUrl: makeVideoPreview(name),
      });
    }
  }
  return messages;
}

// ---------------------------------------------------------------------------
// Inbound media download
// ---------------------------------------------------------------------------
const CONTENT_TYPE_EXT = {
  'image/jpeg': '.jpg',
  'image/png': '.png',
  'image/gif': '.gif',
  'video/mp4': '.mp4',
  'video/quicktime': '.mov',
  'audio/mp4': '.m4a',
  'audio/x-m4a': '.m4a',
  'audio/aac': '.aac',
};

function fallbackExt(message) {
  if (message.type === 'image') return '.jpg';
  if (message.type === 'video') return '.mp4';
  if (message.type === 'audio') return '.m4a';
  if (message.type === 'file' && message.fileName) return path.extname(message.fileName) || '.bin';
  return '.bin';
}

/** Download a message's binary content via the Content API and save it. */
async function downloadContent(message) {
  const url = `https://api-data.line.me/v2/bot/message/${message.id}/content`;
  const resp = await fetch(url, {
    headers: { Authorization: `Bearer ${CHANNEL_ACCESS_TOKEN}` },
  });
  if (!resp.ok) {
    throw new Error(`Content API returned ${resp.status}`);
  }
  const ext = CONTENT_TYPE_EXT[resp.headers.get('content-type')] || fallbackExt(message);
  const dest = path.join(INBOUND_DIR, `${crypto.randomUUID()}${ext}`);
  const buf = Buffer.from(await resp.arrayBuffer());
  fs.writeFileSync(dest, buf);
  return dest;
}

// ---------------------------------------------------------------------------
// Reply helpers
// ---------------------------------------------------------------------------
function toReplyText(raw) {
  const text = (raw || '').trim();
  if (!text) return '(コマンドの出力は空でした)';
  if (text.length <= LINE_MAX_TEXT_LENGTH) return text;
  const suffix = '\n…(出力が長いため切り詰めました)';
  return text.slice(0, LINE_MAX_TEXT_LENGTH - suffix.length) + suffix;
}

async function reply(replyToken, textOrNull, mediaMessages = []) {
  const messages = [];
  if (textOrNull && textOrNull.trim()) {
    messages.push({ type: 'text', text: toReplyText(textOrNull) });
  }
  messages.push(...mediaMessages);
  if (messages.length === 0) {
    messages.push({ type: 'text', text: '(出力はありませんでした)' });
  }
  await lineClient.replyMessage({ replyToken, messages: messages.slice(0, 5) });
}

// ---------------------------------------------------------------------------
// Event handling
// ---------------------------------------------------------------------------
async function handleTextMessage(event, userId) {
  const userText = event.message.text;

  // Fold in any inbound files the user sent just before this instruction.
  const pending = pendingAttachments.get(userId) || [];
  pendingAttachments.delete(userId);

  let prompt = userText;
  if (pending.length > 0) {
    prompt =
      `${userText}\n\n[添付ファイル] 以下のファイルを使用してください:\n` +
      pending.map((p) => p).join('\n');
  }

  console.info(`claude for ${userId} (attachments=${pending.length}): ${JSON.stringify(userText)}`);

  const startedAt = Date.now();
  let stdout;
  try {
    stdout = await runClaude(prompt);
  } catch (err) {
    console.error('claude failed:', err);
    const msg =
      err.killed && err.signal === 'SIGTERM'
        ? '(コマンドがタイムアウトしました)'
        : `(コマンドの実行に失敗しました)\n${(err.stderr || err.message || '').toString().trim().slice(0, 500)}`;
    await reply(event.replyToken, msg);
    return;
  }

  const media = detectGeneratedMedia(startedAt, stdout);
  const mediaMessages = buildMediaMessages(media);

  if (media.length > 0 && !PUBLIC_BASE_URL) {
    stdout = `${stdout}\n\n(生成物 ${media.length} 件がありますが、PUBLIC_BASE_URL 未設定のため送信できません)`;
  } else if (media.length > mediaMessages.length) {
    stdout = `${stdout}\n\n(生成物のうち ${mediaMessages.length}/${media.length} 件を送信しました)`;
  }

  await reply(event.replyToken, stdout, mediaMessages);
}

async function handleMediaMessage(event, userId) {
  try {
    const saved = await downloadContent(event.message);
    const list = pendingAttachments.get(userId) || [];
    list.push(saved);
    pendingAttachments.set(userId, list);
    console.info(`Saved inbound ${event.message.type} for ${userId}: ${saved}`);
    await reply(
      event.replyToken,
      `${event.message.type === 'video' ? '動画' : '画像'}を受け取りました（${list.length}件）。` +
        `\nこのファイルへの指示をテキストで送ってください（例:「この画像の背景を夜空にして」）。`
    );
  } catch (err) {
    console.error('Failed to download inbound media:', err);
    await reply(event.replyToken, '(ファイルの取得に失敗しました)');
  }
}

async function handleEvent(event) {
  if (event.type !== 'message') return;

  const userId = event.source && event.source.userId;
  if (!userId || !ALLOWED_USER_IDS.has(userId)) {
    console.info(`Ignoring message from unauthorized source: ${userId}`);
    return;
  }

  switch (event.message.type) {
    case 'text':
      return handleTextMessage(event, userId);
    case 'image':
    case 'video':
    case 'file':
      return handleMediaMessage(event, userId);
    default:
      return; // audio/location/sticker etc. ignored
  }
}

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------
const app = express();

app.get('/healthz', (_req, res) => res.status(200).send('ok'));

// Serve generated media publicly so LINE can fetch it.
app.use(MEDIA_ROUTE, express.static(MEDIA_SERVE_DIR, { maxAge: '1h' }));

app.post('/webhook', middleware({ channelSecret: CHANNEL_SECRET }), async (req, res) => {
  res.status(200).end(); // ack fast; LINE retries on slow responses
  const events = req.body.events || [];
  await Promise.all(
    events.map((event) =>
      handleEvent(event).catch((err) => console.error('Error processing event:', err))
    )
  );
});

app.use((err, _req, res, _next) => {
  if (err && err.name === 'SignatureValidationFailed') {
    console.warn('Rejected request with invalid LINE signature');
    return res.status(401).send('Invalid signature');
  }
  console.error('Unexpected error:', err);
  return res.status(500).send('Internal Server Error');
});

if (require.main === module) {
  app.listen(PORT, () => {
    console.log(`LINE webhook listening on port ${PORT}`);
    console.log(`Allow-listed user IDs: ${[...ALLOWED_USER_IDS].join(', ') || '(none)'}`);
    console.log(`Work dir: ${WORK_DIR}`);
    console.log(`Public base URL: ${PUBLIC_BASE_URL || '(unset — text only)'}`);
  });
}

// Exported for tests.
module.exports = {
  app,
  detectGeneratedMedia,
  extractMediaPathsFromText,
  scanNewMedia,
  buildMediaMessages,
};
