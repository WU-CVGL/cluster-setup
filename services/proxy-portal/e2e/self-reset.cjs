// The self-service password reset on My access, in a browser against the e2e run (run.sh): the modal opens with
// focus inside, keeps Tab and Shift+Tab inside, closes on Esc and returns focus; confirming writes a request, the
// modal follows it through a stub applier status (applying, then applied with a result written as the portal's uid),
// says so when a poll gets an error page instead of an answer, and reveals the password once, with a working copy button; the result file is deleted and a second reveal is
// refused. A second reset's password answer is held back: the dialog cannot be closed (Esc twice, Close) while it
// is on its way, and when it closes anyway, the password that arrives afterwards is shown when it reopens.
// In English at desktop width and in Chinese at phone width, without horizontal overflow.
//   node e2e/self-reset.cjs <base URL> <screenshot folder>   (run from e2e/, as run.sh does; Playwright in NODE_PATH)
const { chromium } = require('playwright');
const { execFileSync } = require('child_process');
const fs = require('fs');
const path = require('path');

const spool = path.join(__dirname, 'run', 'spool');
const compose = path.join(__dirname, 'compose.yml');

function writeStatus(id, user, status, extra = {}) {
  const data = Object.assign({ id, op: 'rotate', username: user, status, updated_at: new Date().toISOString() }, extra);
  fs.writeFileSync(path.join(spool, 'status', `${id}.json`), JSON.stringify(data));
}

function writeResult(id, user, password) {
  // as the applier does: mode 0400, owned by the portal's uid, which the portal checks
  const code = `import json, os
fd = os.open('/srv/spool/results/${id}.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
os.write(fd, json.dumps({'id': '${id}', 'username': '${user}', 'password': '${password}'}).encode()); os.close(fd)`;
  execFileSync('docker', ['compose', '-f', compose, 'exec', '-T', 'proxy-portal', 'python', '-c', code]);
}

