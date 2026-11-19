// A private proxy granted to one account, in a browser against the e2e run (run.sh): an admin gives alice a private
// proxy from the Users page (the proxy chosen first, then the confirmation page that names it and what restarts),
// the request file carries the proxy, the request page follows a stub applier status to applied, and once the
// applier's account status lists the grant, the Users page shows it and alice's My access shows that proxy with its
// addresses and client settings, and no other private proxy. Then the admin takes it back, typing the account name
// to confirm. In English at desktop width and in Chinese at phone width, without horizontal overflow. Restores the
// account status when done.
//   node e2e/grants.cjs <base URL> <screenshot folder>   (run from e2e/, as run.sh does; Playwright in NODE_PATH)
const { chromium } = require('playwright');
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const spool = path.join(__dirname, 'run', 'spool');
const compose = path.join(__dirname, 'compose.yml');
const JP = 'jpty1-bwh-2tb', UK = 'miyaip-uk-01_relay_jpty1-bwh-2tb';

function readRequest(id) {
  // written by the portal's uid, mode 0600: read inside its container
  const out = execFileSync('docker', ['compose', '-f', compose, 'exec', '-T', 'proxy-portal', 'python', '-c',
    `print(open('/srv/spool/requests/${id}.json').read())`]);
  return JSON.parse(out.toString());
}

function writeStatus(id, data) {
  fs.writeFileSync(path.join(spool, 'status', `${id}.json`),
    JSON.stringify(Object.assign({ id, updated_at: new Date().toISOString() }, data)));
}

function setGrants(original, grants) {
  // the account status as the applier writes it after an apply
  const data = JSON.parse(original);
  for (const a of data.accounts) a.proxies = grants[a.name] || [];
  data.generated_at = new Date().toISOString().replace(/\.\d+Z$/, 'Z');
  fs.writeFileSync(path.join(spool, 'status.json'), JSON.stringify(data, null, 2));
}

