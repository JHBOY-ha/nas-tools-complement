var library_page = 1;
var library_page_size = 18;
var library_items_cache = {};
var library_episodes_cache = [];
var library_current_series_id = "";
var library_current_series_title = "";
var library_poster_observer = null;
var library_eager_poster_count = 6;
var library_default_media_server = "emby";
var library_subtitle_audit_categories = {};
var library_subtitle_audit_roots = {};
var library_items_loading = false;
var library_pending_page = null;
var library_categories_cache = {};
var library_subtitle_audit_task_id = "";
var library_subtitle_upload_task_id = "";
var library_subtitle_upload_request = null;

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
    page: page,
    page_size: library_page_size
  };
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

function load_library_items(page) {
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
      ret = ret || {};
      if (ret.code !== 0) {
        $("#library_items_summary").text("字幕库加载失败");
        show_fail_modal(ret.msg || "字幕库列表加载失败");
        return;
      }
      update_library_category_options(ret.categories || {});
      library_items_cache = {};
      const items = ret.items || [];
      const total = ret.total || 0;
      const total_pages = Math.max(Math.ceil(total / library_page_size), 1);
      $("#library_items_summary").text(
          `找到 ${total} 个媒体项目 · 本页展示 ${items.length} 个 · 第 ${ret.page || 1} / ${total_pages} 页`
      );
      $("#library_page_text").text(`${ret.page || 1} / ${total_pages}`);
      $("#library_prev_btn").prop("disabled", (ret.page || 1) <= 1);
      $("#library_next_btn").prop("disabled", (ret.page || 1) >= total_pages);
      if (!items.length) {
        $("#library_items_grid").html('<div class="empty" style="grid-column:1/-1;margin:3rem 0"><p class="empty-title">暂无媒体</p><p class="empty-subtitle text-muted">请先点击“媒体库同步”，或调整筛选条件。</p></div>');
        return;
      }
      let html = "";
      items.forEach(function (item, index) {
        library_items_cache[item.id] = item;
        html += library_item_card(item, index);
      });
      $("#library_items_grid").html(html);
      init_library_posters();
    },
    error: function () {
      $("#library_prev_btn,#library_next_btn").prop("disabled", false);
      $("#library_items_summary").text("字幕库加载失败，请稍后重试");
      show_fail_modal("网络错误");
    },
    complete: function () {
      NProgress.done();
      library_items_loading = false;
      $("#library_filter_btn,#library_filter_trigger,#library_refresh_btn").prop("disabled", false);
      const pending_page = library_pending_page;
      library_pending_page = null;
      if (pending_page !== null) {
        setTimeout(function () {
          load_library_items(pending_page);
        }, 50);
      }
    }
  });
}

