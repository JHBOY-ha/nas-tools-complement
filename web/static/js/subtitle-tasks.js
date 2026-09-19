(function (window, $) {
  "use strict";

  if (window.SubtitleTasks) {
    return;
  }

  const TERMINAL_STATUSES = new Set([
    "succeeded", "partial", "failed", "canceled", "interrupted"
  ]);
  const ACTIVE_STATUSES = new Set([
    "queued", "recovering", "running", "canceling"
  ]);
  const STORAGE_KEY = "nastool.subtitle.tasks.v1";
  const taskCache = new Map();
  const watchers = new Map();
  const detailLoaded = new Set();
  const detailLoading = new Set();
  let initialized = false;
  let listTimer = null;
  let listBackoff = 15000;
  let centerOpen = false;
  let activeCount = 0;
  let uploadPolicy = {
    text_file_limit_mb: 20,
    vobsub_limit_mb: 200,
    batch_limit_mb: 250,
    max_batch_items: 20,
    llm_max_batch_items: 5
  };

  const STATUS_META = {
    queued: ["排队中", "bg-blue-lt text-blue"],
    recovering: ["恢复中", "bg-azure-lt text-azure"],
    running: ["处理中", "bg-blue-lt text-blue"],
    canceling: ["正在取消", "bg-yellow-lt text-yellow"],
    succeeded: ["已完成", "bg-green-lt text-green"],
    partial: ["部分完成", "bg-yellow-lt text-yellow"],
    failed: ["失败", "bg-red-lt text-red"],
    canceled: ["已取消", "bg-secondary-lt text-secondary"],
    interrupted: ["已中断", "bg-orange-lt text-orange"]
  };
  const TYPE_LABELS = {
    upload: "手动上传",
    repair: "二次处理",
    audit: "字幕检测"
  };
  const PHASE_LABELS = {
    queued: "等待资源",
    validating: "校验",
    validation: "校验",
    normalize: "规范化",
    normalizing: "规范化",
    align: "对齐",
    aligning: "对齐",
    plan: "规划输出",
    planning: "规划输出",
    publish: "发布",
    publishing: "发布",
    refresh: "局部刷新",
    refreshing: "局部刷新",
    enumerate: "枚举目录",
    enumerating: "枚举目录",
    probe: "检测字幕",
    probing: "检测字幕",
    complete: "完成",
    completed: "完成"
  };

  function escapeHtml(value) {
    if (value === null || value === undefined) {
      return "";
    }
    return String(value).replace(/[&<>"']/g, function (char) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[char];
    });
  }

  function payloadOf(response) {
    if (!response || typeof response !== "object") {
      return response || {};
    }
    if (response.data && typeof response.data === "object") {
      return response.data;
    }
    return response;
  }

  function taskOf(response) {
    const payload = payloadOf(response);
    if (payload && payload.task && typeof payload.task === "object") {
      return payload.task;
    }
    if (response && response.task && typeof response.task === "object") {
      return response.task;
    }
    if (payload && (payload.task_id || payload.id) && payload.status) {
      return payload;
    }
    return null;
  }

  function taskIdOf(response) {
    const payload = payloadOf(response);
    const task = taskOf(response);
    return String(
        (task && (task.task_id || task.id)) ||
        (payload && (payload.task_id || payload.id)) ||
        (response && (response.task_id || response.id)) || ""
    );
  }

  function responseMessage(response, fallback) {
    const payload = payloadOf(response);
    return (payload && (payload.msg || payload.message)) ||
        (response && (response.msg || response.message)) || fallback;
  }

  function responseSucceeded(response) {
    return !response || response.code === undefined || Number(response.code) === 0;
  }

  function parseJson(text) {
    if (!text) {
      return {};
    }
    try {
      return JSON.parse(text);
    } catch (error) {
      return {};
    }
  }

  function request(method, url, body) {
    return new Promise(function (resolve, reject) {
      $.ajax({
        type: method,
        url: url + (url.indexOf("?") >= 0 ? "&" : "?") + "random=" + Math.random(),
        dataType: "json",
        contentType: body === undefined ? undefined : "application/json",
        data: body === undefined ? undefined : JSON.stringify(body),
        cache: false,
        timeout: 30000,
        success: function (response, _textStatus, xhr) {
          if (!responseSucceeded(response)) {
            reject({
              status: xhr && xhr.status,
              response: response,
              message: responseMessage(response, "请求失败")
            });
            return;
          }
          resolve(response || {});
        },
        error: function (xhr, textStatus) {
          const response = (xhr && xhr.responseJSON) || parseJson(xhr && xhr.responseText);
          reject({
            status: xhr && xhr.status,
            response: response,
            message: responseMessage(response, textStatus === "timeout" ? "请求超时，请重试" : "连接中断，请稍后重试")
          });
        }
      });
    });
  }

  function rememberTask(taskId) {
    if (!taskId) {
      return;
    }
    try {
      const remembered = JSON.parse(window.localStorage.getItem(STORAGE_KEY) || "[]");
      const next = [taskId].concat(remembered.filter(function (id) {
        return id !== taskId;
      })).slice(0, 30);
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
    } catch (error) {
      // Local storage may be unavailable in private browsing; the server list is authoritative.
    }
  }

  function isTerminal(task) {
    return !!task && TERMINAL_STATUSES.has(String(task.status || "").toLowerCase());
  }

  function isActive(task) {
    return !!task && ACTIVE_STATUSES.has(String(task.status || "").toLowerCase());
  }

  function normalizeTask(task) {
    task = task || {};
    if (!task.task_id && task.id) {
      task.task_id = task.id;
    }
    task.status = String(task.status || "queued").toLowerCase();
    task.progress = task.progress || {};
    if (!task.progress.metrics && task.metrics) {
      task.progress.metrics = task.metrics;
    }
    return task;
  }

  function cacheTask(task) {
    const incoming = normalizeTask(task);
    const incomingId = String(incoming.task_id || "");
    const existing = incomingId ? taskCache.get(incomingId) : null;
    task = existing ? Object.assign({}, existing, incoming) : incoming;
    task.progress = Object.assign({}, (existing && existing.progress) || {}, incoming.progress || {});
    task = normalizeTask(task);
    if (!task.task_id) {
      return task;
    }
    taskCache.set(String(task.task_id), task);
    rememberTask(String(task.task_id));
    return task;
  }

  function emitTask(task) {
    task = cacheTask(task);
    try {
      window.dispatchEvent(new CustomEvent("subtitle-task-update", {detail: task}));
    } catch (error) {
      // IE-style fallback is unnecessary for functionality; polling continues.
    }
    renderCenter();
    updateTaskButtons();
  }

  function notifyWatcher(entry, type, value) {
    entry.listeners.slice().forEach(function (listener) {
      const callback = listener && listener[type];
      if (typeof callback === "function") {
        callback(value);
      }
    });
  }

  function scheduleWatcher(entry, delay) {
    window.clearTimeout(entry.timer);
    entry.timer = window.setTimeout(function () {
      pollWatcher(entry);
    }, delay);
  }

  function pollWatcher(entry) {
    request("GET", "/subtitle/tasks/" + encodeURIComponent(entry.taskId))
        .then(function (response) {
          const task = taskOf(response) || payloadOf(response);
          if (!task || !(task.task_id || task.id)) {
            throw {message: "任务状态响应不完整"};
          }
          entry.delay = String(task.type || "").toLowerCase() === "audit" ? 5000 : 2000;
          entry.failures = 0;
          entry.connectionLost = false;
          setConnectionState("任务状态已连接", false);
          const normalized = cacheTask(task);
          notifyWatcher(entry, "onUpdate", normalized);
          emitTask(normalized);
          if (isTerminal(normalized)) {
            window.clearTimeout(entry.timer);
            watchers.delete(entry.taskId);
            notifyWatcher(entry, "onDone", normalized);
            announce(`${TYPE_LABELS[normalized.type] || "字幕任务"}${STATUS_META[normalized.status] ? STATUS_META[normalized.status][0] : normalized.status}`);
            refreshList(true);
            return;
          }
          const hiddenDelay = document.hidden ? 10000 : entry.delay;
          scheduleWatcher(entry, hiddenDelay);
        })
        .catch(function (error) {
          entry.failures += 1;
          entry.connectionLost = true;
          entry.delay = Math.min(Math.round(entry.delay * 1.8), 30000);
          notifyWatcher(entry, "onConnectionError", error || {message: "连接中断"});
          setConnectionState("连接暂时中断，后台任务仍可能继续；正在自动重连。", true);
          scheduleWatcher(entry, entry.delay);
        });
  }

  function watch(taskId, callbacks) {
    taskId = String(taskId || "");
    if (!taskId) {
      return function () {};
    }
    let entry = watchers.get(taskId);
    if (!entry) {
      entry = {
        taskId: taskId,
        listeners: [],
        delay: 2000,
        failures: 0,
        timer: null,
        connectionLost: false
      };
      watchers.set(taskId, entry);
      scheduleWatcher(entry, 0);
    }
    callbacks = callbacks || {};
    entry.listeners.push(callbacks);
    const cached = taskCache.get(taskId);
    if (cached && typeof callbacks.onUpdate === "function") {
      callbacks.onUpdate(cached);
    }
    return function () {
      const current = watchers.get(taskId);
      if (!current) {
        return;
      }
      current.listeners = current.listeners.filter(function (listener) {
        return listener !== callbacks;
      });
    };
  }

  function requestTask(url, body, callbacks) {
    return request("POST", url, body).then(function (response) {
      const taskId = taskIdOf(response);
      const task = taskOf(response);
      if (task) {
        emitTask(task);
      }
      if (!taskId) {
        return {legacy: true, response: response};
      }
      rememberTask(taskId);
      watch(taskId, callbacks || {});
      refreshList(true);
      return {
        legacy: false,
        reused: !!(payloadOf(response).reused || response.reused),
        task_id: taskId,
        task: task,
        response: response
      };
    });
  }

  function upload(formData, options) {
    options = options || {};
    let xhr = null;
    const promise = new Promise(function (resolve, reject) {
      xhr = new XMLHttpRequest();
      xhr.open("POST", "/subtitle/upload?random=" + Math.random(), true);
      xhr.timeout = 0;
      xhr.upload.addEventListener("progress", function (event) {
        if (event.lengthComputable && typeof options.onUploadProgress === "function") {
          options.onUploadProgress({
            loaded: event.loaded,
            total: event.total,
            percent: event.total ? Math.min(100, Math.round(event.loaded * 100 / event.total)) : null
          });
        }
      });
      xhr.addEventListener("load", function () {
        const response = parseJson(xhr.responseText);
        if (xhr.status < 200 || xhr.status >= 300 || !responseSucceeded(response)) {
          reject({
            status: xhr.status,
            response: response,
            message: responseMessage(response, "字幕上传请求失败")
          });
          return;
        }
        const taskId = taskIdOf(response);
        const task = taskOf(response);
        if (task) {
          emitTask(task);
        }
        if (!taskId) {
          resolve({legacy: true, response: response});
          return;
        }
        rememberTask(taskId);
        watch(taskId, options);
        refreshList(true);
        resolve({
          legacy: false,
          reused: !!(payloadOf(response).reused || response.reused),
          task_id: taskId,
          task: task,
          response: response
        });
      });
      xhr.addEventListener("error", function () {
        reject({
          status: xhr.status,
          message: "上传连接中断；后台可能已收到请求，可在任务中心确认后再重试。"
        });
      });
      xhr.addEventListener("abort", function () {
        reject({status: 0, aborted: true, message: "已停止浏览器上传"});
      });
      xhr.send(formData);
    });
    promise.abort = function () {
      if (xhr && xhr.readyState !== XMLHttpRequest.DONE) {
        xhr.abort();
      }
    };
    return promise;
  }

  function cancel(taskId) {
    taskId = String(taskId || "");
    if (!taskId) {
      return Promise.reject({message: "缺少任务 ID"});
    }
    return request("POST", "/subtitle/tasks/" + encodeURIComponent(taskId) + "/cancel", {})
        .then(function (response) {
          const task = taskOf(response) || payloadOf(response);
          if (task && (task.task_id || task.id)) {
            emitTask(task);
          }
          watch(taskId, {});
          return task;
        });
  }

  function confirmCancel(taskId) {
    const proceed = function () {
      if (typeof window.hide_confirm_modal === "function") {
        window.hide_confirm_modal();
      }
      cancel(taskId).catch(function (error) {
        if (typeof window.show_fail_modal === "function") {
          window.show_fail_modal(error.message || "取消任务失败");
        }
      });
    };
    if (typeof window.show_confirm_modal === "function") {
      window.show_confirm_modal("确认取消这个字幕任务？已原子发布的字幕会保留，正在执行的目录操作可能需要等待安全点。", proceed);
    } else if (window.confirm("确认取消这个字幕任务？")) {
      proceed();
    }
  }

  function randomRequestId() {
    if (window.crypto && typeof window.crypto.randomUUID === "function") {
      return window.crypto.randomUUID();
    }
    return "sub-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2);
  }

  function extensionOf(name) {
    const match = String(name || "").toLowerCase().match(/(\.[^.]+)$/);
    return match ? match[1] : "";
  }

  function stemOf(name) {
    return String(name || "").toLowerCase().replace(/\.[^.]+$/, "");
  }

  function validateFiles(files, alignMode) {
    files = Array.from(files || []);
    const allowed = new Set([".srt", ".ass", ".ssa", ".smi", ".vtt", ".sub", ".idx"]);
    const seen = new Set();
    const subStems = new Set();
    const idxStems = new Set();
    const pairSizes = new Map();
    let totalBytes = 0;
    let logicalCount = 0;
    for (const file of files) {
      const ext = extensionOf(file.name);
      const normalizedName = String(file.name || "").toLowerCase();
      if (!allowed.has(ext)) {
        return {ok: false, message: `不支持的字幕格式：${file.name}`};
      }
      if (seen.has(normalizedName)) {
        return {ok: false, message: `存在同名文件：${file.name}`};
      }
      seen.add(normalizedName);
      totalBytes += Number(file.size || 0);
      if (ext === ".idx") {
        idxStems.add(stemOf(file.name));
        const size = pairSizes.get(stemOf(file.name)) || {sub: 0, idx: 0};
        size.idx = Number(file.size || 0);
        pairSizes.set(stemOf(file.name), size);
      } else {
        logicalCount += 1;
        if (ext === ".sub") {
          subStems.add(stemOf(file.name));
          const size = pairSizes.get(stemOf(file.name)) || {sub: 0, idx: 0};
          size.sub = Number(file.size || 0);
          pairSizes.set(stemOf(file.name), size);
        } else if (Number(file.size || 0) > Number(uploadPolicy.text_file_limit_mb) * 1024 * 1024) {
          return {ok: false, message: `${file.name} 超过文本单文件 ${uploadPolicy.text_file_limit_mb} MiB 限制`};
        }
      }
    }
    for (const idxStem of idxStems) {
      if (!subStems.has(idxStem)) {
        return {ok: false, message: `VobSub 缺少同名 .sub 文件：${idxStem}.idx`};
      }
    }
    for (const subStem of subStems) {
      const sizes = pairSizes.get(subStem) || {sub: 0, idx: 0};
      const limitMb = idxStems.has(subStem)
          ? Number(uploadPolicy.vobsub_limit_mb)
          : Number(uploadPolicy.text_file_limit_mb);
      if (sizes.sub + sizes.idx > limitMb * 1024 * 1024) {
        return {ok: false, message: `${subStem} 超过 ${limitMb} MiB 限制`};
      }
    }
    if (totalBytes > Number(uploadPolicy.batch_limit_mb) * 1024 * 1024) {
      return {ok: false, message: `单批字幕超过 ${uploadPolicy.batch_limit_mb} MiB 限制`};
    }
    if (logicalCount > Number(uploadPolicy.max_batch_items)) {
      return {ok: false, message: `单批最多上传 ${uploadPolicy.max_batch_items} 个逻辑字幕（.sub + .idx 计为一个）`};
    }
    if (String(alignMode || "none") === "llm"
        && logicalCount > Number(uploadPolicy.llm_max_batch_items)) {
      return {ok: false, message: `LLM 对齐单批最多上传 ${uploadPolicy.llm_max_batch_items} 个逻辑字幕`};
    }
    if (idxStems.size && String(alignMode || "none") !== "none") {
      return {ok: false, message: "VobSub 图形字幕不支持时间轴对齐，请选择“不对齐”"};
    }
    return {
      ok: true,
      logicalCount: logicalCount,
      vobsubCount: idxStems.size,
      totalBytes: totalBytes
    };
  }

  function refreshUploadPolicy() {
    return request("GET", "/subtitle/tasks/settings").then(function (response) {
      let policy = payloadOf(response) || {};
      if (policy.policy && typeof policy.policy === "object") {
        policy = policy.policy;
      }
      uploadPolicy = Object.assign({}, uploadPolicy, policy);
      return uploadPolicy;
    });
  }

  function formatBytes(value) {
    value = Number(value || 0);
    if (value < 1024) {
      return value + " B";
    }
    if (value < 1024 * 1024) {
      return (value / 1024).toFixed(1) + " KiB";
    }
    return (value / 1024 / 1024).toFixed(1) + " MiB";
  }

  function formatElapsed(seconds) {
    seconds = Math.max(0, Math.floor(Number(seconds || 0)));
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const remaining = seconds % 60;
    if (hours) {
      return `${hours}小时${minutes}分`;
    }
    if (minutes) {
      return `${minutes}分${remaining}秒`;
    }
    return `${remaining}秒`;
  }

  function metricsText(metrics) {
    metrics = metrics || {};
    const definitions = [
      ["directories", "目录"],
      ["directories_scanned", "目录"],
      ["candidates", "候选字幕"],
      ["inspected", "实际探测"],
      ["scanned", "已检测"],
      ["probed", "深度探测"],
      ["cache_hits", "缓存命中"],
      ["issues", "问题"],
      ["published", "已发布"],
      ["bytes", "读取", formatBytes]
    ];
    const usedLabels = new Set();
    const parts = [];
    definitions.forEach(function (definition) {
      const key = definition[0];
      const label = definition[1];
      if (metrics[key] === undefined || usedLabels.has(label)) {
        return;
      }
      usedLabels.add(label);
      const formatter = definition[2];
      parts.push(`${label} ${formatter ? formatter(metrics[key]) : metrics[key]}`);
    });
    return parts.join(" · ");
  }

  function progressData(task) {
    const progress = (task && task.progress) || {};
    let percent = progress.percent;
    if (percent === undefined && progress.total) {
      percent = Math.round(Number(progress.completed || 0) * 100 / Number(progress.total));
    }
    if (percent !== null && percent !== undefined && Number.isFinite(Number(percent))) {
      percent = Math.max(0, Math.min(100, Math.round(Number(percent))));
    } else {
      percent = null;
    }
    return {
      percent: percent,
      phase: PHASE_LABELS[progress.phase] || progress.phase || "处理中",
      message: progress.message || "",
      currentItem: progress.current_item || "",
      completed: progress.completed,
      total: progress.total,
      elapsedSeconds: progress.elapsed_seconds !== undefined ? progress.elapsed_seconds : task.elapsed_seconds,
      metrics: progress.metrics || {}
    };
  }

  function terminalMessage(task) {
    if (!isTerminal(task)) {
      return "";
    }
    const result = task.result || {};
    const items = Array.isArray(task.items) && task.items.length ? task.items :
        (Array.isArray(result.results) ? result.results.map(function (item, index) {
          return {
            index: index,
            source_name: item.filename || item.source_name,
            status: item.success === false ? "failed" : "succeeded",
            error: item.success === false ? item.message : ""
          };
        }) : []);
    const succeeded = items.filter(function (item) {
      return ["succeeded", "published", "completed"].indexOf(String(item.status || "").toLowerCase()) !== -1;
    }).length;
    const failed = items.filter(function (item) {
      return String(item.status || "").toLowerCase() === "failed" || !!item.error;
    }).length;
    const parts = [];
    if (items.length) {
      parts.push(`逐字幕：完成 ${succeeded}，失败 ${failed}，共 ${items.length}`);
    }
    const refresh = result.refresh || result.refresh_result || task.refresh;
    if (refresh) {
      const refreshStatus = typeof refresh === "string" ? refresh : (refresh.status || refresh.result || "");
      const refreshLabels = {
        refreshed: "已局部刷新当前媒体",
        skipped: "无法可靠定位媒体项目，已跳过刷新",
        failed: "当前媒体局部刷新失败"
      };
      parts.push(
          (refreshStatus === "skipped" && refresh.message) ||
          refreshLabels[refreshStatus] || refresh.message || refreshStatus
      );
    }
    if (!parts.length) {
      parts.push(task.error || statusMeta(task.status)[0]);
    }
    return parts.filter(Boolean).join("；");
  }

  function renderProgress(target, state) {
    const element = typeof target === "string" ? document.querySelector(target) : target;
    if (!element) {
      return;
    }
    state = state || {};
    element.hidden = false;
    const title = element.querySelector("[data-subtitle-progress-title]");
    const message = element.querySelector("[data-subtitle-progress-message]");
    const current = element.querySelector("[data-subtitle-progress-current]");
    const bar = element.querySelector("[data-subtitle-progress-bar]");
    const progress = element.querySelector("[role='progressbar']");
    if (title) {
      title.textContent = state.title || "正在处理字幕";
    }
    if (message) {
      message.textContent = state.message || "";
    }
    if (current) {
      current.textContent = state.currentItem || "";
      current.hidden = !state.currentItem;
    }
    const percent = state.percent === null || state.percent === undefined ? null : Math.max(0, Math.min(100, Math.round(state.percent)));
    if (bar) {
      bar.classList.toggle("progress-bar-indeterminate", percent === null);
      bar.style.width = percent === null ? "100%" : percent + "%";
      bar.textContent = percent === null ? "" : percent + "%";
    }
    if (progress) {
      if (percent === null) {
        progress.removeAttribute("aria-valuenow");
        progress.setAttribute("aria-label", state.title || "处理中，进度未知");
      } else {
        progress.setAttribute("aria-valuenow", String(percent));
        progress.setAttribute("aria-label", `${state.title || "处理进度"} ${percent}%`);
      }
    }
  }

  function renderTaskProgress(target, task) {
    const data = progressData(task);
    const status = STATUS_META[task.status] || [task.status || "处理中", "bg-secondary-lt"];
    const count = data.total !== null && data.total !== undefined
        ? ` ${Number(data.completed || 0)}/${Number(data.total || 0)}` : "";
    renderProgress(target, {
      title: `${status[0]} · ${data.phase}${count}`,
      message: terminalMessage(task) || data.message || (task.status === "queued" && task.queue_position ? `队列位置：${task.queue_position}` : ""),
      currentItem: data.currentItem,
      percent: data.percent
    });
  }

  function statusMeta(status) {
    return STATUS_META[String(status || "").toLowerCase()] || [status || "未知", "bg-secondary-lt text-secondary"];
  }

  function taskScopeText(task) {
    const payload = task.payload || task.scope || {};
    if (task.type === "audit") {
      const category = payload.subcategory || payload.category || "";
      const mode = payload.mode === "deep" ? "深度扫描" : "已链接媒体";
      return [category, mode].filter(Boolean).join(" · ");
    }
    return payload.canonical_media_file || payload.target_media_file || payload.media_file || "";
  }

  function resultDetails(task) {
    const result = task.result || {};
    const items = Array.isArray(task.items) && task.items.length ? task.items :
        (Array.isArray(result.results) ? result.results.map(function (item, index) {
          return {
            index: index,
            source_name: item.filename || item.source_name,
            status: item.success === false ? "failed" : "succeeded",
            output_path: item.data && (item.data.canonical_subtitle || item.data.output_path),
            output_companion_path: item.data && (item.data.companion_subtitle || item.data.output_companion_path),
            error: item.success === false ? item.message : ""
          };
        }) : []);
    const refresh = result.refresh || result.refresh_result || task.refresh || null;
    let html = "";
    if (refresh) {
      const state = typeof refresh === "string" ? refresh : (refresh.status || refresh.result || "");
      const labels = {refreshed: "已局部刷新", skipped: "已跳过刷新", failed: "局部刷新失败"};
      const classes = {refreshed: "text-success", skipped: "text-muted", failed: "text-danger"};
      html += `<div class="small mt-2 ${classes[state] || "text-muted"}">刷新：${escapeHtml(labels[state] || state || "未知")}${refresh.message ? ` · ${escapeHtml(refresh.message)}` : ""}</div>`;
    }
    if (!items.length) {
      return html;
    }
    let itemHtml = "";
    items.slice(0, 20).forEach(function (item) {
      const itemStatus = statusMeta(item.status || (item.error ? "failed" : "succeeded"));
      itemHtml += `
        <div class="py-2 border-top">
          <div class="d-flex flex-wrap align-items-center gap-2">
            <span class="badge ${itemStatus[1]}">${escapeHtml(itemStatus[0])}</span>
            <span class="small fw-bold text-break">${escapeHtml(item.source_name || item.name || `字幕 ${Number(item.index || 0) + 1}`)}</span>
          </div>
          ${item.output_path ? `<div class="text-muted small text-break mt-1">${escapeHtml(item.output_path)}</div>` : ""}
          ${item.output_companion_path ? `<div class="text-muted small text-break mt-1">配对文件：${escapeHtml(item.output_companion_path)}</div>` : ""}
          ${item.error ? `<div class="text-danger small mt-1">${escapeHtml(item.error)}</div>` : ""}
        </div>`;
    });
    html += `<details class="mt-2"><summary class="small text-muted">逐字幕结果（${items.length}）</summary>${itemHtml}</details>`;
    return html;
  }

  function renderTaskCard(task) {
    task = normalizeTask(task);
    const status = statusMeta(task.status);
    const data = progressData(task);
    const knownProgress = data.percent !== null;
    const title = TYPE_LABELS[task.type] || task.type || "字幕任务";
    const server = task.server ? String(task.server).toUpperCase() : "";
    const scope = taskScopeText(task);
    const time = task.created_at || task.started_at || "";
    const elapsed = data.elapsedSeconds !== undefined && data.elapsedSeconds !== null
        ? formatElapsed(data.elapsedSeconds) : "";
    const metrics = metricsText(data.metrics);
    const canCancel = task.cancellable !== false && isActive(task) && task.status !== "canceling";
    const queue = task.status === "queued" && task.queue_position
        ? `<span class="text-muted small">队列第 ${escapeHtml(task.queue_position)} 位</span>` : "";
    const progressLabel = data.total !== null && data.total !== undefined
        ? `${Number(data.completed || 0)}/${Number(data.total || 0)}` : "";
    const error = task.error || (task.result && task.result.error) || "";
    return `
      <article class="card card-sm" data-subtitle-task-id="${escapeHtml(task.task_id)}">
        <div class="card-body">
          <div class="d-flex align-items-start gap-3">
            <div class="flex-fill min-w-0">
              <div class="d-flex flex-wrap align-items-center gap-2">
                <span class="fw-bold">${escapeHtml(title)}</span>
                <span class="badge ${status[1]}">${escapeHtml(status[0])}</span>
                ${server ? `<span class="badge bg-secondary-lt text-secondary">${escapeHtml(server)}</span>` : ""}
                ${queue}
              </div>
              <div class="d-flex flex-wrap gap-2 text-muted small mt-1">
                <span>${escapeHtml(data.phase)}</span>
                ${progressLabel ? `<span>${escapeHtml(progressLabel)}</span>` : ""}
                ${elapsed ? `<span>耗时 ${escapeHtml(elapsed)}</span>` : ""}
                ${time ? `<span>${escapeHtml(time)}</span>` : ""}
              </div>
              ${scope ? `<div class="text-muted small text-break mt-1">${escapeHtml(scope)}</div>` : ""}
            </div>
            ${canCancel ? `<button type="button" class="btn btn-sm btn-outline-danger subtitle-task-cancel" data-task-id="${escapeHtml(task.task_id)}" aria-label="取消${escapeHtml(title)}">取消</button>` : ""}
          </div>
          ${isActive(task) ? `
          <div class="progress progress-sm mt-3" role="progressbar" aria-label="${escapeHtml(title)}进度" ${knownProgress ? `aria-valuenow="${data.percent}"` : ""} aria-valuemin="0" aria-valuemax="100">
            <div class="progress-bar ${knownProgress ? "" : "progress-bar-indeterminate"}" style="width:${knownProgress ? data.percent + "%" : "100%"}"></div>
          </div>` : ""}
          ${data.message ? `<div class="small mt-2">${escapeHtml(data.message)}</div>` : ""}
          ${data.currentItem ? `<div class="text-muted small text-break mt-1">当前：${escapeHtml(data.currentItem)}</div>` : ""}
          ${metrics ? `<div class="text-muted small mt-1">${escapeHtml(metrics)}</div>` : ""}
          ${error ? `<div class="text-danger small mt-2">${escapeHtml(error)}</div>` : ""}
          ${resultDetails(task)}
        </div>
      </article>`;
  }

  function sortedTasks() {
    return Array.from(taskCache.values()).sort(function (a, b) {
      const aTime = Date.parse(a.created_at || a.updated_at || 0) || 0;
      const bTime = Date.parse(b.created_at || b.updated_at || 0) || 0;
      return bTime - aTime;
    });
  }

  function renderCenter() {
    const list = document.getElementById("subtitle-task-center-list");
    if (!list) {
      return;
    }
    const tasks = sortedTasks().slice(0, 100);
    if (!tasks.length) {
      list.innerHTML = '<div class="empty py-5"><p class="empty-title">暂无字幕任务</p><p class="empty-subtitle text-muted">上传、二次处理和检测任务会显示在这里。</p></div>';
      return;
    }
    list.innerHTML = '<div class="d-grid gap-3">' + tasks.map(renderTaskCard).join("") + "</div>";
  }

  function updateTaskButtons() {
    activeCount = sortedTasks().filter(isActive).length;
    document.querySelectorAll("[data-subtitle-task-count]").forEach(function (badge) {
      badge.textContent = String(activeCount);
      badge.classList.toggle("d-none", activeCount === 0);
      badge.setAttribute("aria-label", `当前有 ${activeCount} 个运行中的字幕任务`);
    });
  }

  function announce(message) {
    const live = document.getElementById("subtitle-task-announcer");
    if (!live) {
      return;
    }
    live.textContent = "";
    window.setTimeout(function () {
      live.textContent = message || "";
    }, 20);
  }

  function setConnectionState(message, warning) {
    const node = document.getElementById("subtitle-task-connection");
    if (!node) {
      return;
    }
    node.textContent = message || "";
    node.className = warning ? "text-warning small" : "text-muted small";
  }

  function tasksOf(response) {
    const payload = payloadOf(response);
    if (Array.isArray(payload)) {
      return payload;
    }
    return payload.items || payload.tasks || response.items || response.tasks || [];
  }

  function hydrateTerminalDetails(tasks) {
    (tasks || []).filter(function (task) {
      const taskId = String(task.task_id || task.id || "");
      const cached = taskCache.get(taskId);
      return taskId && isTerminal(task) &&
          !(cached && cached.result !== undefined && cached.items !== undefined) &&
          !detailLoaded.has(taskId) && !detailLoading.has(taskId);
    }).slice(0, 5).forEach(function (task) {
      const taskId = String(task.task_id || task.id);
      detailLoading.add(taskId);
      request("GET", "/subtitle/tasks/" + encodeURIComponent(taskId)).then(function (response) {
        const detail = taskOf(response) || payloadOf(response);
        if (detail && (detail.task_id || detail.id)) {
          emitTask(detail);
        }
        detailLoaded.add(taskId);
      }).catch(function () {
        // List status remains valid; a later task-center refresh can retry detail loading.
      }).finally(function () {
        detailLoading.delete(taskId);
      });
    });
  }

  function scheduleListRefresh(delay) {
    window.clearTimeout(listTimer);
    listTimer = window.setTimeout(function () {
      refreshList(false).catch(function () {});
    }, delay === undefined ? (centerOpen ? 3000 : 15000) : delay);
  }

  function refreshList(immediate) {
    if (!initialized) {
      return Promise.resolve([]);
    }
    if (immediate === true) {
      window.clearTimeout(listTimer);
    }
    return request("GET", "/subtitle/tasks?page=1&page_size=100&limit=100&offset=0")
        .then(function (response) {
          listBackoff = 15000;
          setConnectionState("任务状态已连接", false);
          const tasks = tasksOf(response).map(cacheTask);
          if (centerOpen) {
            hydrateTerminalDetails(tasks);
          }
          renderCenter();
          updateTaskButtons();
          scheduleListRefresh();
          return tasks;
        })
        .catch(function (error) {
          listBackoff = Math.min(Math.round(listBackoff * 1.8), 60000);
          setConnectionState("任务中心连接中断，后台任务不会因此停止；正在自动重连。", true);
          scheduleListRefresh(listBackoff);
          return Promise.reject(error);
        });
  }

  function ensureCenter() {
    if (document.getElementById("subtitle-task-center-modal")) {
      return;
    }
    const style = document.createElement("style");
    style.id = "subtitle-task-center-style";
    style.textContent = [
      ".subtitle-task-modal-list{min-height:12rem}",
      ".subtitle-upload-progress .progress-bar{min-width:0;transition:width .2s ease-out}",
      "[data-subtitle-task-center],.subtitle-task-cancel,.subtitle-task-cancel-action{min-height:2.75rem}",
      "@media (prefers-reduced-motion:reduce){.subtitle-upload-progress .progress-bar{transition:none!important}}"
    ].join("");
    document.head.appendChild(style);
    const wrapper = document.createElement("div");
    wrapper.innerHTML = `
      <div class="modal modal-blur fade" id="subtitle-task-center-modal" tabindex="-1" role="dialog" aria-hidden="true">
        <div class="modal-dialog modal-lg modal-dialog-centered modal-dialog-scrollable" role="document">
          <div class="modal-content">
            <div class="modal-header">
              <div>
                <h5 class="modal-title">字幕任务中心</h5>
                <div id="subtitle-task-connection" class="text-muted small" aria-live="polite">正在连接任务状态...</div>
              </div>
              <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="关闭"></button>
            </div>
            <div class="modal-body">
              <div class="alert alert-info py-2">关闭窗口不会停止后台任务。只有点击任务右侧的“取消”才会请求停止；NAS 目录操作可能在安全点前保持“正在取消”。</div>
              <div id="subtitle-task-center-list" class="subtitle-task-modal-list" aria-live="polite"></div>
            </div>
            <div class="modal-footer">
              <button type="button" class="btn me-auto" data-bs-dismiss="modal">关闭</button>
              <button type="button" class="btn btn-outline-primary" id="subtitle-task-center-refresh">刷新状态</button>
            </div>
          </div>
        </div>
      </div>
      <div id="subtitle-task-announcer" class="visually-hidden" aria-live="polite" aria-atomic="true"></div>`;
    while (wrapper.firstElementChild) {
      document.body.appendChild(wrapper.firstElementChild);
    }
    $("#subtitle-task-center-modal")
        .on("shown.bs.modal", function () {
          centerOpen = true;
          refreshList(true).catch(function () {});
        })
        .on("hidden.bs.modal", function () {
          centerOpen = false;
          scheduleListRefresh();
        });
    $(document).on("click", "#subtitle-task-center-refresh", function () {
      setConnectionState("正在刷新任务状态...", false);
      refreshList(true).catch(function () {});
    });
    $(document).on("click", ".subtitle-task-cancel", function () {
      confirmCancel($(this).data("task-id"));
    });
  }

  function openCenter() {
    ensureCenter();
    $("#subtitle-task-center-modal").modal("show");
  }

  function init() {
    if (initialized) {
      updateTaskButtons();
      return;
    }
    initialized = true;
    ensureCenter();
    $(document).on("click", "[data-subtitle-task-center]", function (event) {
      event.preventDefault();
      openCenter();
    });
    document.addEventListener("visibilitychange", function () {
      if (!document.hidden) {
        watchers.forEach(function (entry) {
          scheduleWatcher(entry, 0);
        });
        refreshList(true).catch(function () {});
      }
    });
    refreshUploadPolicy().catch(function () {});
    refreshList(true).catch(function () {});
  }

  window.SubtitleTasks = {
    init: init,
    openCenter: openCenter,
    refreshList: refreshList,
    request: request,
    requestTask: requestTask,
    upload: upload,
    watch: watch,
    cancel: cancel,
    confirmCancel: confirmCancel,
    validateFiles: validateFiles,
    refreshUploadPolicy: refreshUploadPolicy,
    renderProgress: renderProgress,
    renderTaskProgress: renderTaskProgress,
    terminalMessage: terminalMessage,
    randomRequestId: randomRequestId,
    formatBytes: formatBytes,
    formatElapsed: formatElapsed,
    taskIdOf: taskIdOf,
    taskOf: taskOf,
    payloadOf: payloadOf,
    responseMessage: responseMessage,
    isTerminal: isTerminal,
    isActive: isActive,
    escapeHtml: escapeHtml
  };

  $(function () {
    init();
  });
})(window, window.jQuery);
