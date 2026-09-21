/* Season archives use the same bounded multipart intake and persistent task center. */
(function (window, $) {
  "use strict";
  const root = document.getElementById("season-pack-modal");
  const isCurrent = function () { return root && document.getElementById("season-pack-modal") === root; };
  let context = null;
  let plan = null;
  let request = null;
  let generation = 0;

  function message(text, danger) {
    if (!isCurrent()) return;
    $("#season_pack_message").text(text).toggleClass("text-danger", !!danger);
  }
  function busy(value) {
    if (!isCurrent()) return;
    $("#season-pack-modal input,#season-pack-modal select,#season-pack-modal button").prop("disabled", value);
    $("#season_pack_submit").prop("disabled", value || !plan);
    $("#season_pack_rows input[data-unmatched]").prop("disabled", true);
  }
  window.open_library_season_pack = function () {
    if (request || !isCurrent()) return;
    const season = $("#library_episode_season").val();
    const item = library_items_cache[library_current_series_id];
    if (!item || season === "" || season == null) {
      show_fail_modal("请先选择具体的一季，再上传整季字幕包");
      return;
    }
    generation += 1;
    context = {item_id: library_current_series_id, season: season,
      server: item.server || library_default_media_server,
      request_id: SubtitleTasks.randomRequestId()};
    plan = null;
    $("#season_pack_title").text(`${item.title || "剧集"} · 第 ${season} 季`);
    $("#season_pack_file").val("");
    $("#season_pack_align").val("none");
    $("#season_pack_rows").empty();
    message("选择 ZIP/RAR 后预览匹配，可取消勾选不需要的字幕。");
    busy(false);
    $("#index-library-episodes-modal").modal("hide");
    $("#season-pack-modal").modal("show");
  };
  window.reset_library_season_pack = function () {
    if (request || !isCurrent()) return;
    plan = null;
    if (context) context.request_id = SubtitleTasks.randomRequestId();
    $("#season_pack_rows").empty();
    busy(false);
  };
  window.upload_library_season_pack = function (submit) {
    if (request || !context || !isCurrent()) return;
    const file = $("#season_pack_file")[0].files[0];
    if (!file) { message("请选择 ZIP 或 RAR 字幕包", true); return; }
    if (file.size > 250 * 1024 * 1024) { message("字幕包不能超过 250 MiB", true); return; }
    const data = new FormData();
    Object.keys(context).forEach(function (key) { data.append(key, context[key]); });
    data.append("file", file);
    data.append("align", $("#season_pack_align").val());
    data.append("upload_mode", submit ? "season_submit" : "season_preview");
    if (submit) {
      if (!plan) return;
      const selected = $("#season_pack_rows input:checked:not(:disabled)");
      if (!selected.length) { message("请至少勾选一个已匹配字幕", true); return; }
      data.append("plan_id", plan.plan_id);
      const members = [];
      selected.each(function () { members.push(this.value); });
      data.append("members", JSON.stringify(members));
    }
    const current = generation;
    busy(true);
    message(submit ? "正在上传并提交后台任务…" : "正在读取字幕包并匹配剧集…");
    request = SubtitleTasks.upload(data, {
      onUploadProgress: function (progress) {
        if (current === generation) message(`字幕包上传中：${progress.percent || 0}%`);
      },
      onTerminal: function () { if (isCurrent()) load_library_items(library_page); }
    });
    request.then(function (result) {
      if (current !== generation || !isCurrent()) return;
      if (submit) {
        plan = null;
        $("#season_pack_rows").empty();
        message("整季字幕已加入后台任务，可在任务中心查看逐文件结果或取消任务。");
        return;
      }
      plan = result.response;
      const escape = library_escape_html;
      $("#season_pack_rows").html((plan.rows || []).map(function (row) {
        const matched = !!row.target;
        return `<label class="d-block border-bottom py-2 text-break"><input type="checkbox" class="form-check-input me-2" value="${escape(row.id)}" ${matched ? 'checked' : 'disabled data-unmatched="1"'}> ${escape(row.name)}<span class="d-block small ms-4 ${matched ? 'text-muted' : 'text-warning'}">${escape(matched ? row.season_episode + ' → ' + row.target.path : row.reason)}</span></label>`;
      }).join(""));
      const count = (plan.rows || []).filter(function (row) { return !!row.target; }).length;
      message(`已匹配 ${count} 个字幕，${plan.rows.length - count} 个无法匹配。核对后点击“上传所选字幕”。`);
    }).catch(function (error) {
      if (current === generation) message(error.message || "字幕包处理失败", true);
    }).finally(function () {
      request = null;
      if (current === generation) busy(false);
    });
  };
})(window, window.jQuery);
