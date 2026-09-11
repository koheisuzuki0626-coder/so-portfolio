/* 全部まとめて走らせる。
     python3 -m http.server 8899 --directory . &
     node tests/run.mjs
   BASE で配信先を変えられる（本番に当てるときは BASE=https://... ） */
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
const HERE = dirname(fileURLToPath(import.meta.url));
const SUITES = ['pricing.mjs', 'header.mjs', 'content.mjs', 'funnel.mjs', 'a11y.mjs'];
let failed = 0;
for (const s of SUITES) {
    const out = await new Promise((res) => {
        const c = spawn('node', [join(HERE, s)], { env: process.env });
        let buf = '';
        c.stdout.on('data', (d) => { buf += d; });
        c.stderr.on('data', (d) => { buf += d; });
        c.on('close', (code) => res({ code, buf }));
    });
    const line = out.buf.split('\n').filter(l => l.startsWith('RESULT')).pop() || '(結果行なし)';
    console.log(`${s.padEnd(14)} ${line}`);
    if (out.code !== 0) { failed += 1; console.log(out.buf.split('\n').filter(l => l.startsWith('FAIL')).join('\n')); }
}
console.log(failed ? `\n${failed} スイートが FAILED` : '\nすべてのスイートが PASS');
process.exit(failed ? 1 : 0);
