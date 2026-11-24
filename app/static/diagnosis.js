const sessionId = document.body.dataset.sessionId;
const $ = (id) => document.getElementById(id);
let session = null;
let retrySuppressed = false;
let generationPollToken = 0;
let activeJobId = null;
let cancelInFlight = false;
let planVersionId = null;

const SCHEDULE_KEY = "xunzhi_schedule_v1";

function pad2(value) {
  return String(value).padStart(2, "0");
}

function todayISO() {
  const now = new Date();
  return `${now.getFullYear()}-${pad2(now.getMonth() + 1)}-${pad2(now.getDate())}`;
}

function parseLocalDate(value) {
  if (!value) return null;
  const parts = String(value).slice(0, 10).split("-").map(Number);
  if (parts.length !== 3 || parts.some((part) => Number.isNaN(part))) return null;
  return new Date(parts[0], parts[1] - 1, parts[2]);
}

function formatDateLabel(value) {
  const parsed = parseLocalDate(value);
  if (!parsed) return value || "";
  return `${parsed.getFullYear()} 年 ${parsed.getMonth() + 1} 月 ${parsed.getDate()} 日`;
}

function formatMinutes(value) {
  const minutes = Number(value) || 0;
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} 小时 ${rest} 分钟` : `${hours} 小时`;
}

function readScheduleDefaults() {
  try {
    const saved = JSON.parse(localStorage.getItem(SCHEDULE_KEY) || "{}");
    return {
      start_date: saved.start_date || todayISO(),
      duration_days: saved.duration_days ?? null,
      target_date: saved.target_date || null,
      daily_minutes: saved.daily_minutes || 30,
    };
  } catch (_) {
    return { start_date: todayISO(), duration_days: 14, target_date: null, daily_minutes: 30 };
  }
}

function writeScheduleDefaults(options) {
  try {
    localStorage.setItem(SCHEDULE_KEY, JSON.stringify(options));
  } catch (_) {
    // 本机存储不可用时只影响默认值，不影响确认后的日程。
  }
}

function scheduleOptions() {
  const defaults = readScheduleDefaults();
  const saved = session?.schedule_options || {};
  const start = $("diag-schedule-start")?.value || saved.start_date || defaults.start_date || todayISO();
  if (!parseLocalDate(start)) return { error: "请选择有效的开始日期" };
  const savedDuration = Number(saved.duration_days) || null;
  const savedPeriod = savedDuration
    ? [7, 14, 30].includes(savedDuration)
      ? String(savedDuration)
      : "custom"
    : null;
  let fallbackPeriod = savedPeriod;
  if (!fallbackPeriod) {
    fallbackPeriod = !defaults.duration_days && defaults.target_date
      ? "custom"
      : String(defaults.duration_days || 14);
  }
  const period =
    document.querySelector('input[name="diag_schedule_period"]:checked')?.value || fallbackPeriod;
  let duration = null;
  let target = null;
  if (period === "custom") {
    target =
      $("diag-schedule-target")?.value || saved.target_date || defaults.target_date || "";
    if (!parseLocalDate(target)) return { error: "请选择预计完成日期" };
    if (target < start) return { error: "预计完成日期不能早于开始日期" };
  } else {
    duration = Number(period);
  }
  const savedDaily = Number(saved.daily_minutes) || null;
  const dailyDefault = savedDaily || Number(defaults.daily_minutes) || 30;
  const fallbackDaily = [15, 30, 60].includes(dailyDefault)
    ? String(dailyDefault)
    : "custom";
  const dailyChoice =
    document.querySelector('input[name="diag_schedule_daily"]:checked')?.value ||
    fallbackDaily;
  const customDaily = $("diag-schedule-daily-custom")?.value;
  const daily =
    dailyChoice === "custom"
      ? Number(customDaily || dailyDefault)
      : Number(dailyChoice);
  if (!Number.isFinite(daily) || daily < 5 || daily > 1440) {
    return { error: "每天学习时间需要在 5–1440 分钟之间" };
  }
  return {
    options: {
      start_date: start,
      duration_days: duration,
      target_date: target,
      daily_minutes: daily,
    },
  };
}

function setStatus(text, isError = false) {
  const line = $("diagnosis-status");
  line.textContent = text || "";
  line.classList.toggle("error", Boolean(isError));
}

function setGenerationStatus(text, isError = false) {
  const line = $("generation-status");
  if (!line) return;
  line.textContent = text || "";
  line.classList.toggle("error", Boolean(isError));
}

function showGenerationPanel() {
  const panel = $("generation-panel");
  if (!panel) return null;
  panel.hidden = false;
  panel.scrollIntoView({ behavior: "smooth", block: "nearest" });
  return panel;
}

function setError(text) {
  $("answer-error").textContent = text || "";
}

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function renderQuestion() {
  const box = $("current-question");
  box.innerHTML = "";
  const question = session.current_question;
  $("submit-answer").hidden = !question;
  $("skip-answer").hidden = !question;
  $("finish-diagnosis").hidden = !session.can_finish;
  if (!question) {
    const title = node("h2", null, session.status === "preview"
      ? "诊断已整理，可查看计划"
      : session.status === "confirmed"
        ? "诊断已确认"
        : session.last_turn_error
          ? "本轮问题生成失败，可重试"
          : "诊断已结束");
    box.append(title);
    box.append(node("p", "hint", session.last_turn_error
      ? "资料、目标和已有回答都已保留。点击「重试本轮」重新生成本轮问题。"
      : "可以在右侧查看已确认信息；如需修改，点击对应记录的「修改」。"));
    if (session.status === "confirmed") {
      box.append(node("p", "hint", "生成状态见下方「学习计划生成」区域。"));
    }
    if (session.last_turn_error) {
      box.append(node("p", "hint", `本轮问题生成失败：${session.last_turn_error.message}`));
      const retry = document.createElement("button");
      retry.type = "button";
      retry.id = "retry-turn";
      retry.className = "secondary";
      retry.textContent = "重试本轮";
      retry.addEventListener("click", retryTurn);
      box.append(retry);
    }
    if (session.status === "preview") {
      $("finish-diagnosis").hidden = false;
      $("finish-diagnosis").textContent = "查看建议计划";
    }
    title.tabIndex = -1;
    title.focus();
    return;
  }
  box.append(node("p", "meta", `第 ${session.current_round} 轮 · ${question.kind === "diagnostic" ? "诊断小题" : question.kind === "scope" ? "学习范围" : "复习安排"}`));
  const title = node("h2", null, question.stem);
  title.tabIndex = -1;
  box.append(title);
  const form = node("form", "diagnosis-options");
  (question.options || []).forEach((option) => {
    const label = node("label", "option");
    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "diagnosis-answer";
    radio.value = String(option.key.charCodeAt(0) - 65);
    label.append(radio, node("span", null, `${option.key}. ${option.text}`));
    form.append(label);
  });
  box.append(form);
  const first = document.querySelector('input[name="diagnosis-answer"]');
  if (first) first.focus();
}

function renderConfirmed() {
  const list = $("confirmed-list");
  list.innerHTML = "";
  const confirmed = session.confirmed_info || [];
  confirmed.forEach((item) => {
    const row = node("div", "confirmed-row");
    row.append(node("p", "meta", item.kind === "diagnostic" ? "诊断题" : "学习范围"));
    row.append(node("p", null, item.stem));
    const answer = node("p", "hint", item.selected_index === null || item.selected_index === undefined
      ? (item.status === "skipped" ? "已跳过" : "待确认")
      : `你选择：${item.selected_index + 1}`);
    row.append(answer);
    if (item.kind === "diagnostic" && item.is_correct !== null) {
      row.append(node("p", item.is_correct ? "verdict ok" : "verdict bad", item.is_correct ? "回答正确" : "回答错误"));
    }
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "link";
    edit.textContent = "修改";
    edit.addEventListener("click", () => reopen(item.id));
    row.append(edit);
    list.append(row);
  });
  const pending = session.pending_items || [];
  const empty = $("confirmed-empty");
  if (empty) empty.hidden = confirmed.length > 0 || pending.length > 0;
  $("pending-list").textContent = pending.length
    ? `待确认：${pending.map((item) => item.display_name || item.question_id).join("、")}`
    : "";
}

function previewSummary(preview) {
  const lines = [];
  if (preview.over_capacity) {
    lines.push(
      `预计需要 ${formatMinutes(preview.estimated_minutes)}，可用 ${formatMinutes(
        preview.capacity_minutes
      )}${preview.shortfall_minutes ? `，还差 ${formatMinutes(preview.shortfall_minutes)}` : ""}。可以调长周期或增加每天投入。`
    );
  } else {
    lines.push(
      `预计需要 ${formatMinutes(preview.estimated_minutes)}，可用 ${formatMinutes(preview.capacity_minutes)}。`
    );
  }
  lines.push(
    `${formatDateLabel(preview.start_date)} 开始，预计 ${formatDateLabel(preview.planned_end_date)} 完成。`
  );
  return lines.join("\n");
}

function renderSchedulePreview(box, preview) {
  box.innerHTML = "";
  box.classList.remove("error");
  if (!preview) {
    box.append(node("p", "hint", "正在计算日程…"));
    return;
  }
  box.append(node("p", preview.over_capacity ? "hint schedule-warn" : "hint", previewSummary(preview)));
  box.append(node("h3", "schedule-subhead", "每天大概学什么"));
  const list = node("div", "schedule-day-list");
  (preview.days || []).forEach((day) => {
    const row = node("div", "schedule-day-row");
    row.append(node("strong", null, formatDateLabel(day.date)));
    row.append(node("span", "meta", `${day.tasks.length} 项 · ${formatMinutes(day.estimated_minutes)}`));
    row.append(node("p", "hint", day.tasks.map((task) => task.task_title).join("、")));
    list.append(row);
  });
  if (!(preview.days || []).length) {
    list.append(node("p", "hint", "还没有可安排的任务。"));
  }
  box.append(list);
}

async function previewPlanSchedule(box) {
  const result = scheduleOptions();
  if (result.error) {
    box.classList.add("error");
    box.textContent = result.error;
    return null;
  }
  box.classList.remove("error");
  box.textContent = "正在计算日程…";
  try {
    const response = await fetch("/api/schedule/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_version_id: planVersionId, ...result.options }),
    });
    const preview = await response.json();
    if (!response.ok) throw new Error(preview.error?.message || "日程预览失败");
    writeScheduleDefaults(result.options);
    renderSchedulePreview(box, preview);
    return preview;
  } catch (error) {
    box.classList.add("error");
    box.textContent = error.message;
    return null;
  }
}

function renderScheduleEditor(container, version, preview) {
  planVersionId = version.id;
  const defaults = readScheduleDefaults();
  const startValue = preview?.start_date || defaults.start_date || todayISO();
  const duration = Number(preview?.duration_days || defaults.duration_days) || 14;
  const customPeriod = !preview && !defaults.duration_days && Boolean(defaults.target_date);
  const periodValue = customPeriod ? "custom" : [7, 14, 30].includes(duration) ? String(duration) : "custom";
  const targetValue = preview?.target_date || defaults.target_date || "";
  const daily = Number(preview?.daily_minutes || defaults.daily_minutes) || 30;
  const dailyValue = [15, 30, 60].includes(daily) ? String(daily) : "custom";

  const section = node("section", "plan-schedule");
  section.append(node("h3", null, "时间安排"));
  section.append(node("p", "hint", "时间安排只决定节奏；学什么仍由资料、目标和诊断决定。确认前可以随时修改。"));
  const form = node("div", "time-form");

  const startField = node("label", "field");
  startField.append(node("span", "field-label", "开始日期"));
  const startInput = document.createElement("input");
  startInput.type = "date";
  startInput.id = "diag-schedule-start";
  startInput.value = startValue;
  startField.append(startInput);
  form.append(startField);

  const periodSet = node("fieldset", "segmented");
  periodSet.append(node("legend", null, "你希望多久学完？"));
  [["7", "1 周"], ["14", "2 周"], ["30", "1 个月"], ["custom", "自定义完成日期"]].forEach(
    ([value, label]) => {
      const item = document.createElement("label");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "diag_schedule_period";
      radio.value = value;
      radio.checked = value === periodValue;
      item.append(radio, document.createTextNode(` ${label}`));
      periodSet.append(item);
    }
  );
  form.append(periodSet);

  const targetField = node("label", "field");
  targetField.append(node("span", "field-label", "预计完成日期"));
  const targetInput = document.createElement("input");
  targetInput.type = "date";
  targetInput.id = "diag-schedule-target";
  targetInput.value = targetValue;
  targetField.append(targetInput);
  targetField.hidden = periodValue !== "custom";
  form.append(targetField);

  const dailySet = node("fieldset", "segmented");
  dailySet.append(node("legend", null, "每天大概能学多久？"));
  [["15", "15 分钟"], ["30", "30 分钟"], ["60", "60 分钟"], ["custom", "自定义分钟"]].forEach(
    ([value, label]) => {
      const item = document.createElement("label");
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "diag_schedule_daily";
      radio.value = value;
      radio.checked = value === dailyValue;
      item.append(radio, document.createTextNode(` ${label}`));
      dailySet.append(item);
    }
  );
  form.append(dailySet);

  const dailyField = node("label", "field");
  dailyField.append(node("span", "field-label", "每天分钟数"));
  const dailyInput = document.createElement("input");
  dailyInput.type = "number";
  dailyInput.id = "diag-schedule-daily-custom";
  dailyInput.min = "5";
  dailyInput.max = "1440";
  dailyInput.step = "5";
  dailyInput.value = String(daily);
  dailyField.append(dailyInput);
  dailyField.hidden = dailyValue !== "custom";
  form.append(dailyField);

  const previewBox = node("div", "plan-schedule-preview");
  const actions = node("div", "actions");
  const recalc = document.createElement("button");
  recalc.type = "button";
  recalc.className = "secondary";
  recalc.textContent = "重新计算日程";
  recalc.addEventListener("click", () => previewPlanSchedule(previewBox));
  actions.append(recalc);
  form.append(actions, previewBox);
  section.append(form);
  container.append(section);

  section.querySelectorAll('input[name="diag_schedule_period"]').forEach((input) =>
    input.addEventListener("change", () => {
      targetField.hidden = input.value !== "custom";
      if (input.value === "custom" && !targetInput.value) {
        const start = parseLocalDate(startInput.value || todayISO());
        if (start) {
          const target = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 13);
          targetInput.value = `${target.getFullYear()}-${pad2(target.getMonth() + 1)}-${pad2(target.getDate())}`;
        }
      }
    })
  );
  section.querySelectorAll('input[name="diag_schedule_daily"]').forEach((input) =>
    input.addEventListener("change", () => {
      dailyField.hidden = input.value !== "custom";
    })
  );
  if (preview) renderSchedulePreview(previewBox, preview);
  else previewPlanSchedule(previewBox);
}

function renderPlan(data) {
  const panel = $("plan-preview");
  panel.hidden = false;
  panel.innerHTML = "";
  const heading = node("h2", null, "本次学习路线（待确认）");
  heading.tabIndex = -1;
  panel.append(heading);
  const version = data.version;
  const modules = version.modules || data.modules || [];
  const modeLabel = { auto: "系统建议", foundation: "从基础学", focus: "重点复习" }[
    (version.scope || {}).learning_mode || "auto"
  ] || "系统建议";
  panel.append(
    node(
      "p",
      "meta",
      `${modeLabel} · ${modules.length || (version.items || []).length} 个模块 · 建议 ${version.question_count} 道练习`
    )
  );
  const goalText = String((version.scope || {}).goal || session?.goal || "按诊断确认的范围").replace(/[。！？!?]+$/, "");
  panel.append(node("p", "plan-scope", `这次学什么：${goalText}。`));
  if (modules.length) {
    modules.forEach((module, index) => {
      const card = node("div", "plan-module");
      const head = node("div", "plan-module-head");
      head.append(node("span", "plan-ordinal", `模块 ${index + 1}`));
      head.append(node("strong", null, module.title || `模块 ${index + 1}`));
      head.append(node("span", "badge accent", `${module.suggested_question_count || 0} 道`));
      card.append(head);
      card.append(node("p", "hint", `学习目标：${module.objective || "掌握本模块的核心概念与应用。"}`));
      card.append(
        node(
          "p",
          "hint",
          `核心考点：${(module.core_points || module.knowledge_point_ids || []).join("、")}`
        )
      );
      const sources = module.source_scopes || [];
      if (sources.length) {
        const sourceText = sources.map((item) => {
          const name = item.source_name || item.source_id;
          if (item.page_start === null || item.page_start === undefined) return name;
          const end = item.page_end === null || item.page_end === undefined ? item.page_start : item.page_end;
          return `${name}（第 ${item.page_start}–${end} 页）`;
        }).join("、");
        card.append(node("p", "hint", `覆盖资料：${sourceText}`));
      } else if (module.source_names?.length) {
        card.append(node("p", "hint", `覆盖资料：${module.source_names.join("、")}`));
      }
      card.append(node("p", "hint", `完成标准：${module.completion_standard || "能独立完成本模块练习。"}`));
      if (module.rationale) card.append(node("p", "plan-reason", `安排理由：${module.rationale}`));
      if (module.basis?.evidence_ids?.length) {
        card.append(node("p", "hint", `依据：${module.basis.evidence_ids.length} 条历史作答证据`));
      }
      panel.append(card);
    });
  } else {
    (version.items || []).forEach((item) => {
      panel.append(node("p", "adjustment-row", `${item.display_name}：${item.suggested_question_count} 题`));
    });
  }
  (version.adjustment || []).forEach((item) => {
    panel.append(
      node(
        "p",
        "hint",
        `${item.display_name}：上次 ${item.previous_question_count} 道 → 本次 ${item.current_question_count} 道。${item.reason}`
      )
    );
  });
  const scope = version.scope || {};
  if (scope.unlearned?.length) {
    panel.append(node("p", "hint", `本轮未纳入：${scope.unlearned.join("、")}。`));
  }
  if (data.unplaced_points?.length) {
    panel.append(
      node(
        "p",
        "hint",
        `未安排的考点：${data.unplaced_points.map((item) => item.display_name || item.knowledge_point_id).join("、")}。`
      )
    );
  }
  if (data.excluded_points?.length) {
    panel.append(
      node(
        "p",
        "hint",
        `未纳入的内容：${data.excluded_points.map((item) => item.display_name || item.knowledge_point_id).join("、")}（可能是学习意图或该页段缺少可核对原文）。`
      )
    );
  }
  (data.missing || []).forEach((item) => {
    panel.append(node("p", "hint", `待补齐：${item}`));
  });
  renderScheduleEditor(panel, version, data.schedule_preview || null);
  const confirm = document.createElement("button");
  confirm.type = "button";
  confirm.className = "primary";
  confirm.textContent = "确认学习路线并生成";
  confirm.addEventListener("click", confirmPlan);
  panel.append(confirm);
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
  heading.focus();
}

function clearGenerationActions() {
  const box = $("generation-actions");
  if (box) box.innerHTML = "";
}

function showOpenPack(packId) {
  const box = $("generation-actions");
  if (!box || !packId) return;
  const link = document.createElement("a");
  link.id = "open-pack";
  link.className = "secondary";
  link.href = `/packs/${packId}`;
  link.textContent = "查看学习计划";
  box.append(link);
}

function showOpenPlan(planVersionId, planId) {
  const box = $("generation-actions");
  if (!box || !planVersionId || !planId) return;
  const link = document.createElement("a");
  link.id = "open-plan";
  link.className = "secondary";
  link.href = `/records?plan_id=${encodeURIComponent(planId)}&version_id=${encodeURIComponent(planVersionId)}`;
  link.textContent = "查看已确认计划";
  box.append(link);
}

function removeRetryButton() {
  const existing = $("retry-generation");
  if (existing) existing.remove();
}

function removeCancelGeneration() {
  const existing = $("cancel-generation");
  if (existing) existing.remove();
}

function showCancelGeneration(jobId) {
  const box = $("generation-actions");
  if (!box || !jobId) return;
  const existing = $("cancel-generation");
  if (existing) return;
  const cancel = document.createElement("button");
  cancel.id = "cancel-generation";
  cancel.type = "button";
  cancel.className = "secondary";
  cancel.textContent = "取消生成";
  cancel.addEventListener("click", cancelGeneration);
  box.append(cancel);
}

function applyCancelledState(job) {
  removeCancelGeneration();
  removeRetryButton();
  clearGenerationActions();
  retrySuppressed = false;
  if (session?.generation) {
    session.generation.actions = [];
    session.generation.job_status = "cancelled";
    session.generation.retry_available = job?.retry_available !== false;
    session.generation.error = null;
  }
  const canRetry = job?.retry_available !== false;
  if (canRetry) {
    setGenerationStatus("已取消，资料和学习计划已保留。");
    showRetryGeneration();
  } else {
    setGenerationStatus("已取消，资料和学习计划已保留。已达到重试上限，可返回诊断调整方案后重新生成。");
  }
}

async function refreshFinishedJob(jobId) {
  let job;
  try {
    const response = await fetch(`/api/jobs/${jobId}`);
    job = await response.json();
    if (!response.ok) throw new Error(job.error?.message || "任务状态读取失败");
  } catch (error) {
    setGenerationStatus(`任务状态读取失败：${error.message}。可刷新页面查看最新状态。`, true);
    return;
  }
  if (job.status === "succeeded" && job.pack_id) {
    setGenerationStatus("正式学习计划已生成。");
    clearGenerationActions();
    showOpenPack(job.pack_id);
    return;
  }
  if (job.status === "cancelled") {
    applyCancelledState(job);
    return;
  }
  if (["failed", "insufficient_evidence"].includes(job.status)) {
    removeCancelGeneration();
    if (job.retry_available === false) {
      retrySuppressed = true;
      setGenerationStatus(`${job.error?.message || "生成失败"}（已达到重试上限）`, true);
      return;
    }
    retrySuppressed = false;
    setGenerationStatus(job.error?.message || "生成失败，可按同一计划重试", true);
    showRetryGeneration();
    return;
  }
  if (job.status === "queued" || job.status === "running") {
    showCancelGeneration(jobId);
    pollGeneration(jobId);
    return;
  }
  setGenerationStatus("暂无可执行操作，可刷新页面查看最新状态。");
}

async function cancelGeneration() {
  if (cancelInFlight || !activeJobId) return;
  const jobId = activeJobId;
  const button = $("cancel-generation");
  if (!button) return;
  cancelInFlight = true;
  button.disabled = true;
  button.textContent = "正在取消…";
  setGenerationStatus("正在取消生成…");
  generationPollToken += 1;
  activeJobId = null;
  let response;
  let data = {};
  try {
    response = await fetch(`/api/jobs/${jobId}/cancel`, { method: "POST" });
    try {
      data = await response.json();
    } catch (_) {
      data = {};
    }
  } catch (error) {
    cancelInFlight = false;
    const current = $("cancel-generation");
    if (current) {
      current.disabled = false;
      current.textContent = "取消生成";
    }
    setGenerationStatus("网络异常，取消失败；可重新点击取消，或继续等待生成。", true);
    pollGeneration(jobId);
    return;
  }
  cancelInFlight = false;
  if (response.status === 202) {
    applyCancelledState({ retry_available: data.retry_available !== false });
    return;
  }
  if (response.status === 409 && data.error?.code === "job_finished") {
    await refreshFinishedJob(jobId);
    return;
  }
  const current = $("cancel-generation");
  if (current) {
    current.disabled = false;
    current.textContent = "取消生成";
  }
  setGenerationStatus(data.error?.message || "取消失败，可重新点击取消。", true);
  pollGeneration(jobId);
}

function renderGeneration() {
  const panel = $("generation-panel");
  if (!panel) return;
  if (!session || session.status !== "confirmed") {
    panel.hidden = true;
    clearGenerationActions();
    return;
  }
  panel.hidden = false;
  retrySuppressed = false;
  clearGenerationActions();
  const generation = session.generation;
  if (!generation) {
    setGenerationStatus("诊断已确认，尚未创建生成任务。");
    showRetryGeneration();
    return;
  }
  const actions = generation.actions || [];
  if (actions.includes("open_pack") && generation.pack_id) {
    setGenerationStatus("正式学习计划已生成。");
    retrySuppressed = true;
    removeRetryButton();
    showOpenPack(generation.pack_id);
    return;
  }
  if (actions.includes("open_plan") && generation.plan_id) {
    setGenerationStatus(generation.error?.message || "学习计划已不可用。");
    retrySuppressed = true;
    removeRetryButton();
    showOpenPlan(generation.plan_version_id, generation.plan_id);
    return;
  }
  if (generation.job_status === "cancelled") {
    applyCancelledState(generation);
    return;
  }
  if (actions.includes("replan")) {
    retrySuppressed = true;
    setGenerationStatus(
      generation.error?.message || "原计划无法区分部分考点，需重新制定学习计划",
      true
    );
    removeRetryButton();
    showReplanPlan();
    return;
  }
  if (actions.includes("poll") && generation.job_id) {
    setGenerationStatus(generation.job_status === "running" ? "正在生成正式学习计划…" : "任务正在启动…");
    showCancelGeneration(generation.job_id);
    pollGeneration(generation.job_id);
    return;
  }
  if (generation.retry_available) {
    setGenerationStatus(generation.error?.message
      || (generation.job_id ? "任务待重新调度，可点击「重试生成」继续。" : "尚未创建生成任务，可点击「重试生成」。"));
    if (generation.error?.code === "plan_point_ambiguous") {
      showReplanPlan();
    }
    showRetryGeneration();
    if (generation.job_id && generation.job_status === "queued") {
      showCancelGeneration(generation.job_id);
    }
    return;
  }
  if (generation.error) {
    setGenerationStatus(generation.error.message || "学习计划生成失败", true);
    if (generation.error.code === "plan_point_ambiguous") {
      showReplanPlan();
    }
  } else {
    setGenerationStatus("暂无可用操作，可刷新页面查看最新状态。");
  }
  retrySuppressed = true;
  removeRetryButton();
}

async function load() {
  const response = await fetch(`/api/diagnosis/sessions/${sessionId}`);
  const data = await response.json();
  if (!response.ok) {
    setStatus(data.error?.message || "诊断读取失败", true);
    return;
  }
  session = data;
  setStatus("");
  renderQuestion();
  renderConfirmed();
  renderGeneration();
}

async function answer(status) {
  const question = session.current_question;
  if (!question) return;
  const selected = document.querySelector('input[name="diagnosis-answer"]:checked');
  if (status === "answered" && !selected) {
    setError("请先选择一个选项。");
    return;
  }
  setError("");
  const response = await fetch(`/api/diagnosis/sessions/${sessionId}/answers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      idempotency_key: crypto.randomUUID(),
      question_id: question.id,
      selected_index: selected ? Number(selected.value) : null,
      status,
    }),
  });
  const data = await response.json();
  if (!response.ok) {
    setError(data.error?.message || "提交失败，请重试");
    return;
  }
  session = data;
  renderQuestion();
  renderConfirmed();
  if (data.error) {
    setError(data.error.message || "提交失败，请重试");
    return;
  }
  const next = document.querySelector('input[name="diagnosis-answer"]');
  if (next) next.focus();
}