(async () => {
  const [base, out] = process.argv.slice(2);
  fs.mkdirSync(out, { recursive: true });
  const original = fs.readFileSync(path.join(spool, 'status.json'), 'utf8');
  const args = ['--no-proxy-server'].concat(process.env.RESOLVE ? [`--host-resolver-rules=MAP ${process.env.RESOLVE}`] : []);
  const browser = await chromium.launch({ executablePath: process.env.CHROME || undefined, args });
  const results = [], problems = [];
  const check = (label, ok, got) => { results.push(`${ok ? 'ok  ' : 'FAIL'} ${label}${ok ? '' : ': ' + JSON.stringify(got)}`); if (!ok) problems.push(label); };
  const text = async (page, sel) => ((await page.locator(sel).first().textContent()) || '').replace(/\s+/g, ' ').trim();
  const overflow = page => page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  const waitText = async (page, sel, want, timeout = 15000) => {  // the request page reloads itself on a new state
    for (const end = Date.now() + timeout; Date.now() < end; await page.waitForTimeout(250)) {
      try { if ((await text(page, sel)).includes(want)) return true; } catch (e) { /* reloading */ }
    }
    return false;
  };
  const runs = [
    { lang: 'en', device: 'desktop', viewport: { width: 1280, height: 900 }, locale: 'en-US', header: 'en-US,en;q=0.9', proxy: JP,
      t: { grant: 'Private proxy on', revoke: 'Private proxy off', effect: `Lets alice use the private proxy ${JP} at its own address.`,
        restart: `Applying restarts ${JP}.`, applied: 'Applied', column: 'Private proxies', given: `It may also use the private proxy ${JP}, at its own address.`,
        card: 'One private proxy, no switching, given to your account.', revokeEffect: `${JP} stops accepting alice.`, type: 'Type alice to confirm' } },
    { lang: 'zh', device: 'phone', viewport: { width: 390, height: 844 }, locale: 'zh-CN', header: 'zh-CN,zh;q=0.9', proxy: UK,
      t: { grant: '开通私有代理', revoke: '取消私有代理', effect: `允许 alice 通过私有代理 ${UK} 自己的地址使用它。`,
        restart: `应用时会重启 ${UK}。`, applied: '已应用', column: '私有代理', given: `它还可以使用私有代理 ${UK}`,
        card: '单个私有代理，不切换，已授予你的账号。', revokeEffect: `${UK} 不再接受 alice。`, type: '输入 alice 以确认' } },
  ];
  try {
    for (const run of runs) {
      const tag = `${run.lang}/${run.device}`;
      const newPage = async user => {
        const context = await browser.newContext({ viewport: run.viewport, ignoreHTTPSErrors: true, locale: run.locale,
          extraHTTPHeaders: { 'Accept-Language': run.header } });
        const page = await context.newPage();
        page.on('console', m => { if (m.type() === 'error') problems.push(`${tag} ${user} console: ${m.text()}`); });
        page.on('pageerror', e => problems.push(`${tag} ${user} page error: ${e.message}`));
        await page.goto(base + '/signin', { waitUntil: 'networkidle' });
        await page.fill('#username', user);
        await page.fill('#password', `pw-${user}`);
        await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }), page.click('form[action="/signin"] button[type=submit]')]);
        return page;
      };
      const admin = await newPage('carol');

      // the Users page: alice may be given a private proxy
      await admin.goto(base + '/admin/users', { waitUntil: 'networkidle' });
      check(`${tag}: the Private proxies column`, (await admin.locator('th', { hasText: run.t.column }).count()) >= 1, null);
      const grantLink = admin.locator('a[href="/admin/accounts/grant?username=alice"]');
      check(`${tag}: the grant action`, (await grantLink.textContent()).trim() === run.t.grant, await grantLink.textContent());
      check(`${tag}: no grant action for an admin account`, (await admin.locator('a[href="/admin/accounts/grant?username=carol"]').count()) === 0, null);
      await Promise.all([admin.waitForNavigation({ waitUntil: 'networkidle' }), grantLink.click()]);
      // first the proxy: only private proxies
      const options = await admin.locator('select#proxy option').evaluateAll(os => os.map(o => o.value));
      check(`${tag}: the private proxies to choose from`, options.includes(JP) && options.includes(UK) && !options.some(o => o.startsWith('usca')), options);
      await admin.screenshot({ path: `${out}/grant-choose-${run.lang}-${run.device}.png`, fullPage: true });
      await admin.selectOption('select#proxy', run.proxy);
      await Promise.all([admin.waitForNavigation({ waitUntil: 'networkidle' }), admin.click('main form[method=get] button[type=submit]')]);
      const notice = await text(admin, 'main form[method=post] .notice');
      check(`${tag}: the confirmation names the proxy`, notice.includes(run.t.effect), notice);
      check(`${tag}: the confirmation says what restarts`, notice.includes(run.t.restart), notice);
      check(`${tag}: no name to type for a grant`, (await admin.locator('#confirm').count()) === 0, null);
      check(`${tag}: confirmation without overflow`, (await overflow(admin)) <= 0, await overflow(admin));
      await admin.screenshot({ path: `${out}/grant-confirm-${run.lang}-${run.device}.png`, fullPage: true });
      await Promise.all([admin.waitForNavigation({ waitUntil: 'networkidle' }), admin.click('main form[method=post] button[type=submit]')]);
      const id = admin.url().split('/').pop();
      const request = readRequest(id);
      check(`${tag}: the request file`, JSON.stringify(request) === JSON.stringify({ id, op: 'grant', username: 'alice', proxy: run.proxy }), request);
      // the applier takes it and applies it
      writeStatus(id, { op: 'grant', username: 'alice', proxy: run.proxy, status: 'applied', changed: true,
        restarted: [`xray-${run.proxy}`], applied_seq: 1 });
      check(`${tag}: the request page follows to applied`, await waitText(admin, '#request-state .chip', run.t.applied), await text(admin, '#request-state .chip'));
      const kv = await text(admin, '#request-state dl');
      check(`${tag}: the request page names the proxy and what restarted`, kv.split(run.proxy).length >= 3, kv);
      await admin.screenshot({ path: `${out}/grant-request-${run.lang}-${run.device}.png`, fullPage: true });
      // the applier's next account status lists the grant
      setGrants(original, { alice: [run.proxy] });
      await admin.goto(base + '/admin/users', { waitUntil: 'networkidle' });
      const row = await text(admin, 'tr:has(th:text-is("alice"))');
      check(`${tag}: the Users page shows the grant`, row.includes(run.proxy), row);
      check(`${tag}: Users page without overflow`, (await overflow(admin)) <= 0, await overflow(admin));
      await admin.screenshot({ path: `${out}/grant-users-${run.lang}-${run.device}.png`, fullPage: true });

      // alice's My access: that private proxy, at its own address, with client settings; no other private proxy
      const alice = await newPage('alice');
      await alice.goto(base + '/access', { waitUntil: 'networkidle' });
      const account = await text(alice, 'section[aria-labelledby=account-title] .notice.ok');
      check(`${tag}: My access says which private proxy`, account.includes(run.t.given), account);
      const card = alice.locator('.access-card', { has: alice.locator(`h3 code:text-is("${run.proxy}")`) });
      const cardText = (await card.textContent()).replace(/\s+/g, ' ');
      check(`${tag}: the proxy's card`, cardText.includes(run.t.card), cardText);
      const port = run.proxy === JP ? '55889' : '54889';
      check(`${tag}: the proxy's own addresses`, cardText.includes(`192.168.233.8:${port}`) && cardText.includes(`10.0.1.68:${port}`), cardText);
      const other = run.proxy === JP ? UK : JP;
      const cards = await alice.locator('.access-card h3 code').allTextContents();
      check(`${tag}: no other private proxy`, !cards.includes(other) && !cards.includes('ha-private'), cards);
      const settings = await alice.locator(`#tpl-${run.proxy}-private-environment`).textContent();
      check(`${tag}: client settings for the proxy`, settings.includes(`http://alice:<password>@192.168.233.8:${port}`), settings);
      check(`${tag}: My access without overflow`, (await overflow(alice)) <= 0, await overflow(alice));
      await alice.screenshot({ path: `${out}/grant-access-${run.lang}-${run.device}.png`, fullPage: true });

      // take it back: the name typed to confirm
      await admin.goto(base + '/admin/users', { waitUntil: 'networkidle' });
      const revokeLink = admin.locator('a[href="/admin/accounts/revoke?username=alice"]');
      check(`${tag}: the revoke action`, (await revokeLink.textContent()).trim() === run.t.revoke, await revokeLink.textContent());
      await Promise.all([admin.waitForNavigation({ waitUntil: 'networkidle' }), revokeLink.click()]);
      await Promise.all([admin.waitForNavigation({ waitUntil: 'networkidle' }), admin.click('main form[method=get] button[type=submit]')]);
      const revokeNotice = await text(admin, 'main form[method=post] .notice');
      check(`${tag}: the revoke confirmation`, revokeNotice.includes(run.t.revokeEffect) && revokeNotice.includes(run.t.restart), revokeNotice);
      check(`${tag}: the name to type`, (await text(admin, 'label[for=confirm]')) === run.t.type, await text(admin, 'label[for=confirm]'));
      await admin.screenshot({ path: `${out}/revoke-confirm-${run.lang}-${run.device}.png`, fullPage: true });
      await admin.fill('#confirm', 'alice');
      await Promise.all([admin.waitForNavigation({ waitUntil: 'networkidle' }), admin.click('main form[method=post] button[type=submit]')]);
      const revokeId = admin.url().split('/').pop();
      check(`${tag}: the revoke request file`, readRequest(revokeId).op === 'revoke' && readRequest(revokeId).proxy === run.proxy, readRequest(revokeId));
      writeStatus(revokeId, { op: 'revoke', username: 'alice', proxy: run.proxy, status: 'applied', changed: true,
        restarted: [`xray-${run.proxy}`], applied_seq: 2 });
      check(`${tag}: the revoke is applied`, await waitText(admin, '#request-state .chip', run.t.applied), await text(admin, '#request-state .chip'));
      fs.writeFileSync(path.join(spool, 'status.json'), original);
      await alice.goto(base + '/access', { waitUntil: 'networkidle' });
      const after = await alice.locator('.access-card h3 code').allTextContents();
      check(`${tag}: My access without it`, !after.includes(run.proxy) && (await alice.locator(`#tpl-${run.proxy}-private-environment`).count()) === 0, after);
      await alice.context().close();
      await admin.context().close();
    }
  } finally {
    fs.writeFileSync(path.join(spool, 'status.json'), original);
    await browser.close();
  }
  for (const r of results) console.log(r);
  console.log(`checks: ${results.length}, problems: ${problems.length}`);
  if (problems.length) { console.error('problems:\n' + problems.join('\n')); process.exit(1); }
})().catch(e => { console.error(e); process.exit(1); });
