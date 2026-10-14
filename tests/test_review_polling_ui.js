/* Real shell polling and upload handlers across a page replacement. */
const {chromium} = require('playwright');
const fs = require('fs'), path = require('path'), assert = require('assert');
const root = path.resolve(__dirname, '..');
const nav = fs.readFileSync(path.join(root, 'web/templates/navigation.html'), 'utf8');
const polling = nav.slice(nav.indexOf('  function refresh_message('), nav.indexOf('  //检查系统是否在线'));
const torrentSetup = nav.slice(nav.indexOf('    torrent_dropzone = new Dropzone'), nav.indexOf('    // 单选框事件'));
const service = fs.readFileSync(path.join(root, 'web/templates/service.html'), 'utf8');
const backupSetup = service.slice(service.indexOf('  backup_dropzone = new Dropzone'), service.indexOf('</script>', service.indexOf('  backup_dropzone = new Dropzone')));
(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    let polls = 0, release;
    const pending = new Promise(resolve => { release = resolve; });
    await page.route('http://review.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: `
        <main id="page_content"><div>old page</div></main><div id="system-messages"></div>
        <form id="torrent_files" action="/upload/torrent"></form><form id="back_file" action="/upload"></form>
        <script src="/static/js/jquery-3.3.1.min.js"></script><script src="/static/js/nprogress.js"></script>
        <script src="/static/js/dropzone-min.js"></script><script>Dropzone.autoDiscover=false;</script>
        <script src="/static/js/action-tasks/client.js"></script><script src="/static/js/util.js"></script>
        <script>var torrent_dropzone,backup_dropzone; ${polling}; ${torrentSetup}; ${backupSetup};</script>`});
      if (url.pathname === '/do') {
        const command = new URLSearchParams(route.request().postData()).get('cmd');
        if (command === 'refresh_message') {
          polls++;
          if (polls === 1) await pending;
          return route.fulfill({json: {code: polls === 1 ? 0 : 1, lst_time: 'next', message: ['<span>message</span>']}});
        }
        await pending;
        return route.fulfill({json: {code: 0}});
      }
      if (url.pathname === '/upload/torrent' || url.pathname === '/upload') {
        return route.fulfill({json: {code: 0, filename: url.pathname.endsWith('torrent') ? 'server.torrent' : 'server.zip'}});
      }
      return route.fulfill({path: path.join(root, 'web', url.pathname)});
    });
    await page.goto('http://review.test/');
    await page.evaluate(() => {
      // Accelerate only the ten-second shell poll; preserve all request ordering.
      const timer = window.setTimeout;
      window.setTimeout = (fn, delay, ...args) => timer(fn, delay === 10000 ? 10 : delay, ...args);
      refresh_message('');
      ajax_post('page_read', {}, () => window.staleCalled = true, true, false);
    });
    await page.waitForTimeout(50);
    await page.evaluate(() => document.getElementById('page_content').replaceChildren(document.createElement('div')));
    release();
    await page.waitForFunction(() => document.querySelector('#system-messages span'));
    await page.waitForTimeout(100);
    assert.equal(polls, 2, 'Message callback must rearm after navigation');
    assert.equal(await page.evaluate(() => Boolean(window.staleCalled)), false, 'Page-scoped callbacks still expire');
    // Real Dropzone success events retain original names but submit server names.
    await page.evaluate(() => {
      torrent_dropzone.addFile(new File(['torrent'], 'Movie.torrent'));
      backup_dropzone.addFile(new File(['backup'], 'backup.zip'));
    });
    await page.waitForFunction(() => torrent_dropzone.files[0].upload.filename === 'server.torrent'
      && backup_dropzone.files[0].serverFilename === 'server.zip');
    assert.equal(await page.evaluate(() => torrent_dropzone.files[0].name), 'Movie.torrent');
    assert.deepEqual(errors, []);
    console.log('PASS: navigation-safe message polling, stale page isolation, torrent/backup server filenames');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exit(1); });
