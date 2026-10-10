/* Render the real service/shell templates and verify the shared task drawer. */
const {chromium} = require('playwright');
const {spawnSync} = require('child_process');
const fs = require('fs'), path = require('path'), assert = require('assert');
const root = path.resolve(__dirname, '..');
// Jinja renders disposable values without importing the app or opening a database.
const rendered = spawnSync('python3', ['-c', `
import json
from jinja2 import Environment, FileSystemLoader, ChainableUndefined
env = Environment(loader=FileSystemLoader('web/templates'), undefined=ChainableUndefined, autoescape=True)
env.filters['hash'] = str
context = dict(SiteFavicons={}, UserPris=['服务', '媒体整理'], GoPage='service', AppVersion='v2.8.3',
    UserName='test', SystemFlag='Docker', TMDBFlag=True, CustomScriptCfg={}, SyncMod='copy',
    Config={'app':{}, 'media':{}, 'pt':{}, 'llm':{}}, Count=1,
    NetTestTargets=['www.themoviedb.org', 'api.themoviedb.org', 'api.tmdb.org', 'image.tmdb.org',
                    'webservice.fanart.tv', 'api.telegram.org', 'qyapi.weixin.qq.com', 'api.opensubtitles.com'],
    NetTestAnimeTargets=['bgm.tv', 'api.bgm.tv', 'www.comicat.org', 'mikanani.me'],
    SchedulerTasks=[dict(id='nametest', name='名称识别测试', color='blue', svg='', time='手动执行', state='OFF')])
pages = {name: env.get_template(template).render(**context) for name, template in
    [('shell', 'navigation.html'), ('service', 'service.html'), ('basic', 'setting/basic.html')]}
context.update(Count=0, SchedulerTasks=[])
pages['emptyService'] = env.get_template('service.html').render(**context)
print(json.dumps(pages))
`], {cwd: root, encoding: 'utf8', maxBuffer: 1024 * 1024});
assert.equal(rendered.status, 0, rendered.stderr);
const pages = JSON.parse(rendered.stdout);

