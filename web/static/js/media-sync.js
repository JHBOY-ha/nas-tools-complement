var refresh_sync_process_flag = false;

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
}

function start_media_sync(flag) {
  $("#mediasync_btn").text("关闭")
      .attr("href", "javascript:close_mediasync_modal()");
  if (flag) {
    ajax_post("start_mediasync", {}, function () {
      refresh_sync_process_flag = true;
      refresh_sync_process();
    }, true, false);
  } else {
    refresh_sync_process_flag = true;
    refresh_sync_process();
  }
}

function refresh_sync_process() {
  if (!refresh_sync_process_flag) {
    return;
  }
  ajax_post("refresh_process", {type: "mediasync"}, function (ret) {
    if (ret.code === 0) {
      $("#mediasync_process_bar").attr("style", "width: " + ret.value + "%")
          .attr("aria-valuenow", ret.value);
      $("#mediasync_status").text(ret.text);
    }
    if (ret.value < 100) {
      setTimeout("refresh_sync_process()", 200);
    }
  }, true, false);
}
