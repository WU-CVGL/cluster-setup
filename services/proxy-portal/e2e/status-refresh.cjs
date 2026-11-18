// The Status page refreshes itself: a controlled clock (performance.now, offset by the test) and a stubbed
// /api/status. It polls every 30 s and when the tab becomes visible, advances "N s ago" between polls, and turns
// results older than 90 s, counted from the last answer, to Unknown when polls fail.
//   node e2e/status-refresh.cjs <base URL>       (Playwright for Node in NODE_PATH; CHROME optional)
const { chromium } = require('playwright');

(async () => {
  const [base] = process.argv.slice(2);
  const args = ['--no-proxy-server'].concat(process.env.RESOLVE ? [`--host-resolver-rules=MAP ${process.env.RESOLVE}`] : []);
  const browser = await chromium.launch({ executablePath: process.env.CHROME || undefined, args });
  const context = await browser.newContext({ ignoreHTTPSErrors: true, locale: 'en-US',
    extraHTTPHeaders: { 'Accept-Language': 'en-US,en;q=0.9' } });
  await context.addInitScript(() => {
    const real = performance.now.bind(performance);
    let offset = 0;
    performance.now = () => real() + offset;
    window.__advance = ms => { offset += ms; };
  });
  const page = await context.newPage();
  const problems = [];
  let mode = 'fresh', polls = 0, template = null;
  page.on('console', m => { if (m.type() === 'error' && !(mode === 'fail' && m.text().includes('503'))) problems.push(m.text()); });
  page.on('pageerror', e => problems.push(e.message));
  const stub = async route => {
    polls++;
    if (mode === 'fail') return route.fulfill({ status: 503, body: 'unavailable' });
    const data = JSON.parse(JSON.stringify(template));
    for (const p of ['http', 'socks']) {
      Object.assign(data.entries['ha-general'].protocols[p], { state: 'passing', last_check_age_seconds: 11 });
    }
    for (const proxy of data.proxies) if (proxy.last_check_age_seconds !== null) proxy.last_check_age_seconds = 11;
    data.last_check_age_seconds = 11;
    return route.fulfill({ json: data });
  };
  // selectors that the page had before it refreshed itself too
  const read = () => page.evaluate(() => {
    const text = el => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
    const cards = document.querySelectorAll('.summary-grid .card .m-value');
    return { http: text(cards[0]), socks: text(cards[1]), last: text(document.querySelector('.hero-head .small.muted')),
      headline: text(document.querySelector('#headline')), hero: document.querySelector('.status-hero').className,
      count: text(document.querySelector('.status-number')),
      duration: text(document.querySelector('.table tbody tr:nth-child(2) td.mono.wide')) };
  });
  const results = [];
  const check = (label, ok, got) => { results.push(`${ok ? 'ok  ' : 'FAIL'} ${label}: ${JSON.stringify(got)}`); if (!ok) problems.push(label); };
  const advance = async seconds => { await page.evaluate(ms => window.__advance(ms), seconds * 1000); await page.waitForTimeout(1500); return read(); };

  await page.goto(base + '/', { waitUntil: 'networkidle' });
  template = await page.evaluate(() => fetch('/api/status').then(r => r.json()));  // the real answer's shape
  await page.route('**/api/status', stub);
  let s = await read();
  check('loaded: Passing, as rendered', s.http === 'Passing' && /s ago$/.test(s.last), s);
  s = await advance(30);
  check('30 s later: one poll, its result 11 s old', polls === 1 && s.http === 'Passing' && /: 1[1-4] s ago$/.test(s.last), [polls, s]);
  s = await advance(20);
  check('20 s later, no poll: "N s ago" advanced', polls === 1 && s.http === 'Passing' && /: 3[1-6] s ago$/.test(s.last), [polls, s]);
  mode = 'fail';
  s = await advance(10);
  check('a failed poll: still Passing at about 45 s', polls === 2 && s.http === 'Passing' && /: 4[2-9] s ago$/.test(s.last), [polls, s]);
  s = await advance(30);
  check('another failed poll: Passing at about 75 s', polls === 3 && s.http === 'Passing', [polls, s]);
  s = await advance(20);
  check('older than 90 s: Unknown, no duration, headline unknown', s.http === 'Unknown' && s.socks === 'Unknown' &&
    s.hero === 'status-hero unknown' && s.headline.startsWith('Status unknown') && s.count === '—' &&
    s.duration === '— / —' && /: 9[5-9] s ago$/.test(s.last), [polls, s]);
  mode = 'fresh';
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await page.waitForTimeout(1500);
  s = await read();
  check('tab visible again: polled at once, Passing', polls === 4 && s.http === 'Passing' && !s.hero.endsWith('unknown') &&
    /: 1[1-4] s ago$/.test(s.last), [polls, s]);
  console.log(results.join('\n'));
  await browser.close();
  if (problems.length) { console.error('problems:\n' + problems.join('\n')); process.exit(1); }
  console.log('status refresh: ok');
})().catch(e => { console.error(e); process.exit(1); });
