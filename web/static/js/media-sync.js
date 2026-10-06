var refresh_sync_process_flag = false;
// Generations discard late progress after close, completion, or reopening.
var media_sync_generation = 0;
var media_sync_task_pending = false;

function show_mediasync_modal() {
  ajax_post("refresh_process", {type: "mediasync"}, function (ret) {
    if (ret.code === 0 && ret.value < 100) {
      $("#index-mediasync-modal").modal("show");
      start_media_sync(false);
    } else {
      ajax_post("mediasync_state", {}, function (state_ret) {
        if (state_ret.code === 0) {
          $("#mediasync_status").text(state_ret.text);
        }
        $("#mediasync_btn").text("开始同步")
            .attr("href", "javascript:start_media_sync(true)");
        $("#index-mediasync-modal").modal("show");
      }, true, false);
    }
  }, true, false);
}

function close_mediasync_modal() {
  $("#index-mediasync-modal").modal("hide");
  refresh_sync_process_flag = false;
  media_sync_task_pending = false;
  media_sync_generation += 1;
}

function start_media_sync(flag) {
  const generation = ++media_sync_generation;
  refresh_sync_process_flag = false;
  media_sync_task_pending = flag;
  $("#mediasync_btn").text("关闭")
      .attr("href", "javascript:close_mediasync_modal()");
  if (flag) {
    $("#mediasync_status").text("正在提交同步任务");
    ajax_post("start_mediasync", {}, function (ret) {
      if (generation !== media_sync_generation) return;
      refresh_sync_process_flag = false;
      media_sync_task_pending = false;
      media_sync_generation += 1;
      const succeeded = String(ret.code ?? ret.retcode) === "0";
      $("#mediasync_status").text(ret.msg || ret.retmsg || (succeeded ? "媒体库同步完成" : "媒体库同步未完成，请查看后台操作结果"));
      if (succeeded) $("#mediasync_process_bar").css("width", "100%").attr("aria-valuenow", 100);
      $("#mediasync_btn").text("开始同步").attr("href", "javascript:start_media_sync(true)");
    }, true, false, function (task) {
      if (generation !== media_sync_generation) return;
      // Only running work owns the global sync progress; queued tasks must not
      // display the previous sync's 100% result as their own completion.
      if (task.status === 'running' && !refresh_sync_process_flag) {
        refresh_sync_process_flag = true;
        refresh_sync_process(generation);
      } else if (task.status === 'queued' || task.status === 'accepting') {
        $("#mediasync_status").text(task.message || "已排队，等待同步");
      }
    });
  } else {
    refresh_sync_process_flag = true;
    refresh_sync_process(generation);
  }
}

function refresh_sync_process(generation=media_sync_generation) {
  if (!refresh_sync_process_flag || generation !== media_sync_generation) {
    return;
  }
  ajax_post("refresh_process", {type: "mediasync"}, function (ret) {
    if (!refresh_sync_process_flag || generation !== media_sync_generation) return;
    if (ret.code === 0) {
      $("#mediasync_process_bar").attr("style", "width: " + ret.value + "%")
          .attr("aria-valuenow", ret.value);
      $("#mediasync_status").text(ret.text);
    }
    // The global percentage may briefly belong to the previous completed run;
    // a tracked action's terminal record, not that percentage, stops polling.
    if (media_sync_task_pending || ret.value < 100) {
      setTimeout(() => refresh_sync_process(generation), 200);
    }
  }, true, false);
}