async function skip() {
  const question = session.current_question;
  if (!question) return;
  const confirmText = session.current_round === 1 ? "首轮跳过会保留为待确认，继续吗？" : "跳过只表示缺少信息，继续吗？";
  if (!window.confirm(confirmText)) return;
  await answer(window.confirm("选择「不确定」还是仅跳过？\n确定=不确定，取消=跳过") ? "uncertain" : "skipped");
}

async function reopen(answerId) {
  const response = await fetch(`/api/diagnosis/sessions/${sessionId}/answers/${answerId}/reopen`, {
    method: "POST",
  });
  const data = await response.json();
  if (!response.ok) {
    setError(data.error?.message || "回改失败");
    return;
  }
  session = data;
  $("plan-preview").hidden = true;
  renderQuestion();
  renderConfirmed();
  if (data.error) {
    setStatus(data.error.message || "回改失败", true);
    return;
  }
  setStatus("已回到这一轮，后续依赖答案已失效，请重新回答。");
}

async function finish() {
  const time = scheduleOptions();
  if (time.error) {
    setStatus(time.error, true);
    return;
  }
  writeScheduleDefaults(time.options);
  const response = await fetch(`/api/diagnosis/sessions/${sessionId}/finish`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      goal: session.goal || "",
      learning_mode: session.learning_mode || "auto",
      ...time.options,
    }),
  });
  const data = await response.json();
  if (!response.ok) {
    setStatus(data.error?.message || "计划预览生成失败", true);
    return;
  }
  session = data.session;
  renderQuestion();
  renderConfirmed();
  renderPlan(data);
  setStatus("计划预览已生成；确认后才会写入正式诊断证据并生成学习计划。");
}

