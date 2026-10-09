/* Real-browser contract for accepted operations, safe feedback and recovery. */
const {chromium} = require('playwright');
const fs = require('fs'), path = require('path'), assert = require('assert');
const root = path.resolve(__dirname, '..');
(async function () {
  const browser = await chromium.launch({headless: true, ...(process.env.CHROME_PATH ? {executablePath: process.env.CHROME_PATH} : {})});
  try {
    const page = await browser.newPage({viewport: {width: 1100, height: 800}});
    const errors = [], rows = new Map();
    let next = 0, mutations = 0, progressReads = 0, finish = false, offline = false, fail = false;
    let networkReply = {res: false, reachable: true, http_status: 403,
      msg: 'TMDB 官网拒绝访问（HTTP 403）', time: '120 毫秒'};
    page.on('pageerror', error => errors.push(error.message));
    await page.route('http://tasks.test/**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname === '/') {
        const panel = fs.readFileSync(path.join(root, 'web/templates/action-tasks/panel.html'), 'utf8').replace(/{#[\s\S]*?#}/g, '');
        return route.fulfill({contentType: 'text/html; charset=utf-8', body: `<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/static/css/tabler.min.css"><link rel="stylesheet" href="/static/css/style.css"><body><main id="page_content"><div id="initial-view"><button type="button" id="start" class="btn btn-primary">启动目录同步</button></div></main>${panel}<script src="/static/js/jquery-3.3.1.min.js"></script><script src="/static/js/tabler.min.js"></script><script src="/static/js/nprogress.js"></script><script src="/static/js/action-tasks/client.js"></script><script src="/static/js/util.js"></script><script>window.results=[]; document.getElementById('start').onclick=()=>ajax_post('run_directory_sync',{sid:'one'},r=>window.results.push(r));</script></body></html>`});
      }
      if (url.pathname === '/do') {
        const form = new URLSearchParams(route.request().postData());
        const command = form.get('cmd'), data = JSON.parse(form.get('data') || '{}');
        if (offline && ['get_action_task', 'get_action_tasks'].includes(command)) return route.abort('connectionfailed');
        let response;
        if (['run_directory_sync', 'start_mediasync', 'sch', 'run_userrss', 'run_brushtask', 'auto_remove_torrents'].includes(command)) {
          mutations += 1;
          const id = 'task-' + (++next);
          const row = {task_id: id, owner: '7', command, title: '目录同步', status: 'queued',
            created_at: Date.now() / 1000, message: '已排队，等待执行', result: null};
          rows.set(id, row);
          return route.fulfill({status: 202, json: {code: 0, retcode: 0, async: true,
            operation_type: 'background_action', task_id: id, task: row}});
        }
        if (command === 'refresh_process') { progressReads += 1; response = {code: 0, value: 42, text: '已处理 42%'}; }
        else if (command === 'get_action_tasks') response = {code: 0, tasks: Array.from(rows.values()).reverse()};
        else if (command === 'get_action_task') {
          const row = rows.get(data.task_id);
          if (row.status !== 'canceled') {
            row.status = finish ? (fail ? (fail === 'canceled' ? 'canceled' : 'failed') : 'succeeded') : 'running';
            row.message = finish ? (fail ? '<img src=x onerror=alert(1)>失败' : '目录同步完成') : '正在执行';
            if (finish) row.result = {code: fail ? -1 : 0, retcode: fail ? -1 : 0, msg: row.message, retmsg: row.message};
          }
          response = {code: 0, task: row};
        } else if (command === 'cancel_action_task') {
          const row = rows.get(data.task_id); row.status = 'canceled'; row.message = '已取消';
          row.result = {code: -1, retcode: -1, msg: '已取消', retmsg: '已取消'};
          response = {code: 0, task: row};
        } else if (command === 'net_test') response = networkReply;
        else response = {code: -1, msg: '不存在'};
        return route.fulfill({json: response});
      }
      const file = path.join(root, 'web', url.pathname);
      if (fs.existsSync(file)) return route.fulfill({path: file, contentType: file.endsWith('.css') ? 'text/css' : 'application/javascript'});
      return route.fulfill({status: 404, body: ''});
    });
    await page.goto('http://tasks.test/');
    await page.locator('#start').click();
    await page.getByText('正在执行', {exact: true}).waitFor();
    assert.equal(await page.evaluate(() => results.length), 0, '202 must not execute success callback');
    await page.locator('#start').click();
    assert.equal(mutations, 1, 'Repeated intent stays coalesced in the browser');
    finish = true;
    await page.waitForFunction(() => results.length === 1);
    assert.equal(await page.evaluate(() => results[0].code), 0);
    const resultButton = page.getByRole('button', {name: '查看结果'}).first();
    await resultButton.click();
    await page.locator('#action-task-result.show').waitFor();
    await page.waitForFunction(() => document.getElementById('action-task-result').contains(document.activeElement));
    await page.keyboard.press('Escape');
    await page.locator('#action-task-result').waitFor({state: 'hidden'});
    await page.waitForFunction(() => document.activeElement && document.activeElement.textContent === '查看结果');
    assert(await resultButton.evaluate(element => element === document.activeElement));
    await page.setViewportSize({width: 390, height: 720});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    finish = false; offline = true;
    await page.evaluate(() => { ajax_post('run_directory_sync', {sid:'offline'}, r => results.push(r)); });
    await page.getByText('连接中断，执行结果尚未确认，请刷新状态；不要重复提交操作', {exact: true}).waitFor();
    const before = mutations;
    offline = false; finish = true; fail = true;
    await page.getByRole('button', {name:'刷新状态'}).click();
    await page.waitForFunction(() => results.length === 2);
    assert.equal(mutations, before, 'Read recovery cannot resubmit a mutation');
    await page.getByRole('button', {name:'查看结果'}).first().click();
    await page.waitForFunction(() => document.getElementById('action-task-result').contains(document.activeElement));
    assert.equal(await page.locator('#action-task-result-text img').count(), 0, 'Result messages are text');
    await page.keyboard.press('Escape');
    finish = false; fail = false;
    await page.evaluate(() => { ajax_post('run_directory_sync', {sid:'stale'}, r => window.staleCalled=true); });
    await page.waitForFunction(() => document.getElementById('action-task-list').textContent.includes('正在执行'));
    await page.evaluate(() => document.getElementById('page_content').replaceChildren(document.createElement('div')));
    finish = true;
    await page.waitForFunction(() => document.getElementById('action-task-list').textContent.includes('执行完成'));
    await page.waitForTimeout(1200);
    assert.equal(await page.evaluate(() => Boolean(window.staleCalled)), false, 'Detached page must ignore old callback');
    // Object-key ordering cannot create a second browser mutation.
    finish = false;
    const priorMutations = mutations;
    await page.evaluate(() => {
      ajax_post('run_directory_sync', {sid:'canonical', nested:{a:1,b:2}}, () => {});
      ajax_post('run_directory_sync', {nested:{b:2,a:1}, sid:'canonical'}, () => {});
    });
    await page.waitForTimeout(300);
    assert.equal(mutations, priorMutations + 1);
    finish = true;
    await page.waitForTimeout(1200);

    // Exercise the real service callback with both terminal outcomes.
    const service = fs.readFileSync(path.join(root, 'web/templates/service.html'), 'utf8');
    const runScheduler = service.slice(service.indexOf('  function run_scheduler('), service.indexOf('  // 名称测试'));
    await page.addScriptTag({content: 'window.feedback=[]; window.show_success_modal=m=>feedback.push(["ok",m]); window.show_fail_modal=m=>feedback.push(["error",m]);' + runScheduler});
    fail = true;
    await page.evaluate(() => run_scheduler('sync', '目录同步'));
    await page.waitForFunction(() => feedback.length === 1);
    assert.equal(await page.evaluate(() => feedback[0][0]), 'error');
    fail = false;
    await page.evaluate(() => run_scheduler('sync', '目录同步'));
    await page.waitForFunction(() => feedback.length === 2);
    assert.equal(await page.evaluate(() => feedback[1][1]), '目录同步 服务执行完成');

    // Coalescing retains a single mutation while a replacement page subscribes
    // independently; same-page repeated clicks still produce one callback.
    finish = false;
    const mutationsBeforeReattach = mutations;
    await page.evaluate(() => {
      window.oldCallbacks = 0; window.newCallbacks = 0; window.newProgress = 0; window.newAccepted = 0;
      ActionTaskClient.request('run_directory_sync', {sid:'reattach'}, () => oldCallbacks++);
    });
    await page.waitForFunction(() => document.getElementById('action-task-list').textContent.includes('正在执行'));
    await page.evaluate(() => {
      document.getElementById('page_content').replaceChildren(document.createElement('div'));
      const subscribe = () => ActionTaskClient.request('run_directory_sync', {sid:'reattach'}, () => newCallbacks++, {
        accepted: () => newAccepted++, taskState: () => newProgress++
      });
      window.reattached = subscribe();
      subscribe();
    });
    await page.waitForFunction(() => newAccepted === 2 && newProgress >= 2);
    finish = true;
    await page.waitForFunction(() => reattached.state() === 'resolved');
    assert.equal(await page.evaluate(() => newCallbacks), 1);
    assert.equal(await page.evaluate(() => oldCallbacks), 0);
    assert.equal(mutations, mutationsBeforeReattach + 1);

    // Cached documents keep their pending callbacks/timers. Dispatch the real
    // browser lifecycle events without rebuilding the page or replaying writes.
    finish = false;
    const beforeCache = mutations;
    await page.evaluate(() => {
      window.cacheCallbacks = 0;
      window.cachedRequest = ActionTaskClient.request('run_directory_sync', {sid:'bfcache'}, () => cacheCallbacks++);
    });
    await page.waitForFunction(() => document.getElementById('action-task-list').textContent.includes('正在执行'));
    await page.evaluate(() => {
      window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted:true}));
      window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted:true}));
    });
    finish = true;
    await page.waitForFunction(() => cachedRequest.state() === 'resolved');
    assert.equal(await page.evaluate(() => cacheCallbacks), 1);
    assert.equal(mutations, beforeCache + 1, 'BFCache restoration must not replay the mutation');
    await page.evaluate(() => {
      window.afterCache = ActionTaskClient.request('version', {}, () => cacheCallbacks++);
    });
    await page.waitForFunction(() => afterCache.state() === 'resolved');
    assert.equal(await page.evaluate(() => cacheCallbacks), 2, 'New ordinary requests must still deliver feedback');

    // Exercise all three actual callers with success, failure and cancellation.
    // Only success may offer the normal refresh callback.
    const callers = [
      ['rss/user_rss.html', 'run_userrss_now'],
      ['site/brushtask.html', 'run_brushtask_now'],
      ['download/torrent_remove.html', 'run_torrent_remove_now']
    ];
    for (const [template, functionName] of callers) {
      const text = fs.readFileSync(path.join(root, 'web/templates', template), 'utf8');
      const start = text.indexOf('  function ' + functionName + '(');
      assert(start >= 0, functionName + ' must exist');
      const handler = text.slice(start, text.indexOf('\n  }', start) + 4);
      await page.addScriptTag({content: handler});
      for (const outcome of [false, true, 'canceled']) {
        fail = outcome; finish = true;
        const beforeFeedback = await page.evaluate(() => feedback.length);
        await page.evaluate(name => window[name]('test'), functionName);
        await page.waitForFunction(n => feedback.length === n + 1, beforeFeedback);
        assert.equal(await page.evaluate(() => feedback[feedback.length - 1][0]), outcome ? 'error' : 'ok');
      }
    }

    // Media progress must arrive while work is running, and late progress must
    // never overwrite the terminal error or restart polling after closing.
    await page.evaluate(() => {
      const host = document.createElement('div');
      host.innerHTML = '<div id="index-mediasync-modal"><span id="mediasync_status"></span><button id="mediasync_btn">开始同步</button><div id="mediasync_process_bar"></div></div>';
      document.body.appendChild(host);
    });
    await page.addScriptTag({path:path.join(root, 'web/static/js/media-sync.js')});
    finish = false;
    await page.evaluate(() => start_media_sync(true));
    await page.waitForFunction(() => document.getElementById('mediasync_status').textContent === '已处理 42%');
    assert(progressReads > 0, 'Progress must be read before task completion');
    fail = true; finish = true;
    await page.waitForFunction(() => document.getElementById('mediasync_status').textContent.includes('失败'));
    const readsAtEnd = progressReads;
    await page.waitForTimeout(500);
    assert.equal(progressReads, readsAtEnd, 'Terminal failure must stop progress polling');
    assert.equal(await page.locator('#mediasync_btn').textContent(), '开始同步');
    finish = false; fail = false;
    await page.evaluate(() => start_media_sync(true));
    await page.waitForFunction(() => refresh_sync_process_flag);
    await page.evaluate(() => close_mediasync_modal());
    finish = true;
    await page.waitForTimeout(1200);
    assert.equal(await page.evaluate(() => refresh_sync_process_flag), false);

    // The actual service renderer must expose HTTP rejection reasons as text,
    // clear them on retry, and preserve the shared batch button lifecycle.
    const networkStart = service.indexOf('  function run_net_test_batch(');
    const networkEnd = service.indexOf('  // 网络测速', networkStart);
    await page.addScriptTag({content: service.slice(networkStart, networkEnd)});
    const tableStart = service.indexOf('      <div class="table-responsive">', service.indexOf('id="modal-nettest"'));
    const tableEnd = service.indexOf('      <div class="modal-footer">', tableStart);
    const networkTable = service.slice(tableStart, tableEnd)
      .replace(/{%[\s\S]*?%}/g, '').replace(/{{\s*target\s*}}/g, 'www.themoviedb.org')
      .replace(/{{\s*loop.index0\s*}}/g, '0');
    await page.evaluate(markup => {
      const section = document.createElement('section'); section.className = 'container-xl';
      section.innerHTML = '<h2>网络连通性测试</h2><div class="card">' + markup +
        '<div class="card-footer"><button type="button" id="nettest_btn" class="btn btn-primary">测试</button></div></div>';
      document.getElementById('page_content').replaceChildren(section);
      document.getElementById('nettest_btn').onclick = function () {
        run_net_test_batch($(this), 'nettest_item', 'nettest_item');
      };
    }, networkTable);
    assert.equal(await page.getByText('网络连通性测试', {exact: true}).count(), 1);
    assert.equal(await page.locator('#nettest_item_res_0').count(), 1);
    await page.setViewportSize({width: 1100, height: 800});
    await page.locator('#nettest_btn').click();
    await page.getByText(networkReply.msg, {exact: true}).waitFor();
    assert.equal(await page.locator('#nettest_item_res_0 .bg-yellow').textContent(), '已响应');
    assert.equal(await page.locator('#nettest_item_res_0 .bg-green').count(), 0, '403 cannot report page availability');
    assert.equal(await page.locator('#nettest_item_res_0').getAttribute('aria-live'), 'polite');
    assert.equal(await page.locator('#nettest_item_res_time_0').textContent(), '120 毫秒');
    await page.waitForFunction(() => !document.getElementById('nettest_btn').disabled);
    await page.locator('#page_content').screenshot({path: '/tmp/nas-network-test-desktop.png'});
    await page.setViewportSize({width: 390, height: 720});
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.locator('#page_content').screenshot({path: '/tmp/nas-network-test-mobile.png'});
    networkReply = {res: false, msg: '<img src=x onerror="window.networkXss=true">禁止访问',
      time: '<img src=x onerror="window.networkXss=true">'};
    await page.locator('#nettest_btn').click();
    await page.waitForFunction(() => document.getElementById('nettest_item_res_0').textContent.includes('<img'));
    assert.equal(await page.locator('#nettest_item_res_0 img, #nettest_item_res_time_0 img').count(), 0);
    assert.equal(await page.evaluate(() => Boolean(window.networkXss)), false);
    networkReply = {res: true, time: '80 毫秒'};
    await page.locator('#nettest_btn').focus();
    await page.keyboard.press('Enter');
    await page.waitForFunction(() => document.getElementById('nettest_item_res_0').textContent === '是');
    assert.equal(await page.locator('#nettest_item_res_0 .bg-green').count(), 1);
    assert.equal(await page.locator('#nettest_item_res_0 .small').count(), 0);
    assert.equal(await page.locator('#nettest_item_res_time_0').textContent(), '80 毫秒');
    assert.equal(await page.locator('#nettest_btn').textContent(), '测试');

    assert.equal(await page.locator('#action-task-notice').getAttribute('aria-live'), 'polite');
    await page.screenshot({path:'/tmp/nas-action-tasks-mobile.png'});
    assert.deepEqual(errors, []);
    console.log('PASS: accepted/running/completed, dedupe, safe results, offline recovery, stale page, keyboard, narrow layout, network failure diagnostics');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode=1; });
