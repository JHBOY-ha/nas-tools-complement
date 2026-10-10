/* Shared background-action contract; mutations are never auto-replayed. */
(function (global) {
  'use strict';
  const terminal = new Set(['succeeded', 'failed', 'canceled', 'interrupted']);
  const labels = {accepting: '正在接收', queued: '等待执行', running: '正在执行',
    succeeded: '执行完成', failed: '执行失败', canceled: '已取消', interrupted: '结果未确认'};
  const commands = new Set(['rename', 'rename_udf', 're_identification', 'run_directory_sync',
    'run_userrss', 'run_brushtask', 'auto_remove_torrents', 'start_mediasync', 'sch',
    'download_subtitle', 'special_confirmation']);
  const records = new Map(), inflight = new Map(), watches = new Map();
  const panel = document.getElementById('action-task-panel');
  const list = document.getElementById('action-task-list');
  const notice = document.getElementById('action-task-notice');
  const refreshButton = document.getElementById('action-task-refresh');
  let disposed = false, reading = false, readComplete = false, readFailed = false;
  let drawerTrigger = null, modalSession = null, lastFeedback = '', lastFeedbackAt = 0;

  function recentRecords() {
    return Array.from(records.values()).sort((a, b) => b.created_at - a.created_at).slice(0, 20);
  }
  // 两个入口读取同一份任务状态；新加载的服务页和延迟渲染的页头也能补齐摘要。
  function renderSummary() {
    const values = recentRecords();
    const running = values.filter(record => record.status === 'running').length;
    const queued = values.filter(record => record.status === 'queued').length;
    const count = running + queued;
    const summary = readFailed ? '状态待刷新' :
      (!readComplete && !values.length ? '正在读取任务状态…' : `最近操作：执行中 ${running} 项 · 排队 ${queued} 项`);
    document.querySelectorAll('[data-action-task-summary]').forEach(element => { element.textContent = summary; });
    document.querySelectorAll('[data-action-task-count]').forEach(element => {
      element.textContent = count > 99 ? '99+' : String(count); element.hidden = count === 0;
    });
    document.querySelectorAll('[data-action-task-open]').forEach(element => {
      element.setAttribute('aria-label', '后台操作，' + summary);
      element.setAttribute('aria-expanded', String(Boolean(panel && panel.classList.contains('show'))));
    });
  }

  function openPanel(trigger) {
    if (!panel || modalSession || document.querySelector('.modal.show, .modal.showing')) return;
    drawerTrigger = trigger || document.activeElement;
    refresh();
    $(panel).offcanvas('show');
  }

  function raw(command, data, requestId) {
    return $.ajax({type: 'POST', url: 'do', dataType: 'json', timeout: 30000, cache: false,
      headers: requestId ? {'X-Request-ID': requestId} : {},
      data: {cmd: command, data: JSON.stringify(data || {})}});
  }
  function announce(message, feedback = true) {
    if (notice) notice.textContent = message || '';
    const toast = document.getElementById('action-task-toast');
    const now = global.performance.now();
    // 只对当前可见提示做两秒去重；隐藏后的新任务和较晚的同文案仍应获得反馈。
    const duplicate = toast && (toast.classList.contains('show') || toast.classList.contains('showing')) &&
      message === lastFeedback && now - lastFeedbackAt < 2000;
    // 关闭抽屉时只给简短反馈，不自动打开任务列表或抢走当前页面的焦点。
    if (feedback && message && !duplicate && panel && !panel.classList.contains('show')) {
      const output = document.getElementById('action-task-toast-text');
      if (output && toast) {
        output.textContent = message;
        lastFeedback = message; lastFeedbackAt = now;
        $(toast).toast('show');
      }
    }
  }
  function button(text, action, taskId, kind) {
    const value = document.createElement('button');
    value.type = 'button'; value.className = 'btn btn-sm';
    value.dataset.actionTaskId = taskId; value.dataset.actionTaskKind = kind;
    value.textContent = text; value.addEventListener('click', action); return value;
  }
  function taskButton(taskId, kind) {
    return list && Array.from(list.querySelectorAll('[data-action-task-id]')).find(element =>
      element.dataset.actionTaskId === taskId && element.dataset.actionTaskKind === kind);
  }
  function showResult(record) {
    if (modalSession) return;
    const title = document.getElementById('action-task-result-title');
    const output = document.getElementById('action-task-result-text');
    if (!output) return;
    title.textContent = record.title + ' · ' + (labels[record.status] || record.status);
    output.textContent = record.message + '\n\n任务编号：' + record.task_id +
      (record.result ? '\n\n' + JSON.stringify(record.result, null, 2) : '');
    $('#action-task-result').modal('show');
  }
  function focusDrawerEntry() {
    const target = drawerTrigger && drawerTrigger.isConnected ? drawerTrigger : document.querySelector('[data-action-task-open]');
    if (target) target.focus();
  }
  function restoreDrawerFocus(session) {
    const target = session.focus && session.focus.isConnected ? session.focus :
      taskButton(session.taskId, session.kind) || taskButton(session.taskId, 'result') || refreshButton;
    if (target) target.focus();
  }
  function nextModal(session) {
    if (disposed || modalSession !== session) return;
    // 既有反馈按钮可能继续打开业务对话框，等其关闭后再恢复任务界面。
    const visibleModal = document.querySelector('.modal.show, .modal.showing');
    if (visibleModal) {
      $(visibleModal).one('hidden.bs.modal', () => setTimeout(() => nextModal(session), 0));
      return;
    }
    const target = session.queue.shift();
    if (target) {
      session.active = target;
      $(target).one('hidden.bs.modal', function () {
        session.active = null;
        // 等待 Bootstrap 清理遮罩、滚动锁与焦点陷阱，再继续下一个界面。
        setTimeout(() => nextModal(session), 0);
      });
      $(target).modal('show');
    } else if (session.restore) {
      $(panel).one('shown.bs.offcanvas', function () {
        // 恢复动画期间也可能收到其他任务的终态提示，仍按同一队列串行显示。
        if (session.queue.length) suspendDrawer(session);
        else { modalSession = null; restoreDrawerFocus(session); }
      });
      $(panel).offcanvas('show');
    } else {
      modalSession = null;
      focusDrawerEntry();
    }
  }
  function suspendDrawer(session) {
    $(panel).one('hidden.bs.offcanvas', () => setTimeout(() => nextModal(session), 0));
    if (panel.classList.contains('hiding')) return;
    const hide = () => $(panel).offcanvas('hide');
    if (panel.classList.contains('showing')) $(panel).one('shown.bs.offcanvas', hide);
    else hide();
  }
  function coordinateModal(event) {
    const initial = !modalSession;
    if (initial) {
      if (!panel || !panel.matches('.show, .showing, .hiding')) return;
      const focus = panel.contains(document.activeElement) ? document.activeElement : refreshButton;
      modalSession = {queue: [], active: null, restore: !panel.classList.contains('hiding'), focus,
        taskId: focus && focus.dataset.actionTaskId, kind: focus && focus.dataset.actionTaskKind};
    }
    // 已被选中的模态框可以正常显示；其他请求等当前界面关闭后再显示。
    if (modalSession.active === event.target) return;
    event.preventDefault();
    if (!modalSession.queue.includes(event.target)) modalSession.queue.push(event.target);
    if (initial) suspendDrawer(modalSession);
  }
  function render() {
    if (!list) return;
    const focus = list.contains(document.activeElement) ? document.activeElement : null;
    const focusId = focus && focus.dataset.actionTaskId, focusKind = focus && focus.dataset.actionTaskKind;
    list.replaceChildren();
    list.setAttribute('aria-busy', String(reading));
    renderSummary();
    const values = recentRecords();
    if (!values.length) {
      const empty = document.createElement('div');
      empty.className = 'list-group-item text-reset opacity-75';
      empty.textContent = reading ? '正在读取后台操作…' : (readFailed ? '任务列表暂不可用，请刷新状态' : '暂无后台操作');
      list.appendChild(empty);
      return;
    }
    values.forEach(record => {
      const row = document.createElement('div');
      row.className = 'list-group-item d-flex flex-wrap align-items-center gap-2'; row.setAttribute('role', 'listitem');
      const content = document.createElement('div'); content.className = 'flex-fill text-break';
      const title = document.createElement('strong'); title.textContent = record.title;
      const status = document.createElement('span'); status.className = 'ms-2 text-reset opacity-75';
      status.textContent = labels[record.status] || record.status;
      const message = document.createElement('div'); message.className = 'small text-reset opacity-75 text-break';
      message.textContent = record.message && record.message !== labels[record.status] ? record.message :
        '已用时 ' + Math.max(0, Math.floor(Date.now() / 1000 - record.created_at)) + ' 秒';
      content.append(title, status, message); row.appendChild(content);
      if (record.status === 'queued') {
        row.appendChild(button('取消排队', function () {
          raw('cancel_action_task', {task_id: record.task_id}).done(reply => {
            if (reply.task) update(reply.task);
            if (reply.code !== 0) announce(reply.msg || '任务已开始，无法取消排队');
          }).fail(() => announce('取消状态未确认，请刷新任务状态'));
        }, record.task_id, 'cancel'));
      }
      row.appendChild(button('查看结果', () => showResult(record), record.task_id, 'result'));
      list.appendChild(row);
    });
    // 保留键盘用户正在操作的任务按钮，避免每秒轮询让焦点落回页面背景。
    if (focusId) {
      const target = taskButton(focusId, focusKind) || taskButton(focusId, 'result') || refreshButton;
      if (target) target.focus({preventScroll: true});
    }
  }
  function update(record) {
    records.set(record.task_id, record);
    if (records.size > 100) records.delete(records.keys().next().value);
    render();
  }
  function watch(record, complete, progress) {
    update(record);
    if (progress) progress(record);
    const prior = watches.get(record.task_id);
    if (prior) {
      if (complete) prior.callbacks.push(complete);
      if (progress) prior.progress.push(progress);
      return;
    }
    const state = {callbacks: complete ? [complete] : [], progress: progress ? [progress] : [], failures: 0, timer: null};
    watches.set(record.task_id, state);
    function poll() {
      if (disposed) return;
      raw('get_action_task', {task_id: record.task_id}).done(reply => {
        if (reply.code !== 0 || !reply.task) { unavailable(reply.msg); return; }
        state.failures = 0; update(reply.task);
        state.progress.forEach(callback => callback(reply.task));
        if (terminal.has(reply.task.status)) {
          watches.delete(record.task_id);
          state.callbacks.forEach(callback => callback(reply.task.result || {
            code: -1, retcode: -1, msg: reply.task.message, retmsg: reply.task.message
          }));
          announce(reply.task.title + '：' + (labels[reply.task.status] || reply.task.status));
        } else { state.timer = setTimeout(poll, 1000); }
      }).fail(() => unavailable('连接中断，执行结果尚未确认，请刷新状态；不要重复提交操作'));
    }
    function unavailable(message) {
      announce(message || '无法读取任务状态，请确认登录和网络后刷新');
      state.failures += 1;
      if (state.failures <= 3) state.timer = setTimeout(poll, 1000 * Math.pow(2, state.failures));
      // Keep the watch/callback identity while paused. A user-triggered status
      // refresh resumes polling, rather than resubmitting the mutation.
      else state.resume = poll;
    }
    if (terminal.has(record.status)) {
      watches.delete(record.task_id);
      if (complete) complete(record.result || {code: -1, retcode: -1, msg: record.message, retmsg: record.message});
    } else { poll(); }
  }
  function refresh() {
    if (reading || disposed) return;
    reading = true; readFailed = false;
    // 禁用刷新按钮前将焦点留在抽屉，保证等待期间仍能使用 Tab 和 Escape。
    const restoreRefresh = refreshButton && refreshButton === document.activeElement;
    if (restoreRefresh && panel) panel.focus({preventScroll: true});
    if (refreshButton) { refreshButton.disabled = true; refreshButton.setAttribute('aria-busy', 'true'); }
    render();
    // 刷新只恢复查询；提交响应丢失时仍按原请求编号核对，不重复发送写操作。
    inflight.forEach(value => { if (value.resume) value.resume(); });
    watches.forEach(state => { if (state.resume) { const resume = state.resume; state.resume = null; state.failures = 0; resume(); } });
    raw('get_action_tasks', {}).done(reply => {
      if (reply.code !== 0) { readFailed = true; announce(reply.msg || '任务读取失败，请重新登录后重试'); return; }
      announce('', false);
      (reply.tasks || []).forEach(record => { update(record); if (!terminal.has(record.status)) watch(record); });
      if (!reply.tasks || !reply.tasks.length) render();
    }).fail(() => { readFailed = true; announce('无法读取后台操作，请检查网络或登录状态'); })
      .always(() => {
        reading = false; readComplete = true;
        if (refreshButton) { refreshButton.disabled = false; refreshButton.removeAttribute('aria-busy'); }
        render();
        if (restoreRefresh && panel.classList.contains('show') && document.activeElement === panel) refreshButton.focus();
      });
  }
  function request(command, parameters, handler, options) {
    options = options || {};
    // Canonicalize only JSON objects; array ordering remains meaningful.
    function canonical(value) {
      if (Array.isArray(value)) return value.map(canonical);
      if (value && typeof value === 'object') {
        const ordered = Object.create(null);
        Object.keys(value).sort().forEach(key => { ordered[key] = canonical(value[key]); });
        return ordered;
      }
      return value;
    }
    const key = command + '\n' + JSON.stringify(canonical(JSON.parse(JSON.stringify(parameters || {}))));
    const long = commands.has(command) && (command !== 'special_confirmation' || (parameters || {}).stage === 'confirm');
    const origin = document.getElementById('page_content');
    const view = origin && origin.firstElementChild;
    if (long && inflight.has(key)) {
      const existing = inflight.get(key);
      existing.subscribe(view, handler, options);
      return existing;
    }
    const deferred = $.Deferred();
    const subscribers = new Map();
    const requestId = global.crypto && global.crypto.randomUUID ? global.crypto.randomUUID() :
      Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
    let xhr = null, aborted = false;
    let accepted = false, latest = null;
    function live(page) { return !aborted && !disposed && (!page || page.isConnected); }
    function progress(record) {
      // Progress shares the terminal callback's page lifetime guard.
      latest = record;
      subscribers.forEach((subscriber, page) => {
        if (live(page) && subscriber.options.taskState) subscriber.options.taskState(record);
      });
    }
    const result = deferred.promise();
    result.subscribe = function (page, callback, settings) {
      // One subscription per view prevents duplicate-click feedback, while a
      // replacement view receives its own completion and current progress.
      subscribers.forEach((_subscriber, priorPage) => {
        if (priorPage && !priorPage.isConnected) subscribers.delete(priorPage);
      });
      subscribers.set(page, {handler: callback, options: settings});
      if (accepted && settings.accepted) settings.accepted();
      if (latest && live(page) && settings.taskState) settings.taskState(latest);
    };
    result.subscribe(view, handler, options);
    function acknowledge() {
      accepted = true;
      subscribers.forEach(subscriber => { if (subscriber.options.accepted) subscriber.options.accepted(); });
    }
    result.abort = function () { aborted = true; if (xhr) xhr.abort(); inflight.delete(key); deferred.reject(null, 'abort'); };
    function finish(reply) {
      inflight.delete(key);
      if (aborted || disposed) return;
      subscribers.forEach((subscriber, page) => {
        if (live(page) && subscriber.handler) subscriber.handler(reply);
      });
      subscribers.clear();
      deferred.resolve(reply);
    }
    function recover() {
      announce('提交响应未收到，正在核对任务；不会自动重复执行操作');
      xhr = raw('find_action_task', {request_id: requestId});
      xhr.done(reply => {
        if (reply.code === 0 && reply.task) watch(reply.task, finish, progress);
        else {
          announce('执行结果未确认，请刷新状态或检查记录后再决定是否重试');
          // Keep the intent pending: a lost acknowledgement is not proof that
          // the original mutation never reached the server.
          result.resume = recover;
        }
      }).fail(() => {
        announce('连接中断，无法核对提交结果；请恢复网络后刷新状态，不要重复提交');
        result.resume = recover;
      });
    }
    if (long) inflight.set(key, result);
    xhr = $.ajax({type: 'POST', url: 'do?random=' + Math.random(), dataType: 'json', cache: false,
      async: options.async !== false, timeout: long ? 30000 : 0, headers: {'X-Request-ID': requestId},
      data: {cmd: command, data: JSON.stringify(parameters || {})}});
    xhr.done(reply => {
      acknowledge();
      if (reply && reply.async && reply.operation_type === 'background_action') {
        announce(reply.task.title + '：' + (labels[reply.task.status] || reply.task.status));
        watch(reply.task, finish, progress);
      }
      else finish(reply);
    }).fail((response, reason) => {
      acknowledge();
      if (aborted || reason === 'abort') return;
      if (long) recover();
      else finish({code: -99, retcode: -99, msg: '网络错误，请检查登录或连接', retmsg: '网络错误，请检查登录或连接'});
    });
    return result;
  }
  if (panel) {
    // 仅协调结果和终态反馈，避免延后等待/进度窗口的快速显示与关闭。
    $(document).on('show.bs.modal', '#action-task-result, #system-success-modal, #system-fail-modal', coordinateModal);
    $(panel).on('shown.bs.offcanvas hidden.bs.offcanvas', renderSummary);
    $(panel).on('hidden.bs.offcanvas', function () {
      if (!modalSession) setTimeout(focusDrawerEntry, 0);
    });
  }
  global.addEventListener('pagehide', event => {
    // BFCache freezes and resumes this same document, including its timers and
    // pending callbacks. Keep them intact; only a real unload disposes the client.
    if (event.persisted) return;
    disposed = true; watches.forEach(state => clearTimeout(state.timer));
  });
  // 动态服务页和 Lit 页头只调用共享入口，不创建第二份记录或轮询器。
  global.ActionTaskClient = {request, watch, refresh, renderSummary, open: openPanel};
  // 等待 Bootstrap 注册 jQuery 插件，避免快速恢复的任务先于 toast 初始化结束。
  if (panel) $(refresh);
})(window);