function reset_library_filters() {
  $("#library_filter_type").val("all");
  $("#library_filter_category").html('<option value="">全部</option>').val("");
  $("#library_filter_subtitle").val("all");
  $("#library_sort_by").val("default");
  $("#library_sort_order").val("desc");
  $("#library_filter_keyword").val("");
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
  let active_count = 0;
  if ($("#library_filter_type").val() !== "all") {
    active_count += 1;
  }
  if ($("#library_filter_category").val()) {
    active_count += 1;
  }
  if ($("#library_filter_subtitle").val() !== "all") {
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
    if (rendered_result.category === "movie") {
      load_library_items(library_page);
    }
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

function library_item_card(item, index) {
  const item_id = library_escape_js(item.id);
  const title = library_escape_html(item.title || item.original_title || "未命名媒体");
  const year = item.year ? ` · ${library_escape_html(item.year)}` : "";
  const poster = library_escape_html(item.poster_url || "");
  const type_initial = library_escape_html((item.media_type_name || "媒").substring(0, 1));
  const subtitle_label = library_escape_html(item.subtitle_label || "未检测");
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
  const can_repair = is_movie && item.can_upload
      && ["warning", "error"].indexOf(item.subtitle_audit_status) !== -1;
  const movie_upload_disabled = is_movie && !item.can_upload;
  const btn_attr = is_movie
      ? `${movie_upload_disabled ? "disabled" : ""} onclick="open_library_movie_upload(&quot;${item_id}&quot;)"`
      : `onclick="open_library_episodes(&quot;${item_id}&quot;)"`;
  const btn_text = is_movie ? (movie_upload_disabled ? "不可上传" : "上传字幕") : "选择剧集";
  const poster_attrs = poster
      ? `${eager ? `src="${poster}"` : `data-src="${poster}"`} alt="${title}" loading="${eager ? "eager" : "lazy"}" fetchpriority="${eager ? "high" : "low"}" decoding="async"`
      : `alt="${title}"`;
  return `
    <div class="lit-library-card">
      <div class="lit-library-card-poster${poster ? "" : " no-poster"}" data-type="${library_escape_html(item.media_type || "")}">
        <img ${poster_attrs}
             onerror="this.style.display='none';this.parentNode.classList.add('no-poster')">
        <span class="lit-library-card-fallback">${type_initial}</span>
        <span class="lit-library-card-type-badge">${type_name}</span>
        <span class="lit-library-card-subtitle-indicator ${indicator_class}"></span>
      </div>
      <div class="lit-library-card-body">
        <div class="lit-library-card-title" title="${title}">${title}</div>
        <div class="lit-library-card-meta-line">
          <span class="lit-library-card-meta">${category}${year}</span>
        </div>
        <div class="lit-library-card-subtitle-line">
          <span class="dot ${dot_class}"></span>
          <span class="lit-library-card-meta">${subtitle_label}</span>
        </div>
        ${is_movie && subtitle_audit_label ? `
        <div class="lit-library-card-audit">
          <span class="badge ${subtitle_audit_badge} lit-library-card-audit-badge" title="最近检测：${subtitle_audit_checked_at}">${subtitle_audit_text}</span>
        </div>` : ""}
      </div>
      <div class="lit-library-card-footer">
        ${can_repair ? `<button type="button" class="btn btn-sm btn-outline-warning w-100"
          title="按全局影视服务器规则规范化现有外挂字幕"
          aria-label="二次处理 ${title} 的外挂字幕"
          onclick="repair_library_subtitles(&quot;${item_id}&quot;, this)">二次处理</button>` : ""}
        <button type="button" class="btn btn-sm btn-primary w-100" ${btn_attr}>${btn_text}</button>
      </div>
    </div>`;
}

function repair_library_subtitles(item_id, button) {
  const item = library_items_cache[item_id];
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

function open_library_movie_upload(item_id) {
  const item = library_items_cache[item_id];
  if (!item || !item.target_path) {
    show_fail_modal("未找到可上传字幕的电影文件");
    return;
  }
  show_index_upload_subtitle_modal(item.title, item.target_path, {
    server: item.server || library_default_media_server,
    server_item_id: item.server_item_id || "",
    parent_server_item_id: item.parent_server_item_id || "",
    library_id: item.library_id || ""
  });
}

function open_library_episodes(item_id) {
  const item = library_items_cache[item_id];
  if (!item) {
    show_fail_modal("未找到媒体项目");
    return;
  }
  library_current_series_id = item_id;
  library_current_series_title = item.title || "";
  $("#index_library_episodes_title").text(`${item.title || "剧集"} - 选择剧集`);
  $("#index_library_episodes_body").html('<div class="text-muted py-3">正在加载剧集...</div>');
  $("#index-library-episodes-modal").modal("show");
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
    render_library_episodes(ret.items || []);
  });
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
    const path = library_escape_html(episode.path || "");
    const disabled = episode.can_upload ? "" : "disabled";
    const button = `<button type="button" class="btn btn-sm btn-primary" ${disabled} onclick="open_library_episode_upload(${index})">上传字幕</button>`;
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
  $("#index_library_episodes_body").html(html);
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
  const series = library_items_cache[library_current_series_id] || {};
  show_index_upload_subtitle_modal(title, path, {
    server: episode.server || series.server || library_default_media_server,
    server_item_id: episode.server_item_id || "",
    parent_server_item_id: episode.parent_server_item_id || series.server_item_id || "",
    library_id: episode.library_id || series.library_id || ""
  });
}

function show_index_upload_subtitle_modal(name, media_path, metadata) {
  metadata = metadata || {};
  if (library_subtitle_upload_request) {
    $("#index-upload-subtitle-modal").modal("show");
    return;
  }
  $("#index_upload_subtitle_path").val(media_path);
  $("#index_upload_subtitle_target_path").val(media_path);
  $("#index_upload_subtitle_request_id").val(SubtitleTasks.randomRequestId());
  $("#index_upload_subtitle_server_item_id").val(metadata.server_item_id || "");
  $("#index_upload_subtitle_parent_server_item_id").val(metadata.parent_server_item_id || "");
  $("#index_upload_subtitle_library_id").val(metadata.library_id || "");
  $("#index_upload_subtitle_name").val(name || media_path);
  $("#index_upload_subtitle_file").val("");
  $("#index_upload_subtitle_align").val("none");
  $("#index_upload_subtitle_server").val(metadata.server || library_default_media_server);
  $("#index_upload_subtitle_progress").addClass("d-none");
  $("#index_upload_subtitle_cancel").addClass("d-none").removeData("task-id");
  $("#index_upload_subtitle_btn").prop("disabled", false).text("上传");
  library_subtitle_upload_task_id = "";
  $("#index-upload-subtitle-modal").modal("show");
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
    load_library_items(library_page);
    if ($("#index-library-episodes-modal").hasClass("show") && library_current_series_id) {
      open_library_episodes(library_current_series_id);
    }
  }
}

function init_media_library_page(options) {
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

  $("#library_filter_type").unbind("change").change(function () {
    $("#library_filter_category").val("");
    update_library_category_options();
    update_library_filter_badge();
  });
  $("#library_sort_by").unbind("change").change(function () {
    update_library_sort_order_labels();
    update_library_filter_badge();
  });
  $("#library_filter_category,#library_filter_subtitle,#library_sort_order").unbind("change").change(function () {
    update_library_filter_badge();
  });
  $("#library_filter_keyword").unbind("input").on("input", function () {
    update_library_filter_badge();
  });
  $("#library_filter_keyword").unbind("keydown").keydown(function (event) {
    if (event.key === "Enter") {
      event.preventDefault();
      apply_library_filters();
    }
  });

  update_library_sort_order_labels();
  update_library_filter_badge();
  load_library_items(1);
}
