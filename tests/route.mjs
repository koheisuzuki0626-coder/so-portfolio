import { readFileSync, existsSync } from 'node:fs';
const SHOT = '/tmp/claude-0/-home-user-so-portfolio/a6db23ae-f40c-50fa-ba19-a042a9251f6e/scratchpad';
export function mockYtimg(page) {
  return page.route('**://i.ytimg.com/**', (route) => {
    const id = route.request().url().match(/\/vi\/([\w-]+)\//)?.[1];
    for (const k of ['max', 'mq']) {
      const f = `${SHOT}/real/${id}.${k}.jpg`;
      if (existsSync(f)) return route.fulfill({ status: 200, contentType: 'image/jpeg', body: readFileSync(f) });
    }
    return route.fulfill({ status: 200, contentType: 'image/gif',
      body: Buffer.from('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7', 'base64') });
  });
}
const cache = new Map();
export function mockFonts(page) {
  return page.route('**://fonts.{googleapis,gstatic}.com/**', async (route) => {
    const url = route.request().url();
    try {
      if (!cache.has(url)) {
        const res = await fetch(url, { headers: { 'user-agent':
          'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36' } });
        if (!res.ok) throw new Error(String(res.status));
        cache.set(url, { body: Buffer.from(await res.arrayBuffer()),
                         type: res.headers.get('content-type') || 'application/octet-stream' });
      }
      const h = cache.get(url);
      return route.fulfill({ status: 200, contentType: h.type, body: h.body });
    } catch { return route.fulfill({ status: 404, body: '' }); }
  });
}
