const state = {
  sources: [],
  available: [],
  limits: null,
  jobId: null,
  polling: null,
  consent: sessionStorage.getItem("xunzhi_consent") === "1",
  canGenerate: false,
  keyConfigured: true,
  lastStage: null,
  lastStageAt: 0,
  staleNotified: false,
  kbId: null,
  kbDetail: null,
  kbScope: "all",
  kbSelected: new Set(),
  kbRanges: {},
  entryMode: "kb",
};

const FORM_KEY = "xunzhi_form_v2";
const SCHEDULE_KEY = "xunzhi_schedule_v1";
const stageNames = {
  fetch_sources: "读取资料与证据",
  understand_progress: "理解学习进度",
  write_handout: "组织讲义",
  compose_questions: "编写练习",
  validate: "校验题量与引用",
  persist: "保存学习计划",
};

const $ = (id) => document.getElementById(id);
const esc = (text) =>
  String(text ?? "").replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));

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
    // 本机存储不可用时只影响默认值，不影响本次诊断。
  }
}

function scheduleOptions() {
  const start = $("schedule-start")?.value || todayISO();
  if (!parseLocalDate(start)) return { error: "请选择有效的开始日期" };
  const period = document.querySelector('input[name="schedule_period"]:checked')?.value || "14";
  let duration = null;
  let target = null;
  if (period === "custom") {
    target = $("schedule-target")?.value || "";
    if (!parseLocalDate(target)) return { error: "请选择预计完成日期" };
    if (target < start) return { error: "预计完成日期不能早于开始日期" };
  } else {
    duration = Number(period);
  }
  const dailyChoice = document.querySelector('input[name="schedule_daily"]:checked')?.value || "30";
  const daily =
    dailyChoice === "custom" ? Number($("schedule-daily-custom")?.value) : Number(dailyChoice);
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

function formatDateLabel(value) {
  const parsed = parseLocalDate(value);
  if (!parsed) return value || "";
  return `${parsed.getFullYear()} 年 ${parsed.getMonth() + 1} 月 ${parsed.getDate()} 日`;
}

function updateScheduleSummary() {
  const line = $("schedule-summary");
  if (!line) return;
  const result = scheduleOptions();
  if (result.error) {
    line.textContent = result.error;
    line.classList.add("error");
    return;
  }
  line.classList.remove("error");
  const options = result.options;
  let endLabel = options.target_date ? formatDateLabel(options.target_date) : "";
  if (!endLabel && options.duration_days) {
    const start = parseLocalDate(options.start_date);
    if (start) {
      const end = new Date(
        start.getFullYear(),
        start.getMonth(),
        start.getDate() + Number(options.duration_days) - 1
      );
      endLabel = formatDateLabel(
        `${end.getFullYear()}-${pad2(end.getMonth() + 1)}-${pad2(end.getDate())}`
      );
    }
  }
  line.textContent = `预计 ${formatDateLabel(options.start_date)} 开始${endLabel ? `，${endLabel} 完成` : ""}，每天 ${options.daily_minutes} 分钟。确认计划前都可以修改。`;
}

function applyScheduleDefaults() {
  const defaults = readScheduleDefaults();
  const start = $("schedule-start");
  if (start) start.value = defaults.start_date || todayISO();
  const duration = Number(defaults.duration_days) || 14;
  const customPeriod = !defaults.duration_days && Boolean(defaults.target_date);
  const preset = customPeriod ? "custom" : [7, 14, 30].includes(duration) ? String(duration) : "custom";
  const period = document.querySelector(`input[name="schedule_period"][value="${preset}"]`);
  if (period) period.checked = true;
  if ($("schedule-target-wrap")) $("schedule-target-wrap").hidden = preset !== "custom";
  if (preset === "custom" && $("schedule-target")) {
    $("schedule-target").value = defaults.target_date || "";
  }
  const daily = Number(defaults.daily_minutes) || 30;
  const dailyPreset = [15, 30, 60].includes(daily) ? String(daily) : "custom";
  const dailyRadio = document.querySelector(`input[name="schedule_daily"][value="${dailyPreset}"]`);
  if (dailyRadio) dailyRadio.checked = true;
  if ($("schedule-daily-wrap")) $("schedule-daily-wrap").hidden = dailyPreset !== "custom";
  if (dailyPreset === "custom" && $("schedule-daily-custom")) {
    $("schedule-daily-custom").value = String(daily);
  }
  updateScheduleSummary();
}

function setStatus(text, isError = false) {
  const line = $("status-line");
  line.textContent = text || "";
  line.classList.toggle("error", Boolean(isError));
}

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function selectedIds() {
  return state.sources.map((item) => item.id);
}

function savedForm() {
  return {
    sourceIds: selectedIds(),
    ranges: Object.fromEntries(
      state.sources
        .filter((item) => item.kind === "pdf")
        .map((item) => [item.id, { start: item.page_start, end: item.page_end }])
    ),
    progress: $("progress-input").value,
    difficulty: document.querySelector('input[name="difficulty"]:checked')?.value || "consolidate",
    learningMode: document.querySelector('input[name="learning_mode"]:checked')?.value || "auto",
  };
}

function saveForm() {
  localStorage.setItem(FORM_KEY, JSON.stringify(savedForm()));
}

function rangeState(item) {
  if (item.kind !== "pdf") return { ok: true, text: "" };
  const start = item.page_start;
  const end = item.page_end;
  if (start === null && end === null) return { ok: true, text: `全部 ${item.page_count} 页` };
  if (!Number.isInteger(start) || !Number.isInteger(end) || start < 1 || end < 1) {
    return { ok: false, text: "请填写正整数页码" };
  }
  if (start > end) return { ok: false, text: "起始页不能大于结束页" };
  if (end > item.page_count) return { ok: false, text: `页码范围需在 1–${item.page_count} 之间` };
  return { ok: true, text: `第 ${start}–${end} 页` };
}

function selectedPdfPages() {
  return state.sources.reduce((total, item) => {
    if (item.kind !== "pdf" || !rangeState(item).ok) return total;
    if (item.page_start === null && item.page_end === null) {
      return total + (item.page_count || 0);
    }
    return total + (item.page_end - item.page_start + 1);
  }, 0);
}

function selectedBytes() {
  return state.sources.reduce((total, item) => total + (Number(item.size_bytes) || 0), 0);
}

function disabledReason() {
  if (!state.canGenerate) return "未配置模型密钥，可先用离线示例体验完整流程";
  if (state.entryMode === "kb") {
    if (!state.kbId || !state.kbDetail) return "请先选择或创建一个知识库";
    const ready = (state.kbDetail.items || []).filter((item) => item.usable);
    if (!ready.length) return "知识库还没有整理完成、可学习的资料";
    if (state.kbScope === "selected" && !ready.some((item) => state.kbSelected.has(item.source_id))) {
      return "至少选择 1 份资料";
    }
    const rangeProblem = kbRangeProblem();
    if (rangeProblem) return rangeProblem;
    if (!$("progress-input").value.trim()) return "写一句你学到哪、哪里不懂即可";
    return "";
  }
  if (state.sources.length === 0) return "先上传或选择至少 1 份资料";
  if (!state.sources.every((item) => rangeState(item).ok)) {
    const bad = state.sources.find((item) => !rangeState(item).ok);
    return rangeState(bad).text;
  }
  if (state.limits) {
    const limits = state.limits;
    if (limits.max_sources && state.sources.length > limits.max_sources) {
      return `本轮最多选择 ${limits.max_sources} 份资料，请移除 ${state.sources.length - limits.max_sources} 份。`;
    }
    const maxBytes = Number(limits.max_total_mb) * 1024 * 1024;
    const bytes = selectedBytes();
    if (limits.max_total_mb !== undefined && bytes > maxBytes) {
      return `本轮资料合计 ${formatSize(bytes)}，超过 ${limits.max_total_mb} MB 上限，请移除资料。`;
    }
    const pages = selectedPdfPages();
    if (limits.max_pages && pages > limits.max_pages) {
      return `本轮 PDF 合计 ${pages} 页，超过 ${limits.max_pages} 页上限，请缩小页码范围。`;
    }
  }
  if (!$("progress-input").value.trim()) return "写一句你学到哪、哪里不懂即可";
  return "";
}

function refreshButton() {
  const reason = disabledReason();
  $("generate-btn").disabled = Boolean(reason);
  $("disabled-reason").textContent = reason;
  $("disabled-reason").hidden = !reason;
}

function rangeControl(item) {
  const wrap = document.createElement("div");
  wrap.className = "range";
  if (item.kind !== "pdf") {
    const hint = document.createElement("span");
    hint.className = "range-hint";
    hint.textContent = item.kind === "image" ? "整张图片" : "全文";
    wrap.append(hint);
    return wrap;
  }
  const start = document.createElement("input");
  start.type = "number";
  start.min = "1";
  start.max = String(item.page_count || "");
  start.placeholder = "起始页";
  start.value = item.page_start ?? "";
  const end = document.createElement("input");
  end.type = "number";
  end.min = "1";
  end.max = String(item.page_count || "");
  end.placeholder = "结束页";
  end.value = item.page_end ?? "";
  const hint = document.createElement("span");
  hint.className = "range-hint";
  const update = () => {
    item.page_start = start.value === "" ? null : Number(start.value);
    item.page_end = end.value === "" ? null : Number(end.value);
    const state_ = rangeState(item);
    hint.textContent = state_.text;
    hint.classList.toggle("bad", !state_.ok);
    refreshButton();
    saveForm();
  };
  start.addEventListener("input", update);
  end.addEventListener("input", update);
  hint.textContent = rangeState(item).text;
  wrap.append(start, end, hint);
  return wrap;
}

function renderSourceList() {
  const list = $("source-list");
  list.innerHTML = "";
  state.sources.forEach((item) => {
    const li = document.createElement("li");
    const left = document.createElement("div");
    const title = document.createElement("div");
    title.className = "record-title";
    title.textContent = item.display_name;
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${formatSize(item.size_bytes || 0)} · ${item.kind || "资料"}`;
    left.append(title, meta);

    const right = document.createElement("div");
    right.className = "inline-actions";
    right.append(rangeControl(item));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "secondary";
    remove.textContent = "移除";
    remove.addEventListener("click", () => {
      state.sources = state.sources.filter((row) => row.id !== item.id);
      renderSourceList();
      renderExisting();
      refreshButton();
      saveForm();
    });
    right.append(remove);
    li.append(left, right);
    list.append(li);
  });
}

function renderExisting() {
  const list = $("existing-list");
  list.innerHTML = "";
  state.available.forEach((item) => {
    const label = document.createElement("label");
    label.className = "check-row";
    const box = document.createElement("input");
    box.type = "checkbox";
    box.checked = Boolean(state.sources.find((row) => row.id === item.id));
    box.addEventListener("change", () => {
      if (box.checked) {
        if (!state.sources.find((row) => row.id === item.id)) {
          state.sources.push({ ...item, page_start: null, page_end: null });
        }
      } else {
        state.sources = state.sources.filter((row) => row.id !== item.id);
      }
      renderSourceList();
      renderExisting();
      refreshButton();
      saveForm();
    });
    label.append(box, document.createTextNode(item.display_name));
    list.append(label);
  });
  $("existing-empty").hidden = state.available.length > 0;
}

function updateCounter() {
  $("progress-count").textContent = String($("progress-input").value.length);
}

function kbReadyItems() {
  return ((state.kbDetail && state.kbDetail.items) || []).filter((item) => item.usable);
}

function kbSelectedItems() {
  const ready = kbReadyItems();
  if (state.kbScope === "all") return ready;
  return ready.filter((item) => state.kbSelected.has(item.source_id));
}

function kbRangeProblem() {
  for (const item of kbSelectedItems()) {
    if (item.kind !== "pdf") continue;
    const range = state.kbRanges[item.source_id] || {};
    const start = range.page_start;
    const end = range.page_end;
    if (start === undefined && end === undefined) continue;
    if (!Number.isInteger(start) || !Number.isInteger(end) || start < 1 || end < 1) {
      return `「${item.display_name}」的章节页码需要填写正整数`;
    }
    if (start > end) return `「${item.display_name}」的起始页不能大于结束页`;
    if (end > (item.page_count || 0)) {
      return `「${item.display_name}」的结束页超出 ${item.page_count} 页`;
    }
  }
  return "";
}

function kbScopeRequest() {
  const whole = state.kbScope === "all";
  const items = whole ? kbReadyItems() : kbSelectedItems();
  return {
    mode: state.kbScope,
    source_ids: items.map((item) => item.source_id),
    page_ranges: whole
      ? {}
      : Object.fromEntries(
          items
            .filter((item) => item.kind === "pdf" && state.kbRanges[item.source_id]?.page_start)
            .map((item) => [
              item.source_id,
              {
                page_start: state.kbRanges[item.source_id].page_start,
                page_end: state.kbRanges[item.source_id].page_end,
              },
            ])
        ),
  };
}

function kbRangeControl(item) {
  if (state.kbScope !== "selected" || item.kind !== "pdf") return null;
  const current = state.kbRanges[item.source_id] || {};
  const wrap = document.createElement("div");
  wrap.className = "range";
  const start = document.createElement("input");
  start.type = "number";
  start.min = "1";
  start.max = String(item.page_count || "");
  start.placeholder = "起始页";
  start.value = current.page_start ?? "";
  const end = document.createElement("input");
  end.type = "number";
  end.min = "1";
  end.max = String(item.page_count || "");
  end.placeholder = "结束页";
  end.value = current.page_end ?? "";
  const update = () => {
    state.kbRanges[item.source_id] = {
      page_start: start.value ? Number(start.value) : undefined,
      page_end: end.value ? Number(end.value) : undefined,
    };
    refreshButton();
  };
  start.addEventListener("input", update);
  end.addEventListener("input", update);
  wrap.append(start, end);
  return wrap;
}

function renderKb() {
  const box = $("kb-items");
  box.innerHTML = "";
  const summary = $("kb-summary");
  const scope = $("kb-scope-group");
  if (!state.kbDetail) {
    summary.textContent = "还没有选择知识库";
    scope.hidden = true;
    return;
  }
  summary.textContent = `${state.kbDetail.name} · ${state.kbDetail.ready_count} 份可学习 · ${state.kbDetail.total_pages} 页`;
  const items = state.kbDetail.items || [];
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "kb-choice";
    if (state.kbScope === "selected" && item.usable) {
      const check = document.createElement("input");
      check.type = "checkbox";
      check.checked = state.kbSelected.has(item.source_id);
      check.addEventListener("change", () => {
        if (check.checked) state.kbSelected.add(item.source_id);
        else state.kbSelected.delete(item.source_id);
        refreshButton();
      });
      row.append(check);
    }
    const label = document.createElement("div");
    label.className = "kb-choice-body";
    const title = document.createElement("div");
    title.className = "record-title";
    title.textContent = item.display_name || "资料";
    const meta = document.createElement("div");
    meta.className = "meta";
    meta.textContent = `${item.kind || "资料"} · ${item.page_count || 0} 页 · ${item.status_label}`;
    label.append(title, meta);
    const chapters = item.chapters || [];
    if (chapters.length) {
      const list = document.createElement("div");
      list.className = "chapter-list";
      const current = state.kbRanges[item.source_id] || {};
      chapters.forEach((chapter) => {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "chapter";
        button.textContent = chapter.label;
        if (
          current.page_start === chapter.page_start &&
          current.page_end === chapter.page_end
        ) {
          button.classList.add("active");
        }
        button.addEventListener("click", () => {
          state.kbScope = "selected";
          const radio = document.querySelector('input[name="kb_scope"][value="selected"]');
          if (radio) radio.checked = true;
          state.kbSelected.add(item.source_id);
          state.kbRanges[item.source_id] = {
            page_start: chapter.page_start,
            page_end: chapter.page_end,
          };
          renderKb();
          refreshButton();
        });
        list.append(button);
      });
      label.append(list);
    }
    if (item.status === "failed") {
      label.append(
        document.createTextNode(
          (item.error_message || "整理失败，可在知识库详情中重试或替换文件") +
            (item.usable ? "（本次学习仍使用原版本）" : "")
        )
      );
    }
    row.append(label);
    const range = kbRangeControl(item);
    if (range) row.append(range);
    box.append(row);
  });
  scope.hidden = state.entryMode !== "kb";
}

async function loadKnowledgeBase(id, preset = null) {
  if (!id) {
    state.kbId = null;
    state.kbDetail = null;
    renderKb();
    refreshButton();
    return;
  }
  const response = await fetch(`/api/knowledge-bases/${id}`);
  const data = await response.json();
  if (!response.ok) {
    setStatus(data.error?.message || "知识库读取失败", true);
    return;
  }
  state.kbId = id;
  state.kbDetail = data;
  const usableIds = (data.items || [])
    .filter((item) => item.usable)
    .map((item) => item.source_id);
  state.kbSelected = new Set(preset?.sourceId ? [preset.sourceId] : usableIds);
  state.kbRanges = {};
  state.kbScope = "all";
  if (preset?.sourceId && usableIds.includes(preset.sourceId)) {
    state.kbScope = "selected";
    if (preset.pageStart && preset.pageEnd) {
      state.kbRanges[preset.sourceId] = {
        page_start: preset.pageStart,
        page_end: preset.pageEnd,
      };
    }
  }
  const scopeRadio = document.querySelector(`input[name="kb_scope"][value="${state.kbScope}"]`);
  if (scopeRadio) scopeRadio.checked = true;
  renderKb();
  refreshButton();
}

async function loadKnowledgeBases() {
  const select = $("kb-select");
  const response = await fetch("/api/knowledge-bases");
  const data = await response.json();
  if (!response.ok) {
    setStatus(data.error?.message || "知识库读取失败", true);
    return;
  }
  const items = data.items || [];
  select.innerHTML = '<option value="">选择知识库</option>';
  items.forEach((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = `${item.name} · ${item.ready_count} 份可学习`;
    select.append(option);
  });
  const empty = items.length === 0;
  $("kb-picker").hidden = empty;
  $("kb-empty").hidden = !empty;
  if (empty) {
    state.kbId = null;
    state.kbDetail = null;
    renderKb();
    refreshButton();
    return;
  }
  const params = new URLSearchParams(window.location.search);
  const presetKb = params.get("kb");
  const preset = {
    sourceId: params.get("source"),
    pageStart: Number(params.get("start")) || undefined,
    pageEnd: Number(params.get("end")) || undefined,
  };
  const chosen = items.find((item) => item.id === presetKb) || items[0];
  select.value = chosen.id;
  await loadKnowledgeBase(chosen.id, preset.sourceId ? preset : null);
}

function applyEntryMode(mode) {
  state.entryMode = mode;
  const kbMode = mode === "kb";
  $("kb-panel").hidden = !kbMode;
  $("manual-panel").hidden = kbMode;
  if (kbMode) {
    const selected = $("kb-select").value;
    if (selected && state.kbDetail && state.kbDetail.id === selected) {
      state.kbId = selected;
      renderKb();
    } else if (selected) {
      loadKnowledgeBase(selected);
    } else {
      state.kbId = null;
      renderKb();
    }
  } else {
    state.kbId = null;
    renderKb();
  }
  refreshButton();
  saveForm();
}

async function createKnowledgeBase() {
  const name = window.prompt("新知识库名称");
  if (!name || !name.trim()) return;
  const response = await fetch("/api/knowledge-bases", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: name.trim(), description: "" }),
  });
  const data = await response.json();
  if (!response.ok) {
    setStatus(data.error?.message || "新建知识库失败", true);
    return;
  }
  location.href = `/knowledge-bases/${data.id}`;
}

function privacyText() {
  const names = state.entryMode === "kb" && state.kbId
    ? kbSelectedItems().map((item) => item.display_name)
    : state.sources.map((item) => {
        const range = rangeState(item);
        return item.kind === "pdf" && item.page_start ? `${item.display_name} ${range.text}` : item.display_name;
      });
  return `本次将发送：${names.join("、") || "（无资料）"}；进度描述；学习方式。不会发送未选中的文件、页码或其他学习记录。`;
}

function buildPayload() {
  return {
    idempotency_key: crypto.randomUUID(),
    sources: state.sources.map((item) => ({
      source_id: item.id,
      page_start: item.page_start,
      page_end: item.page_end,
    })),
    progress_text: $("progress-input").value.trim(),
    difficulty: document.querySelector('input[name="difficulty"]:checked')?.value || "consolidate",
    consent_ack: true,
    parent_pack_id: null,
    review_focus: [],
  };
}

async function loadSources() {
  try {
    const response = await fetch("/api/sources");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || "资料读取失败");
    state.available = data.items || [];
    state.limits = data.limits || null;
    let saved = {};
    try {
      saved = JSON.parse(localStorage.getItem(FORM_KEY) || "{}");
    } catch (_) {
      saved = {};
    }
    if (Array.isArray(saved.sourceIds) && state.sources.length === 0) {
      saved.sourceIds.forEach((id) => {
        const item = state.available.find((row) => row.id === id);
        if (item) {
          const range = saved.ranges?.[id] || {};
          state.sources.push({
            ...item,
            page_start: range.start ?? null,
            page_end: range.end ?? null,
          });
        }
      });
    }
    if (saved.progress && !$("progress-input").value) {
      $("progress-input").value = saved.progress;
    }
    if (saved.difficulty) {
      const radio = document.querySelector(`input[name="difficulty"][value="${saved.difficulty}"]`);
      if (radio) radio.checked = true;
    }
    const mode = saved.learningMode || (saved.count ? "focus" : "auto");
    const modeRadio = document.querySelector(`input[name="learning_mode"][value="${mode}"]`);
    if (modeRadio) modeRadio.checked = true;
    updateCounter();
    renderSourceList();
    renderExisting();
    $("limit-hint").textContent = state.limits
      ? `本轮上限：最多 ${state.limits.max_sources} 份资料、合计 ${state.limits.max_total_mb} MB、PDF 合计 ${state.limits.max_pages} 页。`
      : "";
    refreshButton();
  } catch (err) {
    setStatus(`资料读取失败：${err.message}`, true);
  }
}

async function uploadFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  for (const file of files) {
    const form = new FormData();
    form.append("file", file, file.name);
    setStatus(`正在上传 ${file.name}…`);
    try {
      const response = await fetch("/api/sources", { method: "POST", body: form });
      const data = await response.json();
      if (!response.ok) {
        setStatus(data.error?.message || `${file.name} 上传失败`, true);
        continue;
      }
      const source = data.source;
      if (!state.available.find((item) => item.id === source.id)) {
        state.available.push(source);
      }
      if (!state.sources.find((item) => item.id === source.id)) {
        state.sources.push({ ...source, page_start: null, page_end: null });
      }
      setStatus(data.reused ? `已选择已保存的 ${source.display_name}` : `已上传 ${source.display_name}`);
    } catch (err) {
      setStatus(`${file.name} 上传失败：${err.message}`, true);
    }
  }
  renderExisting();
  renderSourceList();
  refreshButton();
  saveForm();
}

async function startDiagnosis() {
  let primarySourceId = null;
  let kbRequest = null;
  if (state.entryMode === "kb" && state.kbId) {
    const items = kbSelectedItems();
    if (!items.length) {
      setStatus(
        state.kbScope === "selected" ? "至少选择 1 份已整理资料" : "知识库还没有可学习资料",
        true
      );
      return;
    }
    const rangeProblem = kbRangeProblem();
    if (rangeProblem) {
      setStatus(rangeProblem, true);
      return;
    }
    primarySourceId = items[0].source_id;
    kbRequest = {
      knowledge_base_id: state.kbId,
      scope_mode: state.kbScope,
      source_ids: kbScopeRequest().source_ids,
      page_ranges: kbScopeRequest().page_ranges,
    };
  } else {
    if (!state.sources.length) {
      setStatus("请先上传或选择 1 份资料", true);
      return;
    }
    if (state.sources.length > 1) {
      setStatus("本轮诊断先选 1 份资料，多资料可在生成计划后继续", true);
      return;
    }
    primarySourceId = state.sources[0].id;
  }
  if (!state.consent) {
    const ok = window.confirm(privacyText().replace("进度描述", "本次目标") + "\n\n确认后开始诊断。");
    if (!ok) return;
    state.consent = true;
    sessionStorage.setItem("xunzhi_consent", "1");
  }
  const time = scheduleOptions();
  if (time.error) {
    setStatus(time.error, true);
    return;
  }
  writeScheduleDefaults(time.options);
  saveForm();
  $("generate-btn").disabled = true;
  setStatus("正在创建诊断…");
  try {
    const response = await fetch(`/api/sources/${primarySourceId}/diagnosis/sessions`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        idempotency_key: crypto.randomUUID(),
        goal: $("progress-input").value.trim(),
        learning_mode: document.querySelector('input[name="learning_mode"]:checked')?.value || "auto",
        ...time.options,
        ...(kbRequest || {}),
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      setStatus(data.error?.message || "诊断创建失败，资料与目标已保留", true);
      $("generate-btn").disabled = false;
      return;
    }
    location.href = `/diagnosis/${data.session.id}`;
  } catch (err) {
    setStatus(`诊断创建失败：${err.message}。资料与目标已保留。`, true);
    $("generate-btn").disabled = false;
  }
}

async function generate() {
  if (!state.consent) {
    const ok = window.confirm(privacyText() + "\n\n确认后开始生成。");
    if (!ok) return;
    state.consent = true;
    sessionStorage.setItem("xunzhi_consent", "1");
  }
  saveForm();
  const payload = buildPayload();
  $("generate-btn").disabled = true;
  $("cancel-btn").hidden = false;
  $("stages").hidden = false;
  state.lastStage = null;
  state.lastStageAt = 0;
  state.staleNotified = false;
  setStatus("正在生成…");
  try {
    const response = await fetch("/api/packs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      setStatus(data.error?.message || "生成失败，资料与进度已保留", true);
      $("generate-btn").disabled = false;
      $("cancel-btn").hidden = true;
      return;
    }
    state.jobId = data.job_id;
    sessionStorage.setItem("xunzhi_job", data.job_id);
    pollJob();
  } catch (err) {
    setStatus(`生成失败：${err.message}。资料与进度已保留。`, true);
    $("generate-btn").disabled = false;
    $("cancel-btn").hidden = true;
  }
}

async function pollJob() {
  if (!state.jobId) return;
  try {
    const response = await fetch(`/api/jobs/${state.jobId}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || "任务状态读取失败");
    const now = Date.now();
    if (data.stage !== state.lastStage) {
      state.lastStage = data.stage;
      state.lastStageAt = now;
    }
    renderStages(data.stage, data.status);
    const seconds = Math.round((data.elapsed_ms || 0) / 1000);
    let text = `生成中 · ${seconds} 秒 · 约需 30–60 秒`;
    if (now - state.lastStageAt > 60000 && !state.staleNotified) {
      state.staleNotified = true;
      text += "。还在处理，可继续等待或取消。";
    }
    setStatus(text);
    if (data.status === "succeeded" && data.pack_id) {
      sessionStorage.removeItem("xunzhi_job");
      location.href = `/packs/${data.pack_id}`;
      return;
    }
    if (["failed", "insufficient_evidence", "cancelled"].includes(data.status)) {
      sessionStorage.removeItem("xunzhi_job");
      setStatus((data.error?.message || "生成失败") + "（已保留资料与进度，可修改后重试）", true);
      $("generate-btn").disabled = false;
      $("cancel-btn").hidden = true;
      $("stages").hidden = true;
      return;
    }
    state.polling = setTimeout(pollJob, 1200);
  } catch (err) {
    setStatus(`生成中断：${err.message}。资料与进度已保留。`, true);
    $("generate-btn").disabled = false;
    $("cancel-btn").hidden = true;
  }
}

async function cancelJob() {
  if (!state.jobId) return;
  clearTimeout(state.polling);
  try {
    await fetch(`/api/jobs/${state.jobId}/cancel`, { method: "POST" });
    setStatus("已取消生成。资料与进度已保留。");
  } catch (_) {
    setStatus("取消失败，可稍后重试。", true);
  }
  $("generate-btn").disabled = false;
  $("cancel-btn").hidden = true;
  $("stages").hidden = true;
  state.jobId = null;
  sessionStorage.removeItem("xunzhi_job");
}

function renderStages(stage, status) {
  const box = $("stages");
  box.innerHTML = "";
  const order = Object.keys(stageNames);
  const current = order.indexOf(stage);
  order.forEach((key, index) => {
    const row = document.createElement("div");
    row.textContent = stageNames[key];
    if (status === "succeeded" || index < current) row.className = "done";
    else if (index === current) row.className = "current";
    box.append(row);
  });
}

let lastLearning = null;

function showNewLearning() {
  const entries = $("home-entries");
  const flow = $("new-learning-flow");
  if (entries) entries.hidden = true;
  if (flow) flow.hidden = false;
  const target = $("kb-select");
  if (target && !target.disabled) target.focus();
  if (flow) flow.scrollIntoView({ block: "start" });
}

function showHomeEntries() {
  const entries = $("home-entries");
  const flow = $("new-learning-flow");
  if (entries) entries.hidden = false;
  if (flow) flow.hidden = true;
}

function renderLastLearning(data) {
  lastLearning = data;
  const card = $("continue-learning");
  const title = $("continue-title");
  const meta = $("continue-meta");
  const action = $("continue-action");
  if (!card || !title || !meta || !action) return;
  if (!data?.available) {
    card.classList.add("entry-empty");
    title.textContent = "还没有学习记录";
    meta.textContent = "先新建学习，学完一包后会自动回到这里。";
    action.textContent = "新建学习";
    $("new-learning")?.classList.add("entry-primary");
    return;
  }
  card.classList.remove("entry-empty");
  $("new-learning")?.classList.remove("entry-primary");
  title.textContent = data.pack_title || "上次的学习计划";
  const parts = [];
  if (data.module_title) parts.push(data.module_title);
  parts.push(data.view_label || "学习");
  parts.push(data.answer_label || "尚未作答");
  if (data.schedule?.assigned && data.task_progress_label) parts.push(data.task_progress_label);
  if (data.fallback) parts.push("上次学习计划已删除，已回退到最近可打开的计划");
  meta.textContent = parts.join(" · ");
  action.textContent = data.answer_state === "submitted" ? "查看结果" : "继续";
}

async function loadLastLearning() {
  try {
    const response = await fetch("/api/learning/last");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || "读取失败");
    renderLastLearning(data);
  } catch (_) {
    renderLastLearning(null);
  }
}

