// The Status page's status bars, in English and Chinese at desktop and phone width: no metric cards and no separate
// history; a 96-hour and a 30-day bar list per row, the hourly one shown first; the switch, used with the keyboard,
// changes every row, the legend with them, and is kept across a reload (and still works when storage throws); a
// bar's tooltip on focus and on hover, the arrow keys, Home and Esc; no page overflow, with the bars inside their row.
// Saves screenshots.
//   node e2e/status-bars.cjs <base URL> <output folder>   (Playwright for Node in NODE_PATH; CHROME optional)
const { chromium } = require('playwright');
const fs = require('fs');

(async () => {
  const [base, out] = process.argv.slice(2);
  fs.mkdirSync(out, { recursive: true });
  const args = ['--no-proxy-server'].concat(process.env.RESOLVE ? [`--host-resolver-rules=MAP ${process.env.RESOLVE}`] : []);
  const browser = await chromium.launch({ executablePath: process.env.CHROME || undefined, args });
  const problems = [];
  const results = [];
  const check = (label, ok, got) => {
    results.push(`${ok ? 'ok  ' : 'FAIL'} ${label}: ${JSON.stringify(got)}`); if (!ok) problems.push(label); };
  const T = {
    en: { hours: '96 hours', days: '30 days', ago: ['96 hours ago', '30 days ago'], state: /· (Passing|Degraded|Failing|Unknown) ·/ },
    zh: { hours: '96 小时', days: '30 天', ago: ['96 小时前', '30 天前'], state: /· (正常|部分异常|故障|未知) ·/ },
  };
  const layout = page => page.evaluate(() => {
    const visible = el => el.offsetParent !== null;
    const rows = Array.from(document.querySelectorAll('tr.row-bars'));
    const lists = rows.map(r => Array.from(r.querySelectorAll('.bars')).filter(visible));
    const switchEl = document.querySelector('[data-switch="bars"]');
    const pressed = Array.from(switchEl.querySelectorAll('button')).map(b => [b.textContent.trim(), b.getAttribute('aria-pressed')]);
    // each shown list inside its row's cell and its card, and not wider than its own box
    const outside = lists.flat().filter(l => {
      const card = l.closest('.card').getBoundingClientRect(), r = l.getBoundingClientRect();
      return l.scrollWidth > l.clientWidth + 1 || r.left < card.left - 1 || r.right > card.right + 1;
    }).length;
    const bar = document.querySelector('tr.row-bars .bars:not([hidden]) .bar');
    return {
      rows: rows.length, shown: lists.map(l => l.length), counts: [...new Set(lists.flat().map(l => l.children.length))],
      kinds: [...new Set(lists.flat().map(l => l.getAttribute('data-value')))], pressed, switchShown: visible(switchEl),
      legend: [...new Set(Array.from(document.querySelectorAll('.bars-legend [data-panel="bars"]')).filter(visible).map(e => e.textContent.trim()))],
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth, outside,
      barWidth: bar ? Math.round(bar.getBoundingClientRect().width * 10) / 10 : null,
      cards: document.querySelectorAll('.summary-grid, .metric, ol.history').length,
    };
  });
  const tip = page => page.evaluate(() => {
    const t = document.querySelector('.bar-tip'), a = document.activeElement;
    const r = t && !t.hidden ? t.getBoundingClientRect() : null;
    return { shown: !!r, text: r ? t.textContent : null, active: a && a.classList.contains('bar') ? a.getAttribute('aria-label') : null,
      index: a && a.classList.contains('bar') ? Array.prototype.indexOf.call(a.parentNode.children, a) : null,
      inside: r ? r.left >= 0 && r.top >= 0 && r.right <= innerWidth && r.bottom <= innerHeight : null };
  });

  for (const [lang, header] of [['en', 'en-US,en;q=0.9'], ['zh', 'zh-CN,zh;q=0.9']]) {
    for (const [device, viewport] of [['desktop', { width: 1280, height: 900 }], ['phone', { width: 390, height: 844 }]]) {
      const t = T[lang], tag = `${lang}/${device}`;
      const context = await browser.newContext({ viewport, ignoreHTTPSErrors: true, locale: lang === 'zh' ? 'zh-CN' : 'en-US',
        timezoneId: 'Asia/Shanghai', extraHTTPHeaders: { 'Accept-Language': header } });
      const page = await context.newPage();
      page.on('console', m => { if (m.type() === 'error') problems.push(`${tag} console: ${m.text()}`); });
      page.on('pageerror', e => problems.push(`${tag} page error: ${e.message}`));
      await page.goto(base + '/', { waitUntil: 'networkidle' });
      let s = await layout(page);
      check(`${tag} loaded: 96 hourly bars per row, no cards, no overflow`, s.cards === 0 && s.rows >= 2 &&
        s.shown.every(n => n === 1) && JSON.stringify(s.counts) === '[96]' && JSON.stringify(s.kinds) === '["hours"]' &&
        s.switchShown && s.pressed[0][1] === 'true' && s.pressed[1][1] === 'false' && s.pressed[0][0] === t.hours &&
        JSON.stringify(s.legend) === JSON.stringify([t.ago[0]]) && s.overflow === 0 && s.outside === 0, s);
      await page.screenshot({ path: `${out}/status-bars-${lang}-${device}-hours.png`, fullPage: true });

      // the switch with the keyboard: Tab to "30 days", then Space
      await page.locator('[data-switch="bars"] button[data-value="hours"]').focus();
      await page.keyboard.press('Tab');
      const focused = await page.evaluate(() => document.activeElement.getAttribute('data-value'));
      await page.keyboard.press('Space');
      s = await layout(page);
      check(`${tag} 30 days with the keyboard: every row`, focused === 'days' && JSON.stringify(s.counts) === '[30]' &&
        JSON.stringify(s.kinds) === '["days"]' && s.shown.every(n => n === 1) && s.pressed[1][1] === 'true' &&
        JSON.stringify(s.legend) === JSON.stringify([t.ago[1]]) && s.overflow === 0 && s.outside === 0, [focused, s]);
      await page.screenshot({ path: `${out}/status-bars-${lang}-${device}-days.png`, fullPage: true });
      await page.reload({ waitUntil: 'networkidle' });
      s = await layout(page);
      check(`${tag} 30 days kept across a reload`, JSON.stringify(s.kinds) === '["days"]' && s.pressed[1][1] === 'true', s);

      // a bar's tooltip on focus: Tab from the switch reaches the first row's newest bar
      await page.locator('[data-switch="bars"] button[data-value="days"]').focus();
      await page.keyboard.press('Tab');
      let p = await tip(page);
      check(`${tag} Tab to the bars: the newest day, its tooltip`, p.shown && p.index === 29 && p.text === p.active &&
        t.state.test(p.text) && p.inside, p);
      await page.keyboard.press('ArrowLeft');
      const left = await tip(page);
      await page.keyboard.press('Home');
      const home = await tip(page);
      check(`${tag} ArrowLeft and Home move the focus and the tooltip`, left.index === 28 && left.text === left.active &&
        home.index === 0 && home.text === home.active && home.inside, [left, home]);
      if (lang === 'en' && device === 'desktop') await page.screenshot({ path: `${out}/status-bars-tooltip-focus.png` });
      await page.keyboard.press('Escape');
      p = await tip(page);
      check(`${tag} Esc hides the tooltip`, !p.shown && p.index === 0, p);
      // a second Tab leaves the list: one tab stop per list
      await page.keyboard.press('Tab');
      const next = await page.evaluate(() => { const a = document.activeElement;
        return [a.closest('tr.row-bars') && a.closest('tr.row-bars').previousElementSibling.getAttribute('data-row'),
          Array.prototype.indexOf.call(a.parentNode.children, a)]; });
      check(`${tag} Tab goes on to the next row's list, at the bar last focused there`, next[0] === 'ha-private' && next[1] === 29, next);

      // back to 96 hours with a click, kept across a reload; then a hover tooltip
      await page.click('[data-switch="bars"] button[data-value="hours"]');
      await page.reload({ waitUntil: 'networkidle' });
      s = await layout(page);
      check(`${tag} 96 hours kept across a reload`, JSON.stringify(s.kinds) === '["hours"]' && s.pressed[0][1] === 'true', s);
      const lastRow = page.locator('tr.row-bars').last().locator('.bars:not([hidden]) .bar').nth(95);
      await lastRow.scrollIntoViewIfNeeded();
      await lastRow.hover();
      p = await tip(page);
      const hovered = await lastRow.getAttribute('aria-label');
      check(`${tag} hover: the tooltip of the last row's newest hour, inside the window`, p.shown && p.text === hovered && p.inside, p);
      if (device === 'phone') await page.screenshot({ path: `${out}/status-bars-tooltip-hover-${lang}-phone.png` });
      await context.close();
    }
  }

  // storage that throws (blocked site data): the switch still works, the page has no error, nothing is kept
  const blocked = await browser.newContext({ ignoreHTTPSErrors: true, viewport: { width: 1280, height: 900 } });
  await blocked.addInitScript(() => {
    for (const name of ['getItem', 'setItem']) Storage.prototype[name] = () => { throw new DOMException('blocked', 'SecurityError'); };
  });
  const page = await blocked.newPage();
  page.on('pageerror', e => problems.push(`blocked storage page error: ${e.message}`));
  await page.goto(base + '/', { waitUntil: 'networkidle' });
  await page.click('[data-switch="bars"] button[data-value="days"]');
  let s = await layout(page);
  const switched = JSON.stringify(s.kinds) === '["days"]';
  await page.reload({ waitUntil: 'networkidle' });
  s = await layout(page);
  check('storage that throws: the switch works, 96 hours after a reload', switched && JSON.stringify(s.kinds) === '["hours"]', [switched, s.kinds]);
  await blocked.close();

  await browser.close();
  console.log(results.join('\n'));
  if (problems.length) { console.error('problems:\n' + problems.join('\n')); process.exit(1); }
  console.log('status bars: ok');
})().catch(e => { console.error(e); process.exit(1); });
