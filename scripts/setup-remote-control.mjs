#!/usr/bin/env node
// Claude Code の Remote Control(スマホ同期)を有効にする。
//
// ~/.claude/settings.json に以下の3つのキーを書き込む。
//
//   remoteControlAtStartup   : セッション起動時に自動でスマホへ繋ぐ
//   agentPushNotifEnabled    : 長い処理が終わったときスマホへ通知
//   inputNeededNotifEnabled  : 許可・質問待ちになったときスマホへ通知
//
// 既存の設定は保持し、書き換える前に .bak を残す。
// JSON が壊れているときは何も書かずに中断する(黙って設定を飛ばさない)。
//
// あわせて「Remote Control が使えなくなる環境変数」が無いかも調べる。
// これらが1つでも立っていると設定を入れても繋がらないため、先に潰す必要がある。
//
// 使い方:
//   node scripts/setup-remote-control.mjs             書き込む
//   node scripts/setup-remote-control.mjs --dry-run   確認だけ(書き込まない)
//   node scripts/setup-remote-control.mjs --check     環境の確認だけ
//   node scripts/setup-remote-control.mjs --off       3つのキーを false に戻す

import { readFile, writeFile, copyFile, mkdir } from 'node:fs/promises';
import { homedir } from 'node:os';
import { resolve } from 'node:path';

const SETTINGS_DIR = resolve(homedir(), '.claude');
const SETTINGS_PATH = resolve(SETTINGS_DIR, 'settings.json');

const KEYS = [
    ['remoteControlAtStartup', '全セッションを自動でスマホに繋ぐ'],
    ['agentPushNotifEnabled', '処理完了時にスマホへ通知'],
    ['inputNeededNotifEnabled', '許可・質問待ちのときスマホへ通知'],
];

// これが設定されていると Remote Control は起動しない。
// 値まで見ないと判断できないものは check に関数を持たせる。
const BLOCKERS = [
    ['ANTHROPIC_API_KEY', 'APIキー認証では使えない。claude auth login でログインし直す'],
    ['CLAUDE_CODE_USE_BEDROCK', 'Amazon Bedrock 経由では使えない'],
    ['CLAUDE_CODE_USE_VERTEX', 'Google Cloud 経由では使えない'],
    ['DISABLE_TELEMETRY', '機能フラグの判定が止まるため使えない'],
    ['DO_NOT_TRACK', '機能フラグの判定が止まるため使えない'],
    ['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC', '機能フラグの判定が止まるため使えない'],
    ['DISABLE_GROWTHBOOK', '機能フラグの判定が止まるため使えない'],
    [
        'ANTHROPIC_BASE_URL',
        'api.anthropic.com 以外を指していると使えない',
        (v) => v && !/(^|\/\/)api\.anthropic\.com(\/|$)/.test(v),
    ],
];

const args = new Set(process.argv.slice(2));
const dryRun = args.has('--dry-run');
const checkOnly = args.has('--check');
const turnOff = args.has('--off');
const target = !turnOff;

/* ---------- 設定ファイルの読み込み ---------- */

// 見つからなければ null。壊れていれば例外。
async function readSettings() {
    let raw;
    try {
        raw = await readFile(SETTINGS_PATH, 'utf8');
    } catch (err) {
        if (err.code === 'ENOENT') return null;
        throw err;
    }
    if (raw.trim() === '') return {};
    try {
        return JSON.parse(raw);
    } catch (err) {
        throw new Error(
            `${SETTINGS_PATH} が JSON として読めません(${err.message})。\n` +
                '  手で直してから、もう一度実行してください。壊れたまま上書きはしません。'
        );
    }
}

/* ---------- 環境の確認 ---------- */

// process.env と settings.json の env ブロックの両方を見る。
// 片方だけ消しても、もう片方が残っていると繋がらないため。
function findBlockers(settings) {
    const found = [];
    const settingsEnv = (settings && settings.env) || {};

    for (const [name, why, check] of BLOCKERS) {
        for (const [where, value] of [
            ['シェルの環境変数', process.env[name]],
            ['settings.json の env', settingsEnv[name]],
        ]) {
            if (value === undefined) continue;
            if (check ? check(value) : true) {
                found.push({ name, why, where, value });
            }
        }
    }

    if (settings && settings.disableRemoteControl === true) {
        found.push({
            name: 'disableRemoteControl',
            why: 'Remote Control が明示的に無効化されている',
            where: 'settings.json',
            value: 'true',
        });
    }
    return found;
}

/* ---------- 実行 ---------- */

async function main() {
    let settings;
    try {
        settings = await readSettings();
    } catch (err) {
        console.error(`エラー: ${err.message}`);
        process.exit(1);
    }

    const isNew = settings === null;
    settings ??= {};

    console.log(`設定ファイル: ${SETTINGS_PATH}${isNew ? ' (新規作成)' : ''}\n`);

    // 1. 環境の確認
    const blockers = findBlockers(settings);
    if (blockers.length === 0) {
        console.log('環境の確認: 問題なし\n');
    } else {
        console.log('環境の確認: 以下が Remote Control を止めます。先に解除してください。\n');
        for (const b of blockers) {
            console.log(`  ✗ ${b.name}  (${b.where})`);
            console.log(`      ${b.why}`);
        }
        console.log('');
    }

    if (checkOnly) return;

    // 2. 差分の表示
    const changes = KEYS.filter(([key]) => settings[key] !== target);
    if (changes.length === 0) {
        console.log(`3つのキーはすでに ${target} です。変更はありません。\n`);
    } else {
        console.log(`次のように${dryRun ? '変更します(--dry-run のため書き込みません)' : '変更します'}:\n`);
        for (const [key, label] of changes) {
            const before = key in settings ? JSON.stringify(settings[key]) : '(未設定)';
            console.log(`  ${key}`);
            console.log(`      ${label}`);
            console.log(`      ${before} → ${target}`);
        }
        console.log('');
    }

    if (dryRun || changes.length === 0) {
        printNextSteps(blockers);
        return;
    }

    // 3. 書き込み(既存があればバックアップを取ってから)
    if (!isNew) {
        const backup = `${SETTINGS_PATH}.bak`;
        await copyFile(SETTINGS_PATH, backup);
        console.log(`バックアップ: ${backup}`);
    }

    for (const [key] of KEYS) settings[key] = target;

    await mkdir(SETTINGS_DIR, { recursive: true });
    await writeFile(SETTINGS_PATH, `${JSON.stringify(settings, null, 2)}\n`, 'utf8');
    console.log('書き込みました。\n');

    printNextSteps(blockers);
}

function printNextSteps(blockers) {
    if (turnOff) {
        console.log('次の手順: Claude Code を再起動すると自動接続が切れます。');
        return;
    }
    console.log('次の手順:');
    if (blockers.length > 0) {
        console.log('  0. 上に出た環境変数を解除する(これを消さないと繋がりません)');
    }
    console.log('  1. Claude Code を一度終了して、プロジェクトのフォルダで claude を起動し直す');
    console.log('  2. 入力欄の下に /rc active と出れば接続済み');
    console.log('  3. スマホの Claude アプリ → 下部の Code タブ を開くとセッションが並ぶ');
    console.log('     (PCアイコン＋緑の点 = オンライン)');
    console.log('');
    console.log('  通知が来ないときは、スマホでアプリを一度開いてから繋ぎ直してください。');
    console.log('  アプリを開かないとプッシュ用のトークンが登録されません。');
}

main().catch((err) => {
    console.error(`エラー: ${err.message}`);
    process.exit(1);
});
