/* Actual shared transfer modal and validation; isolated WebKit with mocked API replies. */
const {webkit} = require('playwright');
const {spawnSync} = require('child_process');
const fs = require('fs');
const path = require('path');
const assert = require('assert');

const root = path.resolve(__dirname, '..');
const artifacts = path.join(root, 'docs/validation/manual-episode-precedence');
const template = fs.readFileSync(path.join(root, 'web/templates/navigation.html'), 'utf8');
const modal = template.slice(template.indexOf('<!-- 手动识别 -->'), template.indexOf('<!-- 手动下载 -->'));
const submit = template.slice(template.indexOf('  function manual_media_transfer()'), template.indexOf('  // 查示查询TMDBID'));
// Render the real Jinja fragment rather than duplicating product fields in a fixture.
const rendered = spawnSync('python3', ['-c',
  'import sys; from jinja2 import Template; print(Template(sys.stdin.read()).render(RmtModeDict=[{"value":"link","name":"硬链接"}], SyncMod="link"))'],
  {input: modal, encoding: 'utf8'});
assert.equal(rendered.status, 0, rendered.stderr);

(async () => {
  fs.mkdirSync(artifacts, {recursive: true});
  const browser = await webkit.launch({headless: true});
  const context = await browser.newContext({viewport: {width: 1100, height: 900}});
  await context.tracing.start({screenshots: true, snapshots: true});
  const page = await context.newPage();
  const errors = [], outcomes = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.route('http://manual-ep.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') {
        return route.fulfill({contentType: 'text/html; charset=utf-8', body:
          `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
          <link rel="stylesheet" href="/static/css/tabler.min.css"><body>${rendered.stdout}
          <script src="/static/js/jquery-3.3.1.min.js"></script><script src="/static/js/tabler.min.js"></script>
          <script>
          window.calls=[]; window.failure=false; window.finished=false;
          window.show_refresh_process=()=>{}; window.hide_refresh_process=()=>{}; window.navmenu=()=>{};
          window.show_success_modal=(message, done)=>{window.finished=true; done();};
          window.show_fail_modal=(message, done)=>{window.errorMessage=message; done();};
          window.ajax_post=(cmd,data,done)=>{calls.push({cmd,data}); setTimeout(()=>done(failure ?
            {retcode:2,retmsg:"目标集数不存在，请重新选择"} : {retcode:0}), 400);};
          ${submit}
          </script></body></html>`});
      }
      if (url.pathname.startsWith('/static/')) {
        const file = path.join(root, 'web', url.pathname);
        if (fs.existsSync(file)) return route.fulfill({path: file});
      }
      return route.fulfill({status: 404, body: ''});
    });
    await page.goto('http://manual-ep.test/');
    const open = async () => {
      await page.evaluate(() => {
        $('#rename_manual_type').val('3');
        $('#rename_type_tv').prop('checked', true);
        $('#rename_path_div').hide();
        $('#rename_inpath').val('/media/Cyberpunk.Edgerunners.S02E03.021.1080p.NF.WEB-DL.mkv');
        $('#rename_tmdb, #rename_episode, #rename_episode_details, #rename_episode_offset').val('');
        window.finished = false;
        $('#modal-media-identification').modal('show');
      });
      await page.locator('#modal-media-identification').waitFor({state: 'visible'});
      await page.waitForFunction(() => getComputedStyle(
        document.querySelector('#modal-media-identification')).opacity === '1');
    };
    const cases = [
      ['automatic', '', '', '', true],
      ['fixed EP', '3', '', '', true],
      ['fixed EP with offset', '3', '', '1', true],
      ['merged range', '3-4', '', '', true],
      ['selection needs pattern', '3,4', '', '', false],
      ['offset needs pattern', '', '', '1', false],
      ['selection with pattern', '3,4', 'Show.S02E{ep}.mkv', '', true],
      ['pattern only', '', 'Show.S02E{ep}.mkv', '', true],
      ['invalid negative EP', '-1', '', '', false]
    ];
    for (const [name, details, pattern, offset, accepted] of cases) {
      await open();
      const count = await page.evaluate(() => calls.length);
      await page.locator('#rename_episode_details').fill(details);
      await page.locator('#rename_episode').fill(pattern);
      await page.locator('#rename_episode_offset').fill(offset);
      await page.locator('#identification_action_btn').click();
      if (accepted) {
        await page.waitForFunction(() => window.finished);
        assert.equal(await page.evaluate(() => calls.length), count + 1, name);
        const request = await page.evaluate(() => calls.at(-1));
        assert.equal(request.cmd, 'rename_udf');
        assert.equal(request.data.tmdb, '', 'auto identity remains available');
        assert.equal(request.data.episode_details, details);
        assert.equal(request.data.episode_format, pattern);
        await page.locator('#modal-media-identification').waitFor({state: 'hidden'});
      } else {
        assert.equal(await page.evaluate(() => calls.length), count, name);
        assert(await page.locator('#modal-media-identification .is-invalid').count() > 0, name);
        await page.evaluate(() => $('#modal-media-identification').modal('hide'));
        await page.locator('#modal-media-identification').waitFor({state: 'hidden'});
      }
      outcomes.push({name, accepted});
    }
    // Failed target validation keeps the form values for correction and keyboard retry.
    await open();
    await page.locator('#rename_episode_details').fill('3');
    await page.evaluate(() => { window.failure = true; });
    await page.locator('#identification_action_btn').click();
    await page.waitForFunction(() => !!window.errorMessage);
    await page.locator('#modal-media-identification').waitFor({state: 'visible'});
    await page.waitForFunction(() => getComputedStyle(
      document.querySelector('#modal-media-identification')).opacity === '1');
    assert.equal(await page.locator('#rename_episode_details').inputValue(), '3');
    assert.equal(await page.locator('#rename_episode').inputValue(), '');
    await page.screenshot({path: path.join(artifacts, 'manual-ep-desktop.png')});
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('#rename_episode_details').scrollIntoViewIfNeeded();
    await page.screenshot({path: path.join(artifacts, 'manual-ep-mobile.png')});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.evaluate(() => { window.failure = false; });
    await page.locator('#identification_action_btn').press('Enter');
    await page.waitForFunction(() => window.finished);
    assert.deepEqual(errors, []);
    fs.writeFileSync(path.join(artifacts, 'ui-report.json'),
      JSON.stringify({browser: 'WebKit', headless: true, outcomes, retry: 'passed', keyboard: 'passed',
        narrowViewport: 'passed', errors}, null, 2));
    console.log('PASS: 9 form cases, failed-target recovery, keyboard retry, desktop/mobile; WebKit headless.');
  } finally {
    await context.tracing.stop({path: path.join(artifacts, 'manual-ep-ui-trace.zip')});
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