(async () => {
  const [base, out] = process.argv.slice(2);
  fs.mkdirSync(out, { recursive: true });
  const args = ['--no-proxy-server'].concat(process.env.RESOLVE ? [`--host-resolver-rules=MAP ${process.env.RESOLVE}`] : []);
  const browser = await chromium.launch({ executablePath: process.env.CHROME || undefined, args });
  const results = [], problems = [];
  const waitChip = async (page, text, timeout = 10000) => {  // page.waitForFunction would need eval, which the CSP refuses
    const chip = page.locator('#reset-dialog [data-field=state]');
    for (const end = Date.now() + timeout; Date.now() < end; await page.waitForTimeout(200)) {
      if (((await chip.textContent()) || '').trim() === text) return;
    }
    throw new Error(`state ${text} not shown within ${timeout} ms: ${await chip.textContent()}`);
  };
  // a dialog step at the run's width and, for the screenshots only, at the other device's width
  const other = { desktop: ['phone', { width: 390, height: 844 }], phone: ['desktop', { width: 1280, height: 900 }] };
  const shoot = async (page, run, step) => {
    await page.screenshot({ path: `${out}/reset-${step}-${run.lang}-${run.device}.png` });
    const [device, viewport] = other[run.device];
    await page.setViewportSize(viewport);
    await page.screenshot({ path: `${out}/reset-${step}-${run.lang}-${device}.png` });
    await page.setViewportSize(run.viewport);
  };
  const check = (label, ok, got) => { results.push(`${ok ? 'ok  ' : 'FAIL'} ${label}${ok ? '' : ': ' + JSON.stringify(got)}`); if (!ok) problems.push(label); };

  const runs = [
    { lang: 'en', user: 'alice', viewport: { width: 1280, height: 900 }, device: 'desktop', locale: 'en-US', header: 'en-US,en;q=0.9',
      t: { button: 'Reset proxy password', collecting: 'Getting the password…', pending: 'Pending', applying: 'Applying', applied: 'Applied', warning: 'will not be shown again', copied: 'Copied',
        state: "The reset's state could not be read. Reload the page to see it.",
        failed: 'The reset could not be started. Nothing changed.' } },
    { lang: 'zh', user: 'carol', viewport: { width: 390, height: 844 }, device: 'phone', locale: 'zh-CN', header: 'zh-CN,zh;q=0.9',
      t: { button: '重置代理密码', collecting: '正在获取密码…', pending: '等待中', applying: '应用中', applied: '已应用', warning: '不会再次显示', copied: '已复制',
        state: '无法读取重置的状态，请刷新页面查看。', failed: '无法开始重置，没有任何变更。' } },
  ];
  for (const run of runs) {
    const tag = `${run.lang}/${run.device}`;
    const context = await browser.newContext({ viewport: run.viewport, ignoreHTTPSErrors: true, locale: run.locale,
      extraHTTPHeaders: { 'Accept-Language': run.header } });
    await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: new URL(base).origin });
    const page = await context.newPage();
    page.on('console', m => { if (m.type() === 'error' && !/\b(403|409|502)\b/.test(m.text())) problems.push(`${tag} console: ${m.text()}`); });
    page.on('pageerror', e => problems.push(`${tag} page error: ${e.message}`));
    await page.goto(base + '/signin?next=/access', { waitUntil: 'networkidle' });
    await page.fill('#username', run.user);
    await page.fill('#password', `pw-${run.user}`);
    await Promise.all([page.waitForNavigation({ waitUntil: 'networkidle' }), page.click('form[action="/signin"] button[type=submit]')]);

    const opener = page.locator('#reset-open');
    check(`${tag}: the button`, (await opener.isVisible()) && (await opener.textContent()).trim() === run.t.button, await opener.textContent());
    const state = () => page.evaluate(() => {
      const d = document.getElementById('reset-dialog'), a = document.activeElement;
      const step = d.querySelector('[data-step]:not([hidden])');
      return { open: d.open, step: step && step.getAttribute('data-step'), inside: d.contains(a),
        focus: a && (a.getAttribute('data-action') || a.getAttribute('data-copy') || a.id || a.tagName),
        chip: (d.querySelector('[data-field=state]').textContent || '').trim(),
        secret: document.getElementById('reset-password').textContent,
        overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth };
    });

    const requestsBefore = fs.readdirSync(path.join(spool, 'requests')).length;
    // open, focus inside, Tab and Shift+Tab stay inside, Esc closes and focus returns to the button
    await opener.click();
    let s = await state();
    check(`${tag}: opens on the confirmation with focus inside`, s.open && s.step === 'confirm' && s.inside, s);
    await shoot(page, run, 'confirm');
    let trapped = true;
    for (const key of ['Tab', 'Tab', 'Tab', 'Shift+Tab', 'Shift+Tab', 'Shift+Tab', 'Shift+Tab']) {
      await page.keyboard.press(key);
      trapped = trapped && (await state()).inside;
    }
    check(`${tag}: Tab and Shift+Tab stay in the dialog`, trapped, await state());
    await page.keyboard.press('Escape');
    s = await state();
    check(`${tag}: Esc closes and returns focus to the button`, !s.open && s.focus === 'reset-open', s);

    // a create answered with an error page: refused before the portal (403) says nothing changed; a 502 that it
    // cannot tell apart from a request written says the state could not be read
    await opener.click();
    const createError = page.locator('[data-step=confirm] .reset-error');
    for (const [status, text] of [[403, run.t.failed], [502, run.t.state]]) {
      await page.route('**/api/me/reset', r => r.fulfill({ status, contentType: 'text/html', body: `<html>${status}</html>` }));
      const answered = page.waitForResponse(r => r.url().endsWith('/api/me/reset') && r.status() === status);
      await page.click('[data-action=confirm]');
      await answered;
      await createError.waitFor({ state: 'visible', timeout: 10000 });
      await page.locator('[data-action=confirm]:not([disabled])').waitFor({ timeout: 10000 });
      check(`${tag}: a create answered ${status} without JSON`, (await createError.textContent()) === text, await createError.textContent());
      await page.unroute('**/api/me/reset');
    }
    check(`${tag}: no request file from those`, fs.readdirSync(path.join(spool, 'requests')).length === requestsBefore, fs.readdirSync(path.join(spool, 'requests')));

    // confirm: a request file for the user's own account
    const [response] = await Promise.all([
      page.waitForResponse(r => r.url().endsWith('/api/me/reset') && r.request().method() === 'POST'
        && (r.headers()['content-type'] || '').includes('json')),  // not the error pages above
      page.click('[data-action=confirm]')]);
    const created = await response.json();
    const id = created.id;
    check(`${tag}: request accepted`, response.status() === 202 && /^[0-9a-f-]{36}$/.test(id), [response.status(), created]);
    check(`${tag}: request file written`, fs.readdirSync(path.join(spool, 'requests')).includes(`${id}.json`), fs.readdirSync(path.join(spool, 'requests')));
    await waitChip(page, run.t.pending);
    s = await state();
    check(`${tag}: pending`, s.step === 'progress' && s.chip === run.t.pending && s.inside, s);

    // a poll answered with an error page, not JSON: the state could not be read (not "nothing changed")
    const poll = `**/api/me/reset/${id}`;
    await page.route(poll, r => r.fulfill({ status: 502, contentType: 'text/html', body: '<html>502 Bad Gateway</html>' }));
    const pollError = page.locator('[data-step=progress] .reset-error');
    await pollError.waitFor({ state: 'visible', timeout: 10000 });
    check(`${tag}: a failed poll says the state could not be read`, (await pollError.textContent()) === run.t.state,
      await pollError.textContent());
    await page.unroute(poll);
    await page.keyboard.press('Escape');
    await opener.click();  // reopening resumes the reset
    await waitChip(page, run.t.pending);
    s = await state();
    check(`${tag}: reopened on the same reset`, s.step === 'progress' && !(await pollError.isVisible()), s);

    // the applier takes it, then applies it and writes the one-time result
    writeStatus(id, run.user, 'applying');
    await waitChip(page, run.t.applying);
    await shoot(page, run, 'applying');
    check(`${tag}: applying, no reveal yet`, !(await page.locator('[data-action=reveal]').isVisible()), await state());
    const password = `E2e-${run.lang}-` + Math.random().toString(36).slice(2, 12);
    writeResult(id, run.user, password);
    writeStatus(id, run.user, 'applied', { changed: true, restarted: ['xray-ha'], result: true, applied_seq: 1 });
    await page.locator('[data-action=reveal]').waitFor({ state: 'visible', timeout: 10000 });
    s = await state();
    check(`${tag}: applied, reveal focused`, s.chip === run.t.applied && s.focus === 'reveal', s);
    check(`${tag}: password not in the page before the reveal`, !(await page.content()).includes(password), null);

    // reveal once, copy
    await page.click('[data-action=reveal]');
    await page.locator('[data-step=password]').waitFor({ state: 'visible' });
    s = await state();
    check(`${tag}: the password shown`, s.step === 'password' && s.secret === password && s.inside, s);
    const warning = await page.locator('[data-step=password] .notice').textContent();
    check(`${tag}: the warning`, warning.includes(run.t.warning), warning);
    check(`${tag}: result file deleted`, !fs.readdirSync(path.join(spool, 'results')).includes(`${id}.json`), fs.readdirSync(path.join(spool, 'results')));
    await shoot(page, run, 'password');
    await page.click('[data-copy=reset-password]');
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    const copyLabel = (await page.locator('[data-copy=reset-password]').textContent()).trim();
    check(`${tag}: copied`, copied === password && copyLabel === run.t.copied, [copied === password, copyLabel]);
    check(`${tag}: no horizontal overflow`, s.overflow <= 0, s.overflow);

    // close: the password leaves the page; a second reveal is refused
    await page.click('[data-step=password] [data-action=close]');
    s = await state();
    check(`${tag}: closed, password cleared, focus back`, !s.open && s.secret === '' && s.focus === 'reset-open', s);
    const again = await page.evaluate(id => fetch(`/api/me/reset/${id}/password`, { method: 'POST', credentials: 'same-origin',
      headers: { 'X-CSRFToken': document.querySelector('#reset-dialog input[name=csrfmiddlewaretoken]').value } })
      .then(r => r.json().then(d => [r.status, d.password, d.value === undefined || d.value === null])), id);
    check(`${tag}: a second reveal is refused`, again[0] === 409 && again[1] === 'shown' && again[2], again);
    await page.goto(base + '/access', { waitUntil: 'networkidle' });
    check(`${tag}: no reset left to resume`, (await page.locator('#reset-current').count()) === 0, null);

    // a second reset whose password answer is held back after the portal has already handed it out (and deleted
    // the result): while it is on its way, neither Esc (twice) nor Close closes the dialog; when the dialog closes
    // anyway, the password that then arrives is shown when it reopens
    await opener.click();
    const [response2] = await Promise.all([
      page.waitForResponse(r => r.url().endsWith('/api/me/reset') && r.request().method() === 'POST'),
      page.click('[data-action=confirm]')]);
    const id2 = (await response2.json()).id;
    check(`${tag}: second request accepted`, response2.status() === 202, response2.status());
    const password2 = `E2e-${run.lang}-` + Math.random().toString(36).slice(2, 12);
    writeResult(id2, run.user, password2);
    writeStatus(id2, run.user, 'applied', { changed: true, restarted: ['xray-ha'], result: true, applied_seq: 2 });
    await page.locator('[data-action=reveal]').waitFor({ state: 'visible', timeout: 10000 });
    // hold the answer in the page (not with page.route and route.fetch: those fetch from Node, which does not use
    // the browser's host resolver rules and would reach the real proxy.cvgl.lab)
    await page.evaluate(() => {
      const real = window.fetch;
      window.__held = { arrived: false };
      const go = new Promise(r => { window.__held.release = r; });
      window.fetch = function (url, options) {
        if (!String(url).endsWith('/password')) return real.apply(this, arguments);
        window.fetch = real;
        return real.call(this, url, options).then(answer => { window.__held.arrived = true; return go.then(() => answer); });
      };
    });
    await page.click('[data-action=reveal]');
    let collected = false;
    for (const end = Date.now() + 10000; !collected && Date.now() < end; await page.waitForTimeout(100)) {
      collected = await page.evaluate(() => window.__held.arrived);
    }
    check(`${tag}: collected on the server`, collected && !fs.readdirSync(path.join(spool, 'results')).includes(`${id2}.json`), [collected, fs.readdirSync(path.join(spool, 'results'))]);
    await page.keyboard.press('Escape');
    await page.keyboard.press('Escape');
    s = await state();
    check(`${tag}: Esc does not close it while the password is on its way`, s.open && s.step === 'progress', s);
    const progressClose = page.locator('[data-step=progress] [data-action=close]');
    check(`${tag}: Close disabled while the password is on its way`, await progressClose.isDisabled(), await progressClose.isDisabled());
    if (s.open) await progressClose.click({ force: true });
    s = await state();
    check(`${tag}: Close does not close it while the password is on its way`, s.open && s.step === 'progress', s);
    const revealLabel = (await page.locator('[data-action=reveal]').textContent()).trim();
    check(`${tag}: the wait is shown`, revealLabel === run.t.collecting && await page.locator('[data-action=reveal]').isDisabled(), revealLabel);
    await shoot(page, run, 'collecting');
    await page.evaluate(() => document.getElementById('reset-dialog').close());  // a close the page could not prevent
    await page.evaluate(() => window.__held.release());
    await page.waitForTimeout(300);
    s = await state();
    check(`${tag}: the late password is not put into the closed dialog`, !s.open && s.secret === '', s);
    await opener.click();
    s = await state();
    check(`${tag}: reopening shows the late password`, s.open && s.step === 'password' && s.secret === password2 && s.inside, s);
    if (s.step === 'password') await page.click('[data-step=password] [data-action=close]');
    else await page.keyboard.press('Escape');
    s = await state();
    check(`${tag}: closed after the password was shown`, !s.open && s.secret === '' && s.focus === 'reset-open', s);
    await opener.click();
    s = await state();
    check(`${tag}: then the next opening starts a new reset`, s.open && s.step === 'confirm', s);
    await page.keyboard.press('Escape');
    await context.close();
  }
  await browser.close();
  console.log(results.join('\n'));
  if (problems.length) { console.error('problems:\n' + problems.join('\n')); process.exit(1); }
  console.log('self-service reset: ok');
})().catch(e => { console.error(e); process.exit(1); });
