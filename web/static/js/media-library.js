var library_page = 1;
var library_page_size = 18;
var library_items_cache = {};
var library_current_series_id = "";
var library_current_series_title = "";
var library_poster_observer = null;
var library_eager_poster_count = 6;

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
    keyword: $("#library_filter_keyword").val(),
    page: page,
    page_size: library_page_size
  };
}

function update_library_category_options(categories) {
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
  library_page = page;
  library_post_json("/library/items", library_filter_data(page), function (ret) {
    if (ret.code !== 0) {
      show_fail_modal(ret.msg || "媒体库列表加载失败");
      return;
    }
    update_library_category_options(ret.categories || {});
    library_items_cache = {};
    const items = ret.items || [];
    const total = ret.total || 0;
    const total_pages = Math.max(Math.ceil(total / library_page_size), 1);
    $("#library_items_summary").text(`共 ${total} 个媒体项目，当前第 ${ret.page || 1} / ${total_pages} 页`);
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
  });
}

function library_item_card(item, index) {
  const item_id = library_escape_js(item.id);
  const title = library_escape_html(item.title || item.original_title || "未命名媒体");
  const year = item.year ? ` · ${library_escape_html(item.year)}` : "";
  const poster = library_escape_html(item.poster_url || "");
  const type_initial = library_escape_html((item.media_type_name || "媒").substring(0, 1));
  const subtitle_label = library_escape_html(item.subtitle_label || "未检测");
  const type_name = library_escape_html(item.media_type_name);
  const category = library_escape_html(item.category || "未分类");
  const eager = index < library_eager_poster_count;

  const sub_status = item.subtitle_status || "unknown";
  let dot_class = "unknown";
  let ok = false;
  let missing = false;
  if (sub_status === "has_chinese_external" || sub_status === "has_chinese_internal") {
    dot_class = "ok";
    ok = true;
  } else if (sub_status === "missing_chinese") {
    dot_class = "missing";
    missing = true;
  }
  const indicator_class = ok ? "has-chinese" : missing ? "missing-chinese" : "unknown";

  const is_movie = item.media_type === "movie";
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
      </div>
      <div class="lit-library-card-footer">
        <button type="button" class="btn btn-sm btn-primary w-100" ${btn_attr}>${btn_text}</button>
      </div>
    </div>`;
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
  show_index_upload_subtitle_modal(item.title, item.target_path);
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
  library_post_json("/library/episodes", {item_id: item_id}, function (ret) {
    if (ret.code !== 0) {
      $("#index_library_episodes_body").html(`<div class="text-danger py-3">${library_escape_html(ret.msg || "剧集加载失败")}</div>`);
      return;
    }
    render_library_episodes(ret.items || []);
  });
}

function render_library_episodes(episodes) {
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
  const row = $(`#index_library_episodes_body [data-episode-index="${index}"]`);
  const path = row.find(".library-episode-path").val();
  const title = row.find(".library-episode-title").val();
  if (!path) {
    show_fail_modal("未找到可上传字幕的剧集文件");
    return;
  }
  show_index_upload_subtitle_modal(title, path);
}

function show_index_upload_subtitle_modal(name, media_path) {
  $("#index_upload_subtitle_path").val(media_path);
  $("#index_upload_subtitle_target_path").val(media_path);
  $("#index_upload_subtitle_name").val(name || media_path);
  $("#index_upload_subtitle_file").val("");
  $("#index_upload_subtitle_server").val("jellyfin");
  $("#index-upload-subtitle-modal").modal("show");
}

function init_media_library_page(options) {
  options = options || {};
  if (options.page_size) {
    library_page_size = options.page_size;
  }
  $("#index_upload_subtitle_btn").unbind("click").click(function () {
    let file = $("#index_upload_subtitle_file")[0].files[0];
    if (!file) {
      show_fail_modal("请选择字幕文件");
      return;
    }
    let form_data = new FormData();
    form_data.append("path", $("#index_upload_subtitle_path").val());
    form_data.append("target_path", $("#index_upload_subtitle_target_path").val());
    form_data.append("server", $("#index_upload_subtitle_server").val());
    form_data.append("file", file);
    NProgress.start();
    $.ajax({
      type: "POST",
      url: "/subtitle/upload?random=" + Math.random(),
      data: form_data,
      cache: false,
      processData: false,
      contentType: false,
      dataType: "json",
      success: function (ret) {
        NProgress.done();
        if (ret.code === 0) {
          $("#index-upload-subtitle-modal").modal("hide");
          show_success_modal(ret.msg || "字幕上传成功");
          load_library_items(library_page);
          if ($("#index-library-episodes-modal").hasClass("show") && library_current_series_id) {
            open_library_episodes(library_current_series_id);
          }
        } else {
          show_fail_modal(ret.msg || "字幕上传失败");
        }
      },
      error: function () {
        NProgress.done();
        show_fail_modal("网络错误");
      }
    });
  });

  $("#library_filter_type").unbind("change").change(function () {
    $("#library_filter_category").val("");
    load_library_items(1);
  });
  $("#library_filter_category,#library_filter_subtitle").unbind("change").change(function () {
    load_library_items(1);
  });
  $("#library_filter_keyword").unbind("keydown").keydown(function (event) {
    if (event.key === "Enter") {
      load_library_items(1);
    }
  });

  load_library_items(1);
}
