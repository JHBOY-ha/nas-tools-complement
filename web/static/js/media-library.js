var library_page = 1;
var library_total_pages = null;
var library_page_size = 18;
var library_items_cache = {};
var library_current_series_id = "";
var library_current_series_title = "";
var library_episodes_cache = [];
var library_online_media = {};
var library_poster_observer = null;
var library_eager_poster_count = 6;
var library_default_media_server = "emby";
var library_subtitle_audit_categories = {};
var library_subtitle_audit_roots = {};
var library_items_loading = false;
var library_refreshing = false;
var library_refresh_notice = "";
var library_summary_generation = 0;
var library_summary_key = "";
var library_drawer_item = null;
var library_drawer_resume_id = "";
var library_upload_preview_generation = 0;
var library_online_serialized = false;
var library_fill = null;
var library_fill_loading = false;
var library_pending_page = null;
var library_view_instance = null;
var library_categories_cache = {};
var library_subtitle_audit_task_id = "";
var library_subtitle_upload_task_id = "";
var library_subtitle_upload_request = null;

// popstate changes the history entry before the SPA replaces its DOM. Invalidate
// immediately so a late response cannot write into that 150 ms transition window.
// The script is reloaded on navigation; install only one window-level listener.
if (typeof window.addEventListener === "function" && !window.library_history_listener_installed) {
  window.addEventListener("popstate", function () {
    library_view_instance = null;
  });
  window.library_history_listener_installed = true;
}

