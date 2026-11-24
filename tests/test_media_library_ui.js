/* Real Bootstrap/Tabler page: status tabs, card menu, detail drawer, drop zone and continuous fill. */
const {firefox} = require('playwright');
const {spawnSync} = require('child_process');
const fs = require('fs'), path = require('path'), assert = require('node:assert/strict');
const root = path.resolve(__dirname, '..');
const evidenceDir = process.env.TEST_ARTIFACT_DIR || '/tmp/nas-tools-library-ui';
fs.mkdirSync(evidenceDir, {recursive: true});
const rendered = spawnSync('python3', ['-c', `
import json, re
from pathlib import Path
from jinja2 import Environment, FileSystemLoader, ChainableUndefined
env = Environment(loader=FileSystemLoader('web/templates'), undefined=ChainableUndefined, autoescape=True)
body = env.get_template('rename/medialibrary.html').render(MediaServerType='emby')
body = re.sub(r'<script type="text/javascript" src="../static/js/[^"]+"></script>', '', body)
body = re.sub(r'<script type="text/javascript">.*?</script>', '', body, flags=re.S)
shell = Path('web/templates/navigation.html').read_text()
confirm = shell[shell.index('<!-- 危险确认提示框 -->'):shell.index('<!-- 成功带后续操作提示框 -->')]
confirm = env.from_string("{% import 'macro/svg.html' as SVG %}" + confirm).render()
print(json.dumps(dict(body=body, confirm=confirm)))
`], {cwd: root, encoding: 'utf8'});
assert.equal(rendered.status, 0, rendered.stderr);
const fixtures = JSON.parse(rendered.stdout);

const movie = (n, extra = {}) => Object.assign({
  id: `m${n}`, server_item_id: `m${n}`, title: `电影${n}`, original_title: `Movie ${n}`, year: '2024',
  media_type: 'movie', media_type_name: '电影', category: '外语电影', linked: true,
  target_path: `/media/Movie ${n}.mkv`, can_upload: true, poster_url: '', subtitle_status: 'missing_chinese',
  subtitle_label: '缺中文字幕', subtitle_badge: 'bg-orange', audio_language: 'eng'}, extra);
const items = [movie(1), movie(2), movie(3),
  movie(4, {linked: false, target_path: '', can_upload: false, subtitle_status: 'unknown', subtitle_label: '未检测'}),
  {id: 's1', server_item_id: 's1', title: '剧集', media_type: 'tv', media_type_name: '电视剧', category: '欧美剧',
    subtitle_status: 'unknown', subtitle_label: '已覆盖 1/2 集', episode_total: 2, episode_covered: 1}];
const episodes = [
  {id: 'e1', server_item_id: 'e1', title: '一', season: 1, episode: 1, season_episode: 'S01E01', path: '/media/s/S01E01.mkv', can_upload: true, subtitle_label: '缺中文字幕', subtitle_badge: 'bg-orange'},
  {id: 'e2', server_item_id: 'e2', title: '二', season: 1, episode: 2, season_episode: 'S01E02', path: '/media/s/S01E02.mkv', can_upload: true, subtitle_label: '缺中文字幕', subtitle_badge: 'bg-orange'}];