async function retryTurn() {
  const response = await fetch(`/api/diagnosis/sessions/${sessionId}/retry`, { method: "POST" });
  const data = await response.json();
  if (!response.ok) {
    setStatus(data.error?.message || "本轮重试失败", true);
    return;
  }
  session = data;
  renderQuestion();
  renderConfirmed();
  if (data.error) {
    setStatus(data.error.message || "本轮重试失败", true);
    return;
  }
  setStatus("本轮已重新生成。");
}

async function pollGeneration(jobId) {
  if (!jobId || activeJobId === jobId) return;
  const token = ++generationPollToken;
  activeJobId = jobId;
  const panel = $("generation-panel");
  if (panel) panel.hidden = false;
  try {
    for (let index = 0; index < 120; index += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      if (token !== generationPollToken) return;
      const response = await fetch(`/api/jobs/${jobId}`);
      let job;
      try {
        job = await response.json();
      } catch (error) {
        setGenerationStatus("生成状态读取失败，可刷新后重试", true);
        return;
      }
      if (!response.ok) {
        setGenerationStatus("生成状态读取失败，可刷新后重试", true);
        return;
      }
      if (job.status === "succeeded" && job.pack_id) {
        removeCancelGeneration();
        setGenerationStatus("正式学习计划已生成。");
        if (session && session.generation) {
          session.generation.pack_id = job.pack_id;
          session.generation.job_status = job.status;
          session.generation.actions = ["open_pack"];
        }
        const box = $("generation-actions");
        if (box) {
          clearGenerationActions();
          showOpenPack(job.pack_id);
        }
        return;
      }
      if (job.status === "cancelled") {
        applyCancelledState(job);
        return;
      }
      if (["failed", "insufficient_evidence"].includes(job.status)) {
        removeCancelGeneration();
        const needsReplan = (job.actions || []).includes("replan")
          || job.error?.code === "plan_point_ambiguous";
        if (needsReplan) {
          retrySuppressed = true;
          hideRetryGeneration();
          setGenerationStatus(job.error?.message || "原计划无法区分部分考点，需重新制定学习计划", true);
          showReplanPlan();
          return;
        }
        if (job.retry_available === false) {
          hideRetryGeneration();
          setGenerationStatus(`${job.error?.message || "生成失败"}（已达到重试上限）`, true);
          return;
        }
        setGenerationStatus(job.error?.message || "生成失败，可按同一计划重试", true);
        showRetryGeneration();
        return;
      }
      if (job.status === "queued" && job.retry_available === true) {
        removeCancelGeneration();
        setGenerationStatus("任务待重新调度，可点击「重试生成」继续。");
        showRetryGeneration();
        return;
      }
      if (job.status === "queued" || job.status === "running") {
        showCancelGeneration(jobId);
        setGenerationStatus(job.status === "running" ? "正在生成正式学习计划…" : "任务正在启动…");
      }
    }
    removeCancelGeneration();
    setGenerationStatus("生成仍在进行，可稍后到学习记录查看", true);
  } finally {
    if (token === generationPollToken) activeJobId = null;
  }
}

