/* 字幕任务设置页：沿用共享请求入口，页面切换后忽略旧请求回调。 */
(function () {
  "use strict";
  const root = document.getElementById("subtitle-task-settings-page");
  if (!root) return;
  const form = root.querySelector("form");
  const fields = Array.from(form.querySelectorAll("[data-policy-key]"));
  const fieldsets = form.querySelectorAll("fieldset");
  const reload = root.querySelector("#subtitle_task_settings_reload");
  const save = root.querySelector("#subtitle_task_settings_save");
  const message = root.querySelector("#subtitle_task_settings_message");
  let loaded = false;
  let busy = false;

  function showMessage(text, tone) {
    message.className = text ? "alert alert-" + tone + " mb-3" : "d-none";
    message.textContent = text;
  }

  function setBusy(value) {
    busy = value;
    form.setAttribute("aria-busy", String(value));
    fieldsets.forEach(function (fieldset) { fieldset.disabled = value || !loaded; });
    reload.disabled = value;
    save.disabled = value || !loaded;
  }

  function applyPolicy(response) {
    let policy = SubtitleTasks.payloadOf(response) || {};
    policy = policy.settings || policy;
    policy = policy.policy || policy;
    // 不完整响应不能解锁表单，避免把空值或旧配置误保存到服务端。
    if (fields.some(function (field) { return !Number.isInteger(policy[field.dataset.policyKey]); })) {
      throw new Error("任务限制数据不完整，请重新读取。");
    }
    fields.forEach(function (field) { field.value = policy[field.dataset.policyKey]; });
    loaded = true;
  }

  function clearErrors() {
    fields.forEach(function (field) {
      field.classList.remove("is-invalid");
      field.removeAttribute("aria-invalid");
      // 清除校验错误时保留字段帮助关联。
      field.setAttribute("aria-describedby", field.id + "_help");
    });
    form.querySelectorAll("[data-policy-error]").forEach(function (node) { node.remove(); });
  }

  function invalid(field, text) {
    const error = document.createElement("div");
    error.className = "invalid-feedback";
    error.id = field.id + "_error";
    error.dataset.policyError = "true";
    error.textContent = text;
    field.after(error);
    field.classList.add("is-invalid");
    field.setAttribute("aria-invalid", "true");
    field.setAttribute("aria-describedby", field.id + "_help " + error.id);
    field.focus();
    showMessage("请修正标出的设置后再保存。", "danger");
    return null;
  }

  function collectPolicy() {
    clearErrors();
    const policy = {};
    for (const field of fields) {
      const value = Number(field.value);
      if (!field.value.trim() || !Number.isInteger(value) || value < Number(field.min) || value > Number(field.max)) {
        return invalid(field, "请输入 " + field.min + " 至 " + field.max + " 之间的整数。");
      }
      policy[field.dataset.policyKey] = value;
    }
    const byKey = function (key) { return fields.find(function (field) { return field.dataset.policyKey === key; }); };
    if (policy.batch_limit_mb < Math.max(policy.text_file_limit_mb, policy.vobsub_limit_mb)) {
      return invalid(byKey("batch_limit_mb"), "单批总量不能小于文本单文件或 VobSub 成对文件上限。");
    }
    if (policy.staging_quota_mb < policy.batch_limit_mb * 6) {
      return invalid(byKey("staging_quota_mb"), "暂存总额度至少为单批总量的 6 倍，以容纳转换和对齐文件。");
    }
    if (policy.llm_max_batch_items > policy.max_batch_items) {
      return invalid(byKey("llm_max_batch_items"), "LLM 对齐单批数量不能大于单批字幕数量。");
    }
    return policy;
  }

  function load() {
    if (busy) return;
    setBusy(true);
    showMessage("正在读取任务限制…", "info");
    SubtitleTasks.request("GET", "/subtitle/tasks/settings").then(function (response) {
      if (!root.isConnected) return;
      applyPolicy(response);
      clearErrors();
      // 读取完成后收起状态，避免常驻说明挤占设置页面。
      showMessage("", "info");
    }).catch(function (error) {
      if (root.isConnected) showMessage((error && error.message) || "读取失败，请点击“重新读取”重试。", "danger");
    }).finally(function () {
      if (root.isConnected) setBusy(false);
    });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (busy || !loaded) return;
    const policy = collectPolicy();
    if (!policy) return;
    setBusy(true);
    showMessage("正在保存任务限制…", "info");
    SubtitleTasks.request("POST", "/subtitle/tasks/settings", policy).then(function (response) {
      if (!root.isConnected) return;
      applyPolicy(response);
      showMessage("任务限制已保存，仅影响新任务。", "success");
    }).catch(function (error) {
      if (root.isConnected) showMessage((error && error.message) || "保存失败，已保留填写内容，请重试。", "danger");
    }).finally(function () {
      if (root.isConnected) setBusy(false);
    });
  });
  reload.addEventListener("click", load);
  SubtitleTasks.init();
  load();
})();
