/* Offline browser integration: serve actual shared modal/assets with mocked TMDB actions. */
const {chromium} = require('playwright');
const fs = require('fs');
const path = require('path');
const assert = require('assert');
const root = path.resolve(__dirname, '..');
(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 1100, height: 850}});
    const errors = [];
    page.on('pageerror', error => errors.push(error.stack || error.message));
    let fail = false, empty = false, confirms = 0;
    const work = {tmdb_id: 42, type: 'tv', title: '冰菓 <img src=x onerror=alert(1)>', date: '2012-04-22', overview: '作品简介', link: 'https://www.themoviedb.org/tv/42'};
    await page.route('http://review.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') {
        return route.fulfill({contentType: 'text/html; charset=utf-8', body: `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/css/tabler.min.css"><body>${fs.readFileSync(path.join(root, 'web/templates/rename/special_confirmation.html'), 'utf8')}<script src="/static/js/jquery-3.3.1.min.js"></script><script src="/static/js/tabler.min.js"></script><script>window.navmenu=()=>{};window.show_success_modal=()=>{window.completed=true};</script><script src="/static/js/special-confirmation.js"></script></body></html>`});
      }
      if (url.pathname === '/do') {
        const payload = JSON.parse(new URLSearchParams(route.request().postData()).get('data'));
        let result;
        if (payload.stage === 'search') result = fail ? {retcode: 2, retmsg: 'TMDB 查询失败，请重试'} : {retcode: 0, query: payload.query || 'Hyouka', fingerprint: ['1','2','3','1720000000000000000'], works: empty ? [] : [work]};
        else if (payload.stage === 'detail') result = {retcode: 0, work, seasons: [0,1], season: payload.season || 0, episodes: [{episode: 1, title: '特别篇', date: '2013-01-01', overview: '单集简介', link: work.link + '/season/0/episode/1'}]};
        else { confirms++; await new Promise(resolve => setTimeout(resolve, 150)); result = {retcode: 0}; }
        return route.fulfill({json: result});
      }
      const file = path.join(root, 'web', url.pathname);
      if (fs.existsSync(file)) return route.fulfill({path: file, contentType: file.endsWith('.js') ? 'application/javascript; charset=utf-8' : file.endsWith('.css') ? 'text/css; charset=utf-8' : 'application/octet-stream'});
      return route.fulfill({status: 404, body: ''});
    });
    await page.goto('http://review.test/');
    assert.deepEqual(errors, [], 'scripts load without errors: ' + JSON.stringify(errors));
    const open = () => page.evaluate(() => SpecialConfirmation.open([{id: 1, flag: 'unidentification', filename: '[T.H.X&VCB-Studio] Hyouka [11.5][Ma10p_1080p][x265_flac_aac].mkv'}]));
    await open();
    await page.locator('input[name="special-work"]').waitFor();
    assert(await page.locator('#special-submit').isDisabled());
    assert.equal(await page.locator('#special-work-list img').count(), 0, 'TMDB text must not execute markup');
    await page.locator('input[name="special-work"]').check();
    await page.locator('input[name="special-episode"]').check();
    assert(await page.locator('#special-preview').innerText().then(t => t.includes('S00E01')));
    assert(await page.locator('#special-submit').isDisabled());
    assert.equal(await page.locator('#special-episode-list a').getAttribute('rel'), 'noopener noreferrer');
    await page.setViewportSize({width: 390, height: 720});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.locator('#special-reviewed').check();
    await page.locator('#special-submit').click();
    assert(await page.locator('#special-submit').isDisabled());
    await page.waitForFunction(() => window.completed);
    assert.equal(confirms, 1);
    await page.locator('#special-confirmation-modal').waitFor({state: 'hidden'});
    fail = true;
    await open();
    await page.getByText('TMDB 查询失败，请重试', {exact: true}).waitFor();
    fail = false; empty = true;
    await page.locator('#special-search').click();
    await page.getByText('未找到相关作品。请修改名称后重新查询。', {exact: true}).waitFor();
    empty = false;
    await page.locator('#special-search').click();
    await page.locator('input[name="special-work"]').waitFor();
    await page.locator('#special-clear').click();
    assert.equal(await page.locator('input[name="special-work"]').count(), 0);
    assert(await page.locator('#special-query').evaluate(el => el === document.activeElement));
    await page.locator('#special-query').fill('Hyouka');
    await page.locator('#special-query').press('Enter');
    await page.locator('input[name="special-work"]').waitFor();
    await page.locator('input[name="special-work"]').check();
    await page.locator('#special-season').selectOption('1');
    await page.locator('input[name="special-episode"]').waitFor();
    await page.screenshot({path: '/tmp/special-confirmation-mobile.png'});
    await page.keyboard.press('Escape');
    await page.locator('#special-confirmation-modal').waitFor({state: 'hidden'});
    assert.deepEqual(errors, []);
    console.log('PASS: selection, review gate, links, safe text, single submit, retry, empty, clear, keyboard, narrow layout');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
