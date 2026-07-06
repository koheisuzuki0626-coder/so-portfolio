'use strict';

/**
 * LINE Messaging API webhook.
 *
 * Flow:
 *   1. LINE delivers a webhook event when a user sends a message.
 *   2. The request signature is verified against the channel secret
 *      (handled by @line/bot-sdk's middleware) so only genuine LINE
 *      requests are processed.
 *   3. Only messages from an allow-listed user ID are acted on.
 *   4. The text is passed to the `claude` CLI as a single argument
 *      (via execFile, never a shell string) and its stdout is sent back.
 */

const { execFile } = require('node:child_process');
const express = require('express');
const { middleware, messagingApi } = require('@line/bot-sdk');

// ---------------------------------------------------------------------------
// Configuration (all via environment variables)
// ---------------------------------------------------------------------------
const CHANNEL_ACCESS_TOKEN = process.env.LINE_CHANNEL_ACCESS_TOKEN;
const CHANNEL_SECRET = process.env.LINE_CHANNEL_SECRET;
const PORT = Number(process.env.PORT) || 3000;

// Comma-separated list of LINE user IDs that are allowed to use the bot.
// e.g. ALLOWED_USER_IDS="Uxxxxxxxxxxxxxxxx,Uyyyyyyyyyyyyyyyy"
const ALLOWED_USER_IDS = new Set(
  (process.env.ALLOWED_USER_IDS || '')
    .split(',')
    .map((id) => id.trim())
    .filter(Boolean)
);

// The command to run. Defaults to `claude`; override with CLAUDE_BIN if the
// binary lives elsewhere (e.g. an absolute path).
const CLAUDE_BIN = process.env.CLAUDE_BIN || 'claude';

// How long to let the command run before giving up (ms).
const COMMAND_TIMEOUT_MS = Number(process.env.COMMAND_TIMEOUT_MS) || 120_000;

// LINE hard-caps a single text message at 5000 characters.
const LINE_MAX_TEXT_LENGTH = 5000;

if (!CHANNEL_ACCESS_TOKEN || !CHANNEL_SECRET) {
  console.error(
    'Missing required env vars: LINE_CHANNEL_ACCESS_TOKEN and/or LINE_CHANNEL_SECRET'
  );
  process.exit(1);
}

if (ALLOWED_USER_IDS.size === 0) {
  console.warn(
    'ALLOWED_USER_IDS is empty — no user will be allowed to use the bot. ' +
      'Set ALLOWED_USER_IDS to enable it.'
  );
}

// ---------------------------------------------------------------------------
// LINE client
// ---------------------------------------------------------------------------
const lineClient = new messagingApi.MessagingApiClient({
  channelAccessToken: CHANNEL_ACCESS_TOKEN,
});

/**
 * Run the Claude CLI with `text` as a single, un-shell-interpreted argument.
 * Returns the trimmed stdout, or throws on failure.
 */
function runClaude(text) {
  return new Promise((resolve, reject) => {
    execFile(
      CLAUDE_BIN,
      [text],
      {
        timeout: COMMAND_TIMEOUT_MS,
        maxBuffer: 10 * 1024 * 1024, // 10 MB of output
        // Deliberately NOT using `shell: true` — passing the message as an
        // argv element means the shell never parses the user's text, so
        // metacharacters like ", $(), ; and | cannot inject commands.
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

/** LINE rejects empty text and anything over 5000 chars — normalise here. */
function toReplyText(raw) {
  const text = (raw || '').trim();
  if (!text) return '(コマンドの出力は空でした)';
  if (text.length <= LINE_MAX_TEXT_LENGTH) return text;
  const suffix = '\n…(出力が長いため切り詰めました)';
  return text.slice(0, LINE_MAX_TEXT_LENGTH - suffix.length) + suffix;
}

/**
 * Handle a single webhook event. Only text messages from an allow-listed
 * user trigger the CLI; everything else is ignored.
 */
async function handleEvent(event) {
  if (event.type !== 'message' || event.message.type !== 'text') {
    return;
  }

  const userId = event.source && event.source.userId;
  if (!userId || !ALLOWED_USER_IDS.has(userId)) {
    // Silently ignore anyone not on the allow-list.
    console.info(`Ignoring message from unauthorized source: ${userId}`);
    return;
  }

  const text = event.message.text;
  console.info(`Running claude for user ${userId}: ${JSON.stringify(text)}`);

  let replyText;
  try {
    const output = await runClaude(text);
    replyText = toReplyText(output);
  } catch (err) {
    console.error('claude command failed:', err);
    if (err.killed && err.signal === 'SIGTERM') {
      replyText = '(コマンドがタイムアウトしました)';
    } else {
      replyText = `(コマンドの実行に失敗しました)\n${(err.stderr || err.message || '')
        .toString()
        .trim()
        .slice(0, 500)}`;
    }
  }

  await lineClient.replyMessage({
    replyToken: event.replyToken,
    messages: [{ type: 'text', text: toReplyText(replyText) }],
  });
}

// ---------------------------------------------------------------------------
// HTTP server
// ---------------------------------------------------------------------------
const app = express();

// Simple health check (no signature required).
app.get('/healthz', (_req, res) => res.status(200).send('ok'));

// The LINE middleware verifies the X-Line-Signature header using the channel
// secret and parses the body. It must run before we read req.body.events.
app.post(
  '/webhook',
  middleware({ channelSecret: CHANNEL_SECRET }),
  async (req, res) => {
    // Acknowledge immediately; LINE expects a fast 200 and retries otherwise.
    res.status(200).end();

    const events = req.body.events || [];
    await Promise.all(
      events.map((event) =>
        handleEvent(event).catch((err) =>
          console.error('Unhandled error while processing event:', err)
        )
      )
    );
  }
);

// Signature-verification failures from the middleware surface as errors here.
app.use((err, _req, res, _next) => {
  if (err && err.name === 'SignatureValidationFailed') {
    console.warn('Rejected request with invalid LINE signature');
    return res.status(401).send('Invalid signature');
  }
  console.error('Unexpected error:', err);
  return res.status(500).send('Internal Server Error');
});

app.listen(PORT, () => {
  console.log(`LINE webhook listening on port ${PORT}`);
  console.log(`Allow-listed user IDs: ${[...ALLOWED_USER_IDS].join(', ') || '(none)'}`);
});