(async () => {
  const browser = await firefox.launch({headless: true, ...(process.env.FIREFOX_PATH ? {executablePath: process.env.FIREFOX_PATH} : {})});
  let page;
  try {
    page = await browser.newPage({viewport: {width: 1280, height: 900}, reducedMotion: 'reduce'});
    const errors = [], requests = [];
    const categories = {movie: ['外语电影'], tv: ['欧美剧', '动漫'], anime: ['动漫']};
    let releaseCategories;
    const categoriesReady = new Promise(resolve => { releaseCategories = resolve; });
    let refreshReply = {code: 0, updated: 1, unchanged: 4, failed: 0, requested: 5};
    const problemItems = [movie(21, {subtitle_audit_status: 'error', subtitle_audit_label: '外挂字幕无法识别', subtitle_audit_badge: 'bg-red'}),
      movie(22, {subtitle_audit_status: 'warning', subtitle_audit_label: '外挂字幕语言需规范', subtitle_audit_badge: 'bg-yellow'}),
      movie(23, {subtitle_audit_status: 'warning', can_upload: false, target_path: ''}),
      movie(24, {subtitle_audit_status: 'warning'}), movie(25, {subtitle_audit_status: 'error'})];
    let loseBatchReceipt = true;
    let authBusy = true;
    let repairStatus = 'running', gonePolls = 0;
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://library.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') {
        return route.fulfill({contentType: 'text/html; charset=utf-8', body: `<!doctype html>
          <html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
          <title>字幕库验证</title><link rel="stylesheet" href="/static/css/tabler.min.css"><link rel="stylesheet" href="/static/css/style.css">
          <body><div class="page"><div class="page-wrapper">${fixtures.body}</div></div>${fixtures.confirm}<div id="feedback" role="status"></div>
          <script src="/static/js/jquery-3.3.1.min.js"></script><script src="/static/js/tabler.min.js"></script>
          <script src="/static/js/subtitle-tasks.js"></script>
          <script>
          window.NProgress = {start() {}, done() {}}; window.navmenu = () => {};
          function show_confirm_modal(title, func) {
            $('#system_confirm_message').text(title); $('#system_confirm_btn').off('click').on('click', func); $('#system-confirm-modal').modal('show');
          }
          function hide_confirm_modal() { $('#system-confirm-modal').modal('hide'); }
          function show_success_modal(message) { $('#feedback').text(message); }
          function show_fail_modal(message) { $('#feedback').text(message); }
          window.window_history = () => {};
          let requestSequence = 0;
          window.SubtitleTasks = {init() {}, openCenter() {}, request: async () => ({}), payloadOf: r => r,
            isTerminal: window.SubtitleTasks.isTerminal,
            requestTask: async (url, payload) => {
              await $.post(url, JSON.stringify(payload));
              return {legacy:true};
            }, randomRequestId: () => 'request-' + (++requestSequence), formatBytes: v => v + ' B',
            validateFiles: files => ({ok: true, logicalCount: files.length, totalBytes: 0}), renderProgress() {},
            watch: window.SubtitleTasks.watch, refreshList: () => Promise.resolve()};
          </script>
          <script src="/static/js/media-library.js"></script>
          <script>init_media_library_page({page_size: 18, default_server: 'emby'});</script></body></html>`});
      }
      if (url.pathname === '/library/items') {
        requests.push({path: url.pathname, body: route.request().postDataJSON()});
        const rows = route.request().postDataJSON().subtitle === 'problem' ? problemItems : items;
        return route.fulfill({json: {code: 0, items: rows, total: rows.length, page: 1, library_total: items.length,
          synced_at: '2026-10-31 08:00:00', categories}});
      }
      if (url.pathname === '/library/subtitle/repair/batch') {
        const body = route.request().postDataJSON();
        requests.push({path: url.pathname, body});
        if (authBusy) {
          authBusy = false;
          return route.fulfill({status: 503, json: {code: -1, retryable: true, msg: '数据库繁忙，暂时无法核验账号，请稍后重试'}});
        }
        if (loseBatchReceipt) {
          loseBatchReceipt = false;
          return route.abort('connectionfailed');
        }
        if (body.items.length === 1 && body.items[0].server_item_id === 'm25') {
          return route.fulfill({json: {code: 0, results: [{task_id: 'retried', status: 'succeeded', reused: false}]}});
        }
        return route.fulfill({json: {code: 0, results: [
          {task_id: 'repair-one', status: 'running', reused: true}, {error: '字幕上传/修复等待队列已满'},
          {task_id: 'repair-gone', status: 'queued'}, {error: '数据库繁忙，本次未尝试提交', retryable: true}
        ]}});
      }
      if (url.pathname === '/subtitle/tasks/repair-gone') {
        gonePolls++;
        // A temporary error must still retry; the task-detail 404 ends polling.
        if (gonePolls === 1) return route.fulfill({status: 503, json: {code: -1, msg: '暂时不可用'}});
        return route.fulfill({status: 404, json: {code: -1, msg: '字幕任务不存在'}});
      }
      if (url.pathname === '/subtitle/tasks/repair-one') {
        return route.fulfill({json: {code: 0, data: {task_id: 'repair-one', type: 'repair', status: repairStatus}}});
      }
      if (url.pathname === '/subtitle/tasks') return route.fulfill({json: {code: 0, items: []}});
      if (url.pathname === '/subtitle/tasks/settings') return route.fulfill({json: {code: 0, data: {}}});
      if (url.pathname === '/library/summary') {
        requests.push({path: url.pathname, body: route.request().postDataJSON()});
        return route.fulfill({json: {code: 0, counts: {all: 5, missing: 3, unknown: 2, problem: problemItems.length, ok: 0},
          category_counts: {'外语电影': 3}}});
      }
      if (url.pathname === '/library/refresh') {
        requests.push({path: url.pathname, body: route.request().postDataJSON()});
        return route.fulfill({json: refreshReply});
      }
      if (url.pathname.indexOf('sync') !== -1) { requests.push({path: url.pathname}); return route.fulfill({json: {code: 0}}); }
      if (url.pathname === '/library/subtitle/audit/categories') {
        await categoriesReady;
        return route.fulfill({json: {code: 0, categories}});
      }
      if (url.pathname === '/library/subtitle/audit/history') return route.fulfill({json: {code: 0, history: []}});
      if (url.pathname === '/library/subtitle/audit') {
        requests.push({path: url.pathname, body: JSON.parse(route.request().postData())});
        return route.fulfill({json: {code: 0}});
      }
      if (url.pathname === '/library/episodes') return route.fulfill({json: {code: 0, items: episodes}});
      if (url.pathname === '/library/subtitle/preview') {
        requests.push({path: url.pathname, body: route.request().postDataJSON()});
        const names = route.request().postDataJSON().filenames;
        return route.fulfill({json: {code: 0, items: names.map(name => ({name, language: 'zh-CN', target: 'Movie 1.zh-CN.srt', note: ''}))}});
      }
      if (url.pathname === '/library/subtitle/search') return route.fulfill({json: {code: 0, items: [], warnings: []}});
      if (url.pathname === '/subtitle/upload') { requests.push({path: url.pathname}); return route.fulfill({json: {code: 0}}); }
      const file = path.join(root, 'web', url.pathname);
      if (file.startsWith(path.join(root, 'web/static') + path.sep) && fs.existsSync(file)) {
        return route.fulfill({path: file, contentType: file.endsWith('.css') ? 'text/css' : 'application/javascript'});
      }
      return route.fulfill({status: 404, body: ''});
    });
    await page.goto('http://library.test/');
    await page.locator('.lit-library-card').first().waitFor();
    const lastList = () => requests.filter(r => r.path === '/library/items').pop().body;

    // Both button sizes have fully rounded ends; the search field keeps its form-control corners.
    const measure = selector => page.locator(selector).first().evaluate(el => {
      const style = getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      const pill = style.borderRadius.split(' ').every(corner => parseFloat(corner) >= Math.min(rect.width, rect.height) / 2);
      return {height: Math.round(rect.height), radius: pill ? 'pill' : style.borderRadius, font: style.fontSize};
    });
    for (const selector of ['#library_fill_btn', '[data-subtitle-task-center]', '#library_refresh_btn', '#library_more_trigger',
      '.lit-library-status-tab', '#library_filter_keyword_clear', '#library_filter_trigger',
      'form[role=search] button[type=submit]', '#library_prev_btn']) {
      assert.deepEqual(await measure(selector), {height: 36, radius: 'pill', font: '14px'}, selector);
    }
    assert.deepEqual(await measure('#library_filter_keyword'), {height: 36, radius: '8px', font: '14px'});
    for (const selector of ['.lit-library-card-actions .btn-primary', '.lit-library-card-actions .btn-icon']) {
      assert.deepEqual(await measure(selector), {height: 28, radius: 'pill', font: '12px'}, selector);
    }
    assert.equal(await page.locator('.lit-library-panel .btn-primary:visible').count(), await page.locator('#library_fill_btn, .lit-library-card-actions .btn-primary').count(),
      'the header holds the only solid button outside the cards');

    // Manual refresh asks the server about the visible items only and never starts a full sync.
    const refreshCalls = () => requests.filter(r => r.path === '/library/refresh');
    await page.getByRole('button', {name: '刷新'}).click();
    await page.waitForFunction(() => /已更新 1 项，4 项无变化/.test(document.getElementById('library_items_summary').textContent));
    assert.deepEqual(refreshCalls().pop().body.item_ids.sort(), ['m1', 'm2', 'm3', 'm4', 's1']);
    assert.equal(await page.locator('#library_synced_at').innerText(), '上次同步：2026-10-31 08:00:00');
    refreshReply = {code: 0, updated: 0, unchanged: 3, failed: 2, requested: 5};
    await page.getByRole('button', {name: '刷新'}).click();
    await page.waitForFunction(() => /2 项读取失败，仍显示原数据/.test(document.getElementById('library_items_summary').textContent));
    refreshReply = {code: 0, updated: 0, unchanged: 0, failed: 5, requested: 5};
    await page.getByRole('button', {name: '刷新'}).click();
    await page.waitForFunction(() => /无法从媒体服务器读取最新信息/.test(document.getElementById('feedback').textContent));
    // The failure dialog appears before the cached list has finished reloading.
    await page.waitForFunction(() => /当前显示上次同步的数据/.test(document.getElementById('library_items_summary').textContent)
      && document.getElementById('library_items_grid').getAttribute('aria-busy') === 'false');
    assert.equal(await page.getByRole('button', {name: '刷新'}).isEnabled(), true);
    assert.equal(refreshCalls().length, 3);
    assert.equal(requests.filter(r => /sync/.test(r.path)).length, 0, 'refresh must not start a full sync');
    assert.equal(await page.locator('.lit-library-card').count(), 5, 'the same page stays on screen');

    // Status and type tabs apply immediately and carry the counters.
    assert.equal(await page.locator('[data-library-count="missing"]').innerText(), '3');
    // Sub-categories sit in their own row of compact chips, grouped by type under "全部类型".
    const chipRow = page.locator('#library_category_chips');
    // The grid stays dimmed with aria-busy until the list (and its pending reloads) settle.
    const settled = () => page.waitForFunction(() => {
      const grid = document.getElementById('library_items_grid');
      return !grid.classList.contains('is-loading') && grid.getAttribute('aria-busy') === 'false';
    });
    const batch = page.locator('#library_repair_page_btn');
    assert.equal(await batch.isVisible(), false);
    await page.locator('[data-library-subtitle="problem"]').click();
    await batch.waitFor();
    assert.match(await batch.innerText(), /修复本页.*4/);
    await page.locator('.lit-library-panel').screenshot({path: path.join(evidenceDir, 'batch-repair-desktop.png')});
    await batch.focus();
    await page.keyboard.press('Enter');
    const confirmation = page.locator('#system-confirm-modal');
    await confirmation.waitFor();
    assert.match(await confirmation.innerText(), /本页 4 部电影/);
    await confirmation.locator('[data-bs-dismiss="modal"]').first().click();
    await confirmation.waitFor({state: 'hidden'});
    const batchCalls = () => requests.filter(r => r.path === '/library/subtitle/repair/batch');
    assert.equal(batchCalls().length, 0);
    await page.setViewportSize({width: 390, height: 800});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    await batch.click();
    await page.locator('#system_confirm_btn').click();
    const batchStatus = page.locator('#library_repair_page_status');
    await page.waitForFunction(() => document.querySelector('#library_repair_page_status').textContent.includes('无法核验账号'));
    await batch.click();
    await page.locator('#system_confirm_btn').click();
    await page.waitForFunction(() => document.querySelector('#library_repair_page_status').textContent.includes('未能确认'));
    await batch.click();
    await page.locator('#system_confirm_btn').click();
    await page.waitForFunction(() => document.querySelector('#library_repair_page_status').textContent.includes('等待队列已满'));
    assert.equal(batchCalls().length, 3);
    assert.deepEqual(batchCalls()[0].body, batchCalls()[1].body);
    assert.deepEqual(batchCalls()[0].body, batchCalls()[2].body);
    assert.deepEqual(batchCalls()[0].body.items.map(i => i.server_item_id), ['m21', 'm22', 'm24', 'm25']);
    assert.equal(new Set(batchCalls()[0].body.items.map(i => i.request_id)).size, 4);
    await page.waitForFunction(() => document.querySelector('#library_repair_page_status').textContent.includes('结果已不可查询 1 部'));
    assert.match(await batchStatus.innerText(), /已受理 2 部（复用 1 个任务）.*待完成 1 部.*未受理 1 部：电影22.*可重试 1 部：电影25/);
    assert.equal(await batch.isDisabled(), true);
    problemItems.shift();
    repairStatus = 'partial';
    await page.waitForFunction(() => document.querySelector('#library_repair_page_status').textContent.includes('部分完成 1 部'));
    await settled();
    assert.match(await batchStatus.innerText(), /部分完成 1 部/);
    assert.equal(await batch.isEnabled(), true);
    await page.locator('.lit-library-panel').screenshot({path: path.join(evidenceDir, 'batch-repair-mobile.png')});
    // After a new list response, retry the previously unattempted movie with its original ID.
    problemItems.splice(0, problemItems.length - 1);
    await page.evaluate(() => load_library_items(library_page));
    await settled();
    await batch.click();
    await page.locator('#system_confirm_btn').click();
    await page.waitForFunction(() => document.querySelector('#library_repair_page_status').textContent.includes('已完成 1 部'));
    assert.deepEqual(batchCalls()[3].body.items, [batchCalls()[0].body.items[3]]);
    await page.setViewportSize({width: 1280, height: 900});
    await page.locator('[data-library-subtitle="all"]').click();
    await settled();
    assert.equal(await batch.isVisible(), false);
    await chipRow.getByRole('button', {name: '外语电影 3'}).waitFor();
    assert.deepEqual(await chipRow.locator('.lit-library-category-group').allInnerTexts(), ['电影', '电视剧']);
    assert.deepEqual(await measure('.lit-library-category-chip'), {height: 28, radius: 'pill', font: '12px'});
    assert.equal(await chipRow.getByRole('button', {name: '欧美剧 0'}).evaluate(el => el.classList.contains('is-empty')), true);
    await chipRow.getByRole('button', {name: /^动漫/}).click();
    await settled();
    await page.locator('#library_more_trigger').click();
    await page.getByRole('button', {name: '检测外挂字幕'}).click();
    const audit = page.locator('#index-library-subtitle-audit-modal');
    const auditType = audit.locator('#index_library_subtitle_audit_category');
    const startAudit = audit.getByRole('button', {name: '开始/重新检测'});
    assert.equal(await startAudit.isDisabled(), true);
    assert.deepEqual(await auditType.locator('option:enabled').evaluateAll(options => options.map(o => o.value)), ['tv', 'anime']);
    await page.screenshot({path: path.join(evidenceDir, 'shared-category-audit.png')});
    await auditType.selectOption('tv');
    assert.equal(await startAudit.isDisabled(), true, 'wait for the configured subcategories');
    releaseCategories();
    await page.waitForFunction(() => document.getElementById('index_library_subtitle_audit_subcategory').value === '动漫');
    await auditType.selectOption('anime');
    assert.equal(await audit.locator('#index_library_subtitle_audit_subcategory').inputValue(), '动漫');
    const submitted = page.waitForRequest(req => new URL(req.url()).pathname === '/library/subtitle/audit');
    await startAudit.click();
    const auditPayload = JSON.parse((await submitted).postData());
    assert.deepEqual([auditPayload.category, auditPayload.subcategory], ['anime', '动漫']);
    await audit.getByRole('button', {name: '关闭', exact:true}).last().click();
    await audit.waitFor({state:'hidden'});
    await page.getByRole('button', {name: '电影', exact: true}).click();
    await page.waitForFunction(() => document.getElementById('library_filter_type').value === 'movie');
    assert.equal(lastList().type, 'movie');
    assert.equal(await chipRow.locator('.lit-library-category-group').count(), 0, 'one type needs no group names');
    // One click filters; the chip keeps focus when the counters re-render the row.
    await chipRow.getByRole('button', {name: /^外语电影/}).click();
    await page.waitForFunction(() => document.getElementById('library_filter_category').value === '外语电影');
    await settled();
    assert.equal(lastList().category, '外语电影');
    assert.equal(await chipRow.getByRole('button', {name: /^外语电影/}).getAttribute('aria-pressed'), 'true');
    await page.waitForFunction(() => document.activeElement && document.activeElement.dataset.libraryCategory === '外语电影');
    assert.equal(requests.filter(r => r.path === '/library/summary').pop().body.subtitle, 'all');
    await page.locator('#library_filter_trigger').click();
    // Audio spans the menu; the two sort fields share the next row and its left edge.
    const labelBox = async id => page.locator(id).evaluate(el => {
      const r = el.closest('[class*="col-"]').querySelector('label').getBoundingClientRect();
      return {x: Math.round(r.left), y: Math.round(r.top)};
    });
    const [audio, sortBy, sortOrder] = [await labelBox('#library_filter_audio'),
      await labelBox('#library_sort_by'), await labelBox('#library_sort_order')];
    assert.ok(audio.y < sortBy.y, 'audio language comes first');
    assert.equal(sortBy.y, sortOrder.y, 'sort labels share a top edge');
    assert.equal(audio.x, sortBy.x, 'left column is aligned');
    await page.locator('#library_filter_audio').selectOption('eng');
    await page.locator('#library_filter_btn').click();
    await page.getByRole('button', {name: /^缺字幕/}).click();
    await page.waitForFunction(() => document.getElementById('library_filter_subtitle').value === 'missing');
    await settled();
    assert.deepEqual([lastList().type, lastList().category, lastList().subtitle, lastList().audio_language],
      ['movie', '外语电影', 'missing', 'eng']);
    assert.equal(await page.getByRole('button', {name: /^缺字幕/}).getAttribute('aria-pressed'), 'true');
    // The menu's audio filter is otherwise hidden, so it shows as a removable tag.
    await page.locator('#library_active_filters').getByRole('button', {name: '移除音轨：英语'}).waitFor();
    // A fresh visit reopens the same filters (not the search word) and says so.
    await page.reload();
    await page.locator('.lit-library-card').first().waitFor();
    await page.waitForFunction(() => /已恢复上次的筛选/.test(document.getElementById('library_items_summary').textContent));
    await settled();
    assert.deepEqual([lastList().type, lastList().category, lastList().subtitle, lastList().audio_language, lastList().page],
      ['movie', '外语电影', 'missing', 'eng', 1]);
    assert.equal(await page.getByRole('button', {name: '电影', exact: true}).getAttribute('aria-pressed'), 'true');
    await page.locator('#library_active_filters').getByRole('button', {name: '移除音轨：英语'}).click();
    await page.waitForFunction(() => document.getElementById('library_filter_audio').value === 'all');
    await settled();
    assert.equal(lastList().audio_language, 'all');
    await page.getByRole('button', {name: '电视剧', exact: true}).click();
    await page.waitForFunction(() => document.getElementById('library_filter_audio').disabled);
    assert.equal(await page.locator('#library_filter_audio').inputValue(), 'all');
    await settled();
    assert.equal(lastList().category, '', 'a movie sub-category is dropped for TV');
    // TV has no fill queue; the button explains instead of queueing every movie.
    await page.locator('#library_fill_btn').click();
    await page.waitForFunction(() => /连续补齐目前只支持电影/.test(document.getElementById('feedback').textContent));
    await page.locator('#library_active_filters').getByRole('button', {name: '清除全部筛选'}).click();
    await page.waitForFunction(() => document.getElementById('library_filter_type').value === 'all' &&
      document.getElementById('library_filter_subtitle').value === 'all');
    await settled();
    await page.locator('.lit-library-card').first().waitFor();
    assert.equal(await page.locator('#library_active_filters').innerHTML(), '');

    // The card menu is not clipped by the card and stays above neighbouring cards.
    const firstMenu = page.locator('.lit-library-card-menu [data-bs-toggle="dropdown"]').first();
    await firstMenu.click();
    const menu = page.locator('.lit-library-card-menu .dropdown-menu.show');
    await menu.waitFor();
    const box = await menu.boundingBox();
    assert.ok(box.width > 100 && box.height > 40);
    // A clipping ancestor would hide part of the menu; every corner must still hit the menu itself.
    const corners = await page.evaluate(({x, y, width, height}) => [[3, 3], [width - 3, 3], [3, height - 3], [width - 3, height - 3]]
      .map(([dx, dy]) => { const hit = document.elementFromPoint(x + dx, y + dy); return !!hit && !!hit.closest('.dropdown-menu.show'); }), box);
    assert.deepEqual(corners, [true, true, true, true]);
    await page.keyboard.press('Escape');

    // The poster is a keyboard-operable button that opens the drawer; drops need a confirmation click.
    const poster = page.getByRole('button', {name: '查看 电影1 的字幕详情'});
    await poster.focus();
    await page.keyboard.press('Enter');
    const drawer = page.locator('#library-drawer');
    await drawer.waitFor({state: 'visible'});
    assert.equal(await page.locator('#library_drawer_title').innerText(), '电影1');
    const zone = page.locator('#library_upload_dropzone');
    const drop = (target, names, type = 'drop') => page.evaluate(({target, names, type}) => {
      const transfer = new DataTransfer();
      names.forEach(name => transfer.items.add(new File(['x'], name)));
      const event = new DragEvent(type, {dataTransfer: transfer, bubbles: true, cancelable: true});
      (target === 'zone' ? document.getElementById('library_upload_dropzone') : document.body).dispatchEvent(event);
      return event.defaultPrevented;
    }, {target, names, type});
    assert.equal(await drop('zone', ['A.chs.srt', 'B.eng.srt']), true);
    await page.locator('#library_upload_pending li').nth(1).waitFor();
    assert.equal(requests.filter(r => r.path === '/subtitle/upload').length, 0, 'a drop must not upload by itself');
    await page.locator('#library_upload_preview_note').waitFor({state: 'visible'});
    assert.match(await page.locator('#library_upload_pending').innerText(), /预计 简体中文 → Movie 1\.zh-CN\.srt/);
    const preview = requests.filter(r => r.path === '/library/subtitle/preview').pop().body;
    assert.deepEqual(preview.filenames, ['A.chs.srt', 'B.eng.srt']);
    assert.equal(Object.keys(preview).includes('file'), false, 'preview sends names only');
    await page.getByRole('button', {name: '移除 A.chs.srt'}).click();
    assert.equal(await page.locator('#library_upload_pending li').count(), 1);
    assert.equal(await drop('body', ['stray.srt']), true, 'drops outside the upload area never navigate away');
    assert.equal(await drop('body', ['stray.srt'], 'dragover'), true);
    await drawer.getByRole('button', {name: '关闭', exact: false}).first().click();
    await drawer.waitFor({state: 'hidden'});

    // A movie without a transfer record explains why it cannot take uploads and ignores drops.
    await page.getByRole('button', {name: '查看 电影4 的字幕详情'}).click();
    await drawer.waitFor({state: 'visible'});
    assert.match(await page.locator('#library_upload_blocked').innerText(), /没有整理记录/);
    assert.equal(await page.locator('#index_upload_subtitle_btn').isDisabled(), true);
    await drop('zone', ['late.srt']);
    assert.equal(await page.locator('#library_upload_pending li').count(), 0);
    await page.locator('#library-drawer .btn-close').click();
    await drawer.waitFor({state: 'hidden'});

    // Both API failures and network failures retry when the same series reopens.
    let episodeCalls = 0;
    await page.route('**/library/episodes?*', route => {
      episodeCalls += 1;
      if (episodeCalls === 1) return route.fulfill({json: {code: -1, msg: '剧集服务暂不可用'}});
      if (episodeCalls === 2) return route.abort('failed');
      return route.fulfill({json: {code: 0, items: episodes}});
    });
    for (const message of ['剧集服务暂不可用', '剧集加载失败，请关闭并重新打开重试']) {
      await page.getByRole('button', {name: '查看 剧集 的字幕详情'}).click();
      await page.waitForFunction(message => document.getElementById('index_library_episodes_body').textContent.includes(message), message);
      await page.locator('#library-drawer .btn-close').click();
      await drawer.waitFor({state: 'hidden'});
    }

    // Series: successful retry restores the season and episode upload controls.
    await page.getByRole('button', {name: '查看 剧集 的字幕详情'}).click();
    await page.locator('#index_library_episodes_body [data-episode-index]').first().waitFor();
    assert.equal(episodeCalls, 3);
    assert.equal(await page.locator('#library_episode_season').inputValue(), '1');
    assert.equal(await page.locator('#index_upload_subtitle_btn').isDisabled(), true);
    await page.locator('#index_library_episodes_body').getByRole('button', {name: '上传字幕'}).first().click();
    assert.equal(await page.locator('#index_upload_subtitle_target_path').inputValue(), '/media/s/S01E01.mkv');
    assert.equal(await page.locator('#index_upload_subtitle_btn').isDisabled(), false);
    await page.locator('#library-drawer .btn-close').click();
    await drawer.waitFor({state: 'hidden'});
    await page.getByRole('button', {name: '查看 剧集 的字幕详情'}).click();
    await drawer.waitFor({state: 'visible'});
    assert.equal(await page.locator('#index_upload_subtitle_target_path').inputValue(), '/media/s/S01E01.mkv');
    assert.equal(episodeCalls, 3, 'a successful load and its selected upload target remain cached');
    await page.screenshot({path: path.join(evidenceDir, 'series-retry-recovered.png'), fullPage: true});
    await page.locator('#library-drawer .btn-close').click();
    await drawer.waitFor({state: 'hidden'});

    // Continuous fill: confirm once, walk the queue, then leave without downloading anything.
    await page.getByRole('button', {name: '开始补齐'}).click();
    await page.locator('#system-confirm-modal').waitFor({state: 'visible'});
    assert.match(await page.locator('#system_confirm_message').innerText(), /共 3 部/);
    await page.locator('#system_confirm_btn').click();
    const footer = page.locator('#library_fill_footer');
    await footer.waitFor({state: 'visible'});
    assert.match(await page.locator('#library_fill_progress').innerText(), /^第 1 \/ 3 部/);
    assert.equal(await page.locator('#library_fill_next').isDisabled(), true);
    await page.locator('#library_fill_skip').click();
    assert.match(await page.locator('#library_fill_progress').innerText(), /^第 2 \/ 3 部/);
    await page.locator('#library_fill_exit').click();
    await page.locator('#library-online-subtitle-modal').waitFor({state: 'hidden'});
    assert.match(await page.locator('#library_items_summary').innerText(), /补齐已结束：保存 0 部，跳过 1 部，未处理 2 部/);

    // Re-search while a save is queued must not strand the continuous-fill footer.
    await page.route('**/library/subtitle/search', route => route.fulfill({json: {
      code: 0, items: [{name: 'Movie subtitle', provider: 'thunder', format: 'srt', ticket: 'fixture-ticket'}]
    }}));
    await page.route('**/library/subtitle/download', route => route.fulfill({json: {code: 0, task_id: 'fixture-task'}}));
    await page.evaluate(() => {
      SubtitleTasks.watch = (_id, callbacks) => { window.fillTaskDone = callbacks.onDone; };
    });
    await page.getByRole('button', {name: '开始补齐', exact: true}).click();
    await page.locator('#system_confirm_btn').click();
    await page.locator('.library-online-download').click();
    await page.waitForFunction(() => typeof window.fillTaskDone === 'function');
    await page.locator('#library_online_search').click();
    await page.locator('.library-online-download').waitFor();
    await page.evaluate(() => window.fillTaskDone({status: 'succeeded'}));
    assert.equal(await page.locator('#library_fill_next').isEnabled(), true);
    assert.match(await page.locator('#library_fill_progress').innerText(), /已保存 1 部/);
    await page.screenshot({path: path.join(evidenceDir, 'fill-after-research-saved.png'), fullPage: true});
    await page.locator('#library_fill_next').click();
    assert.match(await page.locator('#library_fill_progress').innerText(), /第 2 \/ 3 部/);
    await page.locator('#library_fill_exit').click();
    await page.locator('#library-online-subtitle-modal').waitFor({state: 'hidden'});

    // Phone width keeps the sixteen-pixel gutter without sideways scrolling.
    await page.setViewportSize({width: 390, height: 800});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    await page.getByRole('button', {name: '查看 电影1 的字幕详情'}).click();
    await drawer.waitFor({state: 'visible'});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
    await page.screenshot({path: path.join(evidenceDir, 'mobile-drawer.png'), fullPage: true});

    // List reloads after a subtitle save must update the already-open drawer too.
    items[0].subtitle_status = 'has_chinese_external';
    items[0].subtitle_label = '已有外挂中文字幕';
    items[0].subtitle_badge = 'bg-green';
    await page.evaluate(() => load_library_items(library_page));
    await page.waitForFunction(() => document.querySelector('.lit-library-card').textContent.includes('已有外挂中文字幕'));
    assert.match(await page.locator('#library_drawer_overview').innerText(), /已有外挂中文字幕/);
    await page.screenshot({path: path.join(evidenceDir, 'updated-drawer.png'), fullPage: true});

    // Emulate SPA script/DOM replacement without aborting the old document's AJAX.
    await page.goto('http://library.test/');
    await page.locator('.lit-library-card').first().waitFor();
    let previewArrived;
    await page.route('**/library/subtitle/preview', route => previewArrived(route));
    const nextPreview = () => new Promise(resolve => { previewArrived = resolve; });
    await page.getByRole('button', {name: '查看 电影1 的字幕详情'}).click();
    const [oldPreview] = await Promise.all([nextPreview(), drop('zone', ['old.chs.srt'])]);
    const oldGeneration = await page.evaluate(() => library_upload_preview_generation);
    await page.locator('#library-drawer .btn-close').click();
    await drawer.waitFor({state: 'hidden'});
    await page.evaluate(body => { document.querySelector('.page-wrapper').innerHTML = body; }, fixtures.body);
    await page.addScriptTag({path: path.join(root, 'web/static/js/media-library.js')});
    await page.evaluate(() => {
      init_media_library_page({page_size: 18, default_server: 'emby'});
      window.previewCompletions = 0;
      $(document).on('ajaxComplete.reviewPreview', function (_event, _xhr, options) {
        if (options.url === '/library/subtitle/preview') window.previewCompletions += 1;
      });
    });
    await page.locator('.lit-library-card').first().waitFor();
    await page.getByRole('button', {name: '查看 电影2 的字幕详情'}).click();
    const [newPreview] = await Promise.all([nextPreview(), drop('zone', ['new.eng.srt'])]);
    assert.equal(await page.evaluate(() => library_upload_preview_generation), oldGeneration);
    await newPreview.fulfill({json: {code: 0, items: [{language: 'eng', target: 'Movie2.eng.srt'}]}});
    await page.waitForFunction(() => window.previewCompletions === 1);
    await oldPreview.fulfill({json: {code: 0, items: [{language: 'zh-CN', target: 'Movie1.zh-CN.srt'}]}});
    await page.waitForFunction(() => window.previewCompletions === 2);
    assert.match(await page.locator('#library_upload_pending').innerText(), /Movie2.eng.srt/);
    assert.doesNotMatch(await page.locator('#library_upload_pending').innerText(), /Movie1.zh-CN.srt/);
    await page.screenshot({path: path.join(evidenceDir, 'preview-after-navigation.png'), fullPage: true});

    // A late fill response must not open a shared confirmation on another page.
    await page.goto('http://library.test/');
    await page.locator('.lit-library-card').first().waitFor();
    let fillArrived;
    const pendingFill = new Promise(resolve => { fillArrived = resolve; });
    await page.route('**/library/items?*', route => {
      if (route.request().postDataJSON().page_size === 100) { fillArrived(route); return; }
      return route.fallback();
    });
    await page.getByRole('button', {name: '开始补齐', exact: true}).click();
    const oldFill = await pendingFill;
    await page.evaluate(() => {
      document.querySelector('.page-wrapper').innerHTML = '<p>另一个页面</p>';
      window.fillCompleted = false;
      $(document).on('ajaxComplete.reviewFill', function (_event, _xhr, options) {
        if (options.url.startsWith('/library/items?') && JSON.parse(options.data).page_size === 100) {
          window.fillCompleted = true;
        }
      });
    });
    await oldFill.fulfill({json: {code: 0, total: 1, items: [movie(1)]}});
    await page.waitForFunction(() => window.fillCompleted);
    assert.equal(await page.locator('#system-confirm-modal').isVisible(), false);
    assert.equal(await page.evaluate(() => library_fill), null);
    await page.screenshot({path: path.join(evidenceDir, 'fill-after-navigation.png'), fullPage: true});
    assert.equal(gonePolls, 2, 'confirmed 404s must not schedule further detail reads');
    assert.deepEqual(errors, []);
    console.log('Media library UI checks passed: batch repair, receipt retry, partial admission, refresh, tabs, menu, drawer, drops, series retries, fill after re-search, 390px layout, drawer reload and stale preview/fill responses.');
  } catch (error) {
    if (page) await page.screenshot({path: path.join(evidenceDir, 'failure.png'), fullPage: true});
    throw error;
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
