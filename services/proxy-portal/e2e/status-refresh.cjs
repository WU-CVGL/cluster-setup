// The Status page refreshes itself: a controlled clock (performance.now, offset by the test) and a stubbed
// /api/status. It polls every 30 s and when the tab becomes visible, drops a poll that gets no answer in 15 s,
// advances "N s ago" between polls, and turns results older than 90 s, counted from the last answer, to Unknown
// when polls fail, and the counts to "—", also when the default proxy was Unknown from the start. With
// "monitoring-down" (run.sh stops the stubs first), the page loads with monitoring data unavailable and drops that
// note once a poll answers with monitoring data.
//   node e2e/status-refresh.cjs <base URL> [monitoring-down]   (Playwright for Node in NODE_PATH; CHROME optional)
const { chromium } = require('playwright');

(async () => {
  const [base, scenario] = process.argv.slice(2);
  const args = ['--no-proxy-server'].concat(process.env.RESOLVE ? [`--host-resolver-rules=MAP ${process.env.RESOLVE}`] : []);
  const browser = await chromium.launch({ executablePath: process.env.CHROME || undefined, args });
  const clocked = async (locale, language) => {
    const context = await browser.newContext({ ignoreHTTPSErrors: true, locale,
      extraHTTPHeaders: { 'Accept-Language': language } });
    await context.addInitScript(() => {
      const real = performance.now.bind(performance);
      let offset = 0;
      performance.now = () => real() + offset;
      window.__advance = ms => { offset += ms; };
    });
    return context;
  };
  const context = await clocked('en-US', 'en-US,en;q=0.9');
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
    data.entries['ha-general'].last_check_age_seconds = 11;
    for (const proxy of data.proxies) if (proxy.last_check_age_seconds !== null) proxy.last_check_age_seconds = 11;
    data.last_check_age_seconds = 11;
    return route.fulfill({ json: data });
  };
  // selectors that the page had before it refreshed itself too
  // the HA rows and the first proxy row of the tables
  const read = (on = page) => on.evaluate(() => {
    const text = el => (el ? el.textContent.replace(/\s+/g, ' ').trim() : null);
    const general = document.querySelector('tr.row-main[data-row="ha-general"]');
    const proxy = document.querySelector('section[aria-labelledby="proxies-title"] tr.row-main');
    return { http: text(general.querySelector('[data-live-state="entries.ha-general.protocols.http"]')),
      socks: text(general.querySelector('[data-live-state="entries.ha-general.protocols.socks"]')),
      last: text(document.querySelector('.hero-head .small.muted')),
      headline: text(document.querySelector('#headline')), hero: document.querySelector('.status-hero').className,
      count: text(document.querySelector('.status-number')),
      haDuration: text(general.querySelector('td.mono.wide')), haLast: text(general.querySelector('[data-live-age]')),
      notes: Array.from(document.querySelectorAll('.status-hero p')).filter(p => p.offsetParent !== null).map(text),
      entries: Array.from(document.querySelectorAll('[data-live-entry-count]')).map(text),
      duration: text(proxy.querySelector('td.mono.wide')),
      cards: document.querySelectorAll('.summary-grid, .metric').length };
  });
  const results = [];
  const check = (label, ok, got) => { results.push(`${ok ? 'ok  ' : 'FAIL'} ${label}: ${JSON.stringify(got)}`); if (!ok) problems.push(label); };
  const advance = async (seconds, on = page) => {
    await on.evaluate(ms => window.__advance(ms), seconds * 1000); await on.waitForTimeout(1500); return read(on); };
  const report = () => {
    console.log(results.join('\n'));
    if (problems.length) { console.error('problems:\n' + problems.join('\n')); process.exit(1); }
    console.log('status refresh: ok');
  };
  const fresh = (d, monitoring) => {  // an answer from /api/status, fresh: both entries passing, results 11 s old
    const data = JSON.parse(JSON.stringify(d));
    for (const entry of Object.values(data.entries)) {
      for (const p of ['http', 'socks']) Object.assign(entry.protocols[p], { state: 'passing', last_check_age_seconds: 11 });
      entry.last_check_age_seconds = 11;
    }
    for (const proxy of data.proxies) if (proxy.last_check_age_seconds !== null) proxy.last_check_age_seconds = 11;
    data.last_check_age_seconds = 11;
    if (monitoring) Object.assign(data, monitoring);
    return data;
  };

  if (scenario === 'monitoring-down') {
    // the page as the portal renders it without Prometheus; then a poll answers with monitoring data
    let answers = 0;
    await page.goto(base + '/', { waitUntil: 'networkidle' });
    const down = await page.evaluate(() => fetch('/api/status').then(r => r.json()));
    const up = fresh(down, { monitoring_available: true, headline: { level: 'ok', text: 'Default proxy available' } });
    up.entries['ha-general'].proxies = { passing: 4, configured: 4 };
    await page.route('**/api/status', route => { answers++; return route.fulfill({ json: up }); });
    let s = await read();
    check('loaded without monitoring data: the note, Unknown', down.monitoring_available === false &&
      s.notes.length === 1 && s.notes[0].startsWith('Monitoring data is unavailable') && s.http === 'Unknown', [down.monitoring_available, s]);
    s = await advance(30);
    check('a poll with monitoring data: the note gone, Passing', answers === 1 && s.notes.length === 1 &&
      !s.notes[0].startsWith('Monitoring data is unavailable') && s.http === 'Passing' && /s ago$/.test(s.haLast) && s.count === '4 / 4', [answers, s]);
    await browser.close();
    return report();
  }

  await page.goto(base + '/', { waitUntil: 'networkidle' });
  template = await page.evaluate(() => fetch('/api/status').then(r => r.json()));  // the real answer's shape
  await page.route('**/api/status', stub);
  let s = await read();
  const loaded = s.entries;
  check('loaded: Passing, as rendered, without metric cards', s.http === 'Passing' && /s ago$/.test(s.last) &&
    s.cards === 0 && /^\d+ ms \/ \d+ ms$/.test(s.haDuration) && /s ago$/.test(s.haLast) &&
    loaded.length === 2 && loaded.every(e => /^\d+ of \d+ proxies passing$/.test(e)), s);
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
    s.duration === '— / —' && s.haDuration === '— / —' && /: 9[5-9] s ago$/.test(s.last) &&
    s.entries.every(e => e === '— of — proxies passing'), [polls, s]);
  mode = 'fresh';
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await page.waitForTimeout(1500);
  s = await read();
  check('tab visible again: polled at once, Passing', polls === 4 && s.http === 'Passing' && !s.hero.endsWith('unknown') &&
    /: 1[1-4] s ago$/.test(s.last) && JSON.stringify(s.entries) === JSON.stringify(loaded), [polls, s]);

  // a poll that never gets an answer: dropped after 15 s, so the next poll still comes
  const hangPage = await context.newPage();
  hangPage.on('pageerror', e => problems.push(e.message));
  hangPage.on('console', m => { if (m.type() === 'error') problems.push(m.text()); });
  let hangPolls = 0;
  await hangPage.goto(base + '/', { waitUntil: 'networkidle' });
  await hangPage.route('**/api/status', route => { if (++hangPolls > 1) return route.fulfill({ json: fresh(template) }); });
  await advance(30, hangPage);
  s = await advance(20, hangPage);
  check('a poll with no answer: still one poll after 20 s', hangPolls === 1, [hangPolls, s]);
  s = await advance(15, hangPage);
  check('a poll with no answer: dropped, the next poll at 30 s, Passing', hangPolls === 2 && s.http === 'Passing' &&
    /: 1[1-4] s ago$/.test(s.last), [hangPolls, s]);
  await hangPage.close();

  // both HA entries Unknown from the start, the proxy counts known, every poll failing: the counts age out too
  const unknownPage = await context.newPage();
  unknownPage.on('pageerror', e => problems.push(e.message));
  // the page as the portal serves it, fetched by the browser (which resolves the test host), with its CSP
  const served = await page.evaluate(() => fetch('/').then(async r => [await r.text(), r.headers.get('content-security-policy')]));
  await unknownPage.route(base + '/', route => {
    const body = served[0].replace(/(<script id="status-live" type="application\/json">)(.*?)(<\/script>)/s,
      (_, open, json, close) => {
        const data = JSON.parse(json);
        for (const entry of Object.values(data.entries)) for (const p of ['http', 'socks']) {
          Object.assign(entry.protocols[p], { state: 'unknown', last_check_age_seconds: null });
        }
        Object.assign(data, { last_check_age_seconds: null,
          headline: { level: 'unknown', text: 'Status unknown: no recent probe results for the default proxy' } });
        return open + JSON.stringify(data).replace(/</g, '\\u003C') + close;
      });
    return route.fulfill({ contentType: 'text/html; charset=utf-8', headers: { 'content-security-policy': served[1] }, body });
  });
  await unknownPage.route('**/api/status', route => route.fulfill({ status: 503, body: 'unavailable' }));
  await unknownPage.goto(base + '/', { waitUntil: 'networkidle' });
  await unknownPage.waitForTimeout(1500);
  s = await read(unknownPage);
  check('Unknown from the start: the counts still shown', s.http === 'Unknown' && s.hero === 'status-hero unknown' &&
    s.count === '3 / 4' &&
    JSON.stringify(s.entries) === JSON.stringify(['3 of 4 proxies passing', '7 of 9 proxies passing']), s);
  s = await advance(60, unknownPage);
  check('Unknown from the start, about 60 s: the counts still shown', s.count === '3 / 4', s);
  s = await advance(40, unknownPage);
  check('Unknown from the start, polls failing, older than 90 s: no counts', s.count === '—' &&
    s.entries.every(e => e === '— of — proxies passing'), s);

  // the same entry line in Chinese, once the results are older than 90 s
  const zh = await clocked('zh-CN', 'zh-Hans,zh;q=0.9');
  const zhPage = await zh.newPage();
  zhPage.on('pageerror', e => problems.push(e.message));
  await zhPage.goto(base + '/', { waitUntil: 'networkidle' });
  await zhPage.route('**/api/status', route => route.fulfill({ status: 503, body: 'unavailable' }));
  const zhEntries = () => zhPage.evaluate(() => Array.from(document.querySelectorAll('[data-live-entry-count]'))
    .map(el => el.textContent.replace(/\s+/g, ' ').trim()));
  const zhLoaded = await zhEntries();
  await zhPage.evaluate(ms => window.__advance(ms), 100000);
  await zhPage.waitForTimeout(1500);
  const zhStale = await zhEntries();
  check('zh-Hans: the entry line, then "—" past 90 s', zhLoaded.every(e => /^\d+ 个代理中 \d+ 个正常$/.test(e)) &&
    zhStale.length === 2 && zhStale.every(e => e === '— 个代理中 — 个正常'), [zhLoaded, zhStale]);
  await browser.close();
  report();
})().catch(e => { console.error(e); process.exit(1); });
