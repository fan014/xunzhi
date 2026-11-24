const $ = (id) => document.getElementById(id);

const state = {
  identity: null,
  model: null,
  appearance: null,
  reminder: null,
  switching: false,
};

function setStatus(target, text, isError = false) {
  const line = typeof target === "string" ? $(target) : target;
  if (!line) return;
  line.textContent = text || "";
  line.classList.toggle("error", Boolean(isError));
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  let data = {};
  try {
    data = await response.json();
  } catch (_) {
    data = {};
  }
  if (!response.ok) {
    const error = new Error(data.error?.message || "请求失败");
    error.code = data.error?.code;
    error.payload = data;
    throw error;
  }
  return data;
}

function userLabel(user) {
  return `${user.display_name}${user.is_default ? " · 默认工作区" : ""}`;
}

function renderIdentity(data) {
  state.identity = data;
  const current = data.current;
  $("current-user-name").textContent = current.display_name;
  $("current-user-badge").textContent = current.is_default ? "默认工作区" : "独立工作区";
  $("display-name").value = current.display_name;
  const select = $("user-select");
  select.innerHTML = "";
  (data.users || []).forEach((user) => {
    const option = document.createElement("option");
    option.value = user.id;
    option.textContent = userLabel(user);
    select.append(option);
  });
  select.value = current.id;
  const canSwitch = Boolean(data.switch_supported);
  $("switch-user").disabled = !canSwitch || state.switching;
  $("switch-note").textContent = canSwitch
    ? "切换会重启桌面版的本机服务，并落到目标学习者首页。切换前会自动保存当前草稿。"
    : "网页服务没有受控工作区切换能力；请使用桌面版「循知」切换学习者。个人、模型和外观设置仍可保存。";
  if (data.busy?.busy) {
    setStatus(
      "switch-status",
      `当前有 ${data.busy.generation_count} 个生成任务在进行；完成前不能切换。`,
      true
    );
  }
}

async function loadIdentity() {
  try {
    const data = await api("/api/settings/identity");
    renderIdentity(data);
  } catch (error) {
    setStatus("profile-status", error.message, true);
  }
}