function hideRetryGeneration() {
  retrySuppressed = true;
  const existing = $("retry-generation");
  if (existing) existing.remove();
}

function showReplanPlan() {
  const box = $("generation-actions");
  if (!box || box.querySelector("#replan-plan")) return;
  const button = document.createElement("button");
  button.id = "replan-plan";
  button.type = "button";
  button.className = "secondary";
  button.textContent = "重新制定学习计划";
  button.addEventListener("click", replanPlan);
  box.append(button);
}

async function replanPlan() {
  const time = scheduleOptions();
  if (time.error) {
    setGenerationStatus(time.error, true);
    return;
  }
  writeScheduleDefaults(time.options);
  const button = $("replan-plan");
  if (button) {
    button.disabled = true;
    button.textContent = "正在重新制定…";
  }
  setGenerationStatus("正在根据已确认的资料和回答重新制定学习计划…");
  let response;
  let data;
  try {
    response = await fetch(`/api/diagnosis/sessions/${sessionId}/replan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        goal: session?.goal || "",
        question_count: null,
        learning_mode: session?.learning_mode || "auto",
        ...time.options,
      }),
    });
    data = await response.json();
  } catch (error) {
    setGenerationStatus("网络异常，重新制定学习计划失败，可重试。", true);
    if (button) {
      button.disabled = false;
      button.textContent = "重新制定学习计划";
    }
    return;
  }
  if (!response.ok) {
    setGenerationStatus(data.error?.message || "重新制定学习计划失败，可重试。", true);
    if (button) {
      button.disabled = false;
      button.textContent = "重新制定学习计划";
    }
    return;
  }
  session = data.session;
  renderQuestion();
  renderConfirmed();
  renderPlan(data);
  setStatus("已生成新的学习计划，请确认后重新生成学习计划。");
  setGenerationStatus("");
  clearGenerationActions();
  const panel = $("generation-panel");
  if (panel) panel.hidden = true;
}

function showRetryGeneration() {
  if (retrySuppressed) return;
  const box = $("generation-actions");
  if (!box || box.querySelector("#retry-generation")) return;
  const retry = document.createElement("button");
  retry.id = "retry-generation";
  retry.type = "button";
  retry.className = "secondary";
  retry.textContent = "重试生成";
  retry.addEventListener("click", confirmPlan);
  box.append(retry);
}

function mergeGeneration(data) {
  if (!session) return;
  session.generation = {
    ...(session.generation || {}),
    job_id: data.job_id ?? null,
    job_status: data.job_status ?? null,
    attempt_no: data.attempt_no ?? session.generation?.attempt_no ?? 0,
    retry_available: data.retry_available === true,
    container_pack_id: data.container_pack_id ?? null,
    pack_id: data.pack_id ?? null,
    plan_id: data.plan_id ?? null,
    plan_version_id: session.plan_version_id ?? null,
    error: data.error || null,
    actions: data.actions || [],
  };
}

async function confirmPlan() {
  const time = scheduleOptions();
  if (time.error) {
    setGenerationStatus(time.error, true);
    return;
  }
  writeScheduleDefaults(time.options);
  const retry = $("retry-generation");
  if (retry) {
    retry.disabled = true;
    retry.textContent = "正在重试…";
  }
  showGenerationPanel();
  setGenerationStatus("正在确认计划并启动生成…");
  let response;
  let data;
  try {
    response = await fetch(`/api/diagnosis/sessions/${sessionId}/confirm`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        idempotency_key: crypto.randomUUID(),
        ...time.options,
      }),
    });
    data = await response.json();
  } catch (error) {
    setGenerationStatus("网络异常，确认结果未知，请刷新后查看生成状态。", true);
    if (retry) {
      retry.disabled = false;
      retry.textContent = "重试生成";
    }
    return;
  }
  if (!response.ok) {
    setGenerationStatus(data.error?.message || "确认失败", true);
    if (retry) {
      retry.disabled = false;
      retry.textContent = "重试生成";
    }
    return;
  }
  if (data.session) session = data.session;
  mergeGeneration(data);
  if (!session || session.status !== "confirmed") {
    setGenerationStatus(data.error?.message || "确认未完成，请重试确认。", true);
    if (data.retry_available === true) {
      retrySuppressed = false;
      showRetryGeneration();
    } else {
      retrySuppressed = true;
      removeRetryButton();
    }
    return;
  }
  renderGeneration();
  showGenerationPanel();
}

const confirmedDetails = document.querySelector(".confirmed-details");
if (confirmedDetails && window.matchMedia("(max-width: 860px)").matches) {
  confirmedDetails.open = false;
}

$("submit-answer").addEventListener("click", () => answer("answered"));
$("skip-answer").addEventListener("click", skip);
$("finish-diagnosis").addEventListener("click", finish);
load();