function library_escape_html(value) {
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

function library_escape_js(value) {
  return library_escape_html(value).replace(/\\/g, "\\\\").replace(/"/g, "&quot;");
}

function library_post_json(url, data, handler) {
  NProgress.start();
  $.ajax({
    type: "POST",
    url: url + "?random=" + Math.random(),
    dataType: "json",
    contentType: "application/json",
    data: JSON.stringify(data || {}),
    timeout: 0,
    success: function (ret) {
      NProgress.done();
      handler(ret || {});
    },
    error: function () {
      NProgress.done();
      show_fail_modal("网络错误");
    }
  });
}

function library_filter_data(page) {
  return {
    type: $("#library_filter_type").val(),
    category: $("#library_filter_category").val(),
    subtitle: $("#library_filter_subtitle").val(),
    sort_by: $("#library_sort_by").val(),
    sort_order: $("#library_sort_order").val(),
    keyword: $("#library_filter_keyword").val(),
    audio_language: $("#library_filter_audio").val() || "all",
    page: page,
    page_size: library_page_size
  };
}

// Counters ignore the subtitle filter itself, so tabs show what each tab would list.
function library_summary_data() {
  return {
    type: $("#library_filter_type").val() || "all",
    category: $("#library_filter_category").val() || "",
    keyword: String($("#library_filter_keyword").val() || "").trim(),
    audio_language: $("#library_filter_audio").val() || "all"
  };
}

function library_render_summary(counts) {
  ["all", "missing", "unknown", "problem", "ok"].forEach(function (key) {
    const value = counts && counts[key] !== undefined ? Number(counts[key]).toLocaleString("zh-CN") : "–";
    $(`[data-library-count="${key}"]`).text(value);
  });
}

function load_library_summary() {
  const view = library_view_instance;
  const grid = document.getElementById("library_items_grid");
  const generation = ++library_summary_generation;
  const data = library_summary_data();
  const key = JSON.stringify(data);
  const is_current = () => generation === library_summary_generation && grid && view &&
      view === library_view_instance && document.getElementById("library_items_grid") === grid;
  $.ajax({
    type: "POST",
    url: "/library/summary?random=" + Math.random(),
    dataType: "json",
    contentType: "application/json",
    data: JSON.stringify(data),
    timeout: 0,
    success: function (ret) {
      if (!is_current()) { return; }
      if (ret && ret.code === 0) {
        library_summary_key = key;
        library_render_summary(ret.counts || {});
      } else {
        library_summary_key = "";
        library_render_summary(null);
      }
    },
    error: function () {
      if (!is_current()) { return; }
      library_summary_key = "";
      library_render_summary(null);
    }
  });
}

function sync_library_tabs() {
  const type = $("#library_filter_type").val() || "all";
  const subtitle = $("#library_filter_subtitle").val() || "all";
  $("#library_type_tabs [data-library-type]").each(function () {
    const active = $(this).attr("data-library-type") === type;
    $(this).toggleClass("active", active).attr("aria-pressed", active ? "true" : "false");
  });
  $("#library_status_tabs [data-library-subtitle]").each(function () {
    const active = $(this).attr("data-library-subtitle") === subtitle;
    $(this).toggleClass("active", active).attr("aria-pressed", active ? "true" : "false");
  });
}

// Audio language comes from synced movie streams, so it only applies to movies.
function update_library_audio_control() {
  const type = $("#library_filter_type").val() || "all";
  const unavailable = type === "tv" || type === "anime";
  if (unavailable) {
    $("#library_filter_audio").val("all");
  }
  $("#library_filter_audio").prop("disabled", unavailable);
}

function set_library_type(type) {
  if (["all", "movie", "tv", "anime"].indexOf(type) === -1) { return; }
  $("#library_filter_type").val(type);
  $("#library_filter_category").val("");
  update_library_category_options();
  update_library_audio_control();
  sync_library_tabs();
  update_library_filter_badge();
  load_library_items(1);
}

function set_library_subtitle(subtitle) {
  if (["all", "missing", "unknown", "problem", "ok"].indexOf(subtitle) === -1) { return; }
  $("#library_filter_subtitle").val(subtitle);
  sync_library_tabs();
  load_library_items(1);
}

function update_library_category_options(categories) {
  if (categories) {
    library_categories_cache = categories;
  }
  categories = library_categories_cache || {};
  const type = $("#library_filter_type").val();
  const selected = $("#library_filter_category").val();
  const category_list = type === "all"
      ? Array.from(new Set([].concat(categories.movie || [], categories.tv || [], categories.anime || []))).sort()
      : (categories[type] || []);
  let html = '<option value="">全部</option>';
  category_list.forEach(function (category) {
    const escaped = library_escape_html(category);
    html += `<option value="${escaped}" ${category === selected ? "selected" : ""}>${escaped}</option>`;
  });
  $("#library_filter_category").html(html);
}

function library_saved_view() {
  const extra = window.history.state && window.history.state.extra;
  return (extra && extra.library_view) || null;
}

function library_save_history_state() {
  // Record the current filter/sort/page onto the active history entry so
  // SPA back/forward restores this view instead of the default one.
  if (typeof window_history !== "function") {
    return;
  }
  const extra = (window.history.state && window.history.state.extra) || {};
  window_history(false, Object.assign({}, extra, {library_view: {
    type: $("#library_filter_type").val() || "all",
    category: $("#library_filter_category").val() || "",
    subtitle: $("#library_filter_subtitle").val() || "all",
    sort_by: $("#library_sort_by").val() || "default",
    sort_order: $("#library_sort_order").val() || "desc",
    keyword: String($("#library_filter_keyword").val() || "").trim(),
    audio_language: $("#library_filter_audio").val() || "all",
    page: library_page || 1
  }}));
}

function load_library_items(page) {
  // DOM identity and an initialization token distinguish a departed view from
  // a newly opened library, even when it has the same URL and element IDs.
  const view = library_view_instance;
  const grid = document.getElementById("library_items_grid");
  const is_current_view = () => grid && view && view === library_view_instance &&
      document.getElementById("library_items_grid") === grid;
  if (!is_current_view()) {
    return;
  }
  page = page || 1;
  if (page < 1) {
    return;
  }
  if (library_items_loading) {
    library_pending_page = page;
    return;
  }
  library_items_loading = true;
  library_pending_page = null;
  // A failed request must leave the pager on the page the user still sees.
  const previous_page = library_page;
  library_page = page;
  $("#library_prev_btn,#library_next_btn,#library_filter_btn,#library_filter_trigger,#library_refresh_btn").prop("disabled", true);
  $("#library_items_summary").html(
      '<span class="d-inline-flex align-items-center gap-2"><span class="spinner-border spinner-border-sm" aria-hidden="true"></span><span>正在加载字幕状态...</span></span>'
  );
  NProgress.start();
  $.ajax({
    type: "POST",
    url: "/library/items?random=" + Math.random(),
    dataType: "json",
    contentType: "application/json",
    data: JSON.stringify(library_filter_data(page)),
    timeout: 0,
    success: function (ret) {
      if (!is_current_view()) { return; }
      ret = ret || {};
      if (ret.code !== 0) {
        library_page = previous_page;
        update_library_pager();
        $("#library_items_summary").text("字幕库加载失败");
        show_fail_modal(ret.msg || "字幕库列表加载失败");
        return;
      }
      update_library_category_options(ret.categories || {});
      library_items_cache = {};
      const items = ret.items || [];
      const total = ret.total || 0;
      const total_pages = Math.max(Math.ceil(total / library_page_size), 1);
      // The server clamps a page past the end; follow the page it returned.
      library_page = ret.page || 1;
      $("#library_items_summary").text(
          `找到 ${total} 个媒体项目 · 本页展示 ${items.length} 个 · 第 ${library_page} / ${total_pages} 页` +
          (library_refresh_notice ? ` · ${library_refresh_notice}` : "")
      );
      library_refresh_notice = "";
      $("#library_synced_at").text(ret.synced_at ? `上次同步：${ret.synced_at}` : "尚未同步");
      // Pure paging keeps the counters; filter changes and data changes refetch them.
      if (!(page !== previous_page && JSON.stringify(library_summary_data()) === library_summary_key)) {
        load_library_summary();
      }
      $("#library_page_text").text(`${library_page} / ${total_pages}`);
      update_library_pager(total_pages);
      if (!items.length) {
        $("#library_items_grid").html(library_empty_state(ret.library_total || 0));
        return;
      }
      let html = "";
      items.forEach(function (item, index) {
        library_items_cache[item.id] = item;
        html += library_item_card(item, index);
      });
      // The drawer reads this cache, so publish all new records before updating it.
      library_refresh_drawer_item();
      $("#library_items_grid").html(html);
      init_library_posters();
    },
    error: function () {
      if (!is_current_view()) { return; }
      library_page = previous_page;
      update_library_pager();
      $("#library_items_summary").text("字幕库加载失败，请稍后重试");
      show_fail_modal("网络错误");
    },
    complete: function () {
      if (!is_current_view()) { return; }
      NProgress.done();
      library_items_loading = false;
      library_save_history_state();
      $("#library_filter_btn,#library_filter_trigger,#library_refresh_btn").prop("disabled", false);
      const pending_page = library_pending_page;
      library_pending_page = null;
      if (pending_page !== null) {
        setTimeout(function () {
          if (is_current_view()) { load_library_items(pending_page); }
        }, 50);
      }
    }
  });
}

function update_library_pager(total_pages) {
  if (total_pages) {
    library_total_pages = total_pages;
  }
  $("#library_prev_btn").prop("disabled", library_page <= 1);
  // An unknown total (first load failed) keeps next available for a retry.
  $("#library_next_btn").prop(
      "disabled", library_total_pages !== null && library_page >= library_total_pages
  );
}

function library_empty_state(library_total) {
  const body = library_total
      ? '<p class="empty-title">没有符合条件的媒体</p><p class="empty-subtitle text-muted">请调整或重置筛选条件。</p>'
      : '<p class="empty-title">字幕库还没有媒体</p><p class="empty-subtitle text-muted">请先在首页执行“媒体库同步”，再回到这里查看。</p>';
  return `<div class="empty" style="grid-column:1/-1;margin:3rem 0">${body}</div>`;
}

function refresh_library_items() {
  // Manual refresh only asks the media server about the items on screen; it
  // never starts a full library sync.
  const view = library_view_instance;
  const grid = document.getElementById("library_items_grid");
  const is_current_view = () => grid && view && view === library_view_instance &&
      document.getElementById("library_items_grid") === grid;
  if (!is_current_view() || library_refreshing || library_items_loading) {
    return;
  }
  const item_ids = Object.keys(library_items_cache);
  if (!item_ids.length) {
    load_library_items(library_page);
    return;
  }
  library_refreshing = true;
  const button = $("#library_refresh_btn");
  const original_html = button.html();
  button.prop("disabled", true).html(
      '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>正在刷新'
  );
  NProgress.start();
  $.ajax({
    type: "POST",
    url: "/library/refresh?random=" + Math.random(),
    dataType: "json",
    contentType: "application/json",
    data: JSON.stringify({item_ids: item_ids}),
    timeout: 0,
    success: function (ret) {
      if (!is_current_view()) { return; }
      ret = ret || {};
      if (ret.code !== 0) {
        library_refresh_notice = "服务器信息未更新，当前显示上次同步的数据";
        show_fail_modal(ret.msg || "刷新失败，当前显示上次同步的数据");
      } else if (ret.failed && ret.failed >= ret.requested) {
        library_refresh_notice = "服务器信息未更新，当前显示上次同步的数据";
        show_fail_modal("无法从媒体服务器读取最新信息，当前显示上次同步的数据");
      } else {
        library_refresh_notice = ret.failed
            ? `已更新 ${ret.updated || 0} 项，${ret.failed} 项读取失败，仍显示原数据`
            : `已更新 ${ret.updated || 0} 项，${ret.unchanged || 0} 项无变化`;
      }
    },
    error: function () {
      if (!is_current_view()) { return; }
      library_refresh_notice = "服务器信息未更新，当前显示上次同步的数据";
      show_fail_modal("网络错误");
    },
    complete: function () {
      library_refreshing = false;
      if (!is_current_view()) { return; }
      NProgress.done();
      button.html(original_html);
      load_library_items(library_page);
    }
  });
}

function reset_library_filters() {
  $("#library_filter_type").val("all");
  $("#library_filter_category").html('<option value="">全部</option>').val("");
  $("#library_filter_subtitle").val("all");
  $("#library_filter_audio").val("all");
  $("#library_sort_by").val("default");
  $("#library_sort_order").val("desc");
  $("#library_filter_keyword").val("");
  update_library_audio_control();
  sync_library_tabs();
  update_library_sort_order_labels();
  update_library_filter_badge();
  hide_library_filter_menu();
  load_library_items(1);
}

function apply_library_filters() {
  update_library_filter_badge();
  hide_library_filter_menu();
  load_library_items(1);
}

function hide_library_filter_menu() {
  const trigger = document.getElementById("library_filter_trigger");
  if (!trigger) {
    return;
  }
  if (window.bootstrap && window.bootstrap.Dropdown) {
    let instance = window.bootstrap.Dropdown.getInstance
        ? window.bootstrap.Dropdown.getInstance(trigger)
        : null;
    if (!instance) {
      instance = new window.bootstrap.Dropdown(trigger);
    }
    instance.hide();
  } else if ($.fn.dropdown) {
    $(trigger).dropdown("hide");
  }
}

function update_library_filter_badge() {
  // Type and subtitle state are visible as tabs, so only menu filters count here.
  let active_count = 0;
  if ($("#library_filter_category").val()) {
    active_count += 1;
  }
  if (($("#library_filter_audio").val() || "all") !== "all") {
    active_count += 1;
  }
  if ($("#library_sort_by").val() !== "default") {
    active_count += 1;
  }
  if (String($("#library_filter_keyword").val() || "").trim()) {
    active_count += 1;
  }
  $("#library_filter_count").text(active_count).toggleClass("d-none", active_count === 0);
  $("#library_filter_count").attr("aria-label", `已启用 ${active_count} 个筛选条件`);
  $("#library_filter_trigger")
      .toggleClass("active", active_count > 0)
      .attr("data-filter-active", active_count > 0 ? "true" : "false");
}

function update_library_sort_order_labels() {
  const sort_by = $("#library_sort_by").val();
  const current = $("#library_sort_order").val() || "desc";
  if (sort_by === "audit") {
    $("#library_sort_order").html(
        '<option value="desc">降序（问题优先）</option><option value="asc">升序（正常优先）</option>'
    );
  } else {
    $("#library_sort_order").html(
        '<option value="desc">降序</option><option value="asc">升序</option>'
    );
  }
  $("#library_sort_order").val(current);
}

function run_library_subtitle_audit(retry) {
  if (!retry) {
    $("#index-library-subtitle-audit-modal").modal("show");
    $("#index_library_subtitle_audit_summary").html('<div class="text-muted">请选择分类后开始检测。</div>');
    $("#index_library_subtitle_audit_issues").html("");
    $("#index_library_subtitle_audit_server").val(library_default_media_server);
    $("#index_library_subtitle_audit_mode").val("linked");
    $("#index_library_subtitle_audit_deep_confirm").prop("checked", false);
    update_library_subtitle_audit_mode();
    load_library_subtitle_task_limits();
    load_library_subtitle_audit_categories();
    load_library_subtitle_audit_history();
    return;
  }
  const category = $("#index_library_subtitle_audit_category").val();
  const subcategory = $("#index_library_subtitle_audit_subcategory").val();
  const mode = $("#index_library_subtitle_audit_mode").val() || "linked";
  const server = $("#index_library_subtitle_audit_server").val() || library_default_media_server;
  if (mode === "deep" && !$("#index_library_subtitle_audit_deep_confirm").prop("checked")) {
    show_fail_modal("请先确认已了解深度扫描的高 IO 风险");
    $("#index_library_subtitle_audit_deep_confirm").trigger("focus");
    return;
  }
  const payload = {
    category: category,
    subcategory: subcategory,
    server: server,
    mode: mode,
    confirmed: mode === "deep",
    deep_confirmed: mode === "deep"
  };
  if (mode === "deep") {
    const scope = $("#index_library_subtitle_audit_deep_scope").text() || "所选媒体目录";
    show_confirm_modal(
        "深度扫描将遍历：" + scope + "。这会产生持续磁盘读取，确认现在启动？关闭窗口不会停止任务。",
        function () {
          hide_confirm_modal();
          start_library_subtitle_audit(payload);
        }
    );
    return;
  }
  start_library_subtitle_audit(payload);
}

function load_library_subtitle_task_limits() {
  SubtitleTasks.request("GET", "/subtitle/tasks/settings").then(function (response) {
    const policy = SubtitleTasks.payloadOf(response) || {};
    const format = function (value) {
      return Number(value || 0).toLocaleString("zh-CN");
    };
    if (policy.audit_max_minutes !== undefined) {
      $("#index_library_subtitle_audit_limit_minutes").text(format(policy.audit_max_minutes));
    }
    if (policy.audit_max_directories !== undefined) {
      $("#index_library_subtitle_audit_limit_directories").text(format(policy.audit_max_directories));
    }
    if (policy.audit_max_changed !== undefined) {
      $("#index_library_subtitle_audit_limit_changed").text(format(policy.audit_max_changed));
    }
    if (policy.audit_max_issues !== undefined) {
      $("#index_library_subtitle_audit_limit_issues").text(format(policy.audit_max_issues));
    }
  }).catch(function () {
    // Keep the conservative defaults shown in the dialog if settings cannot be read.
  });
}

function update_library_subtitle_audit_mode() {
  const deep = $("#index_library_subtitle_audit_mode").val() === "deep";
  const category = $("#index_library_subtitle_audit_category option:selected").text() || "所选媒体分类";
  const categoryKey = $("#index_library_subtitle_audit_category").val();
  const subcategory = $("#index_library_subtitle_audit_subcategory").val();
  const roots = library_subtitle_audit_roots[categoryKey] || [];
  const targetRoots = roots.map(function (root) {
    return subcategory ? String(root).replace(/[\\\/]$/, "") + "/" + subcategory : String(root);
  });
  $("#index_library_subtitle_audit_deep_scope").text(
      targetRoots.length ? targetRoots.join("；") : (subcategory || category)
  );
  $("#index_library_subtitle_audit_deep_warning").toggleClass("d-none", !deep);
  if (!deep) {
    $("#index_library_subtitle_audit_deep_confirm").prop("checked", false);
  }
}

function render_library_subtitle_audit_task(task) {
  const task_id = task.task_id || task.id || library_subtitle_audit_task_id;
  library_subtitle_audit_task_id = task_id;
  $("#index_library_subtitle_audit_summary").html(`
    <div class="card card-sm">
      <div class="card-body">
        <div id="index_library_subtitle_audit_task_progress" class="subtitle-upload-progress" aria-live="polite">
          <div class="fw-bold mb-1" data-subtitle-progress-title>字幕检测任务</div>
          <div class="progress" role="progressbar" aria-valuemin="0" aria-valuemax="100">
            <div class="progress-bar" data-subtitle-progress-bar style="width:100%"></div>
          </div>
          <div class="small mt-2" data-subtitle-progress-message></div>
          <div class="text-muted small text-break mt-1" data-subtitle-progress-current hidden></div>
        </div>
      </div>
    </div>`);
  SubtitleTasks.renderTaskProgress("#index_library_subtitle_audit_task_progress", task);
  const active = SubtitleTasks.isActive(task);
  $("#index_library_subtitle_audit_retry").prop("disabled", active);
  $("#index_library_subtitle_audit_cancel")
      .toggleClass("d-none", !active || task.status === "canceling")
      .data("task-id", task_id);
}

function finish_library_subtitle_audit_task(task) {
  $("#index_library_subtitle_audit_retry").prop("disabled", false);
  $("#index_library_subtitle_audit_cancel").addClass("d-none");
  const result = (task.result && (task.result.audit || task.result.data)) || task.result || {};
  if (["succeeded", "partial"].indexOf(task.status) !== -1 && result && result.summary) {
    const rendered_result = task.status === "partial"
        ? Object.assign({}, result, {partial: true, coverage_complete: false})
        : result;
    render_library_subtitle_audit(rendered_result);
    if (rendered_result.history) {
      render_library_subtitle_audit_history(rendered_result.history);
    } else {
      load_library_subtitle_audit_history();
    }
    load_library_items(library_page);
    return;
  }
  const label = task.status === "canceled" ? "检测已取消" :
      (task.status === "interrupted" ? "服务重启后检测已标记为中断，请手动重新发起" :
          (task.error || "字幕检测失败"));
  $("#index_library_subtitle_audit_summary").html(
      `<div class="alert ${task.status === "canceled" ? "alert-secondary" : "alert-danger"} mb-0">${library_escape_html(label)}</div>`
  );
}

function start_library_subtitle_audit(payload) {
  const category_name = payload.subcategory || $("#index_library_subtitle_audit_category option:selected").text();
  const retry_btn = $("#index_library_subtitle_audit_retry");
  retry_btn.prop("disabled", true);
  $("#index_library_subtitle_audit_summary").html(
      `<div class="d-flex align-items-center gap-2 text-muted" aria-live="polite"><span class="spinner-border spinner-border-sm"></span><span>正在创建${library_escape_html(category_name)}检测任务...</span></div>`
  );
  $("#index_library_subtitle_audit_issues").html("");
  NProgress.start();
  SubtitleTasks.requestTask("/library/subtitle/audit", payload, {
    onUpdate: render_library_subtitle_audit_task,
    onDone: finish_library_subtitle_audit_task,
    onConnectionError: function () {
      if (!$("#index_library_subtitle_audit_connection_warning").length) {
        $("#index_library_subtitle_audit_summary").prepend(
            '<div id="index_library_subtitle_audit_connection_warning" class="alert alert-warning mb-2">连接暂时中断，后台检测不会因此停止，正在自动重连。</div>'
        );
      }
    }
  }).then(function (created) {
    NProgress.done();
    if (created.legacy) {
      const ret = created.response || {};
      render_library_subtitle_audit(ret);
      render_library_subtitle_audit_history(ret.history || []);
      retry_btn.prop("disabled", false);
      return;
    }
    library_subtitle_audit_task_id = created.task_id;
    render_library_subtitle_audit_task(created.task || {
      task_id: created.task_id,
      type: "audit",
      status: "queued",
      server: payload.server,
      progress: {phase: "queued", percent: 0, message: created.reused ? "已连接到相同范围的现有任务" : "已进入检测队列"}
    });
  }).catch(function (error) {
    NProgress.done();
    retry_btn.prop("disabled", false);
    $("#index_library_subtitle_audit_summary").html(
        `<div class="alert alert-danger mb-0">${library_escape_html((error && error.message) || "无法创建检测任务")}</div>`
    );
  });
}

function library_subtitle_audit_incomplete_reason(record) {
  record = record || {};
  const stop_reason = String(record.stop_reason || "");
  const stop_labels = {
    time_limit: "已达到最长检测时间",
    subtitle_limit: "已达到新增或变化字幕检测上限",
    candidate_limit: "已达到候选字幕数量上限",
    directory_limit: "已达到目录数量上限",
    media_limit: "已达到已链接媒体数量上限",
    scan_error: "部分目录或文件读取失败",
    inaccessible: "部分媒体库目录无法访问",
    canceled: "检测已取消",
    interrupted: "检测因服务重启而中断"
  };
  const inaccessible = record.inaccessible_roots || [];
  const scan_errors = record.scan_errors || [];
  const incomplete = record.partial === true || record.coverage_complete === false
      || !!stop_reason || inaccessible.length > 0 || scan_errors.length > 0;
  if (!incomplete) {
    return "";
  }
  if (stop_reason) {
    return stop_labels[stop_reason] || `检测提前结束（${stop_reason}）`;
  }
  if (scan_errors.length) {
    return stop_labels.scan_error;
  }
  if (inaccessible.length) {
    return stop_labels.inaccessible;
  }
  return "检测未完整覆盖全部目标";
}

function render_library_subtitle_audit(ret) {
  const summary = ret.summary || {};
  const server = library_escape_html(String(ret.server || "").toUpperCase());
  const category = library_escape_html(ret.scope_name || ret.category_name || "");
  const probe_text = ret.probe_available ? "ffprobe 已启用" : "ffprobe 不可用，仅完成基础检查";
  const incomplete_reason = library_subtitle_audit_incomplete_reason(ret);
  let summary_html = `
    <div class="row row-cards">
      <div class="col-6 col-md-3"><div class="card card-sm"><div class="card-body"><div class="text-muted">服务器</div><div class="h3 mb-0">${server}</div></div></div></div>
      <div class="col-6 col-md-3"><div class="card card-sm"><div class="card-body"><div class="text-muted">检测分类</div><div class="h3 mb-0">${category}</div></div></div></div>
      <div class="col-6 col-md-2"><div class="card card-sm"><div class="card-body"><div class="text-muted">字幕总数</div><div class="h3 mb-0">${summary.total || 0}</div></div></div></div>
      <div class="col-6 col-md-1"><div class="card card-sm"><div class="card-body"><div class="text-muted">可识别</div><div class="h3 text-success mb-0">${summary.ok || 0}</div></div></div></div>
      <div class="col-6 col-md-1"><div class="card card-sm"><div class="card-body"><div class="text-muted">需规范</div><div class="h3 text-warning mb-0">${summary.warning || 0}</div></div></div></div>
      <div class="col-6 col-md-2"><div class="card card-sm"><div class="card-body"><div class="text-muted">无法识别</div><div class="h3 text-danger mb-0">${summary.error || 0}</div></div></div></div>
    </div>
    <div class="text-muted small mt-2">${library_escape_html(probe_text)}</div>`;
  if (incomplete_reason) {
    summary_html += `<div class="alert alert-warning mt-3 mb-0"><strong>本次检测未完整覆盖全部目标。</strong>${library_escape_html(incomplete_reason)}；结果仅代表已经完成的范围，不能视为全量通过。</div>`;
  }
  const inaccessible = ret.inaccessible_roots || [];
  if (inaccessible.length) {
    summary_html += `<div class="alert alert-warning mt-3 mb-0">无法访问的媒体库目录：${inaccessible.map(library_escape_html).join("、")}</div>`;
  }
  const scan_errors = ret.scan_errors || [];
  if (scan_errors.length) {
    summary_html += `<div class="alert alert-warning mt-3 mb-0">扫描中有 ${scan_errors.length} 个目录或文件无法读取；首条错误：${library_escape_html(scan_errors[0])}</div>`;
  }
  if (ret.history_warning) {
    summary_html += `<div class="alert alert-warning mt-3 mb-0">${library_escape_html(ret.history_warning)}</div>`;
  }
  $("#index_library_subtitle_audit_summary").html(summary_html);

  const issues = ret.issues || [];
  if (!issues.length) {
    if (incomplete_reason) {
      $("#index_library_subtitle_audit_issues").html('<div class="empty py-4"><p class="empty-title">已检测范围内未发现问题</p><p class="text-muted">由于检测未完整覆盖，未检测范围不能判定为通过。</p></div>');
    } else {
      $("#index_library_subtitle_audit_issues").html('<div class="empty py-4"><p class="empty-title">全部外挂字幕均通过检测</p></div>');
    }
    return;
  }
  let issues_html = '<div class="fw-bold py-2">问题明细</div>';
  issues.forEach(function (issue) {
    const warning = issue.status === "warning";
    const badge = warning ? "bg-yellow-lt text-yellow" : "bg-red-lt text-red";
    const label = warning ? "语言需规范" : "无法识别";
    issues_html += `
      <div class="py-3">
        <div class="d-flex align-items-start gap-3">
          <span class="badge ${badge}">${label}</span>
          <div class="flex-fill min-w-0">
            <div class="fw-bold text-break">${library_escape_html(issue.path || "")}</div>
            <div class="text-muted small mt-1">${library_escape_html(issue.reason || "")}</div>
            ${issue.media_path ? `<div class="text-muted small text-break mt-1">媒体：${library_escape_html(issue.media_path)}</div>` : ""}
          </div>
        </div>
      </div>`;
  });
  if (ret.issues_truncated) {
    issues_html += `<div class="alert alert-warning mt-3">另有 ${ret.issues_truncated} 条问题未在页面展开，请先处理当前明细后重新检测。</div>`;
  }
  $("#index_library_subtitle_audit_issues").html(issues_html);
}

function load_library_subtitle_audit_history() {
  $("#index_library_subtitle_audit_history").html('<div class="text-muted small">正在读取检测记录...</div>');
  const server = $("#index_library_subtitle_audit_server").val() || library_default_media_server;
  $.ajax({
    type: "GET",
    url: "/library/subtitle/audit/history?server=" + encodeURIComponent(server) + "&random=" + Math.random(),
    dataType: "json",
    success: function (ret) {
      if (!ret || ret.code !== 0) {
        $("#index_library_subtitle_audit_history").html(
            `<div class="text-danger small">${library_escape_html((ret && ret.msg) || "读取检测记录失败")}</div>`
        );
        return;
      }
      render_library_subtitle_audit_history(ret.history || []);
    },
    error: function () {
      $("#index_library_subtitle_audit_history").html('<div class="text-danger small">读取检测记录失败</div>');
    }
  });
}

function load_library_subtitle_audit_categories() {
  $.ajax({
    type: "GET",
    url: "/library/subtitle/audit/categories?random=" + Math.random(),
    dataType: "json",
    success: function (ret) {
      if (!ret || ret.code !== 0) {
        library_subtitle_audit_categories = {};
        library_subtitle_audit_roots = {};
      } else {
        library_subtitle_audit_categories = ret.categories || {};
        library_subtitle_audit_roots = ret.roots || {};
      }
      update_library_subtitle_audit_subcategories();
    },
    error: function () {
      library_subtitle_audit_categories = {};
      library_subtitle_audit_roots = {};
      update_library_subtitle_audit_subcategories();
    }
  });
}

function update_library_subtitle_audit_subcategories() {
  const category = $("#index_library_subtitle_audit_category").val();
  const category_name = $("#index_library_subtitle_audit_category option:selected").text();
  const current = $("#index_library_subtitle_audit_subcategory").val();
  const categories = library_subtitle_audit_categories[category] || [];
  let html = `<option value="">全部${library_escape_html(category_name)}</option>`;
  categories.forEach(function (name) {
    const escaped = library_escape_html(name);
    html += `<option value="${escaped}" ${name === current ? "selected" : ""}>${escaped}</option>`;
  });
  $("#index_library_subtitle_audit_subcategory").html(html);
  $("#index_library_subtitle_audit_subcategory").prop("disabled", categories.length === 0);
  update_library_subtitle_audit_mode();
}

function render_library_subtitle_audit_history(history) {
  if (!history.length) {
    $("#index_library_subtitle_audit_history").html('<div class="text-muted small">暂无检测记录</div>');
    return;
  }
  let html = '<div class="row row-cards">';
  history.slice(0, 3).forEach(function (record) {
    const summary = record.summary || {};
    const issues = record.issues || [];
    const incomplete_count = (record.inaccessible_roots || []).length + (record.scan_errors || []).length;
    const incomplete_reason = library_subtitle_audit_incomplete_reason(record);
    let issue_html = "";
    issues.slice(0, 10).forEach(function (issue) {
      issue_html += `<div class="text-muted small text-break mt-1">· ${library_escape_html(issue.path || "")}：${library_escape_html(issue.reason || "")}</div>`;
    });
    const hidden_count = Math.max(issues.length - 10, 0) + (record.issues_truncated || 0);
    if (hidden_count) {
      issue_html += `<div class="text-muted small mt-1">另有 ${hidden_count} 条问题未展开</div>`;
    }
    html += `
      <div class="col-12">
        <div class="card card-sm">
          <div class="card-body">
            <div class="d-flex flex-wrap justify-content-between gap-2">
              <div class="fw-bold">${library_escape_html(record.scope_name || record.category_name || "")} · ${library_escape_html(String(record.server || "").toUpperCase())}</div>
              <div class="text-muted small">${library_escape_html(record.checked_at || "")}</div>
            </div>
            <div class="mt-2">总数 ${summary.total || 0}，<span class="text-success">通过 ${summary.ok || 0}</span>，<span class="text-warning">需规范 ${summary.warning || 0}</span>，<span class="text-danger">无法识别 ${summary.error || 0}</span></div>
            ${incomplete_reason ? `<div class="text-warning small mt-2"><strong>检测未完整覆盖：</strong>${library_escape_html(incomplete_reason)}。${incomplete_count ? `另有 ${incomplete_count} 个路径未完整读取；` : ""}未覆盖范围的旧状态不会被删除。</div>` : ""}
            ${issue_html ? `<details class="mt-2"><summary class="text-muted small">查看问题摘要</summary>${issue_html}</details>` : ""}
          </div>
        </div>
      </div>`;
  });
  html += "</div>";
  $("#index_library_subtitle_audit_history").html(html);
}

function library_upload_block_reason(item) {
  if (!item || item.media_type !== "movie" || item.can_upload) {
    return "";
  }
  if (!item.linked) {
    return "没有整理记录，无法定位目标媒体文件";
  }
  if (!item.target_path) {
    return "整理记录缺少目标媒体文件";
  }
  return "媒体文件不存在或已移动，请检查后重试";
}

function library_upload_block_label(item) {
  if (!item || item.media_type !== "movie" || item.can_upload) {
    return "";
  }
  if (!item.linked) {
    return "没有整理记录";
  }
  return item.target_path ? "媒体文件不存在" : "缺少目标文件";
}

// Same glyph as the header's SVG.dots() macro, which is not available to rendered cards.
var library_dots_icon = '<svg xmlns="http://www.w3.org/2000/svg" class="icon icon-tabler icon-tabler-dots" width="24" height="24" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor" fill="none" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path stroke="none" d="M0 0h24v24H0z" fill="none"></path><circle cx="5" cy="12" r="1"></circle><circle cx="12" cy="12" r="1"></circle><circle cx="19" cy="12" r="1"></circle></svg>';

function library_card_primary(item) {
  const status = item.subtitle_status || "unknown";
  const is_movie = item.media_type === "movie";
  const repairable = is_movie && item.can_upload
      && ["warning", "error"].indexOf(item.subtitle_audit_status) !== -1;
  if (repairable) {
    return {key: "repair", label: "修复", cls: "btn-outline-warning"};
  }
  if (status === "missing_chinese" || status === "unknown") {
    return {key: is_movie ? "online" : "episodes", label: "找字幕", cls: "btn-primary"};
  }
  return {key: "detail", label: "管理", cls: "btn-outline-primary"};
}

function library_card_actions(item, item_id, title) {
  const is_movie = item.media_type === "movie";
  const block_reason = library_upload_block_reason(item);
  const repairable = is_movie && item.can_upload
      && ["warning", "error"].indexOf(item.subtitle_audit_status) !== -1;
  const primary = library_card_primary(item);
  const call = function (name) { return `${name}(&quot;${item_id}&quot;)`; };
  const handlers = {
    detail: call("open_library_drawer"),
    online: call("open_library_online_movie"),
    upload: `open_library_drawer(&quot;${item_id}&quot;, &quot;upload&quot;)`,
    episodes: `open_library_drawer(&quot;${item_id}&quot;, &quot;episodes&quot;)`,
    repair: `repair_library_subtitles(&quot;${item_id}&quot;, this)`
  };
  const blocked = block_reason && (primary.key === "online");
  const reason_attr = block_reason ? ` title="${library_escape_html(block_reason)}"` : "";
  const primary_button = `<button type="button" class="btn btn-sm ${primary.cls} flex-fill" ${blocked ? "disabled" : ""}${blocked ? reason_attr : ""}
      aria-label="${library_escape_html(primary.label)}：${title}" onclick="${handlers[primary.key]}">${primary.label}</button>`;
  // Everything the primary button does not cover stays one click away.
  const entries = [];
  entries.push({key: "detail", label: "打开详情"});
  if (is_movie) {
    entries.push({key: "online", label: "在线字幕", reason: block_reason});
    entries.push({key: "upload", label: "上传字幕", reason: block_reason});
  } else {
    entries.push({key: "episodes", label: "选择剧集"});
  }
  if (repairable) {
    entries.push({key: "repair", label: "二次处理"});
  }
  const menu_items = entries.filter(function (entry) { return entry.key !== primary.key; })
      .map(function (entry) {
        if (entry.reason) {
          return `<button type="button" class="dropdown-item disabled" disabled>${entry.label}<span class="d-block small text-muted">${library_escape_html(entry.reason)}</span></button>`;
        }
        return `<button type="button" class="dropdown-item" onclick="${handlers[entry.key]}">${entry.label}</button>`;
      }).join("");
  return `<div class="lit-library-card-actions">
        ${primary_button}
        <div class="dropdown lit-library-card-menu">
          <button type="button" class="btn btn-sm btn-icon" data-bs-toggle="dropdown" aria-expanded="false"
                  aria-label="更多操作：${title}" title="更多操作">${library_dots_icon}</button>
          <div class="dropdown-menu dropdown-menu-end">${menu_items}</div>
        </div>
      </div>`;
}

function library_item_card(item, index) {
  const item_id = library_escape_js(item.id);
  const title = library_escape_html(item.title || item.original_title || "未命名媒体");
  const year = item.year ? ` · ${library_escape_html(item.year)}` : "";
  const poster = library_escape_html(item.poster_url || "");
  const type_initial = library_escape_html((item.media_type_name || "媒").substring(0, 1));
  const subtitle_label = library_escape_html(item.subtitle_label || "未检测");
  const subtitle_checked_at = library_escape_html(item.subtitle_status_checked_at || "");
  const subtitle_source = library_escape_html(item.subtitle_status_source || "");
  const subtitle_state_title = subtitle_checked_at
      ? `状态来源：${subtitle_source || "快照"}；检查时间：${subtitle_checked_at}`
      : "尚无媒体级字幕状态快照";
  const subtitle_audit_label = library_escape_html(item.subtitle_audit_label || "");
  const subtitle_audit_badge = library_escape_html(item.subtitle_audit_badge || "");
  const subtitle_audit_checked_at = library_escape_html(item.subtitle_audit_checked_at || "");
  const subtitle_audit_count = Math.max(parseInt(item.subtitle_audit_count || 0, 10), 0);
  const subtitle_audit_text = subtitle_audit_count > 1
      ? `${subtitle_audit_label} · ${subtitle_audit_count} 条`
      : subtitle_audit_label;
  const type_name = library_escape_html(item.media_type_name);
  const category = library_escape_html(item.category || "未分类");
  const eager = index < library_eager_poster_count;

  const sub_status = item.subtitle_status || "unknown";
  let dot_class = "unknown";
  let indicator_class = "unknown";
  if (sub_status === "has_chinese_external") {
    dot_class = "external";
    indicator_class = "external";
  } else if (sub_status === "has_chinese_internal") {
    dot_class = "internal";
    indicator_class = "internal";
  } else if (sub_status === "missing_chinese") {
    dot_class = "missing";
    indicator_class = "missing-chinese";
  }

  const is_movie = item.media_type === "movie";
  const episode_total = Math.max(parseInt(item.episode_total || 0, 10), 0);
  const episode_covered = Math.min(Math.max(parseInt(item.episode_covered || 0, 10), 0), episode_total);
  const coverage_text = `字幕已覆盖 ${episode_covered}/${episode_total} 集`;
  const coverage_bar = !is_movie && episode_total > 0
      ? `<span class="lit-library-card-coverage" role="progressbar" aria-label="${coverage_text}" aria-valuemin="0" aria-valuemax="${episode_total}" aria-valuenow="${episode_covered}" title="${coverage_text}"><span style="width:${Math.round(episode_covered * 100 / episode_total)}%"></span></span>`
      : "";
  const poster_attrs = poster
      ? `${eager ? `src="${poster}"` : `data-src="${poster}"`} alt="" loading="${eager ? "eager" : "lazy"}" fetchpriority="${eager ? "high" : "low"}" decoding="async"`
      : `alt=""`;
  return `
    <div class="lit-library-card">
      <button type="button" class="lit-library-card-poster${poster ? "" : " no-poster"}" data-type="${library_escape_html(item.media_type || "")}"
              aria-label="查看 ${title} 的字幕详情" onclick="open_library_drawer(&quot;${item_id}&quot;)">
        <img ${poster_attrs}
             onerror="this.style.display='none';this.parentNode.classList.add('no-poster')">
        <span class="lit-library-card-fallback">${type_initial}</span>
        <span class="lit-library-card-type-badge">${type_name}</span>
        <span class="lit-library-card-subtitle-indicator ${indicator_class}"></span>
        ${coverage_bar}
      </button>
      <div class="lit-library-card-body">
        <div class="lit-library-card-title" title="${title}">${title}</div>
        <div class="lit-library-card-meta-line">
          <span class="lit-library-card-meta">${category}${year}</span>
        </div>
        <div class="lit-library-card-subtitle-line">
          <span class="dot ${dot_class}"></span>
          <span class="lit-library-card-meta" title="${subtitle_state_title}">${subtitle_label}</span>
        </div>
        ${coverage_bar && item.subtitle_status !== "unknown" ? `<div class="lit-library-card-meta">${coverage_text}</div>` : ""}
        ${library_upload_block_label(item) ? `<div class="lit-library-card-meta text-warning">不可上传：${library_upload_block_label(item)}</div>` : ""}
        ${is_movie && subtitle_audit_label ? `
        <div class="lit-library-card-audit">
          <span class="badge ${subtitle_audit_badge} lit-library-card-audit-badge" title="最近检测：${subtitle_audit_checked_at}">${subtitle_audit_text}</span>
        </div>` : ""}
      </div>
      <div class="lit-library-card-footer">
        ${library_card_actions(item, item_id, title)}
      </div>
    </div>`;
}

function repair_library_subtitles(item_id, button) {
  const item = library_lookup_item(item_id);
  if (!item || !item.target_path) {
    show_fail_modal("未找到可处理的媒体文件");
    return;
  }
  show_confirm_modal(
      "将按全局影视服务器规则规范化现有外挂字幕。多来源字幕会保留为独立轨道，不会相互覆盖。是否继续？",
      function () {
        hide_confirm_modal();
        const repair_button = $(button);
        const original_html = repair_button.html();
        repair_button.prop("disabled", true).html(
            '<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>正在创建任务'
        );
        NProgress.start();
        SubtitleTasks.requestTask("/library/subtitle/repair", {
          media_path: item.target_path,
          server: item.server || library_default_media_server,
          server_item_id: item.server_item_id || "",
          parent_server_item_id: item.parent_server_item_id || "",
          library_id: item.library_id || "",
          refresh_context: {
            server_item_id: item.server_item_id || "",
            parent_server_item_id: item.parent_server_item_id || "",
            library_id: item.library_id || ""
          }
        }, {
          onUpdate: function (task) {
            const progress = task.progress || {};
            repair_button.html(
                `<span class="spinner-border spinner-border-sm me-1" aria-hidden="true"></span>${library_escape_html(progress.message || "后台处理中")}`
            );
          },
          onDone: function (task) {
            repair_button.prop("disabled", false).html(original_html);
            if (["succeeded", "partial"].indexOf(task.status) !== -1) {
              show_success_modal(task.status === "partial" ? "外挂字幕二次处理部分完成，请在任务中心查看明细" : "外挂字幕二次处理完成");
              load_library_items(library_page);
            } else if (task.status !== "canceled") {
              show_fail_modal(task.error || "外挂字幕二次处理未完成，请在任务中心查看明细");
            }
          },
          onConnectionError: function () {
            repair_button.html("连接中断，后台继续");
          }
        }).then(function (created) {
          NProgress.done();
          if (created.legacy) {
            repair_button.prop("disabled", false).html(original_html);
            show_success_modal(SubtitleTasks.responseMessage(created.response, "外挂字幕二次处理完成"));
            load_library_items(library_page);
            return;
          }
          repair_button.html(created.reused ? "已连接现有任务" : "后台处理中");
          SubtitleTasks.refreshList(true).catch(function () {});
        }).catch(function (error) {
          NProgress.done();
          repair_button.prop("disabled", false).html(original_html);
          show_fail_modal((error && error.message) || "无法创建二次处理任务");
        });
      }
  );
}

function init_library_posters() {
  if (library_poster_observer) {
    library_poster_observer.disconnect();
  }
  const lazy_images = document.querySelectorAll("#library_items_grid img[data-src]");
  if (!("IntersectionObserver" in window)) {
    lazy_images.forEach(activate_library_poster);
    return;
  }
  library_poster_observer = new IntersectionObserver(function (entries, observer) {
    entries.forEach(function (entry) {
      if (entry.isIntersecting) {
        activate_library_poster(entry.target);
        observer.unobserve(entry.target);
      }
    });
  }, {rootMargin: "320px 0px"});
  lazy_images.forEach(function (img) {
    library_poster_observer.observe(img);
  });
}

function activate_library_poster(img) {
  const src = img.getAttribute("data-src");
  if (!src) {
    return;
  }
  img.setAttribute("src", src);
  img.removeAttribute("data-src");
}

function library_lookup_item(item_id) {
  // The drawer outlives list reloads: an item that left the filtered page must stay usable.
  return library_items_cache[item_id] || (
      library_drawer_item && String(library_drawer_item.id) === String(item_id) ? library_drawer_item : null);
}

function library_drawer_element() {
  return typeof document.getElementById === "function" ? document.getElementById("library-drawer") : null;
}

function library_drawer_instance() {
  const element = library_drawer_element();
  if (!element || !window.bootstrap || !window.bootstrap.Offcanvas) {
    return null;
  }
  return window.bootstrap.Offcanvas.getOrCreateInstance(element);
}

function library_drawer_is_open() {
  const element = library_drawer_element();
  return !!(element && element.classList && element.classList.contains("show"));
}

function library_drawer_show() {
  const drawer = library_drawer_instance();
  if (drawer) { drawer.show(); }
}

function library_drawer_hide() {
  const drawer = library_drawer_instance();
  if (drawer) { drawer.hide(); }
}

// Modal dialogs and the offcanvas must not trap focus together: close the drawer while a
// dialog is open and bring it back when that dialog closes.
function library_drawer_hide_for_modal() {
  if (library_drawer_is_open() && library_drawer_item) {
    library_drawer_resume_id = String(library_drawer_item.id);
    library_drawer_hide();
  }
}

function library_drawer_resume() {
  const item_id = library_drawer_resume_id;
  library_drawer_resume_id = "";
  if (item_id && library_drawer_item && String(library_drawer_item.id) === item_id) {
    library_drawer_show();
  }
}

function library_audio_language_label(code) {
  return {zho: "中文", eng: "英语", fra: "法语", rus: "俄语", jpn: "日语", kor: "韩语", other: "其他语言", unknown: "未知"}[code] || "";
}

function render_library_drawer_overview(item) {
  const is_movie = item.media_type === "movie";
  const badge = function (cls, label, extra) {
    return `<span class="badge ${library_escape_html(cls)} ${extra || ""}">${library_escape_html(label)}</span>`;
  };
  const rows = [];
  rows.push(`<div class="d-flex flex-wrap gap-2 align-items-center">${badge(item.subtitle_badge || "bg-secondary", item.subtitle_label || "未检测", "text-white")}${
      item.subtitle_status_checked_at ? `<span class="text-muted small">检查于 ${library_escape_html(item.subtitle_status_checked_at)}</span>` : ""}</div>`);
  if (is_movie && item.subtitle_audit_label) {
    rows.push(`<div class="d-flex flex-wrap gap-2 align-items-center">${badge(item.subtitle_audit_badge || "bg-secondary-lt", item.subtitle_audit_label)}${
        item.subtitle_audit_checked_at ? `<span class="text-muted small">最近检测 ${library_escape_html(item.subtitle_audit_checked_at)}</span>` : ""}</div>`);
  }
  const episode_total = Math.max(parseInt(item.episode_total || 0, 10), 0);
  if (!is_movie && episode_total > 0) {
    const covered = Math.min(Math.max(parseInt(item.episode_covered || 0, 10), 0), episode_total);
    rows.push(`<div class="small">字幕已覆盖 ${covered}/${episode_total} 集${item.missing_count ? `，缺中文字幕 ${parseInt(item.missing_count, 10)} 集` : ""}</div>`);
  }
  const language = is_movie ? library_audio_language_label(item.audio_language) : "";
  if (language) {
    rows.push(`<div class="small text-muted">默认音轨语言：${language}</div>`);
  }
  const location = item.target_path || item.path || "";
  if (location) {
    rows.push(`<div class="small text-muted text-break">位置：${library_escape_html(location)}</div>`);
  }
  const reason = library_upload_block_reason(item);
  const item_id = library_escape_js(item.id);
  const buttons = [];
  if (is_movie) {
    buttons.push(`<button type="button" class="btn btn-sm btn-primary" ${reason ? `disabled title="${library_escape_html(reason)}"` : ""} onclick="open_library_online_movie(&quot;${item_id}&quot;)">在线字幕</button>`);
    if (item.can_upload && ["warning", "error"].indexOf(item.subtitle_audit_status) !== -1) {
      buttons.push(`<button type="button" class="btn btn-sm btn-outline-warning" onclick="repair_library_subtitles(&quot;${item_id}&quot;, this)">二次处理</button>`);
    }
  }
  if (buttons.length) {
    rows.push(`<div class="d-flex flex-wrap gap-2">${buttons.join("")}</div>`);
  }
  $("#library_drawer_overview").html(`<div class="d-grid gap-2">${rows.join("")}</div>`);
}

// A list reload may change the item's status; keep the open drawer in step.
function library_refresh_drawer_item() {
  if (!library_drawer_item) {
    return;
  }
  const fresh = library_items_cache[library_drawer_item.id];
  if (fresh) {
    library_drawer_item = fresh;
    render_library_drawer_overview(fresh);
  }
}

function open_library_drawer(item_id, section) {
  const item = library_lookup_item(item_id);
  if (!item) {
    show_fail_modal("未找到媒体项目");
    return;
  }
  const same = library_drawer_item && String(library_drawer_item.id) === String(item.id);
  if (library_subtitle_upload_request && !same) {
    show_fail_modal("字幕正在上传到暂存区，请等待上传完成后再打开其他媒体");
    return;
  }
  library_drawer_item = item;
  const is_movie = item.media_type === "movie";
  const year = item.year ? ` · ${item.year}` : "";
  $("#library_drawer_title").text(item.title || item.original_title || "未命名媒体");
  $("#library_drawer_meta").text(`${item.media_type_name || ""} · ${item.category || "未分类"}${year}`);
  render_library_drawer_overview(item);
  $("#library_drawer_episodes").toggleClass("d-none", is_movie);
  if (is_movie) {
    if (!same) {
      library_set_upload_target(item.title, item.target_path, {
        server: item.server || library_default_media_server,
        server_item_id: item.server_item_id || "",
        parent_server_item_id: item.parent_server_item_id || "",
        library_id: item.library_id || ""
      }, library_upload_block_reason(item));
    }
  } else if (!same) {
    library_current_series_id = item.id;
    library_current_series_title = item.title || "";
    library_episodes_cache = [];
    $("#library_episode_season,#library_episode_number").empty();
    library_set_upload_target("", "", {}, "请先在上方剧集列表中选择要上传字幕的一集");
    load_library_episodes(item.id);
  }
  library_drawer_show();
  if (section === "upload" || section === "episodes") {
    library_drawer_scroll_to(section === "upload" ? "library_drawer_upload" : "library_drawer_episodes");
  }
}

function library_drawer_scroll_to(element_id) {
  const element = typeof document.getElementById === "function" ? document.getElementById(element_id) : null;
  if (element && typeof element.scrollIntoView === "function") {
    element.scrollIntoView({block: "start"});
  }
}

function open_library_movie_upload(item_id) {
  open_library_drawer(item_id, "upload");
}

function open_library_episodes(item_id) {
  open_library_drawer(item_id, "episodes");
}

function load_library_episodes(item_id) {
  const item = library_lookup_item(item_id);
  if (!item) {
    return;
  }
  const previous_season = $("#library_episode_season").val() || "";
  const previous_episode = $("#library_episode_number").val() || "";
  $("#index_library_episodes_body").html('<div class="text-muted py-3">正在加载剧集...</div>');
  library_post_json("/library/episodes", {
    item_id: item_id,
    server: item.server || library_default_media_server,
    server_item_id: item.server_item_id || "",
    library_id: item.library_id || ""
  }, function (ret) {
    if (ret.code !== 0) {
      $("#index_library_episodes_body").html(`<div class="text-danger py-3">${library_escape_html(ret.msg || "剧集加载失败")}</div>`);
      return;
    }
    if (library_current_series_id !== item_id) { return; }
    library_episodes_cache = (ret.items || []).map(library_fill_episode_numbers);
    const seasons = Array.from(new Set(library_episodes_cache.map(function (episode) { return String(episode.season); })))
        .sort(function (a, b) { return Number(a) - Number(b); });
    const season_select = $("#library_episode_season").empty().append($("<option>").val("").text("全部季"));
    // A single season is selected for the user, which is what season-pack upload needs.
    seasons.forEach(function (season) {
      if (season !== "" && season !== "undefined") {
        season_select.append($("<option>").val(season).text(`第 ${season} 季`));
      }
    });
    const real_seasons = seasons.filter(function (season) { return season !== "" && season !== "undefined"; });
    if (previous_season && real_seasons.indexOf(previous_season) !== -1) {
      season_select.val(previous_season);
    } else if (real_seasons.length === 1) {
      season_select.val(real_seasons[0]);
    }
    filter_library_episodes(true);
    if (previous_episode && $("#library_episode_number option").filter(function () { return this.value === previous_episode; }).length) {
      $("#library_episode_number").val(previous_episode);
      filter_library_episodes(false);
    }
  });
}

function library_fill_episode_numbers(episode) {
  episode = Object.assign({}, episode);
  const basename = String(episode.path || "").replace(/\\/g, "/").split("/").pop();
  const match = basename.match(/(?:^|[^a-z0-9])S(\d{1,3})[ ._-]*E(\d{1,4})(?!\d)/i);
  if (match) {
    if (episode.season === "" || episode.season == null) { episode.season = Number(match[1]); }
    if (episode.episode === "" || episode.episode == null) { episode.episode = Number(match[2]); }
  }
  if (episode.season !== "" && episode.season != null && episode.episode !== "" && episode.episode != null) {
    episode.season_episode = `S${String(episode.season).padStart(2, "0")}E${String(episode.episode).padStart(2, "0")}`;
  }
  return episode;
}

function filter_library_episodes(reset_episode) {
  const season = $("#library_episode_season").val();
  if (reset_episode) {
    const number_select = $("#library_episode_number").empty().append($("<option>").val("").text("全部集"));
    const numbers = Array.from(new Set(library_episodes_cache.filter(function (episode) {
      return !season || String(episode.season) === season;
    }).map(function (episode) { return String(episode.episode); }))).sort(function (a, b) { return Number(a) - Number(b); });
    numbers.forEach(function (number) {
      if (number !== "" && number !== "undefined") { number_select.append($("<option>").val(number).text(`第 ${number} 集`)); }
    });
  }
  render_library_episodes(library_episodes_cache);
}

function render_library_episodes(episodes) {
  library_episodes_cache = episodes || [];
  if (!episodes.length) {
    $("#index_library_episodes_body").html('<div class="empty"><p class="empty-title">暂无剧集</p><p class="empty-subtitle text-muted">媒体服务器未返回可上传字幕的剧集文件。</p></div>');
    return;
  }
  let html = "";
  episodes.forEach(function (episode, index) {
    const title = library_escape_html(episode.title || episode.season_episode || "未命名剧集");
    const selected_season = $("#library_episode_season").val();
    const selected_episode = $("#library_episode_number").val();
    if ((selected_season && String(episode.season) !== selected_season)
        || (selected_episode && String(episode.episode) !== selected_episode)) { return; }
    const path = library_escape_html(episode.path || "");
    const disabled = episode.can_upload ? "" : "disabled";
    const button = `<button type="button" class="btn btn-sm btn-outline-primary me-2" ${disabled} onclick="open_library_online_episode(${index})">检索本集字幕</button><button type="button" class="btn btn-sm btn-primary" ${disabled} onclick="open_library_episode_upload(${index})">上传字幕</button>`;
    html += `
      <div class="py-3" data-episode-index="${index}">
        <input type="hidden" class="library-episode-path" value="${path}">
        <input type="hidden" class="library-episode-title" value="${title}">
        <div class="row align-items-center">
          <div class="col">
            <div class="fw-bold">${library_escape_html(episode.season_episode || "")} ${title}</div>
            <div class="text-muted small text-truncate" title="${path}">${path}</div>
          </div>
          <div class="col-auto">
            <span class="badge ${library_escape_html(episode.subtitle_badge || "bg-secondary")}">${library_escape_html(episode.subtitle_label || "未检测")}</span>
          </div>
          <div class="col-auto">${button}</div>
        </div>
      </div>`;
  });
  $("#index_library_episodes_body").html(html || '<p class="text-muted py-3">当前季集没有可用的媒体文件。</p>');
}

function open_library_episode_upload(index) {
  const episode = library_episodes_cache[index] || {};
  const row = $(`#index_library_episodes_body [data-episode-index="${index}"]`);
  const path = episode.path || row.find(".library-episode-path").val();
  const title = episode.title || row.find(".library-episode-title").val();
  if (!path) {
    show_fail_modal("未找到可上传字幕的剧集文件");
    return;
  }
  const series = library_lookup_item(library_current_series_id) || {};
  const label = [episode.season_episode, title].filter(Boolean).join(" ");
  library_set_upload_target(label || title, path, {
    server: episode.server || series.server || library_default_media_server,
    server_item_id: episode.server_item_id || "",
    parent_server_item_id: episode.parent_server_item_id || series.server_item_id || "",
    library_id: episode.library_id || series.library_id || ""
  });
  library_drawer_scroll_to("library_drawer_upload");
  const input = document.getElementById("index_upload_subtitle_file");
  if (input && typeof input.focus === "function") { input.focus(); }
}

// Points the drawer's upload form at one media file; blocked_reason disables it with an explanation.
function library_set_upload_target(name, media_path, metadata, blocked_reason) {
  metadata = metadata || {};
  if (library_subtitle_upload_request) {
    library_drawer_show();
    return;
  }
  $("#index_upload_subtitle_path").val(media_path || "");
  $("#index_upload_subtitle_target_path").val(media_path || "");
  $("#index_upload_subtitle_request_id").val(SubtitleTasks.randomRequestId());
  $("#index_upload_subtitle_server_item_id").val(metadata.server_item_id || "");
  $("#index_upload_subtitle_parent_server_item_id").val(metadata.parent_server_item_id || "");
  $("#index_upload_subtitle_library_id").val(metadata.library_id || "");
  $("#index_upload_subtitle_name").val(name || media_path || "");
  $("#index_upload_subtitle_file").val("");
  $("#index_upload_subtitle_align").val("none");
  $("#index_upload_subtitle_server").val(metadata.server || library_default_media_server);
  $("#index_upload_subtitle_progress").addClass("d-none");
  $("#index_upload_subtitle_cancel").addClass("d-none").removeData("task-id");
  library_subtitle_upload_task_id = "";
  const blocked = !media_path || !!blocked_reason;
  $("#index_upload_subtitle_btn").prop("disabled", blocked).text("上传");
  $("#index_upload_subtitle_file,#index_upload_subtitle_align,#index_upload_subtitle_server").prop("disabled", blocked);
  $("#library_upload_dropzone").toggleClass("is-disabled", blocked).attr("aria-disabled", blocked ? "true" : "false");
  $("#library_upload_blocked").text(blocked_reason || "").toggleClass("d-none", !blocked_reason);
  render_library_pending_files();
}

function library_format_bytes(value) {
  return typeof SubtitleTasks !== "undefined" && SubtitleTasks.formatBytes
      ? SubtitleTasks.formatBytes(value) : `${value} B`;
}

function library_pending_files() {
  const input = typeof document.getElementById === "function" ? document.getElementById("index_upload_subtitle_file") : null;
  return input ? Array.from(input.files || []) : [];
}

// Replaces the file input's selection; returns false where DataTransfer cannot build a FileList.
function library_set_pending_files(files) {
  const input = document.getElementById("index_upload_subtitle_file");
  try {
    const transfer = new DataTransfer();
    files.forEach(function (file) { transfer.items.add(file); });
    input.files = transfer.files;
  } catch (error) {
    return false;
  }
  render_library_pending_files();
  return true;
}

function remove_library_pending_file(index) {
  if (library_subtitle_upload_request) { return; }
  const files = library_pending_files();
  files.splice(index, 1);
  if (!library_set_pending_files(files)) {
    $("#index_upload_subtitle_file").val("");
    render_library_pending_files();
  }
}

function render_library_pending_files(options) {
  const files = library_pending_files();
  const box = $("#library_upload_pending");
  library_upload_preview_generation += 1;
  if (!files.length) {
    box.empty();
    return;
  }
  const validation = SubtitleTasks.validateFiles(files, $("#index_upload_subtitle_align").val() || "none");
  let html = validation.ok ? "" : `<div class="alert alert-warning py-2">${library_escape_html(validation.message)}</div>`;
  html += '<ul class="list-unstyled mb-0 lit-library-pending">';
  files.forEach(function (file, index) {
    const name = library_escape_html(file.name);
    html += `<li class="lit-library-pending-row"><div class="d-flex gap-2 align-items-start">
      <div class="flex-fill min-w-0"><div class="fw-bold text-break">${name}</div>
      <div class="small text-muted">${library_escape_html(library_format_bytes(file.size || 0))}<span data-library-preview="${index}"></span></div></div>
      <button type="button" class="btn btn-sm" aria-label="移除 ${name}" onclick="remove_library_pending_file(${index})">移除</button>
      </div></li>`;
  });
  html += '</ul><div class="small text-muted mt-2 d-none" id="library_upload_preview_note">预计语言与文件名仅依据文件名推断，实际以处理结果为准。</div>';
  box.html(html);
  if (!options || options.preview !== false) {
    request_library_upload_preview(files);
  }
}

function library_language_label(code) {
  const labels = {"zh-CN": "简体中文", "zh-TW": "繁体中文", "eng": "英文"};
  return labels[code] || code || "";
}

function request_library_upload_preview(files) {
  const view = library_view_instance;
  const pending = document.getElementById("library_upload_pending");
  const media_path = $("#index_upload_subtitle_target_path").val();
  if (!view || !pending || !media_path || !files.length) { return; }
  const generation = library_upload_preview_generation;
  $.ajax({
    type: "POST", url: "/library/subtitle/preview", contentType: "application/json", dataType: "json", timeout: 15000,
    data: JSON.stringify({
      media_path: media_path,
      filenames: files.map(function (file) { return file.name; }),
      server: $("#index_upload_subtitle_server").val() || library_default_media_server
    }),
    success: function (ret) {
      // Script reloads reset the counter; it only identifies a request within
      // one page instance, never a preview belonging to a restored page.
      if (view !== library_view_instance || document.getElementById("library_upload_pending") !== pending
          || generation !== library_upload_preview_generation
          || $("#index_upload_subtitle_target_path").val() !== media_path || !ret || ret.code !== 0) { return; }
      (ret.items || []).forEach(function (row, index) {
        const text = row.target
            ? ` · 预计${library_language_label(row.language) ? ` ${library_language_label(row.language)}` : ""} → ${row.target}${row.note ? `（${row.note}）` : ""}`
            : (row.note ? ` · ${row.note}` : "");
        $(`[data-library-preview="${index}"]`).text(text);
      });
      $("#library_upload_preview_note").removeClass("d-none");
    }
  });
}

function library_clear_pending_files() {
  $("#index_upload_subtitle_file").val("");
  render_library_pending_files();
}

function library_drag_has_files(event) {
  const types = event && event.dataTransfer && event.dataTransfer.types;
  return !!types && Array.prototype.indexOf.call(types, "Files") !== -1;
}

// Dropping is accepted only inside the drawer's upload area; elsewhere the browser must not
// navigate to the file. The window listeners are installed once and ignore other pages.
function init_library_dropzone() {
  const zone = typeof document.getElementById === "function" ? document.getElementById("library_upload_dropzone") : null;
  if (!zone) {
    return;
  }
  const disabled = () => zone.classList.contains("is-disabled") || !!library_subtitle_upload_request;
  ["dragenter", "dragover"].forEach(function (type) {
    zone.addEventListener(type, function (event) {
      if (!library_drag_has_files(event)) { return; }
      event.preventDefault();
      event.dataTransfer.dropEffect = disabled() ? "none" : "copy";
      zone.classList.toggle("is-dragover", !disabled());
    });
  });
  zone.addEventListener("dragleave", function (event) {
    if (!zone.contains(event.relatedTarget)) { zone.classList.remove("is-dragover"); }
  });
  zone.addEventListener("drop", function (event) {
    if (!library_drag_has_files(event)) { return; }
    event.preventDefault();
    event.stopPropagation();
    zone.classList.remove("is-dragover");
    if (disabled()) { return; }
    const dropped = Array.from(event.dataTransfer.files || []);
    if (!dropped.length) { return; }
    const merged = library_pending_files();
    dropped.forEach(function (file) {
      if (!merged.some(function (existing) { return existing.name === file.name; })) { merged.push(file); }
    });
    if (!library_set_pending_files(merged)) {
      document.getElementById("index_upload_subtitle_file").files = event.dataTransfer.files;
      render_library_pending_files();
    }
  });
  if (!window.library_dnd_listener_installed && typeof window.addEventListener === "function") {
    ["dragover", "drop"].forEach(function (type) {
      window.addEventListener(type, function (event) {
        const current = document.getElementById("library_upload_dropzone");
        if (!current || !library_view_instance || !library_drag_has_files(event) || current.contains(event.target)) {
          return;
        }
        event.preventDefault();
        if (event.dataTransfer) { event.dataTransfer.dropEffect = "none"; }
      });
    });
    window.library_dnd_listener_installed = true;
  }
}

function render_library_upload_task(task) {
  library_subtitle_upload_task_id = task.task_id || task.id || library_subtitle_upload_task_id;
  $("#index_upload_subtitle_progress").removeClass("d-none");
  SubtitleTasks.renderTaskProgress("#index_upload_subtitle_progress", task);
  const active = SubtitleTasks.isActive(task);
  $("#index_upload_subtitle_cancel")
      .toggleClass("d-none", !active || task.status === "canceling")
      .data("task-id", library_subtitle_upload_task_id);
  $("#index_upload_subtitle_btn").prop("disabled", active).text(active ? "后台处理中" : "再次上传");
}

function finish_library_upload_task(task) {
  render_library_upload_task(task);
  $("#index_upload_subtitle_request_id").val(SubtitleTasks.randomRequestId());
  $("#index_upload_subtitle_cancel").addClass("d-none");
  $("#index_upload_subtitle_btn").prop("disabled", false).text("再次上传");
  if (["succeeded", "partial"].indexOf(task.status) !== -1) {
    library_clear_pending_files();
    load_library_items(library_page);
    if (library_drawer_is_open() && library_current_series_id
        && library_drawer_item && library_drawer_item.media_type !== "movie") {
      load_library_episodes(library_current_series_id);
    }
  }
}

function init_media_library_page(options) {
  // A previous view's requests may still be in flight; they must not own the
  // new view's loading flags or pending page after navigation/restoration.
  library_view_instance = {};
  library_items_loading = false;
  library_pending_page = null;
  // Session state belongs to the previous DOM; a re-initialised page starts clean.
  library_fill = null;
  library_fill_loading = false;
  library_drawer_item = null;
  library_drawer_resume_id = "";
  library_online_serialized = false;
  library_refreshing = false;
  options = options || {};
  SubtitleTasks.init();
  library_default_media_server = (options.default_server || window.CURRENT_MEDIA_SERVER_TYPE || library_default_media_server || "emby").toLowerCase();
  if (["emby", "jellyfin", "plex"].indexOf(library_default_media_server) === -1) {
    library_default_media_server = "emby";
  }
  if (options.page_size) {
    library_page_size = options.page_size;
  }
  $("#index_upload_subtitle_btn").unbind("click").click(function () {
    const files = Array.from($("#index_upload_subtitle_file")[0].files || []);
    if (!files.length) {
      show_fail_modal("请选择字幕文件");
      return;
    }
    const align_mode = $("#index_upload_subtitle_align").val() || "none";
    const validation = SubtitleTasks.validateFiles(files, align_mode);
    if (!validation.ok) {
      show_fail_modal(validation.message);
      return;
    }
    const upload_button = $("#index_upload_subtitle_btn");
    let form_data = new FormData();
    form_data.append("path", $("#index_upload_subtitle_path").val());
    form_data.append("target_path", $("#index_upload_subtitle_target_path").val());
    form_data.append("server", $("#index_upload_subtitle_server").val());
    form_data.append("align", align_mode);
    form_data.append("request_id", $("#index_upload_subtitle_request_id").val() || SubtitleTasks.randomRequestId());
    form_data.append("server_item_id", $("#index_upload_subtitle_server_item_id").val() || "");
    form_data.append("parent_server_item_id", $("#index_upload_subtitle_parent_server_item_id").val() || "");
    form_data.append("library_id", $("#index_upload_subtitle_library_id").val() || "");
    form_data.append("refresh_context", JSON.stringify({
      server_item_id: $("#index_upload_subtitle_server_item_id").val() || "",
      parent_server_item_id: $("#index_upload_subtitle_parent_server_item_id").val() || "",
      library_id: $("#index_upload_subtitle_library_id").val() || ""
    }));
    files.forEach(function (file) {
      form_data.append("file", file);
    });
    upload_button.prop("disabled", true).text("上传中");
    $("#index_upload_subtitle_cancel").removeClass("d-none").removeData("task-id").text("停止上传");
    $("#index_upload_subtitle_progress").removeClass("d-none");
    SubtitleTasks.renderProgress("#index_upload_subtitle_progress", {
      title: "正在上传到暂存区",
      message: `${validation.logicalCount} 个逻辑字幕，共 ${SubtitleTasks.formatBytes(validation.totalBytes)}`,
      percent: 0
    });
    NProgress.start();
    const submission_request_id = $("#index_upload_subtitle_request_id").val();
    const upload_request = SubtitleTasks.upload(form_data, {
      onUploadProgress: function (progress) {
        if ($("#index_upload_subtitle_request_id").val() !== submission_request_id) {
          return;
        }
        SubtitleTasks.renderProgress("#index_upload_subtitle_progress", {
          title: "正在上传到暂存区",
          message: `${SubtitleTasks.formatBytes(progress.loaded)} / ${SubtitleTasks.formatBytes(progress.total)}`,
          percent: progress.percent
        });
      },
      onUpdate: function (task) {
        if ($("#index_upload_subtitle_request_id").val() === submission_request_id) {
          render_library_upload_task(task);
        }
      },
      onDone: function (task) {
        if ($("#index_upload_subtitle_request_id").val() === submission_request_id) {
          finish_library_upload_task(task);
        } else if (["succeeded", "partial"].indexOf(task.status) !== -1) {
          load_library_items(library_page);
        }
      },
      onConnectionError: function () {
        if ($("#index_upload_subtitle_request_id").val() !== submission_request_id) {
          return;
        }
        SubtitleTasks.renderProgress("#index_upload_subtitle_progress", {
          title: "任务连接暂时中断",
          message: "后台处理不会因此停止，正在自动重连。",
          percent: null
        });
      }
    });
    library_subtitle_upload_request = upload_request;
    upload_request.then(function (created) {
      NProgress.done();
      if (library_subtitle_upload_request === upload_request) {
        library_subtitle_upload_request = null;
      }
      if ($("#index_upload_subtitle_request_id").val() !== submission_request_id) {
        return;
      }
      if (created.legacy) {
        SubtitleTasks.renderProgress("#index_upload_subtitle_progress", {
          title: "字幕处理完成",
          message: SubtitleTasks.responseMessage(created.response, "字幕上传成功"),
          percent: 100
        });
        upload_button.prop("disabled", false).text("再次上传");
        $("#index_upload_subtitle_cancel").addClass("d-none");
        $("#index_upload_subtitle_request_id").val(SubtitleTasks.randomRequestId());
        library_clear_pending_files();
        load_library_items(library_page);
        return;
      }
      library_subtitle_upload_task_id = created.task_id;
      $("#index_upload_subtitle_cancel").text("取消任务").data("task-id", created.task_id);
      render_library_upload_task(created.task || {
        task_id: created.task_id,
        type: "upload",
        status: "queued",
        progress: {phase: "queued", percent: 0, message: created.reused ? "已连接到现有上传任务" : "上传完成，已进入后台队列"}
      });
    }).catch(function (error) {
      NProgress.done();
      if (library_subtitle_upload_request === upload_request) {
        library_subtitle_upload_request = null;
      }
      if ($("#index_upload_subtitle_request_id").val() !== submission_request_id) {
        return;
      }
      $("#index_upload_subtitle_cancel").addClass("d-none");
      upload_button.prop("disabled", false).text("重试上传");
      if (error && error.status) {
        $("#index_upload_subtitle_request_id").val(SubtitleTasks.randomRequestId());
      }
      SubtitleTasks.renderProgress("#index_upload_subtitle_progress", {
        title: error && error.aborted ? "浏览器上传已停止" : "上传请求未完成",
        message: (error && error.message) || "连接中断，请在任务中心确认后重试。",
        percent: null
      });
      if (!error || !error.aborted) {
        show_fail_modal((error && error.message) || "字幕上传请求失败");
      }
    });
  });

  $("#index_upload_subtitle_cancel").unbind("click").click(function () {
    const task_id = $(this).data("task-id") || library_subtitle_upload_task_id;
    if (task_id) {
      SubtitleTasks.confirmCancel(task_id);
    } else if (library_subtitle_upload_request && typeof library_subtitle_upload_request.abort === "function") {
      library_subtitle_upload_request.abort();
    }
  });

  $("#index_library_subtitle_audit_cancel").unbind("click").click(function () {
    const task_id = $(this).data("task-id") || library_subtitle_audit_task_id;
    if (task_id) {
      SubtitleTasks.confirmCancel(task_id);
    }
  });
  $("#index_library_subtitle_audit_server").unbind("change").change(load_library_subtitle_audit_history);

  $("#index_upload_subtitle_file").unbind("change").on("change", function () {
    render_library_pending_files();
  });
  $("#index_upload_subtitle_align").unbind("change").on("change", function () {
    render_library_pending_files({preview: false});
  });
  $("#index_upload_subtitle_server").unbind("change").on("change", function () {
    render_library_pending_files();
  });
  init_library_dropzone();
  // Closing the online dialog ends a fill session, otherwise it hands focus back to the drawer.
  $("#library-online-subtitle-modal").off("hidden.bs.modal.library").on("hidden.bs.modal.library", function () {
    if (library_online_serialized) { return; }
    if (library_fill) {
      library_fill_exit();
      return;
    }
    library_drawer_resume();
  });
  $("#season-pack-modal").off("hidden.bs.modal.library").on("hidden.bs.modal.library", library_drawer_resume);
  $("#library_fill_exit").unbind("click").click(library_fill_exit);
  $("#library_fill_skip").unbind("click").click(function () { library_fill_advance(false); });
  $("#library_fill_next").unbind("click").click(function () { library_fill_advance(true); });

  $("#library_type_tabs").unbind("click").on("click", "[data-library-type]", function () {
    set_library_type($(this).attr("data-library-type"));
  });
  $("#library_status_tabs").unbind("click").on("click", "[data-library-subtitle]", function () {
    set_library_subtitle($(this).attr("data-library-subtitle"));
  });
  $("#library_sort_by").unbind("change").change(function () {
    update_library_sort_order_labels();
    update_library_filter_badge();
  });
  $("#library_filter_category,#library_filter_audio,#library_sort_order").unbind("change").change(function () {
    update_library_filter_badge();
  });
  $("#library_filter_keyword").unbind("input").on("input", function () {
    update_library_filter_badge();
  });
  $("#library_filter_keyword").unbind("keydown").keydown(function (event) {
    if (event.key === "Enter" && !event.isComposing) {
      event.preventDefault();
      apply_library_filters();
    }
  });
  $("#library_filter_keyword_clear").unbind("click").click(function () {
    $("#library_filter_keyword").val("").trigger("input").trigger("focus");
  });

  const saved_view = library_saved_view();
  if (saved_view) {
    $("#library_filter_type").val(saved_view.type || "all");
    $("#library_filter_subtitle").val(saved_view.subtitle || "all");
    $("#library_filter_audio").val(saved_view.audio_language || "all");
    $("#library_sort_by").val(saved_view.sort_by || "default");
    $("#library_sort_order").val(saved_view.sort_order || "desc");
    $("#library_filter_keyword").val(saved_view.keyword || "");
    // Category options only exist after the first server response, so seed
    // the select with the saved value; update_library_category_options
    // preserves it through the selected check and rebuilds the real list.
    const saved_category = String(saved_view.category || "");
    if (saved_category) {
      $("#library_filter_category").html(
          `<option value="${library_escape_html(saved_category)}" selected></option>`);
    }
  }
  update_library_audio_control();
  sync_library_tabs();
  update_library_sort_order_labels();
  update_library_filter_badge();
  load_library_items(saved_view ? Math.max(parseInt(saved_view.page, 10) || 1, 1) : 1);
}

var library_online_path = "";
var library_online_generation = 0;
var library_online_results = [];
var library_online_default_keyword = "";

function open_library_online_movie(item_id) {
  open_library_online_item(library_lookup_item(item_id));
}

function open_library_online_item(item) {
  if (item && item.target_path) {
    open_library_online_subtitles(item.title || item.original_title, item.target_path, {
      title: item.title, original_title: item.original_title, year: item.year, media_type: "movie",
      imdbid: item.imdbid
    });
  }
}

function open_library_online_episode(index) {
  const cached_episode = library_episodes_cache[index];
  if (!cached_episode) { return; }
  const episode = library_fill_episode_numbers(cached_episode);
  const series = library_lookup_item(library_current_series_id) || {};
  if (!episode || !episode.path) { return; }
  open_library_online_subtitles(library_current_series_title, episode.path, {
    title: library_current_series_title, original_title: series.original_title, year: series.year,
    media_type: "episode", season: episode.season, episode: episode.episode, imdbid: series.imdbid
  });
}

function library_online_keyword_for(media, fallback) {
  const parts = [];
  [media.title || fallback, media.original_title].forEach(function (value) {
    const title = String(value || "").trim();
    if (title && !parts.some(function (part) { return part.toLowerCase() === title.toLowerCase(); })) {
      parts.push(title);
    }
  });
  if (media.year) { parts.push(String(media.year)); }
  if (media.media_type === "episode" && media.season !== "" && media.season != null
      && media.episode !== "" && media.episode != null) {
    parts.push(`S${String(media.season).padStart(2, "0")}E${String(media.episode).padStart(2, "0")}`);
  }
  return parts.join(" ");
}

function reset_library_online_keyword() {
  $("#library_online_keyword").val(library_online_default_keyword);
}

function open_library_online_subtitles(name, path, media) {
  library_online_serialized = false;
  library_drawer_hide_for_modal();
  library_online_path = path;
  library_online_media = media || {};
  const episode_label = library_online_media.media_type === "episode"
      ? (library_online_media.season !== "" && library_online_media.season != null
          && library_online_media.episode !== "" && library_online_media.episode != null
          ? ` · 第 ${library_online_media.season} 季 第 ${library_online_media.episode} 集` : " · 季集未识别") : (library_online_media.year ? ` · ${library_online_media.year}` : "");
  library_online_generation += 1;
  library_online_results = [];
  $("#library_online_media").text((name || "") + episode_label);
  $("#library_online_target_path").text(`保存到：${path}`);
  library_online_default_keyword = library_online_keyword_for(library_online_media, name);
  reset_library_online_keyword();
  $("#library_online_results").empty();
  $("#library_online_search").prop("disabled", false);
  $("#library-online-subtitle-modal").modal("show");
  search_library_online_subtitles();
}

function search_library_online_subtitles() {
  const keyword = String($("#library_online_keyword").val() || "").trim();
  if (!keyword) { return; }
  const generation = ++library_online_generation;
  library_online_results = [];
  $("#library_online_search").prop("disabled", true);
  $("#library_online_results").text("正在检索在线字幕...");
  $.ajax({
    type: "POST", url: "/library/subtitle/search", contentType: "application/json", dataType: "json", timeout: 90000,
    data: JSON.stringify({keyword: keyword, media_path: library_online_path, provider: $("#library_online_provider").val(), media: Object.assign({}, library_online_media, {query_edited: keyword !== library_online_default_keyword})}),
    success: function (ret) {
      if (generation !== library_online_generation) { return; }
      if (ret.code !== 0) { $("#library_online_results").text(ret.msg || "检索失败"); return; }
      library_online_results = ret.items || [];
      let html = (ret.warnings || []).map(function (warning) {
        return `<div class="alert alert-warning">${library_escape_html(warning)}</div>`;
      }).join("");
      if (!library_online_results.length) { html += '<p class="text-muted">当前字幕源未返回符合目标片名和季集的字幕。可精简为单一片名检索，或配置 Assrt / OpenSubtitles 后重试；请核对目标文件的季集编号。</p>'; }
      library_online_results.forEach(function (item, index) {
        const provider = {thunder: "迅雷", assrt: "Assrt", opensubtitles: "OpenSubtitles"}[item.provider] || "未知来源";
        html += `<div class="border-bottom py-3">
          <div class="fw-bold text-break">${library_escape_html(item.name)}</div>
          <div class="text-muted small mb-2">${provider} · ${library_escape_html(item.language || "未知语言")} · ${library_escape_html(item.format)}  · ${library_escape_html(item.match_label || '')}</div>
          <div id="library_online_members_${index}"></div>
          <button type="button" class="btn btn-sm btn-primary library-online-download" onclick="download_library_online_subtitle(${index}, this)">下载并保存</button>
        </div>`;
      });
      $("#library_online_results").html(html);
    },
    error: function () {
      if (generation === library_online_generation) { $("#library_online_results").text("检索请求失败或超时，请重试"); }
    },
    complete: function () {
      if (generation === library_online_generation) { $("#library_online_search").prop("disabled", false); }
    }
  });
}

function download_library_online_subtitle(index, button, quota_confirmed) {
  const item = library_online_results[index];
  if (!item || $(button).prop("disabled") || $(button).data("confirming")) { return; }
  const generation = library_online_generation;
  if (item.provider === "opensubtitles" && !quota_confirmed) {
    // Serialize the existing Bootstrap dialogs so cancellation restores the
    // search and neither focus trap is active behind the other dialog.
    $(button).data("confirming", true).prop("disabled", true);
    // The online dialog closes only to show the confirmation; that is not a user exit.
    library_online_serialized = true;
    $("#library-online-subtitle-modal").one("hidden.bs.modal", function () {
      if (generation !== library_online_generation) {
        library_online_serialized = false;
        $(button).data("confirming", false).prop("disabled", false);
        return;
      }
      let confirmed = false;
      $("#system-confirm-modal").one("hidden.bs.modal", function () {
        $(button).data("confirming", false).prop("disabled", false);
        if (generation !== library_online_generation) { library_online_serialized = false; return; }
        $("#library-online-subtitle-modal").one("shown.bs.modal", function () {
          library_online_serialized = false;
          if (generation !== library_online_generation) { return; }
          if (confirmed) { download_library_online_subtitle(index, button, true); }
          else { $(button).trigger("focus"); }
        }).modal("show");
      });
      show_confirm_modal("下载该 OpenSubtitles 字幕将消耗一次账户下载配额，是否下载并保存？", function () {
        confirmed = true;
        hide_confirm_modal();
      });
    }).modal("hide");
    return;
  }
  const payload = {ticket: item.ticket};
  if (item.provider === "opensubtitles") { payload.confirm_quota = true; }
  const member = $(`#library_online_member_${index}`);
  if (member.length) { payload.member = member.val(); }
  $(".library-online-download").prop("disabled", true);
  $(button).text("处理中...");
  $.ajax({
    type: "POST", url: "/library/subtitle/download", contentType: "application/json", dataType: "json", timeout: 0,
    data: JSON.stringify(payload),
    success: function (ret) {
      if (generation !== library_online_generation) { return; }
      if (ret.code !== 0) { show_fail_modal(ret.msg || "下载失败"); return; }
      if (ret.members) {
        const select = $('<select class="form-select mb-2" aria-label="选择压缩包中的字幕文件">').attr("id", `library_online_member_${index}`);
        ret.members.forEach(function (name) { select.append($("<option>").val(name).text(name)); });
        $(`#library_online_members_${index}`).empty().append(select);
        return;
      }
      if (ret.task_id) {
        $(button).data("queued", true);
        SubtitleTasks.watch(ret.task_id, {
          onDone: function (task) {
            load_library_items(library_page);
            if (generation !== library_online_generation) { return; }
            const saved = ["succeeded", "partial"].indexOf(task.status) !== -1;
            $(button).data("queued", false).data("saved", saved).prop("disabled", saved)
                .text(saved ? "已保存" : "下载并保存");
            if (saved) { library_fill_note_saved(); }
            if (!saved) { show_fail_modal(task.error || "字幕处理未完成，请在任务中心查看详情"); }
          }
        });
        SubtitleTasks.refreshList(true).catch(function () {});
        show_success_modal(ret.msg || "字幕已加入处理队列");
      } else {
        show_success_modal(ret.msg || "字幕已保存");
        load_library_items(library_page);
        $(button).data("saved", true);
        library_fill_note_saved();
      }
    },
    error: function () { show_fail_modal("字幕下载请求失败，请检查字幕库后重试"); },
    complete: function () {
      if (generation !== library_online_generation) { return; }
      $(".library-online-download").each(function () {
        $(this).prop("disabled", !!$(this).data("saved") || !!$(this).data("queued"));
      });
      $(button).prop("disabled", !!$(button).data("saved") || !!$(button).data("queued"))
          .text($(button).data("queued") ? "后台处理中" : ($(button).data("saved") ? "已保存" : "下载并保存"));
    }
  });
}

// ---- Continuous fill -------------------------------------------------------------
// Walks a fixed queue of movies through the existing online search. Nothing is
// downloaded automatically; each save is still the user's explicit click.

function start_library_fill() {
  const view = library_view_instance;
  const grid = document.getElementById("library_items_grid");
  // Ordinary menu navigation replaces the DOM without clearing the view token.
  const is_current_view = () => grid && view && view === library_view_instance &&
      document.getElementById("library_items_grid") === grid;
  if (!is_current_view() || library_fill || library_fill_loading || library_items_loading) {
    return;
  }
  const data = Object.assign(library_filter_data(1), {
    type: "movie", page_size: 100, sort_by: "default", sort_order: "desc"
  });
  // Filling a finished library makes no sense; "all" and "completed" fall back to missing subtitles.
  if (["all", "ok"].indexOf(data.subtitle) !== -1) {
    data.subtitle = "missing";
  }
  const status_label = {missing: "缺中文字幕", unknown: "未检测", problem: "有问题"}[data.subtitle] || "缺中文字幕";
  library_fill_loading = true;
  $("#library_fill_btn").prop("disabled", true);
  NProgress.start();
  $.ajax({
    type: "POST",
    url: "/library/items?random=" + Math.random(),
    dataType: "json",
    contentType: "application/json",
    data: JSON.stringify(data),
    timeout: 0,
    success: function (ret) {
      if (!is_current_view()) { return; }
      if (!ret || ret.code !== 0) {
        show_fail_modal((ret && ret.msg) || "无法生成补齐队列");
        return;
      }
      const items = ret.items || [];
      const queue = items.filter(function (item) {
        return item.media_type === "movie" && item.can_upload && item.target_path;
      });
      if (!queue.length) {
        show_fail_modal("当前筛选下没有可补齐的电影。请调整筛选，或先执行外挂字幕检测。");
        return;
      }
      const truncated = (ret.total || 0) > items.length ? `，仅取前 ${items.length} 部` : "";
      const skipped = items.length - queue.length;
      show_confirm_modal(
          `将按当前筛选（电影、${status_label}）逐部检索字幕：共 ${queue.length} 部${truncated}` +
          `${skipped ? `，另有 ${skipped} 部无法上传已排除` : ""}。不会自动下载，每部都由你确认。是否开始？`,
          function () {
            if (!is_current_view()) { return; }
            library_fill = {queue: queue, index: 0, saved: 0, skipped: 0, current_saved: false};
            $("#system-confirm-modal").one("hidden.bs.modal", function () {
              if (is_current_view()) { library_fill_open(0); }
            });
            hide_confirm_modal();
          });
    },
    error: function () {
      if (!is_current_view()) { return; }
      show_fail_modal("网络错误");
    },
    complete: function () {
      if (!is_current_view()) { return; }
      library_fill_loading = false;
      NProgress.done();
      $("#library_fill_btn").prop("disabled", false);
    }
  });
}

function library_fill_open(index) {
  if (!library_fill) {
    return;
  }
  const total = library_fill.queue.length;
  if (index >= total) {
    library_fill_exit(true);
    return;
  }
  const item = library_fill.queue[index];
  library_fill.index = index;
  library_fill.current_saved = false;
  const last = index + 1 >= total;
  $("#library_fill_footer").removeClass("d-none");
  $("#library_fill_progress").text(`第 ${index + 1} / ${total} 部 · 已保存 ${library_fill.saved} 部`);
  $("#library_fill_skip").prop("disabled", false).text(last ? "跳过并结束" : "跳过");
  $("#library_fill_next").prop("disabled", true).text(last ? "完成" : "下一部");
  open_library_online_item(item);
}

function library_fill_advance(saved) {
  if (!library_fill) {
    return;
  }
  if (!saved && !library_fill.current_saved) {
    library_fill.skipped += 1;
  }
  library_fill_open(library_fill.index + 1);
}

function library_fill_note_saved() {
  if (!library_fill || library_fill.current_saved) {
    return;
  }
  library_fill.current_saved = true;
  library_fill.saved += 1;
  $("#library_fill_progress").text(
      `第 ${library_fill.index + 1} / ${library_fill.queue.length} 部 · 已保存 ${library_fill.saved} 部`);
  $("#library_fill_next").prop("disabled", false).trigger("focus");
  $("#library_fill_skip").prop("disabled", true);
}

function library_fill_exit(completed) {
  if (!library_fill) {
    return;
  }
  const run = library_fill;
  library_fill = null;
  $("#library_fill_footer").addClass("d-none");
  $("#library-online-subtitle-modal").modal("hide");
  const untouched = run.queue.length - run.saved - run.skipped;
  library_refresh_notice = `补齐${completed === true ? "完成" : "已结束"}：保存 ${run.saved} 部，跳过 ${run.skipped} 部` +
      (untouched > 0 ? `，未处理 ${untouched} 部` : "");
  load_library_items(library_page);
}
