const $ = (id) => document.getElementById(id);
const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
const SCHEDULE_KEY = "xunzhi_schedule_v1";

const state = {
  data: null,
  planId: null,
  versionId: null,
  month: null,
  selectedDate: null,
  loading: false,
  shiftPreview: null,
  reschedule: null,
  lastMoved: null,
};

function pad(value) {
  return String(value).padStart(2, "0");
}

function todayISO() {
  const now = new Date();
  return `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
}

function parseLocalDate(value) {
  if (!value) return null;
  const parts = String(value).slice(0, 10).split("-").map(Number);
  if (parts.length !== 3 || parts.some((part) => Number.isNaN(part))) return null;
  return new Date(parts[0], parts[1] - 1, parts[2]);
}

function isoOf(date) {
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function formatDateLabel(value) {
  const parsed = parseLocalDate(value);
  if (!parsed) return value || "";
  const suffix = WEEKDAYS[parsed.getDay()];
  const prefix = value === todayISO() ? "今天 · " : "";
  return `${prefix}${parsed.getMonth() + 1} 月 ${parsed.getDate()} 日 ${suffix}`;
}

function formatShortDate(value) {
  const parsed = parseLocalDate(value);
  if (!parsed) return value || "";
  return `${parsed.getMonth() + 1} 月 ${parsed.getDate()} 日`;
}

function formatMinutes(value) {
  const minutes = Number(value) || 0;
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} 小时 ${rest} 分钟` : `${hours} 小时`;
}

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function scheduleDefaults() {
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

function saveScheduleDefaults(options) {
  try {
    localStorage.setItem(SCHEDULE_KEY, JSON.stringify(options));
  } catch (_) {
    // 本机存储不可用时只影响下次默认值，不影响当前日程。
  }
}

function setFeedback(text, isError = false) {
  const line = $("calendar-feedback");
  if (!line) return;
  line.textContent = text || "";
  line.classList.toggle("error", Boolean(isError));
}

function updateUrl() {
  const params = new URLSearchParams();
  if (state.planId) params.set("plan_id", state.planId);
  if (state.versionId) params.set("version_id", state.versionId);
  if (state.month) params.set("month", state.month);
  if (state.selectedDate) params.set("date", state.selectedDate);
  const query = params.toString();
  history.replaceState(null, "", `/calendar${query ? `?${query}` : ""}`);
}

async function load() {
  if (state.loading) return;
  state.loading = true;
  const params = new URLSearchParams();
  if (state.planId) params.set("plan_id", state.planId);
  if (state.versionId) params.set("version_id", state.versionId);
  if (state.month) params.set("month", state.month);
  if (state.selectedDate) params.set("date", state.selectedDate);
  try {
    const response = await fetch(`/api/schedule?${params.toString()}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || "学习日历读取失败");
    state.data = data;
    state.month = data.month;
    state.selectedDate = data.selected_date;
    state.planId = data.plan?.id || null;
    if (state.versionId && data.plan?.version_id !== state.versionId) {
      state.versionId = null;
    }
    render();
    updateUrl();
  } catch (error) {
    setFeedback(error.message, true);
  } finally {
    state.loading = false;
  }
}

function render() {
  const data = state.data;
  if (!data) return;
  renderCalendar();
  renderPlan();
  renderSelected();
  renderShift();
  renderBrief();
}

function renderCalendar() {
  const data = state.data;
  const monthLabel = $("calendar-month");
  const [year, month] = data.month.split("-").map(Number);
  monthLabel.textContent = `${year} 年 ${month} 月`;
  const grid = $("calendar-grid");
  grid.innerHTML = "";
  const first = new Date(year, month - 1, 1);
  const offset = (first.getDay() + 6) % 7;
  for (let index = 0; index < offset; index += 1) {
    const blank = node("span", "calendar-cell is-blank");
    blank.setAttribute("aria-hidden", "true");
    grid.append(blank);
  }
  const days = new Date(year, month, 0).getDate();
  for (let day = 1; day <= days; day += 1) {
    const value = `${year}-${pad(month)}-${pad(day)}`;
    const info = data.month_days[value] || null;
    const cell = document.createElement("button");
    cell.type = "button";
    cell.className = "calendar-cell";
    cell.dataset.date = value;
    if (value === data.today) cell.classList.add("is-today");
    if (value === data.selected_date) cell.classList.add("is-selected");
    if (info && info.total) cell.classList.add("has-tasks");
    if (info && info.total && info.completed === info.total) cell.classList.add("is-complete");
    if (info && info.overdue > 0) cell.classList.add("has-overdue");
    cell.append(node("span", "calendar-day", String(day)));
    if (info && info.total) {
      const markers = node("span", "calendar-markers");
      markers.append(node("i", "calendar-marker"));
      if (info.completed) markers.append(node("i", "calendar-marker completed"));
      if (info.overdue) markers.append(node("i", "calendar-marker overdue"));
      cell.append(markers);
    }
    cell.setAttribute(
      "aria-label",
      `${month} 月 ${day} 日${info && info.total ? `，${info.total} 项任务` : "，没有任务"}`
    );
    cell.addEventListener("click", () => {
      state.lastMoved = null;
      state.selectedDate = value;
      state.month = value.slice(0, 7);
      load();
    });
    grid.append(cell);
  }
}

function renderPlan() {
  const data = state.data;
  const plan = data.plan;
  const select = $("plan-select");
  select.innerHTML = "";
  (data.plans || []).forEach((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = `${item.display_title || item.title}${item.unassigned ? " · 未安排日程" : ` · ${item.progress.label}`}`;
    select.append(option);
  });
  select.disabled = (data.plans || []).length <= 1;
  $("plan-empty").hidden = Boolean(plan);
  $("plan-body").hidden = !plan;
  const unassignedPanel = $("unassigned-panel");
  unassignedPanel.hidden = !plan || !plan.unassigned;
  if (!plan) {
    $("plan-name").textContent = "";
    $("plan-range").textContent = "";
    $("plan-progress-label").textContent = "";
    $("plan-progress-bar").style.width = "0%";
    $("plan-pack-status").textContent = "";
    return;
  }
  select.value = plan.id;
  $("plan-name").textContent = plan.display_title || plan.title || plan.pack_title || "学习计划";
  const progress = data.progress;
  const schedule = data.schedule || {};
  if (plan.unassigned) {
    $("plan-range").textContent = "还没有开始日期与每日安排";
    $("plan-progress-label").textContent = "未安排日程";
  } else {
    const start = schedule.start_date ? `${formatShortDate(schedule.start_date)} 开始` : "";
    const end = schedule.planned_end_date
      ? `预计 ${formatShortDate(schedule.planned_end_date)} 完成`
      : schedule.target_date
        ? `计划 ${formatShortDate(schedule.target_date)} 完成`
        : "";
    $("plan-range").textContent = [start, end].filter(Boolean).join(" · ");
    $("plan-progress-label").textContent =
      progress.label +
      (progress.today_total
        ? ` · 今天 ${progress.today_completed} / ${progress.today_total} 项`
        : "");
  }
  $("plan-progress-bar").style.width = `${progress.percent}%`;
  const status = $("plan-pack-status");
  if (plan.pack_status === "ready") {
    status.textContent = "学习内容已生成，可以从任务直接进入。";
  } else if (plan.pack_status === "pending") {
    status.textContent = "学习内容还在生成。完成后可以在这里直接进入任务。";
  } else {
    status.textContent = "这个计划还没有可打开的学习内容。";
  }
  fillScheduleForm();
}

function stateLabel(task) {
  return {
    completed: "已完成",
    overdue: "待补",
    today: "今天",
    upcoming: "未开始",
  }[task.state] || "未开始";
}

function typeLabel(type) {
  return { reading: "阅读", practice: "练习", review: "回顾" }[type] || "学习";
}

function taskAction(task, plan) {
  if (task.status === "completed") {
    if (task.action_url) {
      const link = document.createElement("a");
      link.className = "link";
      link.href = task.action_url;
      link.textContent = "再看一遍";
      return link;
    }
    return node("span", "meta", "已完成");
  }
  if (task.can_mark_complete) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.textContent = "标记学完";
    button.addEventListener("click", () => markComplete(task.id, button));
    return button;
  }
  if (task.task_type === "practice" && task.practice_available === false) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.disabled = true;
    button.textContent = "本组暂无题目";
    button.title = task.practice_unavailable_reason || "本组没有可对应题目";
    return button;
  }
  if (task.action_url) {
    const link = document.createElement("a");
    link.className = "primary link-button";
    link.href = task.action_url;
    link.textContent = "做本组";
    return link;
  }
  if (plan?.session_id) {
    const link = document.createElement("a");
    link.className = "secondary link-button";
    link.href = `/diagnosis/${plan.session_id}`;
    link.textContent = "查看生成进度";
    return link;
  }
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary";
  button.disabled = true;
  button.textContent = "等待学习内容";
  return button;
}

async function markComplete(taskId, button) {
  if (button) {
    button.disabled = true;
    button.textContent = "正在保存…";
  }
  try {
    const response = await fetch(`/api/schedule/tasks/${taskId}/complete`, { method: "POST" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || "标记失败");
    setFeedback("已标记学完，进度已更新。");
    await load();
  } catch (error) {
    setFeedback(error.message, true);
    if (button) {
      button.disabled = false;
      button.textContent = "标记学完";
    }
  }
}

function feedbackText(data, plan) {
  const progress = data.progress;
  if (!plan) return "还没有学习计划。先在首页新建学习并确认学习计划。";
  if (plan.unassigned) {
    return "这条学习计划还没有安排日程。定一个开始日期和每天的学习时间，就能看到每天的任务。";
  }
  const total = (data.selected_tasks || []).length;
  const done = (data.selected_tasks || []).filter((task) => task.status === "completed").length;
  if (data.selected_date === data.today) {
    if (total > 0 && done === total) {
      return data.next_task && data.next_task.action_url
        ? `今天的 ${total} 项都完成了。想再学一点，可以接着看下一项。`
        : "今天的任务都完成了，先休息也很好。";
    }
    if (total > 0) {
      return `今天还有 ${total - done} 项任务，按自己的节奏来。${
        progress.overdue_count > 0 ? `另有 ${progress.overdue_count} 项待补，可以慢慢补上。` : ""
      }`;
    }
    if (progress.overdue_count > 0) {
      return `今天留了空档，有 ${progress.overdue_count} 项可以按自己的节奏补上。`;
    }
    return "今天留了空档，可以按自己的节奏来。";
  }
  if (total > 0 && done === total) return "这天的任务已经完成。";
  if (total > 0) {
    return `这天安排了 ${total} 项任务，预计 ${formatMinutes(data.selected_estimated_minutes)}。`;
  }
  return "这天没有安排任务，可以把时间留给复习或休息。";
}

function renderSelected() {
  const data = state.data;
  const plan = data.plan;
  $("selected-title").textContent = formatDateLabel(data.selected_date);
  const tasks = data.selected_tasks || [];
  const done = tasks.filter((task) => task.status === "completed").length;
  $("selected-meta").textContent = tasks.length
    ? `${tasks.length} 项 · 预计 ${formatMinutes(data.selected_estimated_minutes)}${
        done ? ` · 已完成 ${done} 项` : ""
      }`
    : "没有任务";
  setFeedback(feedbackText(data, plan));
  const list = $("task-list");
  list.innerHTML = "";
  tasks.forEach((task) => {
    const li = node("li", "schedule-task");
    li.classList.add(`state-${task.state}`);
    const head = node("div", "schedule-task-head");
    head.append(node("span", `badge ${task.task_type === "practice" ? "accent" : ""}`, typeLabel(task.task_type)));
    head.append(node("strong", "schedule-task-title", task.task_title || task.module_title));
    head.append(node("span", `badge ${task.state === "completed" ? "ok" : task.state === "overdue" ? "warn" : ""}`, stateLabel(task)));
    const body = node("div", "schedule-task-body");
    body.append(node("p", "meta", `${task.module_title} · 预计 ${formatMinutes(task.estimated_minutes)}`));
    if (task.task_type === "practice") {
      body.append(
        node(
          "p",
          "hint",
          `本组 ${task.practice_question_count || 0} 道${
            task.planned_question_count && task.practice_question_count !== task.planned_question_count
              ? `（原计划 ${task.planned_question_count} 道）`
              : ""
          }`
        )
      );
    }
    if (task.completed_at) {
      body.append(
        node(
          "p",
          "hint",
          `${task.completed_ahead ? "提前完成" : "完成"}于 ${String(task.completed_at).replace("T", " ").slice(0, 16)}`
        )
      );
    }
    const actions = node("div", "schedule-task-actions");
    if (task.status !== "completed" && task.can_mark_complete && task.action_url) {
      const open = document.createElement("a");
      open.className = "secondary link-button";
      open.href = task.action_url;
      open.textContent = task.task_type === "review" ? "去回顾" : "去学习";
      actions.append(open);
    }
    actions.append(taskAction(task, plan));
    if (task.status !== "completed") actions.append(rescheduleButton(task));
    li.append(head, body, actions);
    list.append(li);
  });
  const actionBox = $("selected-actions");
  actionBox.innerHTML = "";
  if (state.lastMoved) {
    const moved = document.createElement("button");
    moved.type = "button";
    moved.className = "secondary";
    moved.textContent = `查看 ${formatShortDate(state.lastMoved.date)}`;
    moved.addEventListener("click", () => {
      const target = state.lastMoved.date;
      state.lastMoved = null;
      state.selectedDate = target;
      state.month = target.slice(0, 7);
      load();
    });
    actionBox.append(moved);
  }
  if (!plan || plan.unassigned) return;
  const pending = tasks.find((task) => task.status !== "completed" && task.action_url);
  if (pending) {
    const link = document.createElement("a");
    link.className = "primary link-button";
    link.href = pending.action_url;
    const hasCompleted = (data.progress.total || 0) > 0 && (data.progress.completed || 0) > 0;
    link.textContent = pending.task_type === "practice"
      ? (hasCompleted ? "继续练习" : "开始练习")
      : (hasCompleted ? "继续学习" : "开始学习");
    actionBox.append(link);
    return;
  }
  if (tasks.length && done === tasks.length) {
    const first = tasks[0];
    if (data.selected_date === data.today && data.today_all_done && data.next_task?.action_url) {
      const link = document.createElement("a");
      link.className = "primary link-button";
      link.href = data.next_task.action_url;
      link.textContent = "继续下一项";
      actionBox.append(link);
    } else if (first?.action_url) {
      const link = document.createElement("a");
      link.className = "secondary link-button";
      link.href = first.action_url;
      link.textContent = "再看一遍";
      actionBox.append(link);
    }
    if (data.selected_date === data.today) {
      const briefButton = document.createElement("button");
      briefButton.type = "button";
      briefButton.className = "secondary";
      briefButton.textContent = "看看今天学得怎么样";
      briefButton.addEventListener("click", () => showBrief());
      actionBox.append(briefButton);
    }
    return;
  }
  if (plan.pack_status === "ready" && plan.pack_url && !tasks.some((task) => task.task_type === "practice" && task.practice_available === false)) {
    const link = document.createElement("a");
    link.className = "secondary link-button";
    link.href = plan.pack_url;
    link.textContent = "查看学习内容";
    actionBox.append(link);
  } else if (plan.session_id) {
    const link = document.createElement("a");
    link.className = "secondary link-button";
    link.href = `/diagnosis/${plan.session_id}`;
    link.textContent = "查看生成进度";
    actionBox.append(link);
  }
}

function setShiftStatus(text, isError = false) {
  const line = $("shift-status");
  if (!line) return;
  line.textContent = text || "";
  line.classList.toggle("error", Boolean(isError));
}

function renderShift() {
  const panel = $("shift-panel");
  if (!panel) return;
  const shift = state.data?.shift;
  if (!shift || !shift.available) {
    panel.hidden = true;
    state.shiftPreview = null;
    return;
  }
  panel.hidden = false;
  $("shift-message").textContent =
    shift.message || "后面的任务可以整体提前，先看看会调整哪些日期。";
  const parts = [`影响 ${shift.task_count} 项任务`, `预计 ${formatMinutes(shift.estimated_minutes)}`];
  if (shift.planned_end_date) parts.push(`预计 ${formatShortDate(shift.planned_end_date)} 完成`);
  $("shift-summary").textContent = parts.join(" · ") + "。";
  $("shift-changes").hidden = true;
  $("shift-changes").innerHTML = "";
  $("shift-confirm").hidden = true;
  $("shift-view").hidden = false;
  $("shift-dismiss").hidden = false;
  setShiftStatus("");
}

async function loadShiftPreview() {
  const data = state.data;
  if (!data?.plan) return;
  setShiftStatus("正在计算调整…");
  try {
    const response = await fetch("/api/schedule/shift/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        plan_id: data.plan.id,
        version_id: data.plan.version_id,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error?.message || "调整预览失败");
    const preview = payload.preview || {};
    state.shiftPreview = preview;
    const box = $("shift-changes");
    box.innerHTML = "";
    if (!preview.available) {
      box.hidden = true;
      $("shift-view").hidden = false;
      setShiftStatus("现在没有可以前移的任务。");
      return;
    }
    (preview.days || []).forEach((day) => {
      const from = (day.from_dates || []).map(formatShortDate).join("、");
      box.append(
        node(
          "p",
          "shift-change",
          `${from} → ${formatShortDate(day.date)} · ${day.task_count} 项 · ${formatMinutes(day.estimated_minutes)}`
        )
      );
    });
    box.hidden = false;
    $("shift-view").hidden = true;
    $("shift-confirm").hidden = false;
    $("shift-dismiss").hidden = true;
    setShiftStatus(
      `确认后需重新打开日历查看新日期。预计 ${formatShortDate(preview.planned_end_date)} 完成。`
    );
  } catch (error) {
    setShiftStatus(error.message, true);
  }
}

async function confirmShift() {
  const data = state.data;
  const preview = state.shiftPreview;
  if (!data?.plan || !preview?.preview_key) return;
  const button = $("shift-confirm");
  button.disabled = true;
  button.textContent = "正在保存…";
  try {
    const response = await fetch("/api/schedule/shift/confirm", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        plan_id: data.plan.id,
        version_id: data.plan.version_id,
        preview_key: preview.preview_key,
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      if (response.status === 409) {
        setShiftStatus(payload.error?.message || "安排已变化，请重新查看调整。", true);
        state.shiftPreview = null;
        await load();
        return;
      }
      throw new Error(payload.error?.message || "保存调整失败");
    }
    await load();
    setFeedback(
      payload.duplicate
        ? "这次调整已经保存过了，日期没有再次提前。"
        : "已按新的日期调整，后面的任务整体提前了。"
    );
  } catch (error) {
    setShiftStatus(error.message, true);
  } finally {
    button.disabled = false;
    button.textContent = "确认调整";
  }
}

async function dismissShift() {
  const data = state.data;
  const shift = data?.shift;
  if (!data?.plan || !shift?.preview_key) return;
  try {
    const response = await fetch("/api/schedule/shift/dismiss", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        plan_id: data.plan.id,
        version_id: data.plan.version_id,
        preview_key: shift.preview_key,
      }),
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error?.message || "保存失败");
    await load();
    setFeedback("已保持原安排，这一批提前完成不会再自动提示。");
  } catch (error) {
    setShiftStatus(error.message, true);
  }
}

function showBrief() {
  const panel = $("brief-panel");
  if (!panel || panel.hidden) return;
  panel.scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderBrief() {
  const panel = $("brief-panel");
  if (!panel) return;
  const brief = state.data?.brief;
  if (!brief) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  $("brief-date").textContent = formatDateLabel(brief.date);
  $("brief-summary").textContent = brief.summary || "";

  const stats = $("brief-stats");
  stats.innerHTML = "";
  if (!brief.empty) {
    const counts = brief.counts || {};
    const extras = [];
    if (counts.ahead) extras.push(`提前 ${counts.ahead} 项`);
    if (counts.makeup) extras.push(`补做 ${counts.makeup} 项`);
    stats.append(
      node(
        "p",
        "brief-stat",
        `完成任务 ${counts.completed || 0} 项${extras.length ? `（${extras.join("，")}）` : ""}`
      )
    );
    const practice = brief.practice || {};
    if (practice.total_attempts) {
      stats.append(
        node(
          "p",
          "brief-stat",
          `练习：已作答 ${practice.answered} 题，答对 ${practice.correct} 题，未作答 ${practice.unanswered} 题`
        )
      );
      if (practice.accuracy !== null && practice.accuracy !== undefined) {
        stats.append(
          node(
            "p",
            "hint",
            `正确率按已作答题计算：${Math.round(practice.accuracy * 100)}%（${practice.answered} 题）`
          )
        );
      }
      if (practice.previous_accuracy !== null && practice.previous_accuracy !== undefined) {
        stats.append(
          node(
            "p",
            "hint",
            `上一次可比练习的正确率是 ${Math.round(practice.previous_accuracy * 100)}%。`
          )
        );
      }
      if (practice.redo_questions) {
        stats.append(
          node(
            "p",
            "hint",
            `其中 ${practice.redo_questions} 题是重做，${practice.new_questions} 题是这次新做的。`
          )
        );
      }
    } else {
      stats.append(node("p", "brief-stat", "这天没有练习，只统计阅读与回顾。"));
    }
    if (state.data?.progress?.label) {
      stats.append(node("p", "hint", `整体进度：${state.data.progress.label}`));
    }
  }

  const advice = $("brief-advice");
  advice.innerHTML = "";
  (brief.advice || []).forEach((item) => {
    const row = node("div", "brief-advice-item");
    row.append(
      node(
        "p",
        "meta",
        [item.source_name, item.knowledge_point].filter(Boolean).join(" · ")
      )
    );
    row.append(node("p", "brief-advice-text", item.text || ""));
    if (item.url) {
      const link = document.createElement("a");
      link.className = "link";
      link.href = item.url;
      link.textContent = "回看这个知识点";
      row.append(link);
    }
    advice.append(row);
  });

  const actions = $("brief-actions");
  actions.innerHTML = "";
  if (brief.next_step?.url) {
    const link = document.createElement("a");
    link.className = "primary link-button";
    link.href = brief.next_step.url;
    link.textContent = "回看这个知识点";
    actions.append(link);
  }
  if (brief.next_task?.action_url) {
    const link = document.createElement("a");
    link.className = "secondary link-button";
    link.href = brief.next_task.action_url;
    link.textContent = "继续下一项";
    actions.append(link);
  }
  if (!actions.children.length && brief.empty) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary";
    button.textContent = "回到今天";
    button.addEventListener("click", () => {
      state.selectedDate = todayISO();
      state.month = state.selectedDate.slice(0, 7);
      load();
    });
    actions.append(button);
  }
}

function rescheduleButton(task) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "secondary";
  button.textContent = "改期";
  button.addEventListener("click", () => openReschedule(task));
  return button;
}

function openReschedule(task) {
  const dialog = $("reschedule-dialog");
  if (!dialog) return;
  state.reschedule = { task, preview: null };
  $("reschedule-task").textContent = `${task.task_title || task.module_title} · 原日期 ${formatShortDate(task.scheduled_date)}`;
  const input = $("reschedule-date");
  const today = todayISO();
  input.min = today;
  input.value = task.scheduled_date >= today ? task.scheduled_date : today;
  $("reschedule-preview").textContent = "选择目标日期后查看当天预计时间。";
  $("reschedule-preview").classList.remove("error");
  dialog.showModal();
  previewReschedule();
}

async function previewReschedule() {
  const stateItem = state.reschedule;
  const input = $("reschedule-date");
  if (!stateItem?.task || !input?.value) return;
  const line = $("reschedule-preview");
  line.classList.remove("error");
  line.textContent = "正在计算…";
  try {
    const response = await fetch(
      `/api/schedule/tasks/${stateItem.task.id}/reschedule/preview`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scheduled_date: input.value }),
      }
    );
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error?.message || "改期预览失败");
    const preview = payload.preview || {};
    stateItem.preview = preview;
    let text = `原日期 ${formatShortDate(preview.from_date)} → 目标日期 ${formatShortDate(preview.to_date)}；目标日预计 ${formatMinutes(preview.target_day_minutes)}`;
    if (preview.over_capacity) {
      text += `，超过每天 ${formatMinutes(preview.daily_minutes)} 的安排，仍可保存。`;
    }
    line.textContent = text;
  } catch (error) {
    stateItem.preview = null;
    line.classList.add("error");
    line.textContent = error.message;
  }
}

async function saveReschedule() {
  const stateItem = state.reschedule;
  const input = $("reschedule-date");
  if (!stateItem?.task || !input?.value) return;
  const button = $("reschedule-save");
  button.disabled = true;
  button.textContent = "正在保存…";
  try {
    const response = await fetch(`/api/schedule/tasks/${stateItem.task.id}/reschedule`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        scheduled_date: input.value,
        expected_date: stateItem.task.scheduled_date,
      }),
    });
    const payload = await response.json();
    if (!response.ok) {
      if (response.status === 409) {
        $("reschedule-dialog").close();
        state.reschedule = null;
        await load();
        setFeedback(
          payload.error?.message || "任务安排已变化，请重新选择日期。",
          true
        );
        return;
      }
      throw new Error(payload.error?.message || "改期失败");
    }
    state.lastMoved = {
      date: payload.to_date,
      title: stateItem.task.task_title || stateItem.task.module_title,
    };
    $("reschedule-dialog").close();
    state.reschedule = null;
    await load();
    setFeedback(payload.message || `已移至 ${formatShortDate(payload.to_date)}。`);
  } catch (error) {
    $("reschedule-preview").classList.add("error");
    $("reschedule-preview").textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = "确认改期";
  }
}

function fillScheduleForm() {
  const defaults = scheduleDefaults();
  const start = $("schedule-start");
  if (!start.value) start.value = defaults.start_date || todayISO();
  const duration = Number(defaults.duration_days) || 14;
  const customPeriod = !defaults.duration_days && Boolean(defaults.target_date);
  const preset = customPeriod ? "custom" : [7, 14, 30].includes(duration) ? String(duration) : "custom";
  const period = document.querySelector(`input[name="schedule_period"][value="${preset}"]`);
  if (period) period.checked = true;
  $("target-wrap").hidden = preset !== "custom";
  if (preset === "custom") $("schedule-target").value = defaults.target_date || "";
  const daily = Number(defaults.daily_minutes) || 30;
  const dailyPreset = [15, 30, 60].includes(daily) ? String(daily) : "custom";
  const dailyRadio = document.querySelector(`input[name="schedule_daily"][value="${dailyPreset}"]`);
  if (dailyRadio) dailyRadio.checked = true;
  $("daily-wrap").hidden = dailyPreset !== "custom";
  if (dailyPreset === "custom") $("schedule-daily-custom").value = String(daily);
}

function collectOptions() {
  const start = $("schedule-start").value || todayISO();
  if (!parseLocalDate(start)) return { error: "开始日期格式不正确" };
  const period = document.querySelector('input[name="schedule_period"]:checked')?.value || "14";
  let duration = null;
  let target = null;
  if (period === "custom") {
    target = $("schedule-target").value;
    if (!parseLocalDate(target)) return { error: "请选择预计完成日期" };
    if (target < start) return { error: "预计完成日期不能早于开始日期" };
  } else {
    duration = Number(period);
  }
  const dailyChoice = document.querySelector('input[name="schedule_daily"]:checked')?.value || "30";
  const daily = dailyChoice === "custom" ? Number($("schedule-daily-custom").value) : Number(dailyChoice);
  if (!Number.isFinite(daily) || daily < 5 || daily > 1440) {
    return { error: "每天学习时间需要在 5–1440 分钟之间" };
  }
  return {
    start_date: start,
    duration_days: duration,
    target_date: target,
    daily_minutes: daily,
  };
}

function previewText(preview) {
  const lines = [];
  const capacity = preview.capacity_minutes;
  const estimated = preview.estimated_minutes;
  if (preview.over_capacity) {
    const gap = preview.shortfall_minutes;
    lines.push(
      `预计需要 ${formatMinutes(estimated)}，可用 ${formatMinutes(capacity)}${
        gap ? `，还差 ${formatMinutes(gap)}` : ""
      }。可以调长周期或增加每天投入。`
    );
  } else {
    lines.push(`预计需要 ${formatMinutes(estimated)}，可用 ${formatMinutes(capacity)}。`);
  }
  lines.push(
    `${formatDateLabel(preview.start_date)} 开始，预计 ${formatDateLabel(preview.planned_end_date)} 完成。`
  );
  (preview.days || []).slice(0, 3).forEach((day) => {
    const titles = (day.tasks || []).map((task) => task.task_title).join("、");
    lines.push(`${formatDateLabel(day.date)}：${titles}（${formatMinutes(day.estimated_minutes)}）`);
  });
  return lines.join("\n");
}

async function previewSchedule() {
  const options = collectOptions();
  if (options.error) {
    $("schedule-preview").textContent = options.error;
    $("schedule-preview").classList.add("error");
    return null;
  }
  $("schedule-preview").classList.remove("error");
  $("schedule-preview").textContent = "正在计算…";
  const data = state.data;
  try {
    const response = await fetch("/api/schedule/preview", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan_version_id: data.plan.version_id, ...options }),
    });
    const preview = await response.json();
    if (!response.ok) throw new Error(preview.error?.message || "日程预览失败");
    $("schedule-preview").textContent = previewText(preview);
    saveScheduleDefaults({
      start_date: options.start_date,
      duration_days: options.duration_days,
      target_date: options.target_date,
      daily_minutes: options.daily_minutes,
    });
    return options;
  } catch (error) {
    $("schedule-preview").textContent = error.message;
    $("schedule-preview").classList.add("error");
    return null;
  }
}

async function saveSchedule() {
  const data = state.data;
  if (!data?.plan) return;
  const options = await previewSchedule();
  if (!options) return;
  const button = $("save-schedule");
  button.disabled = true;
  button.textContent = "正在保存…";
  try {
    const response = await fetch(
      `/api/plans/${data.plan.id}/versions/${data.plan.version_id}/schedule`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(options),
      }
    );
    const result = await response.json();
    if (!response.ok) throw new Error(result.error?.message || "保存日程失败");
    $("schedule-form").hidden = true;
    $("open-schedule-form").hidden = true;
    state.selectedDate = todayISO();
    state.month = state.selectedDate.slice(0, 7);
    await load();
    setFeedback(result.duplicate ? "这份学习计划已有日程，已沿用原安排。" : "时间安排已保存，可以按每天的任务开始学习。");
  } catch (error) {
    $("schedule-preview").textContent = error.message;
    $("schedule-preview").classList.add("error");
  } finally {
    button.disabled = false;
    button.textContent = "保存时间安排";
  }
}

function shiftMonth(delta) {
  const base = parseLocalDate(`${state.month}-01`);
  if (!base) return;
  const next = new Date(base.getFullYear(), base.getMonth() + delta, 1);
  state.month = `${next.getFullYear()}-${pad(next.getMonth() + 1)}`;
  const today = todayISO();
  state.lastMoved = null;
  state.selectedDate = state.month === today.slice(0, 7) ? today : `${state.month}-01`;
  load();
}

function bind() {
  $("calendar-prev").addEventListener("click", () => shiftMonth(-1));
  $("calendar-next").addEventListener("click", () => shiftMonth(1));
  $("calendar-today").addEventListener("click", () => {
    state.lastMoved = null;
    state.selectedDate = todayISO();
    state.month = state.selectedDate.slice(0, 7);
    load();
  });
  $("plan-select").addEventListener("change", (event) => {
    state.planId = event.target.value;
    state.versionId = null;
    state.lastMoved = null;
    load();
  });
  $("shift-view").addEventListener("click", loadShiftPreview);
  $("shift-confirm").addEventListener("click", confirmShift);
  $("shift-dismiss").addEventListener("click", dismissShift);
  $("reschedule-date").addEventListener("change", previewReschedule);
  $("reschedule-cancel").addEventListener("click", () => {
    $("reschedule-dialog").close();
    state.reschedule = null;
  });
  $("reschedule-save").addEventListener("click", saveReschedule);
  $("open-schedule-form").addEventListener("click", () => {
    $("schedule-form").hidden = false;
    $("open-schedule-form").hidden = true;
    previewSchedule();
  });
  document.querySelectorAll('input[name="schedule_period"]').forEach((input) =>
    input.addEventListener("change", () => {
      $("target-wrap").hidden = input.value !== "custom";
      if (input.value === "custom" && !$("schedule-target").value) {
        const start = parseLocalDate($("schedule-start").value || todayISO());
        if (start) {
          const target = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 13);
          $("schedule-target").value = isoOf(target);
        }
      }
    })
  );
  document.querySelectorAll('input[name="schedule_daily"]').forEach((input) =>
    input.addEventListener("change", () => {
      $("daily-wrap").hidden = input.value !== "custom";
    })
  );
  $("preview-schedule").addEventListener("click", previewSchedule);
  $("save-schedule").addEventListener("click", saveSchedule);
}

let lastToday = todayISO();

function bindClock() {
  setInterval(() => {
    const current = todayISO();
    if (current !== lastToday) {
      lastToday = current;
      load();
    }
  }, 60000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && todayISO() !== lastToday) {
      lastToday = todayISO();
      load();
    }
  });
}

const params = new URLSearchParams(window.location.search);
state.planId = params.get("plan_id");
state.versionId = params.get("version_id");
state.month = params.get("month");
state.selectedDate = params.get("date") || todayISO();
state.month = state.month || state.selectedDate.slice(0, 7);
bind();
bindClock();
load();