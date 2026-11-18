// Renders Status, My access, My usage and the admin console in English and Chinese, at desktop and phone width, and saves
// screenshots. Needs Playwright for Node (NODE_PATH) and a Chromium (CHROME, optional).
//   node e2e/shots.cjs <base URL> <output folder> [user] [password]
// Fails on a console error, a failed request, a request to another origin, or a page without its main heading.
const { chromium } = require('playwright');
const fs = require('fs');

(async () => {
  const [base, out, user = 'carol', password = 'pw-carol'] = process.argv.slice(2);
  fs.mkdirSync(out, { recursive: true });
  const origin = new URL(base).origin;
  const args = ['--no-proxy-server'].concat(process.env.RESOLVE ? [`--host-resolver-rules=MAP ${process.env.RESOLVE}`] : []);
  const browser = await chromium.launch({ executablePath: process.env.CHROME || undefined, args });
  const problems = [];
  let requests = 0;
  const results = [];
  for (const [lang, header] of [['en', 'en-US,en;q=0.9'], ['zh', 'zh-CN,zh;q=0.9']]) {
    for (const [device, viewport] of [['desktop', { width: 1280, height: 900 }], ['phone', { width: 390, height: 844 }]]) {
      const context = await browser.newContext({ viewport, ignoreHTTPSErrors: true, locale: lang === 'zh' ? 'zh-CN' : 'en-US',
        extraHTTPHeaders: { 'Accept-Language': header } });
      const page = await context.newPage();
      let signedOut = false;  // the 401 of /api/me/usage after signing out is expected
      page.on('console', m => { if (m.type() === 'error' && !(signedOut && m.text().includes('401'))) problems.push(`${lang}/${device} console: ${m.text()}`); });
      page.on('pageerror', e => problems.push(`${lang}/${device} page error: ${e.message}`));
      page.on('request', r => { requests++; if (new URL(r.url()).origin !== origin) problems.push(`off-origin request: ${r.url()}`); });
      page.on('requestfailed', r => problems.push(`failed request: ${r.url()}`));
      await page.goto(base + '/', { waitUntil: 'networkidle' });
      await page.screenshot({ path: `${out}/status-${lang}-${device}.png`, fullPage: true });
      results.push([`status-${lang}-${device}`, await page.locator('h1').first().textContent(),
        await page.locator('#headline').textContent(), await page.locator('html').getAttribute('lang')]);
      await page.goto(base + '/access', { waitUntil: 'networkidle' });
      await page.fill('#username', user);
      await page.fill('#password', password);
      await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }), page.click('form[action="/signin"] button[type=submit]')]);
      await page.screenshot({ path: `${out}/access-${lang}-${device}.png`, fullPage: true });
      results.push([`access-${lang}-${device}`, await page.locator('h1').first().textContent(), page.url()]);
      await page.goto(base + '/usage', { waitUntil: 'networkidle' });
      await page.screenshot({ path: `${out}/usage-${lang}-${device}.png`, fullPage: true });
      results.push([`usage-${lang}-${device}`, await page.locator('h1').first().textContent(),
        await page.locator('.usage-card .big').first().textContent()]);
      const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
      if (overflow > 0) problems.push(`${lang}/${device}: horizontal overflow ${overflow}px on usage`);
      // the admin console (the user is a Determined admin in the stub master)
      for (const [path, name] of [['/admin/users', 'admin-users'], ['/admin/users?filter=no-account', 'admin-users-filter'],
                                  ['/admin/accounts/disable?username=alice', 'admin-confirm'],
                                  ['/admin/log', 'admin-log'], ['/admin/incidents', 'admin-incidents']]) {
        await page.goto(base + path, { waitUntil: 'networkidle' });
        await page.screenshot({ path: `${out}/${name}-${lang}-${device}.png`, fullPage: true });
        results.push([`${name}-${lang}-${device}`, await page.locator('h1').first().textContent()]);
      }
      for (const path of ['/', '/access', '/admin/users', '/admin/log']) {
        await page.goto(base + path, { waitUntil: 'networkidle' });
        const o = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
        if (o > 0) problems.push(`${lang}/${device}: horizontal overflow ${o}px on ${path}`);
      }
      if (device === 'desktop') {
        // the language switch: the other language's button, then back
        await page.goto(base + '/', { waitUntil: 'networkidle' });
        const other = lang === 'en' ? 'zh-hans' : 'en';
        await Promise.all([page.waitForNavigation(), page.click(`form.lang button[value="${other}"]`)]);
        results.push([`switch-${lang}->${other}`, await page.locator('html').getAttribute('lang'), await page.locator('h1').first().textContent()]);
        await Promise.all([page.waitForNavigation(), page.click(`form.lang button[value="${lang === 'en' ? 'en' : 'zh-hans'}"]`)]);
        // sign out
        await Promise.all([page.waitForNavigation(), page.click('form[action="/signout"] button')]);
        signedOut = true;
        const anon = await page.evaluate(() => fetch('/api/me/usage').then(r => r.status));
        results.push([`signed-out-${lang}`, anon]);
      }
      await context.close();
    }
  }
  await browser.close();
  for (const r of results) console.log(r.map(x => String(x).trim()).join(' | '));
  console.log(`requests: ${requests}, problems: ${problems.length}`);
  for (const p of problems) console.log('PROBLEM ' + p);
  process.exit(problems.length ? 1 : 0);
})();
