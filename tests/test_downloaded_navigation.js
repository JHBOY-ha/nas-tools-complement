// Run: node tests/test_downloaded_navigation.js (requires Playwright WebKit).
const {webkit} = require('playwright');
const {spawnSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const rendered = spawnSync('python3', ['-c', `
from tests.test_recommend_rendering import create_app
with create_app().test_client() as client:
    print(client.post('/downloaded').get_data(as_text=True))
`], {cwd: root, encoding: 'utf8'});
assert.equal(rendered.status, 0, rendered.stderr);
const shell = fs.readFileSync(path.join(root, 'web/templates/navigation.html'), 'utf8');
const history = shell.slice(shell.indexOf('  let CURRENT_PAGE_URI'), shell.indexOf('  //刷新LOG标志'));
const navigation = shell.slice(shell.indexOf('  function navmenu('), shell.indexOf('</script>', shell.indexOf('  function navmenu(')));

(async () => {
  const browser = await webkit.launch({headless: true,
    ...(process.env.WEBKIT_PATH ? {executablePath: process.env.WEBKIT_PATH} : {})});
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://navigation.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: `
        <meta charset="utf-8"><title>Navigation regression</title>
        <nav id="navbar-menu">
          <button onclick="navmenu('downloaded')">近期下载</button>
          <button onclick="navmenu('downloading')">正在下载</button>
          <button onclick="navmenu('history')">历史记录</button>
        </nav><main class="page" id="page_content"></main>
        <script src="/static/js/jquery-3.3.1.min.js"></script>
        <script src="/static/js/nprogress.js"></script>
        <script src="/static/js/util.js"></script>
        <script>
          document.querySelector('#navbar-menu').update_active = page => {
            document.querySelector('#navbar-menu').dataset.active = page;
          };
          function fresh_tooltip() {}
          function init_filetree_element() {}
          ${history}
          ${navigation}
        </script>`});
      if (url.pathname === '/downloaded') return route.fulfill({contentType: 'text/html', body: rendered.stdout});
      if (url.pathname === '/downloading' || url.pathname === '/history') {
        return route.fulfill({contentType: 'text/html', body: `<h2>${url.pathname === '/history' ? '历史记录' : '正在下载'}</h2>`});
      }
      if (url.pathname === '/do') return route.fulfill({json: {Items: []}});
      if (url.pathname.startsWith('/static/')) return route.fulfill({path: path.join(root, 'web', url.pathname)});
      throw new Error(`Unexpected request: ${url}`);
    });
    await page.goto('http://navigation.test/');
    for (const previous of ['正在下载', '历史记录', '正在下载']) {
      await page.getByRole('button', {name: '近期下载', exact: true}).click();
      await page.waitForFunction(() => document.querySelector('#recommend_title')?.textContent === '近期下载'
        && document.querySelector('#recommend_content')?.textContent.includes('没有数据')
        && !document.querySelector('#page_content').classList.contains('content-fade-out'));
      assert.equal(await page.evaluate(() => window.history.state.page), 'downloaded');
      assert.equal(await page.locator('#navbar-menu').getAttribute('data-active'), 'downloaded');
      await page.getByRole('button', {name: previous, exact: true}).click();
      await page.waitForFunction(title => document.querySelector('#page_content h2')?.textContent === title
        && !document.querySelector('#page_content').classList.contains('content-fade-out'), previous);
    }
    assert.deepEqual(errors, []);
    if (process.env.TEST_ARTIFACT_DIR) {
      fs.mkdirSync(process.env.TEST_ARTIFACT_DIR, {recursive: true});
      await page.screenshot({path: path.join(process.env.TEST_ARTIFACT_DIR, 'navigation.png')});
    }
    console.log('PASS: repeated recent-download navigation, content, active menu and history; no script errors');
  } finally {
    await browser.close();
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