async function saveProfile(event) {
  event.preventDefault();
  const name = $("display-name").value.trim();
  if (!name) {
    setStatus("profile-status", "显示名不能为空。", true);
    return;
  }
  setStatus("profile-status", "正在保存…");
  const button = $("save-profile");
  button.disabled = true;
  try {
    const data = await api("/api/settings/profile", {
      method: "PATCH",
      body: JSON.stringify({ display_name: name }),
    });
    setStatus("profile-status", "显示名已保存。");
    await loadIdentity();
    $("display-name").value = data.current.display_name;
  } catch (error) {
    setStatus("profile-status", error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function createUser(event) {
  event.preventDefault();
  const name = $("new-user-name").value.trim();
  if (!name) {
    setStatus("profile-status", "请填写新学习者的显示名。", true);
    return;
  }
  const button = $("create-user");
  button.disabled = true;
  try {
    const data = await api("/api/settings/users", {
      method: "POST",
      body: JSON.stringify({ display_name: name }),
    });
    $("new-user-name").value = "";
    $("create-user-form").hidden = true;
    await loadIdentity();
    $("user-select").value = data.user.id;
    setStatus(
      "profile-status",
      `已创建学习者「${data.user.display_name}」。首次进入时知识库、计划和学习记录为空。`
    );
  } catch (error) {
    setStatus("profile-status", error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function pollSwitch(requestId, targetUserId, targetName) {
  const started = Date.now();
  while (Date.now() - started < 60000) {
    await new Promise((resolve) => setTimeout(resolve, 650));
    try {
      const health = await api("/api/health");
      if (health.user_id === targetUserId) {
        setStatus("switch-status", `已切换到「${targetName}」，正在打开首页…`);
        window.location.href = "/";
        return;
      }
      if (health.user_id && health.user_id !== targetUserId) {
        const result = await api(
          `/api/settings/switch-result?request_id=${encodeURIComponent(requestId)}`
        );
        if (result.status === "failed") {
          setStatus(
            "switch-status",
            result.message || "切换失败，已恢复原工作区。",
            true
          );
          state.switching = false;
          await loadIdentity();
          return;
        }
      }
    } catch (_) {
      // 旧服务正在停止或新服务尚未就绪，继续等待。
    }
  }
  state.switching = false;
  setStatus("switch-status", "切换未在限定时间内完成。请回到桌面版检查状态；原工作区数据仍保留。", true);
  await loadIdentity();
}

async function switchUser() {
  if (!state.identity?.switch_supported || state.switching) return;
  const select = $("user-select");
  const target = state.identity.users.find((user) => user.id === select.value);
  if (!target || target.id === state.identity.current.id) {
    setStatus("switch-status", "请选择另一个学习者。", true);
    return;
  }
  state.switching = true;
  $("switch-user").disabled = true;
  setStatus("switch-status", `正在切换到「${target.display_name}」…`);
  try {
    const data = await api("/api/settings/switch", {
      method: "POST",
      body: JSON.stringify({ user_id: target.id }),
    });
    try {
      localStorage.clear();
      sessionStorage.clear();
    } catch (_) {
      // 浏览器存储不可用时不影响切换。
    }
    setStatus(
      "switch-status",
      `切换请求已提交；原工作区已保留 ${data.draft_count || 0} 份草稿。正在等待「${target.display_name}」工作区启动…`
    );
    await pollSwitch(data.request_id, target.id, target.display_name);
  } catch (error) {
    state.switching = false;
    await loadIdentity();
    setStatus("switch-status", error.message, true);
  }
}

function renderModel(data) {
  state.model = data;
  $("model-source").textContent = data.source_label || "尚未配置";
  $("model-masked").textContent = data.masked || "—";
  $("model-name").textContent = data.model || "—";
  $("model-status").textContent = data.fake_mode
    ? "当前为测试替身模式；真实调用需关闭替身模式。"
    : data.configured
      ? "当前配置可用于诊断、生成和测评。"
      : "尚未配置密钥；请填写并保存后再开始学习。";
  $("model-status").classList.toggle("error", !data.configured && !data.fake_mode);
}

async function loadModel() {
  try {
    renderModel(await api("/api/settings/model"));
  } catch (error) {
    setStatus("model-status", error.message, true);
  }
}

async function saveModel(event) {
  event.preventDefault();
  const key = $("model-key").value.trim();
  if (!key) {
    setStatus("model-status", "请填写要保存的 DeepSeek 密钥；留空不会覆盖现有配置。", true);
    return;
  }
  const button = $("save-model");
  button.disabled = true;
  setStatus("model-status", "正在保存…");
  try {
    const data = await api("/api/settings/model", {
      method: "PUT",
      body: JSON.stringify({ api_key: key }),
    });
    $("model-key").value = "";
    renderModel(data);
    setStatus("model-status", "密钥已验证并保存到本机受保护存储。");
  } catch (error) {
    await loadModel();
    setStatus("model-status", error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function testModel() {
  const button = $("test-model");
  button.disabled = true;
  setStatus("model-test-status", "正在测试连接…");
  try {
    const key = $("model-key").value.trim();
    const data = await api("/api/settings/model/test", {
      method: "POST",
      body: JSON.stringify({ api_key: key || null }),
    });
    const labels = {
      ok: "连接成功",
      invalid_key: "密钥无效",
      quota_limited: "调用受限：额度不足",
      rate_limited: "调用受限：模型服务限流",
      unavailable: "模型暂时不可用",
      network_error: "网络错误",
      missing_key: "尚未填写密钥",
      timeout: "连接超时",
      probe_request_invalid: "探测请求格式错误（不是密钥问题）",
      probe_invalid_response: "探测响应格式错误（不是密钥问题）",
      test_double_error: "测试替身调用失败",
    };
    setStatus(
      "model-test-status",
      `${labels[data.code] || data.message || "测试完成"}${data.message ? `：${data.message}` : ""}`,
      !data.ok
    );
  } catch (error) {
    setStatus("model-test-status", `测试请求失败：${error.message}`, true);
  } finally {
    button.disabled = false;
  }
}

async function resetModel() {
  if (!window.confirm("恢复默认配置只影响当前学习者；默认工作区将回退到 .env 配置。继续吗？")) {
    return;
  }
  const button = $("reset-model");
  button.disabled = true;
  try {
    const data = await api("/api/settings/model/reset", { method: "POST" });
    renderModel(data);
    setStatus("model-status", `已恢复：${data.source_label || "尚未配置"}`);
  } catch (error) {
    setStatus("model-status", error.message, true);
  } finally {
    button.disabled = false;
  }
}

function selectedAppearance() {
  const value = (name) =>
    document.querySelector(`input[name="${name}"]:checked`)?.value || "";
  return {
    theme: value("theme") || "system",
    font_size: value("font_size") || "standard",
    font_family: value("font_family") || "system",
  };
}

function previewAppearance() {
  const values = selectedAppearance();
  const link = $("theme-style");
  if (!link) return;
  const params = new URLSearchParams({
    preview_theme: values.theme,
    preview_font_size: values.font_size,
    preview_font_family: values.font_family,
  });
  link.href = `/api/settings/theme.css?${params.toString()}`;
  setStatus("appearance-status", "预览中，尚未保存。");
}

function renderAppearance(data) {
  state.appearance = data;
  document.querySelectorAll('input[name="theme"]').forEach((input) => {
    input.checked = input.value === data.theme;
  });
  document.querySelectorAll('input[name="font_size"]').forEach((input) => {
    input.checked = input.value === data.font_size;
  });
  document.querySelectorAll('input[name="font_family"]').forEach((input) => {
    input.checked = input.value === data.font_family;
  });
  const link = $("theme-style");
  if (link) link.href = "/api/settings/theme.css";
  setStatus("appearance-status", "");
}

async function loadAppearance() {
  try {
    renderAppearance(await api("/api/settings/appearance"));
  } catch (error) {
    setStatus("appearance-status", error.message, true);
  }
}

async function saveAppearance() {
  const button = $("save-appearance");
  button.disabled = true;
  const values = selectedAppearance();
  try {
    const data = await api("/api/settings/appearance", {
      method: "PUT",
      body: JSON.stringify(values),
    });
    renderAppearance(data);
    setStatus("appearance-status", "外观已保存，当前学习者下次打开会自动使用。");
  } catch (error) {
    setStatus("appearance-status", error.message, true);
  } finally {
    button.disabled = false;
  }
}


const REMINDER_STATE_LABELS = {
  not_enabled: "未启用",
  waiting: "等待发送",
  sent: "已发送",
  failed: "发送失败",
  unconfirmed: "结果待确认",
  no_tasks: "今天无待办",
  config_incomplete: "配置未就绪",
  paused: "已暂停",
};

function reminderFormValues() {
  return {
    email: $("reminder-email").value.trim(),
    time: $("reminder-time").value,
    smtp_host: $("reminder-smtp-host").value.trim(),
    smtp_port: Number($("reminder-smtp-port").value || 587),
    smtp_encryption: $("reminder-smtp-encryption").value,
    smtp_username: $("reminder-smtp-username").value.trim(),
    password: $("reminder-smtp-password").value || null,
  };
}

function renderReminder(data) {
  state.reminder = data;
  const config = data.config || {};
  $("reminder-email").value = config.email || "";
  $("reminder-time").value = config.time || "20:00";
  $("reminder-smtp-host").value = config.smtp_host || "";
  $("reminder-smtp-port").value = config.smtp_port || 587;
  $("reminder-smtp-encryption").value = config.smtp_encryption || "starttls";
  $("reminder-smtp-username").value = config.smtp_username || "";
  $("reminder-smtp-password").value = "";
  const password = data.password || {};
  $("reminder-password-hint").textContent = password.configured
    ? `已保存 ${password.masked || ""}；更新设置时留空保持不变。`
    : "尚未保存 SMTP 密码/授权码。";
  $("reminder-timezone").textContent = `时间按本机时区解释：${data.timezone || "本机时区"}`;
  const status = data.status || {};
  const stateKey = config.enabled ? status.state || "waiting" : "not_enabled";
  const badge = $("reminder-state-badge");
  badge.textContent = REMINDER_STATE_LABELS[stateKey] || stateKey;
  badge.className = "badge";
  if (stateKey === "sent") badge.classList.add("ok");
  else if (stateKey === "failed") badge.classList.add("danger");
  else if (["waiting", "no_tasks", "config_incomplete", "unconfirmed"].includes(stateKey)) badge.classList.add("warn");
  $("toggle-reminder").textContent = config.enabled ? "暂停提醒" : "启用提醒";
  const details = [];
  if (status.last_check_at) details.push(`最近检查：${formatMoment(status.last_check_at)}`);
  if (status.task_count !== undefined && status.task_count !== null) {
    details.push(`待办：${status.task_count} 项 / ${status.minutes || 0} 分钟`);
  }
  if (status.error) details.push(`原因：${status.error}`);
  if (data.next_check_at) details.push(`下一次预计检查：${formatMoment(data.next_check_at)}`);
  if (data.scheduler?.message) details.push(`本机调度：${data.scheduler.message}`);
  const lastTest = status.last_test;
  if (lastTest) {
    const label = REMINDER_STATE_LABELS[lastTest.state] || lastTest.state;
    details.push(`最近测试邮件：${label}${lastTest.at ? `（${formatMoment(lastTest.at)}）` : ""}${lastTest.error ? ` · ${lastTest.error}` : ""}`);
  }
  $("reminder-status").textContent = details.join("\n");
}

function formatMoment(value) {
  if (!value) return "";
  return String(value).replace("T", " ").slice(0, 16);
}

async function loadReminder() {
  try {
    renderReminder(await api("/api/settings/reminder"));
  } catch (error) {
    setStatus("reminder-status", error.message, true);
  }
}

async function persistReminder() {
  const data = await api("/api/settings/reminder", {
    method: "PUT",
    body: JSON.stringify(reminderFormValues()),
  });
  renderReminder(data);
  return data;
}

async function saveReminder(event) {
  event.preventDefault();
  const button = $("save-reminder");
  button.disabled = true;
  setStatus("reminder-status", "正在保存提醒设置…");
  try {
    await persistReminder();
    setStatus("reminder-status", "提醒设置已保存；密码只写入本机受保护存储。");
  } catch (error) {
    await loadReminder();
    setStatus("reminder-status", error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function previewReminder() {
  const button = $("preview-reminder");
  button.disabled = true;
  setStatus("reminder-test-status", "正在生成今天的提醒预览…");
  try {
    const data = await api("/api/settings/reminder/preview", { method: "POST" });
    $("reminder-preview-wrap").hidden = false;
    $("reminder-preview-subject").textContent = data.subject || "今日提醒";
    $("reminder-preview").textContent = data.text || "";
    setStatus(
      "reminder-test-status",
      data.task_count ? `预览生成：${data.task_count} 项 / ${data.minutes} 分钟；未发送。` : "今天没有待办任务；预览未发送。"
    );
  } catch (error) {
    setStatus("reminder-test-status", error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function testReminder() {
  const email = $("reminder-email").value.trim();
  if (!email) {
    setStatus("reminder-test-status", "请先填写收件邮箱。", true);
    return;
  }
  if (!window.confirm(`确认向 ${email} 发送一封测试邮件吗？测试不会占用当天提醒次数。`)) {
    setStatus("reminder-test-status", "已取消发送测试邮件。");
    return;
  }
  const button = $("test-reminder");
  button.disabled = true;
  setStatus("reminder-test-status", "正在发送测试邮件…");
  try {
    const data = await api("/api/settings/reminder/test", {
      method: "POST",
      body: JSON.stringify({ confirm_email: email }),
    });
    const result = data.result || {};
    setStatus(
      "reminder-test-status",
      result.ok ? "测试邮件已由邮件服务接收。" : `测试邮件失败：${result.message || "未知原因"}`,
      !result.ok
    );
    renderReminder(data.reminder || await api("/api/settings/reminder"));
  } catch (error) {
    setStatus("reminder-test-status", error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function toggleReminder() {
  if (!state.reminder) return;
  const button = $("toggle-reminder");
  button.disabled = true;
  try {
    if (state.reminder.config?.enabled) {
      const data = await api("/api/settings/reminder/pause", { method: "POST" });
      renderReminder(data);
      setStatus("reminder-status", "学习提醒已暂停；即使本机调度仍触发也会跳过发送。");
      return;
    }
    await persistReminder();
    const data = await api("/api/settings/reminder/enable", { method: "POST" });
    renderReminder(data);
    setStatus("reminder-status", "学习提醒已启用，并已请求注册本机每 5 分钟检查。");
  } catch (error) {
    await loadReminder();
    setStatus("reminder-status", error.message, true);
  } finally {
    button.disabled = false;
  }
}


const SETTINGS_PANEL_IDS = [
  "settings-person",
  "settings-model",
  "settings-appearance",
  "settings-reminder",
];

function activateSettingsPanel(panelId, { push = true } = {}) {
  const target = SETTINGS_PANEL_IDS.includes(panelId) ? panelId : "settings-person";
  document.querySelectorAll("[data-settings-panel]").forEach((panel) => {
    panel.hidden = panel.id !== target;
  });
  document.querySelectorAll("[data-settings-link]").forEach((link) => {
    const active = link.dataset.settingsLink === target;
    if (active) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  if (push) {
    if (window.location.hash !== `#${target}`) {
      window.history.pushState(null, "", `#${target}`);
    }
  } else if (window.location.hash !== `#${target}`) {
    window.history.replaceState(null, "", `#${target}`);
  }
}

function initSettingsNav() {
  document.querySelectorAll("[data-settings-link]").forEach((link) => {
    link.addEventListener("click", (event) => {
      event.preventDefault();
      activateSettingsPanel(link.dataset.settingsLink);
    });
  });
  const fromHash = () =>
    (window.location.hash || "").replace(/^#/, "");
  window.addEventListener("hashchange", () => {
    activateSettingsPanel(fromHash(), { push: false });
  });
  window.addEventListener("popstate", () => {
    activateSettingsPanel(fromHash(), { push: false });
  });
  activateSettingsPanel(fromHash(), { push: false });
}

function bind() {
  $("profile-form").addEventListener("submit", saveProfile);
  $("open-create-user").addEventListener("click", () => {
    $("create-user-form").hidden = false;
    $("new-user-name").focus();
  });
  $("cancel-create-user").addEventListener("click", () => {
    $("create-user-form").hidden = true;
    setStatus("profile-status", "");
  });
  $("create-user-form").addEventListener("submit", createUser);
  $("switch-user").addEventListener("click", switchUser);
  $("model-form").addEventListener("submit", saveModel);
  $("test-model").addEventListener("click", testModel);
  $("reset-model").addEventListener("click", resetModel);
  $("save-appearance").addEventListener("click", saveAppearance);
  $("reminder-form").addEventListener("submit", saveReminder);
  $("preview-reminder").addEventListener("click", previewReminder);
  $("test-reminder").addEventListener("click", testReminder);
  $("toggle-reminder").addEventListener("click", toggleReminder);
  document
    .querySelectorAll('input[name="theme"], input[name="font_size"], input[name="font_family"]')
    .forEach((input) => input.addEventListener("change", previewAppearance));
  initSettingsNav();
}

bind();
loadIdentity();
loadModel();
loadAppearance();
loadReminder();
