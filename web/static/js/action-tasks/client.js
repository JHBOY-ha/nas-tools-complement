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
  let disposed = false;

  function raw(command, data, requestId) {
    return $.ajax({type: 'POST', url: 'do', dataType: 'json', timeout: 30000, cache: false,
      headers: requestId ? {'X-Request-ID': requestId} : {},
      data: {cmd: command, data: JSON.stringify(data || {})}});
  }
  function announce(message) {
    if (notice) notice.textContent = message || '';
    if (panel) panel.hidden = false;
  }
  function button(text, action) {
    const value = document.createElement('button');
    value.type = 'button'; value.className = 'btn btn-sm btn-outline-secondary';
    value.textContent = text; value.addEventListener('click', action); return value;
  }
  function showResult(record) {
    const focus = document.activeElement;
    const title = document.getElementById('action-task-result-title');
    const output = document.getElementById('action-task-result-text');
    if (!output) return;
    title.textContent = record.title + ' · ' + (labels[record.status] || record.status);
    output.textContent = record.message + '\n\n任务编号：' + record.task_id +
      (record.result ? '\n\n' + JSON.stringify(record.result, null, 2) : '');
    $('#action-task-result').modal('show');
    $('#action-task-result').one('hidden.bs.modal', function () {
      const target = focus && focus.isConnected ? focus : document.getElementById('action-task-refresh');
      // Run after Bootstrap's hidden/focus-trap cleanup has completed.
      if (target) setTimeout(() => target.focus(), 0);
    });
  }
  function render() {
    if (!list) return;
    list.replaceChildren();
    const values = Array.from(records.values()).sort((a, b) => b.created_at - a.created_at).slice(0, 20);
    if (!values.length) {
      const empty = document.createElement('div');
      empty.className = 'list-group-item text-muted'; empty.textContent = '暂无后台操作'; list.appendChild(empty);
      return;
    }
    values.forEach(record => {
      const row = document.createElement('div');
      row.className = 'list-group-item d-flex flex-wrap align-items-center gap-2'; row.setAttribute('role', 'listitem');
      const content = document.createElement('div'); content.className = 'flex-fill text-break';
      const title = document.createElement('strong'); title.textContent = record.title;
      const status = document.createElement('span'); status.className = 'ms-2 text-muted';
      status.textContent = labels[record.status] || record.status;
      const message = document.createElement('div'); message.className = 'small text-muted text-break';
      message.textContent = record.message && record.message !== labels[record.status] ? record.message :
        '已用时 ' + Math.max(0, Math.floor(Date.now() / 1000 - record.created_at)) + ' 秒';
      content.append(title, status, message); row.appendChild(content);
      if (record.status === 'queued') {
        row.appendChild(button('取消排队', function () {
          raw('cancel_action_task', {task_id: record.task_id}).done(reply => {
            if (reply.task) update(reply.task);
            if (reply.code !== 0) announce(reply.msg || '任务已开始，无法取消排队');
          }).fail(() => announce('取消状态未确认，请刷新任务状态'));
        }));
      }
      row.appendChild(button('查看结果', () => showResult(record)));
      list.appendChild(row);
    });
  }
  function update(record) {
    records.set(record.task_id, record);
    if (records.size > 100) records.delete(records.keys().next().value);
    if (panel) panel.hidden = false;
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
    watches.forEach(state => { if (state.resume) { const resume = state.resume; state.resume = null; state.failures = 0; resume(); } });
    raw('get_action_tasks', {}).done(reply => {
      if (reply.code !== 0) { announce(reply.msg || '任务读取失败，请重新登录后重试'); return; }
      (reply.tasks || []).forEach(record => { update(record); if (!terminal.has(record.status)) watch(record); });
      if (!reply.tasks || !reply.tasks.length) render();
    }).fail(() => announce('无法读取后台操作，请检查网络或登录状态'));
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
      if (reply && reply.async && reply.operation_type === 'background_action') watch(reply.task, finish, progress);
      else finish(reply);
    }).fail((response, reason) => {
      acknowledge();
      if (aborted || reason === 'abort') return;
      if (long) recover();
      else finish({code: -99, retcode: -99, msg: '网络错误，请检查登录或连接', retmsg: '网络错误，请检查登录或连接'});
    });
    return result;
  }
  const refreshButton = document.getElementById('action-task-refresh');
  if (refreshButton) refreshButton.addEventListener('click', function () {
    inflight.forEach(value => { if (value.resume) value.resume(); }); refresh();
  });
  global.addEventListener('pagehide', () => {
    disposed = true; watches.forEach(state => clearTimeout(state.timer));
  });
  global.ActionTaskClient = {request, watch, refresh};
  if (panel) refresh();
})(window);
