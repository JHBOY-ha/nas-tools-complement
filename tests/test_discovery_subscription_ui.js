// Run with Node and Playwright WebKit. All provider and subscription responses are local fixtures.
const {webkit} = require('playwright');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '..');
const shell = fs.readFileSync(path.join(root, 'web/templates/navigation.html'), 'utf8');
const ask = shell.slice(shell.indexOf('<!-- 询问框 -->'), shell.indexOf('<!-- 成功提示 -->'));
const seasons = shell.slice(shell.indexOf('<!-- 订阅季 -->'), shell.indexOf('<!-- 手动订阅 -->'));
const dialogs = shell.slice(shell.indexOf('  function show_ask_modal('), shell.indexOf('  // 显示成功提示'));
const subscriptions = shell.slice(shell.indexOf('  function add_rss_media('), shell.indexOf('  // 刷新订阅\n'));
const chooseSeasons = shell.slice(shell.indexOf('  function show_rss_seasons_modal('), shell.indexOf('  //搜索\n', shell.indexOf('  function show_rss_seasons_modal(')));

(async () => {
  const browser = await webkit.launch({headless: true,
    ...(process.env.WEBKIT_PATH ? {executablePath: process.env.WEBKIT_PATH} : {})});
  try {
    const page = await browser.newPage({reducedMotion: 'reduce'});
    const calls = [], errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://discover.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') return route.fulfill({contentType:'text/html; charset=utf-8', body: `
        <meta charset="utf-8"><link rel="stylesheet" href="/static/css/tabler.min.css">
        <body><page-discovery discovery-type="BANGUMI"></page-discovery>${ask}${seasons}<output id="notice"></output>
        <script src="/static/js/jquery-3.3.1.min.js"></script><script src="/static/js/tabler.min.js"></script>
        <script src="/static/js/nprogress.js"></script><script src="/static/js/util.js"></script><script>
        history.replaceState({page:'bangumi', extra:{}}, '');
        function window_history(_, extra) {history.replaceState({page:'bangumi', extra}, '');}
        function hide_mediainfo_modal() {}
        function show_success_modal(text) {document.querySelector('#notice').textContent = text;}
        ${dialogs}
        ${subscriptions}
        ${chooseSeasons}
        </script><script type="module">
        import '/static/components/card/normal/index.js';
        import '/static/components/custom/slide/index.js';
        import '/static/components/page/discovery/index.js';
        </script></body>`});
      if (url.pathname === '/do') {
        const form = new URLSearchParams(route.request().postData());
        const cmd = form.get('cmd'), data = JSON.parse(form.get('data'));
        calls.push({cmd, data});
        if (cmd === 'get_recommend') return route.fulfill({json:{code:0, Items:[
          {type:'TV', id:'BG:'+data.week, title:'番剧'+data.week, year:'2026', fav:null, rssid:null, image:'/static/img/no-image.png'}
        ]}});
        if (cmd === 'get_tvseason_list') return route.fulfill({json:{code:0, seasons:[{num:1,text:'第一季'},{num:2,text:'第二季'}]}});
        if (cmd === 'add_rss_media') return route.fulfill({json:{code:9, msg:'订阅已存在', rssid:17}});
        if (cmd === 'remove_rss_media') return route.fulfill({json:{code:0}});
        throw new Error('Unexpected action '+cmd);
      }
      if (url.pathname.startsWith('/static/')) return route.fulfill({path:path.join(root, 'web', url.pathname)});
      return route.fulfill({status:404, body:''});
    });
    await page.goto('http://discover.test/');
    const card = page.locator('normal-card[card-tmdbid="BG:1"]');
    await page.waitForFunction(() => document.querySelectorAll('normal-card').length === 7);
    await card.hover();
    await card.getByRole('button', {name:'订阅此作品'}).click();
    const dialog = page.locator('#system-ask-modal');
    await dialog.getByRole('button', {name:'取消'}).click();
    await dialog.waitFor({state:'hidden'});
    assert.deepEqual(calls.map(c => c.cmd), Array(7).fill('get_recommend'));

    await card.hover();
    await card.getByRole('button', {name:'订阅此作品'}).click();
    await dialog.getByRole('button', {name:'确定'}).click();
    const selector = page.locator('#system-rss-seasons');
    await selector.getByRole('button', {name:'Close'}).click();
    await selector.waitFor({state:'hidden'});
    assert.deepEqual(calls.slice(7).map(c => c.cmd), ['get_tvseason_list']);
    await card.hover();
    await card.getByRole('button', {name:'订阅此作品'}).click();
    await dialog.getByRole('button', {name:'确定'}).click();
    await selector.getByText('第二季').click();
    await selector.getByText('确定').click();
    await page.waitForFunction(() => document.querySelector('#notice').textContent.includes('已在订阅中'));
    assert.deepEqual(calls.slice(7).map(c => c.cmd), ['get_tvseason_list','get_tvseason_list','add_rss_media']);
    assert.equal(calls.at(-1).data.mediaid, 'BG:1');
    assert.deepEqual(calls.at(-1).data.season, ['2']);
    const cached = await page.evaluate(() => history.state.extra['get_recommendBANGUMI星期一'].Items[0]);
    assert.equal(cached.fav, '1');
    assert.equal(cached.rssid, 17);
    await card.hover();
    await card.getByRole('button', {name:'取消订阅'}).click();
    await dialog.getByRole('button', {name:'确定'}).click();
    await page.waitForFunction(() => document.querySelector('normal-card[card-tmdbid="BG:1"]').fav === '0');
    assert.equal(calls.at(-1).cmd, 'remove_rss_media');
    assert.equal(calls.at(-1).data.rssid, 17);
    assert.deepEqual(errors, []);
    if (process.env.TEST_ARTIFACT_DIR) {
      fs.mkdirSync(process.env.TEST_ARTIFACT_DIR, {recursive:true});
      await page.screenshot({path:path.join(process.env.TEST_ARTIFACT_DIR, 'discovery-confirmation.png')});
    }
    console.log('PASS: seven display-only lists, canceled confirmation, selected title/season, existing subscription and confirmed removal.');
  } finally { await browser.close(); }
})().catch(error => {console.error(error); process.exitCode = 1;});