(async () => {
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 1512, height: 870}, reducedMotion: 'reduce'});
    const errors = [], tasks = new Map();
    let emptyService = false, readFailure = false, slowRead = false, nextSubmitted = 0;
    page.on('pageerror', error => errors.push(error.message));
    await page.route('**/*', async route => {
      const url = new URL(route.request().url());
      if (url.hostname !== 'service.test') return route.abort();
      if (url.pathname === '/') return route.fulfill({contentType: 'text/html; charset=utf-8', body: pages.shell});
      if (url.pathname === '/service') return route.fulfill({contentType: 'text/html; charset=utf-8', body: pages[emptyService ? 'emptyService' : 'service']});
      if (url.pathname === '/basic') return route.fulfill({contentType: 'text/html; charset=utf-8', body: pages.basic});
      if (url.pathname === '/do') {
        const form = new URLSearchParams(route.request().postData()), command = form.get('cmd');
        const data = JSON.parse(form.get('data') || '{}');
        if (command === 'run_directory_sync' || command === 'sch') {
          const task = {task_id: 'submitted-' + (++nextSubmitted), command, title: '目录同步', status: 'queued',
            created_at: Date.now() / 1000, message: '已排队，等待执行', result: null};
          tasks.set(task.task_id, task);
          return route.fulfill({status: 202, json: {code: 0, retcode: 0, async: true,
            operation_type: 'background_action', task_id: task.task_id, task}});
        }
        if (command === 'get_action_tasks') {
          if (slowRead) await new Promise(resolve => setTimeout(resolve, 400));
          return route.fulfill({json: readFailure ? {code: -1, msg: '任务状态读取失败，请重试'} : {code: 0, tasks: Array.from(tasks.values())}});
        }
        if (command === 'get_action_task') return route.fulfill({json: {code: 0, task: tasks.get(data.task_id)}});
        if (command === 'cancel_action_task') {
          const task = tasks.get(data.task_id);
          task.status = 'canceled'; task.message = '已取消'; task.result = {code: -1, msg: '已取消'};
          return route.fulfill({json: {code: 0, task}});
        }
        // 普通和动漫弹窗都从服务器动作读取出口，不请求浏览器所在设备的公网地址。
        if (command === 'net_test') return route.fulfill({json: {res: true, time: '80 毫秒'}});
        if (command === 'egress_ip_test') return route.fulfill({json: {res: true,
          ip: data.mode === 'direct' ? '2001:4860:4860:1234:5678:1234:5678:8888' : '1.1.1.1'}});
        return route.fulfill({json: {code: -1, msg: '布局测试未配置此接口'}});
      }
      const file = path.join(root, 'web', url.pathname);
      if (file.startsWith(path.join(root, 'web/static') + path.sep) && fs.existsSync(file) && fs.statSync(file).isFile()) {
        const types = {'.js': 'application/javascript', '.css': 'text/css', '.svg': 'image/svg+xml', '.png': 'image/png', '.woff2': 'font/woff2'};
        return route.fulfill({path: file, contentType: types[path.extname(file)] || 'application/octet-stream'});
      }
      return route.fulfill({status: 404, body: ''});
    });
    await page.goto('http://service.test/');
    const card = page.locator('#service-action-tasks [data-action-task-open]');
    const shortcut = page.locator('layout-searchbar [data-action-task-open]');
    const panel = page.locator('#action-task-panel');
    await card.waitFor();
    await page.waitForFunction(() => document.querySelector('#service-action-tasks [data-action-task-summary]').textContent.includes('执行中 0 项'));
    assert.equal(await panel.isVisible(), false);
    assert.equal(await page.locator('.page-wrapper #action-task-panel').count(), 0, 'No task list at the page footer');
    assert.equal(await page.locator('[data-action-task-count]').isVisible(), false, 'Zero count must not take space');
    await card.focus();
    await page.keyboard.press('Enter');
    await page.locator('#action-task-panel.show').waitFor();
    await panel.getByText('暂无后台操作', {exact: true}).waitFor();
    await page.waitForFunction(() => document.getElementById('action-task-panel').contains(document.activeElement));
    // 提供抽屉后的可聚焦背景目标，检验应用内焦点隔离而非浏览器工具栏的 Tab 顺序。
    await page.evaluate(() => {
      const target = document.createElement('button');
      target.id = 'task-focus-outside'; target.textContent = '背景操作'; document.body.appendChild(target);
    });
    for (let i = 0; i < 6; i++) {
      await page.keyboard.press('Tab');
      const focusState = await page.evaluate(() => ({
        inside: document.getElementById('action-task-panel').contains(document.activeElement),
        active: document.activeElement.outerHTML.slice(0, 450),
        panel: document.getElementById('action-task-panel').className,
        trap: window.bootstrap?.Offcanvas?.getInstance(document.getElementById('action-task-panel'))?._focustrap?._isActive
      }));
      assert(focusState.inside, 'Drawer must trap keyboard focus: ' + JSON.stringify({tab: i, ...focusState}));
    }
    await page.evaluate(() => document.getElementById('task-focus-outside').remove());
    await page.keyboard.press('Escape');
    await panel.waitFor({state: 'hidden'});
    await page.waitForFunction(() => document.activeElement.matches('#service-action-tasks [data-action-task-open]'));

    emptyService = true;
    await page.evaluate(() => navmenu('service?empty=1'));
    await page.getByText('没有启用其他服务', {exact: true}).waitFor();
    assert.equal(await card.count(), 1, 'Task entry must survive an empty service list');
    const created = Date.now() / 1000;
    for (const [task_id, status, title, offset] of [['run', 'running', '目录同步', 0], ['queue', 'queued', '媒体库同步', 1], ['fail', 'failed', '下载字幕', 2]]) {
      tasks.set(task_id, {task_id, status, title, command: 'run_directory_sync', created_at: created + offset,
        message: status === 'failed' ? 'A'.repeat(800) + '.mkv 无法查询媒体信息' : title,
        result: status === 'failed' ? {code: -1} : null});
    }
    await page.evaluate(() => ActionTaskClient.refresh());
    await page.waitForFunction(() => document.querySelector('[data-action-task-count]').textContent === '2');
    const badge = await page.locator('[data-action-task-count]').boundingBox();
    assert(badge.y >= 0 && badge.x + badge.width <= 1512, 'Task count must stay within the header viewport');
    assert((await page.locator('#service-action-tasks [data-action-task-summary]').textContent()).includes('排队 1 项'));
    // The shortcut remains available without either admin or service menu permission.
    await page.evaluate(() => { document.querySelector('layout-searchbar').layout_userpris = ['媒体整理']; });
    await shortcut.click();
    await page.locator('#action-task-panel.show').waitFor();
    const desktop = await panel.boundingBox();
    assert.equal(Math.round(desktop.width), 480);
    assert.equal(Math.round(desktop.x + desktop.width), 1512);
    assert.equal(await page.locator('#action-task-list [role="listitem"]').count(), 3);
    const result = panel.locator('[data-action-task-id="fail"][data-action-task-kind="result"]');
    await page.emulateMedia({reducedMotion: 'no-preference'});
    await result.click();
    await page.locator('#action-task-result.show').waitFor();
    assert.equal(await panel.isVisible(), false, 'Only one modal focus trap may be open');
    await page.waitForFunction(() => document.getElementById('action-task-result').contains(document.activeElement));
    await page.keyboard.press('Escape');
    await page.locator('#action-task-panel.show').waitFor();
    await page.waitForFunction(() => document.activeElement.dataset.actionTaskId === 'fail');
    await panel.getByRole('button', {name: '取消排队', exact: true}).click();
    await page.waitForFunction(() => document.querySelector('[data-action-task-count]').textContent === '1');
    await page.screenshot({path: '/tmp/nas-service-tasks-desktop.png'});
    await panel.getByRole('button', {name: '关闭后台操作', exact: true}).click();
    await panel.waitFor({state: 'hidden'});

    // SPA navigation keeps one drawer and its records while long forms scroll naturally.
    await page.evaluate(() => { window.taskPanelReference = document.getElementById('action-task-panel'); navmenu('basic'); });
    await page.getByRole('heading', {name: 'LLM识别', exact: true}).waitFor();
    await page.waitForFunction(() => getComputedStyle(document.getElementById('page_content')).opacity === '1');
    await page.evaluate(() => window.scrollTo(0, document.documentElement.scrollHeight));
    assert(await page.evaluate(() => taskPanelReference === document.getElementById('action-task-panel')));
    assert.equal(await page.locator('#page_content #action-task-panel').count(), 0);
    await shortcut.click();
    await page.locator('#action-task-panel.show').waitFor();
    readFailure = true;
    await panel.getByRole('button', {name: '刷新状态', exact: true}).click();
    await panel.getByText('任务状态读取失败，请重试', {exact: true}).waitFor();
    assert.equal(await page.locator('#action-task-list [role="listitem"]').count(), 3, 'Read failures must preserve prior results');
    readFailure = false; slowRead = true;
    const refresh = panel.getByRole('button', {name: '刷新状态', exact: true});
    await refresh.click();
    await page.waitForFunction(() => document.getElementById('action-task-refresh').disabled);
    assert(await page.evaluate(() => document.getElementById('action-task-panel').contains(document.activeElement)), 'Busy refresh must keep keyboard focus in the drawer');
    await page.waitForFunction(() => !document.getElementById('action-task-refresh').disabled);
    await page.waitForFunction(() => document.activeElement.id === 'action-task-refresh');
    assert.equal(await page.locator('#action-task-notice').textContent(), '');
    await page.setViewportSize({width: 390, height: 720});
    const mobile = await panel.boundingBox();
    assert.equal(Math.round(mobile.x), 0);
    assert.equal(Math.round(mobile.width), 390);
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({path: '/tmp/nas-service-tasks-mobile.png'});
    await page.evaluate(() => { document.body.classList.remove('theme-light'); document.body.classList.add('theme-dark'); });
    await page.evaluate(async () => {
      const panel = document.getElementById('action-task-panel');
      await Promise.all(document.getAnimations().filter(animation => panel.contains(animation.effect?.target))
        .map(animation => animation.finished.catch(() => {})));
    });
    // 检查真实主题切换后的最终颜色，避免把仍在过渡中的截图当作暗色状态。
    const buttonContrast = await page.evaluate(() => {
      const rgb = color => color.match(/[\d.]+/g).map(Number);
      const blend = (color, background) => color.slice(0, 3).map((value, i) => value * (color[3] ?? 1) + background[i] * (1 - (color[3] ?? 1)));
      const luminance = color => color.map(value => {
        value /= 255; return value <= 0.04045 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4);
      }).reduce((sum, value, i) => sum + value * [0.2126, 0.7152, 0.0722][i], 0);
      const button = getComputedStyle(document.getElementById('action-task-refresh'));
      const background = blend(rgb(button.backgroundColor), rgb(getComputedStyle(document.getElementById('action-task-panel')).backgroundColor));
      const foreground = blend(rgb(button.color), background);
      const light = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
      return {ratio: (light[0] + 0.05) / (light[1] + 0.05), foreground: button.color, background: button.backgroundColor};
    });
    assert(buttonContrast.ratio >= 4.5, 'Focused dark-theme refresh must remain readable: ' + JSON.stringify(buttonContrast));
    await page.screenshot({path: '/tmp/nas-service-tasks-dark.png'});
    await page.keyboard.press('Escape');
    await panel.waitFor({state: 'hidden'});
    await page.waitForFunction(() => document.activeElement.matches('layout-searchbar [data-action-task-open]'));
    // Capture the fixed service entry independently of the drawer backdrop.
    emptyService = false;
    await page.evaluate(() => {
      document.body.classList.remove('theme-dark'); document.body.classList.add('theme-light');
      navmenu('service');
    });
    await page.setViewportSize({width: 1512, height: 870});
    await card.waitFor();
    await page.waitForFunction(() => getComputedStyle(document.getElementById('page_content')).opacity === '1');
    await page.locator('#nprogress').waitFor({state: 'detached'});
    await page.screenshot({path: '/tmp/nas-service-task-entry.png'});

    // 相同文案在短时间内合并，超过窗口或提示隐藏后必须再次显示。
    await page.evaluate(() => $('#action-task-toast').toast('hide'));
    await page.locator('#action-task-toast').waitFor({state: 'hidden'});
    await page.evaluate(() => {
      window.toastShows = 0;
      $('#action-task-toast').on('show.bs.toast', () => toastShows++);
    });
    async function submitQueued(sid) {
      const id = 'submitted-' + (nextSubmitted + 1);
      await page.evaluate(sid => { ActionTaskClient.request('run_directory_sync', {sid}, () => {}); }, sid);
      await page.locator(`[data-action-task-id="${id}"]`).first().waitFor({state: 'attached'});
    }
    await submitQueued('toast-one');
    await page.waitForFunction(() => toastShows === 1);
    await submitQueued('toast-two');
    assert.equal(await page.evaluate(() => toastShows), 1, 'Immediate same-text feedback is coalesced');
    await page.waitForTimeout(2100);
    await submitQueued('toast-three');
    await page.waitForFunction(() => toastShows === 2);
    await page.locator('#action-task-toast').waitFor({state: 'hidden'});
    await submitQueued('toast-four');
    await page.waitForFunction(() => toastShows === 3);
    await page.getByRole('button', {name: '关闭任务提示', exact: true}).click();
    await page.locator('#action-task-toast').waitFor({state: 'hidden'});

    async function startService(item, name) {
      const id = 'submitted-' + (nextSubmitted + 1);
      await page.evaluate(({item, name}) => { run_scheduler(item, name); }, {item, name});
      await page.locator(`[data-action-task-id="${id}"]`).first().waitFor({state: 'attached'});
      return id;
    }
    function completeService(id, status) {
      const task = tasks.get(id);
      task.status = status; task.message = status === 'succeeded' ? '服务执行完成' : '服务执行失败';
      task.result = {code: status === 'succeeded' ? 0 : -1, msg: task.message};
    }
    async function modalFocus(id) {
      await page.locator('#' + id + '.show').waitFor();
      await panel.waitFor({state: 'hidden'});
      await page.waitForFunction(id => document.getElementById(id).contains(document.activeElement), id);
      assert.equal(await page.locator('.modal.show').count(), 1, 'Only one modal may own focus');
    }
    async function restoredTask(id) {
      await page.locator('#action-task-panel.show').waitFor();
      await page.waitForFunction(id => document.activeElement.dataset.actionTaskId === id, id);
      await card.evaluate(element => element.focus());
      assert(await page.evaluate(() => document.getElementById('action-task-panel').contains(document.activeElement)), 'Restored drawer must isolate background focus');
      await page.keyboard.press('Escape');
      await panel.waitFor({state: 'hidden'});
      await page.waitForFunction(() => !document.body.style.overflow && !document.querySelector('.modal-backdrop, .offcanvas-backdrop'));
    }
    // 保留真实的成功/失败函数，覆盖终态回调而不是用数组记录替代弹窗。
    for (const status of ['succeeded', 'failed']) {
      const id = await startService('regression-' + status, '回归服务');
      await card.click();
      await page.locator('#action-task-panel.show').waitFor();
      await page.waitForFunction(() => document.getElementById('action-task-panel').contains(document.activeElement));
      await panel.locator(`[data-action-task-id="${id}"][data-action-task-kind="result"]`).focus();
      completeService(id, status);
      await modalFocus(status === 'succeeded' ? 'system-success-modal' : 'system-fail-modal');
      await page.keyboard.press('Escape');
      await restoredTask(id);
    }

    // 查看结果期间到达的完成提示排队，不覆盖当前模态框或提前恢复抽屉。
    const queuedId = await startService('regression-queued', '排队回归服务');
    await card.click();
    await page.locator('#action-task-panel.show').waitFor();
    await panel.locator(`[data-action-task-id="${queuedId}"][data-action-task-kind="result"]`).click();
    await modalFocus('action-task-result');
    completeService(queuedId, 'succeeded');
    await page.waitForFunction(() => document.getElementById('system_success_message').textContent === '排队回归服务 服务执行完成');
    assert.equal(await page.locator('#system-success-modal').isVisible(), false);
    assert.equal(await page.locator('#action-task-result').isVisible(), true);
    await page.keyboard.press('Escape');
    await modalFocus('system-success-modal');
    await page.keyboard.press('Escape');
    await restoredTask(queuedId);

    // 原反馈按钮继续打开业务窗口时，抽屉必须等业务窗口关闭再恢复。
    await card.click();
    await page.waitForFunction(() => document.getElementById('action-task-panel').contains(document.activeElement));
    await page.evaluate(() => show_fail_modal('继续处理', () => $('#modal-backup').modal('show')));
    await modalFocus('system-fail-modal');
    await page.locator('#system_fail_modal_btn').click();
    await page.locator('#system-fail-modal').waitFor({state: 'hidden'});
    await page.locator('#modal-backup.show').waitFor();
    await page.waitForFunction(() => !bootstrap.Modal.getInstance(document.getElementById('modal-backup'))._isTransitioning);
    assert.equal(await panel.isVisible(), false);
    await page.evaluate(() => $('#modal-backup').modal('hide'));
    await page.waitForFunction(() => document.getElementById('action-task-panel').contains(document.activeElement));
    await page.keyboard.press('Escape');
    await panel.waitFor({state: 'hidden'});

    // 用户关闭抽屉的动画期间收到提示，结束后保持关闭，不强制重新打开。
    await card.click();
    await page.waitForFunction(() => document.getElementById('action-task-panel').contains(document.activeElement));
    await page.evaluate(() => {
      $('#action-task-panel').offcanvas('hide');
      show_success_modal('关闭期间完成');
    });
    await modalFocus('system-success-modal');
    await page.keyboard.press('Escape');
    await page.locator('#system-success-modal').waitFor({state: 'hidden'});
    await page.waitForFunction(() => document.activeElement.matches('#service-action-tasks [data-action-task-open]'));
    assert.equal(await panel.isVisible(), false);
    // 检查真实 Jinja 宏、两个测试弹窗及手机上的 IPv6 换行，沿用 Bootstrap 焦点行为。
    for (const [id, prefix] of [['nettest', 'nettest_item'], ['nettest_anime', 'nettest_anime_item']]) {
      for (const narrow of [false, true]) {
        await page.setViewportSize(narrow ? {width: 390, height: 720} : {width: 1100, height: 900});
        await page.evaluate(dark => {
          document.body.classList.toggle('theme-dark', dark);
          document.body.classList.toggle('theme-light', !dark);
        }, narrow);
        await page.evaluate(id => show_service_modal(id), id);
        const modalId = id === 'nettest' ? 'modal-nettest' : 'modal-nettest-anime';
        const modal = page.locator('#' + modalId);
        await modal.waitFor({state: 'visible'});
        await page.waitForFunction(id => !bootstrap.Modal.getInstance(document.getElementById(id))._isTransitioning, modalId);
        assert.equal(await page.locator(`#${prefix}_egress_direct`).textContent(), '尚未测试');
        const button = page.locator(`#${id}_btn`);
        await button.focus();
        await page.keyboard.press('Enter');
        await page.waitForFunction(id => !document.getElementById(id).disabled, `${id}_btn`);
        assert.equal(await page.locator(`#${prefix}_egress_direct`).textContent(), '2001:4860:4860:1234:5678:1234:5678:8888');
        assert.equal(await page.locator(`#${prefix}_egress_proxy`).textContent(), '1.1.1.1');
        assert.equal(await page.locator(`#${prefix}_res_0`).textContent(), '是');
        assert(await modal.evaluate(element => element.scrollWidth <= element.clientWidth));
        const captionContrast = await modal.evaluate(element => {
          const rgb = color => color.match(/[\d.]+/g).slice(0, 3).map(Number);
          const luminance = color => color.map(value => {
            value /= 255; return value <= 0.04045 ? value / 12.92 : Math.pow((value + 0.055) / 1.055, 2.4);
          }).reduce((sum, value, i) => sum + value * [0.2126, 0.7152, 0.0722][i], 0);
          const background = rgb(getComputedStyle(element.querySelector('.modal-content')).backgroundColor);
          const style = getComputedStyle(element.querySelector('dt'));
          const opacity = Number(style.opacity);
          const foreground = rgb(style.color).map((value, i) => value * opacity + background[i] * (1 - opacity));
          const light = [luminance(foreground), luminance(background)].sort((a, b) => b - a);
          return (light[0] + 0.05) / (light[1] + 0.05);
        });
        assert(captionContrast >= 4.5, 'Egress labels must remain readable in both themes');
        await modal.screenshot({path: `/tmp/nas-egress-${id}-${narrow ? 'mobile-dark' : 'desktop'}.png`});
        await modal.locator('[data-bs-dismiss="modal"]').first().click();
        await modal.waitFor({state: 'hidden'});
      }
    }
    assert.deepEqual(errors, []);
    console.log('PASS: real service card, state recovery, shared counts, modal/focus queue, success/failure callbacks, bounded toast dedupe, SPA, narrow/dark/reduced-motion layout');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
