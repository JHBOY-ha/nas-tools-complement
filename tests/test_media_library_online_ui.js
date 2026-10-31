/* Real Bootstrap dialogs and source selection with offline subtitle API fixtures. */
const {firefox} = require('playwright');
const {spawnSync} = require('child_process');
const fs = require('fs'), path = require('path'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const rendered = spawnSync('python3', ['-c', `
import json
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, ChainableUndefined
env = Environment(loader=FileSystemLoader('web/templates'), undefined=ChainableUndefined, autoescape=True)
library = env.get_template('rename/medialibrary.html').render(MediaServerType='emby')
online = library[library.index('<div class="modal modal-blur fade lit-library-modal" id="library-online-subtitle-modal"'):]
shell = Path('web/templates/navigation.html').read_text()
confirm = shell[shell.index('<!-- 危险确认提示框 -->'):shell.index('<!-- 成功带后续操作提示框 -->')]
confirm = env.from_string("{% import 'macro/svg.html' as SVG %}" + confirm).render()
print(json.dumps(dict(online=online, confirm=confirm)))
`], {cwd: root, encoding: 'utf8'});
assert.equal(rendered.status, 0, rendered.stderr);
const fixtures = JSON.parse(rendered.stdout);

(async () => {
  const browser = await firefox.launch({headless: true, ...(process.env.FIREFOX_PATH ? {executablePath: process.env.FIREFOX_PATH} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 1100, height: 800}, reducedMotion: 'reduce'});
    const errors = [], downloads = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://subtitles.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') {
        return route.fulfill({contentType: 'text/html; charset=utf-8', body: `<!doctype html>
          <html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
          <title>字幕库在线字幕验证</title><link rel="stylesheet" href="/static/css/tabler.min.css">
          <body><main class="container-xl py-4"><button id="open" class="btn btn-primary">在线字幕</button><div id="feedback" role="status"></div></main>
          ${fixtures.online}${fixtures.confirm}
          <script src="/static/js/jquery-3.3.1.min.js"></script><script src="/static/js/tabler.min.js"></script>
          <script src="/static/js/media-library.js"></script><script>
          // Only service boundaries are fixtures; dialog and result handlers are production code.
          function show_confirm_modal(title, func) {
            $('#system_confirm_message').text(title); $('#system_confirm_btn').off('click').on('click', func); $('#system-confirm-modal').modal('show');
          }
          function hide_confirm_modal() { $('#system-confirm-modal').modal('hide'); }
          function show_success_modal(message) { $('#feedback').text(message); }
          function show_fail_modal(message) { $('#feedback').text(message); }
          window.SubtitleTasks = {watch: (id, callbacks) => setTimeout(() => callbacks.onDone({status: 'succeeded'}), 150), refreshList: () => Promise.resolve()};
          load_library_items = function () {};
          document.getElementById('open').onclick = () => open_library_online_subtitles('Movie', '/library/Movie.mkv', {title: 'Movie', year: 2024, media_type: 'movie', imdbid: 'tt123'});
          </script></body></html>`});
      }
      if (url.pathname === '/library/subtitle/search') {
        const payload = route.request().postDataJSON();
        await new Promise(resolve => setTimeout(resolve, 80));
        if (payload.keyword === 'empty') return route.fulfill({json: {code: 0, items: [], warnings: []}});
        if (payload.keyword === 'error') return route.fulfill({json: {code: -1, msg: 'OpenSubtitles请求失败：HTTP 403'}});
        return route.fulfill({json: {code: 0, items: [{name: 'Movie.2024.1080p', provider: 'opensubtitles',
          format: 'srt', language: '简体中文', match_label: '片名与年份匹配', ticket: 'signed-result'}], warnings: []}});
      }
      if (url.pathname === '/library/subtitle/download') {
        const payload = route.request().postDataJSON();
        downloads.push(payload);
        assert.deepEqual(payload, {ticket: 'signed-result', confirm_quota: true});
        return route.fulfill({json: {code: 0, task_id: 'task-1', msg: '字幕已加入处理队列'}});
      }
      const file = path.join(root, 'web', url.pathname);
      if (file.startsWith(path.join(root, 'web/static') + path.sep) && fs.existsSync(file)) {
        return route.fulfill({path: file, contentType: file.endsWith('.css') ? 'text/css' : 'application/javascript'});
      }
      return route.fulfill({status: 404, body: ''});
    });
    await page.goto('http://subtitles.test/');
    await page.getByRole('button', {name: '在线字幕', exact: true}).click();
    const online = page.locator('#library-online-subtitle-modal');
    const confirmation = page.locator('#system-confirm-modal');
    await page.getByRole('button', {name: '下载并保存', exact: true}).waitFor();
    // Popup rendering belongs to the OS; verify the native option and the
    // application's real keyboard focus order without automating OS menus.
    await page.getByRole('combobox', {name: '字幕来源'}).selectOption('opensubtitles');
    await page.getByRole('combobox', {name: '字幕来源'}).focus();
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement.innerText), '恢复默认词');
    assert.equal(await page.locator('#library_online_provider').inputValue(), 'opensubtitles');
    await page.getByRole('button', {name: '检索字幕', exact: true}).click();
    assert.equal(await page.locator('#library_online_search').isDisabled(), true);
    await page.getByRole('button', {name: '下载并保存', exact: true}).waitFor();
    assert.match(await page.locator('#library_online_results').innerText(), /OpenSubtitles/);
    await page.getByRole('button', {name: '下载并保存', exact: true}).click();
    await confirmation.waitFor({state: 'visible'});
    assert.equal(await online.isVisible(), false);
    assert.match(await confirmation.innerText(), /一次账户下载配额/);
    await page.keyboard.press('Escape');
    await online.waitFor({state: 'visible'});
    assert.equal(downloads.length, 0);
    await page.waitForFunction(() => document.activeElement.classList.contains('library-online-download'));
    await page.getByRole('button', {name: '下载并保存', exact: true}).click();
    await confirmation.waitFor({state: 'visible'});
    await page.locator('#system_confirm_btn').click();
    await page.getByRole('button', {name: '已保存', exact: true}).waitFor();
    assert.equal(downloads.length, 1);
    assert.equal(await page.getByRole('button', {name: '已保存', exact: true}).isDisabled(), true);
    for (const [keyword, text] of [['empty', '未返回符合目标片名'], ['error', 'HTTP 403']]) {
      await page.locator('#library_online_keyword').fill(keyword);
      await page.getByRole('button', {name: '检索字幕', exact: true}).click();
      await page.waitForFunction(expected => document.getElementById('library_online_results').innerText.includes(expected), text);
      assert.equal(await page.locator('#library_online_search').isDisabled(), false);
    }
    await page.setViewportSize({width: 390, height: 844});
    await page.locator('#library_online_keyword').fill('Movie 2024');
    await page.getByRole('button', {name: '检索字幕', exact: true}).click();
    await page.getByRole('button', {name: '下载并保存', exact: true}).waitFor();
    assert.equal(await page.getByRole('button', {name: '下载并保存', exact: true}).isVisible(), true);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({path: '/tmp/nas-tools-opensubtitles-online-mobile.png', fullPage: true});
    assert.deepEqual(errors, []);
    console.log('OpenSubtitles browser checks passed: native source selection, keyboard focus, loading, confirmation/cancel focus, queued save, errors, empty results and 390px layout.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