async function init() {
  const drop = $("dropzone");
  const input = $("file-input");
  input.addEventListener("change", () => uploadFiles(input.files));
  const openPicker = () => {
    input.value = "";
    input.click();
  };
  drop.addEventListener("click", openPicker);
  drop.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      openPicker();
    }
  });
  ["dragenter", "dragover"].forEach((name) =>
    drop.addEventListener(name, (event) => {
      event.preventDefault();
      drop.classList.add("drag");
    })
  );
  ["dragleave", "drop"].forEach((name) =>
    drop.addEventListener(name, (event) => {
      event.preventDefault();
      drop.classList.remove("drag");
    })
  );
  drop.addEventListener("drop", (event) => uploadFiles(event.dataTransfer.files));

  $("pick-existing").addEventListener("click", () => {
    const panel = $("existing-panel");
    panel.hidden = !panel.hidden;
    $("pick-existing").setAttribute("aria-expanded", String(!panel.hidden));
    if (!panel.hidden) panel.scrollIntoView({ block: "nearest" });
  });
  $("select-all").addEventListener("click", () => {
    state.available.forEach((item) => {
      if (!state.sources.find((row) => row.id === item.id)) {
        state.sources.push({ ...item, page_start: null, page_end: null });
      }
    });
    renderSourceList();
    renderExisting();
    refreshButton();
    saveForm();
  });
  $("clear-selection").addEventListener("click", () => {
    state.sources = [];
    renderSourceList();
    renderExisting();
    refreshButton();
    saveForm();
  });
  $("progress-input").addEventListener("input", () => {
    updateCounter();
    refreshButton();
    saveForm();
  });
  applyScheduleDefaults();
  ["schedule-start", "schedule-target", "schedule-daily-custom"].forEach((id) => {
    const input = $(id);
    if (!input) return;
    input.addEventListener("change", () => {
      const result = scheduleOptions();
      if (!result.error) writeScheduleDefaults(result.options);
      updateScheduleSummary();
      refreshButton();
    });
  });
  document.querySelectorAll('input[name="schedule_period"]').forEach((input) =>
    input.addEventListener("change", () => {
      const wrap = $("schedule-target-wrap");
      if (wrap) wrap.hidden = input.value !== "custom";
      if (input.value === "custom" && $("schedule-target") && !$("schedule-target").value) {
        const start = parseLocalDate($("schedule-start").value || todayISO());
        if (start) {
          const target = new Date(start.getFullYear(), start.getMonth(), start.getDate() + 13);
          $("schedule-target").value = `${target.getFullYear()}-${pad2(target.getMonth() + 1)}-${pad2(target.getDate())}`;
        }
      }
      const result = scheduleOptions();
      if (!result.error) writeScheduleDefaults(result.options);
      updateScheduleSummary();
    })
  );
  document.querySelectorAll('input[name="schedule_daily"]').forEach((input) =>
    input.addEventListener("change", () => {
      const wrap = $("schedule-daily-wrap");
      if (wrap) wrap.hidden = input.value !== "custom";
      const result = scheduleOptions();
      if (!result.error) writeScheduleDefaults(result.options);
      updateScheduleSummary();
    })
  );
  document.querySelectorAll('input[name="learning_mode"], input[name="difficulty"]').forEach((input_) =>
    input_.addEventListener("change", () => {
      refreshButton();
      saveForm();
    })
  );
  $("privacy-toggle").addEventListener("click", () => {
    const box = $("privacy-detail");
    box.hidden = !box.hidden;
    box.textContent = privacyText();
  });
  $("generate-btn").addEventListener("click", startDiagnosis);
  $("cancel-btn").addEventListener("click", cancelJob);
  $("kb-select").addEventListener("change", (event) => loadKnowledgeBase(event.target.value));
  $("kb-new").addEventListener("click", createKnowledgeBase);
  $("kb-empty-create").addEventListener("click", createKnowledgeBase);
  $("kb-empty-manual").addEventListener("click", () => {
    const radio = document.querySelector('input[name="entry_mode"][value="manual"]');
    if (radio) radio.checked = true;
    applyEntryMode("manual");
  });
  document.querySelectorAll('input[name="entry_mode"]').forEach((input_) =>
    input_.addEventListener("change", () => applyEntryMode(input_.value))
  );
  document.querySelectorAll('input[name="kb_scope"]').forEach((input_) =>
    input_.addEventListener("change", () => {
      state.kbScope = input_.value;
      if (input_.value === "all") state.kbRanges = {};
      state.kbSelected = new Set(kbReadyItems().map((item) => item.source_id));
      renderKb();
      refreshButton();
    })
  );

  $("continue-learning").addEventListener("click", () => {
    if (lastLearning?.available && lastLearning.url) {
      location.href = lastLearning.url;
      return;
    }
    showNewLearning();
  });
  $("new-learning").addEventListener("click", showNewLearning);
  $("back-to-entries").addEventListener("click", showHomeEntries);

  await loadLastLearning();

  try {
    const response = await fetch("/api/settings/model-status");
    const data = await response.json();
    state.canGenerate = Boolean(data.can_generate);
    state.keyConfigured = Boolean(data.configured);
    if (!data.can_generate) {
      setStatus("未配置模型密钥。可先用离线示例体验完整流程，或由部署者在 .env 中配置 DEEPSEEK_API_KEY。", true);
    } else if (data.is_test_double) {
      setStatus("当前使用确定性模型替身（仅测试/离线调试），结果带测试标识。");
    }
  } catch (_) {
    setStatus("无法读取模型配置状态，请确认服务已启动。", true);
  }
  await loadSources();
  await loadKnowledgeBases();
  const pendingJob = sessionStorage.getItem("xunzhi_job");
  if (pendingJob) {
    showNewLearning();
    state.jobId = pendingJob;
    $("stages").hidden = false;
    $("cancel-btn").hidden = false;
    $("generate-btn").disabled = true;
    setStatus("正在恢复上次的生成任务…");
    pollJob();
  }
}

init();