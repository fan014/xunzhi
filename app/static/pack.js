const packId = document.body.dataset.packId;
const $ = (id) => document.getElementById(id);
let pack = null;
let draftTimer = null;
let positionTimer = null;
let currentModuleId = null;
let restoringPosition = false;
let submitted = false;
const scopeTaskId = new URLSearchParams(window.location.search).get("task_id");
let practiceScope = null;

function scopedPractice() {
  return Boolean(scopeTaskId);
}

function cleanModuleTitle(module, index = 0) {
  let title = String(module?.title || "").trim();
  title = title
    .replace(/[、,，]\s*\d+\/\d+(?![0-9/])/g, "")
    .replace(/^\d+\/\d+\s*[、,，]?/g, "")
    .replace(/第\s*\d+\s*[-–—]\s*\d+\s*页/g, "")
    .replace(/[^\s、,，]{1,40}\.(pdf|txt|md|png|jpe?g|webp)(?![A-Za-z])/gi, "")
    .trim();
  if (/^\d{1,3}(?:\.\d{1,3}){3}\/\d{1,2}$/.test(title)) title = `如何理解 ${title}`;
  if (!title || title.length < 2) {
    title = String(module?.objective || "").replace(/^掌握/, "").trim() || `模块 ${index + 1}`;
  }
  return title;
}

function savePosition(view) {
  clearTimeout(positionTimer);
  positionTimer = setTimeout(async () => {
    if (!pack || !view) return;
    try {
      await fetch(`/api/packs/${packId}/position`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          module_id: currentModuleId || null,
          page_id:
            window.XunzhiReader?.currentPageId?.() ||
            (pack.position && pack.position.page_id) ||
            null,
          view,
        }),
      });
    } catch (_) {
      // 位置保存失败不影响学习；下次打开回到默认位置。
    }
  }, 250);
}

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function setStatus(text, isError = false, target = "submit-status") {
  const line = $(target);
  if (!line) return;
  line.textContent = text || "";
  line.classList.toggle("error", Boolean(isError));
}

function currentAnswers() {
  return pack.questions.map((question) => {
    const selected = document.querySelector(`input[name="q-${question.id}"]:checked`);
    return { question_id: question.id, selected_index: selected ? Number(selected.value) : null };
  });
}

function updateAnswered() {
  const answered = currentAnswers().filter((item) => item.selected_index !== null).length;
  $("answered-count").textContent = `已作答 ${answered} / ${pack.questions.length}`;
}

function scheduleDraft() {
  updateAnswered();
  if (submitted) return;
  clearTimeout(draftTimer);
  draftTimer = setTimeout(saveDraft, 400);
}

async function saveDraft() {
  if (submitted) return;
  try {
    const query = scopeTaskId ? `?task_id=${encodeURIComponent(scopeTaskId)}` : "";
    const response = await fetch(`/api/packs/${packId}/draft${query}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answers: currentAnswers() }),
    });
    if (!response.ok) return;
    setStatus("草稿已保存");
  } catch (_) {
    setStatus("草稿保存失败，选择仍保留在本页", true);
  }
}

function switchTab(name) {
  if (name === "modules" && pack) {
    const modules = pack.modules || [];
    if (modules.length && !modules.some((item) => item.module_id === currentModuleId)) {
      currentModuleId = modules[0].module_id;
    }
    renderModules();
    renderModuleNav();
  }
  if (name === "review" && pack) {
    currentModuleId = "review";
    renderModuleNav();
  }
  ["handout", "modules", "review", "questions"].forEach((key) => {
    const tab = $(`tab-${key}`);
    const view = $(`view-${key}`);
    if (!tab || !view) return;
    const active = key === name;
    tab.classList.toggle("active", active);
    tab.setAttribute("aria-selected", String(active));
    view.hidden = !active;
  });
  if (!restoringPosition) savePosition(name);
}

function moduleTitles() {
  const map = {};
  (pack.modules || []).forEach((module, index) => {
    map[module.module_id] = cleanModuleTitle(module, index);
  });
  if (pack.review?.questions?.length) map.review = "综合复习";
  return map;
}

function topicTitle() {
  const modules = pack.modules || [];
  if (modules.length) return cleanModuleTitle(modules[0], 0);
  return displayTitle();
}

function renderModuleNav() {
  const nav = $("module-nav");
  if (!nav) return;
  nav.innerHTML = "";
  const modules = pack.modules || [];
  if (!modules.length) {
    nav.hidden = true;
    return;
  }
  nav.hidden = false;
  modules.forEach((module, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "module-nav-item";
    button.dataset.moduleId = module.module_id;
    button.classList.toggle("active", module.module_id === currentModuleId);
    button.textContent = cleanModuleTitle(module, index);
    button.addEventListener("click", () => {
      currentModuleId = module.module_id;
      renderModuleNav();
      renderModules();
      switchTab("modules");
    });
    nav.append(button);
  });
}

function renderRoute() {
  const box = $("handout");
  box.innerHTML = "";
  if (!(pack.modules || []).length) {
    window.XunzhiRender.handout(box, pack.handout, { hideScope: true });
    return;
  }
  const modeLabel = { auto: "系统建议", foundation: "从基础学", focus: "重点复习" }[
    (pack.plan?.modules?.[0] || {}).learning_mode || "auto"
  ] || "系统建议";
  box.append(node("h2", null, `学习路线 · ${pack.modules.length} 个模块`));
  box.append(
    node(
      "p",
      "hint",
      `${modeLabel}：建议 ${pack.full_question_count ?? pack.question_count} 道练习，覆盖 ${pack.modules.reduce(
        (total, item) => total + (item.suggested_question_count || 0),
        0
      )} 道模块练习。`
    )
  );
  pack.modules.forEach((module, index) => {
    const card = node("article", "route-module");
const head = node("div", "route-module-head");
    head.append(node("span", "route-index", String(index + 1).padStart(2, "0")));
    head.append(node("h3", null, cleanModuleTitle(module, index)));
    const actual = module.actual_question_count ?? (module.questions || []).length;
    head.append(node("span", "badge accent", `${actual} 道`));
    card.append(head);
    if (actual !== module.suggested_question_count) {
      card.append(
        node(
          "p",
          "hint",
          `计划 ${module.suggested_question_count} 道，模块内实际 ${actual} 道；其余为综合复习题。`
        )
      );
    }
    if (module.objective) card.append(node("p", "hint", `学习目标：${module.objective}`));
    if (module.core_points?.length) {
      card.append(node("p", "hint", `核心考点：${module.core_points.join("、")}`));
    }
    if (module.source_names?.length) {
      card.append(node("p", "hint", `覆盖资料：${module.source_names.join("、")}`));
    }
    if (module.completion_standard) {
      card.append(node("p", "hint", `完成标准：${module.completion_standard}`));
    }
    if (module.rationale) card.append(node("p", "plan-reason", `安排理由：${module.rationale}`));
    const actions = node("div", "actions");
    const open = document.createElement("button");
    open.type = "button";
    open.className = "secondary";
    open.textContent = "打开模块";
    open.addEventListener("click", () => {
      currentModuleId = module.module_id;
      renderModuleNav();
      renderModules();
      switchTab("modules");
      const target = document.getElementById(`module-${module.module_id}`);
      if (target) target.scrollIntoView({ block: "start" });
    });
    actions.append(open);
    if (module.questions?.length && !scopedPractice()) {
      const practice = document.createElement("button");
      practice.type = "button";
      practice.className = "link";
      practice.textContent = "直接练本模块";
      practice.addEventListener("click", () => {
        switchTab("questions");
        const target = document.getElementById(`q-${module.questions[0].id}`);
        if (target) target.scrollIntoView({ block: "start" });
      });
      actions.append(practice);
    }
    card.append(actions);
    box.append(card);
  });
  const scope = pack.scope || {};
  if (scope.not_covered?.length) {
    box.append(node("p", "hint", `本次未纳入：${scope.not_covered.join("；")}`));
  }
}

function practiceModule(module) {
  switchTab("questions");
  const first = (module.questions || [])[0];
  const target = first ? document.getElementById(`q-${first.id}`) : null;
  if (target) target.scrollIntoView({ block: "start" });
  else $("question-form")?.scrollIntoView({ block: "start" });
}

function renderRelatedSources(card, module) {
  const citations = [];
  const seen = new Set();
  for (const section of [...(module.handout || []), ...(module.pages || []), ...(module.questions || [])]) {
    for (const citation of section.citations || []) {
      const key = citation.id || `${citation.source_name}|${citation.page_no}|${citation.claim}`;
      if (seen.has(key)) continue;
      seen.add(key);
      citations.push(citation);
    }
  }
  if (!citations.length) return;
  const details = document.createElement("details");
  details.className = "related-sources";
  const summary = document.createElement("summary");
  summary.textContent = `相关资料（${citations.length}）`;
  details.append(summary);
  const list = node("div", "related-list");
  const groups = new Map();
  citations.forEach((citation) => {
    const name = citation.source_name || "资料来源";
    if (!groups.has(name)) groups.set(name, []);
    groups.get(name).push(citation);
  });
  groups.forEach((items, name) => {
    const group = node("div", "related-group");
    group.append(node("p", "related-source-name", name));
    items.forEach((citation) => {
      const row = node("div", "related-row");
      const label = citation.label || (citation.page_no ? `第 ${citation.page_no} 页` : "原文");
      row.append(node("span", "related-label", label));
      row.append(
        node(
          "span",
          "related-claim",
          String(citation.claim || citation.text_alternative || "").slice(0, 120)
        )
      );
      const button = document.createElement("button");
      button.type = "button";
      button.className = "chip";
      button.textContent = "查看原文";
      button.addEventListener("click", () =>
        window.XunzhiCitation.open(citation.id, button, citation)
      );
      row.append(button);
      group.append(row);
    });
    list.append(group);
  });
  details.append(list);
  card.append(details);
}

function renderModules() {
  const box = $("module-list");
  box.innerHTML = "";
  const modules = pack.modules || [];
  if (!modules.length) return;
  let module = modules.find((item) => item.module_id === currentModuleId);
  if (!module) {
    module = modules[0];
    currentModuleId = module.module_id;
  }
  const index = modules.indexOf(module);
  const card = node("article", "module-card study-module");
  card.id = `module-${module.module_id}`;
  const head = node("div", "panel-head");
  const title = node("div");
  title.append(node("p", "eyebrow", `模块 ${index + 1}`));
  title.append(node("h2", null, cleanModuleTitle(module, index)));
  head.append(title);
  const actions = node("div", "inline-actions");
  if (!scopedPractice()) {
    const practice = document.createElement("button");
    practice.type = "button";
    practice.className = "secondary";
    practice.textContent = "练本模块";
    practice.addEventListener("click", () => practiceModule(module));
    actions.append(practice);
  }
  head.append(actions);
  card.append(head);
  if (module.objective) card.append(node("p", "hint", `学习目标：${module.objective}`));
  if (module.completion_standard) {
    card.append(node("p", "hint", `完成标准：${module.completion_standard}`));
  }
  const visualBox = node("div", "module-visual");
  window.XunzhiVisual?.render(visualBox, module);
  if (visualBox.childNodes.length) card.append(visualBox);
  const handoutBox = node("div", "module-handout");
  if (module.handout?.length) {
    window.XunzhiRender.handout(handoutBox, module.handout, {
      hideScope: true,
      hideCitations: true,
    });
  } else {
    handoutBox.append(node("p", "hint", "本模块讲义暂未生成，可返回记录页按同一计划重试。"));
  }
  card.append(handoutBox);
  const firstQuestion = (module.questions || [])[0];
  if (firstQuestion && !scopedPractice()) {
    const practiceBox = node("section", "study-practice");
    practiceBox.append(node("h3", null, "练一题"));
    practiceBox.append(node("p", "stem", firstQuestion.stem));
    const button = document.createElement("button");
    button.type = "button";
    button.className = "primary";
    button.textContent = "去作答";
    button.addEventListener("click", () => practiceModule(module));
    practiceBox.append(button);
    card.append(practiceBox);
  }
  renderRelatedSources(card, module);
  box.append(card);
}

function renderReview() {
  const route = $("review-route");
  const handoutBox = $("review-handout");
  const questionBox = $("review-questions");
  route.innerHTML = "";
  handoutBox.innerHTML = "";
  questionBox.innerHTML = "";
  if (!(pack.modules || []).length) return;
  route.append(node("h2", null, "综合复习"));
  route.append(
    node(
      "p",
      "hint",
      `覆盖 ${pack.modules.length} 个模块的主要考点；综合题用于跨模块应用或对比。`
    )
  );
  const sections = pack.review?.handout || [];
  if (sections.length) {
    window.XunzhiRender.handout(handoutBox, sections, { hideScope: false });
  } else {
    handoutBox.append(node("p", "hint", "综合复习讲义以各模块要点为准。"));
  }
  const reviewQuestions = pack.review?.questions || [];
  if (!reviewQuestions.length) {
    questionBox.append(
      node("p", "hint", "本方案没有单独的综合题，可直接回到模块练习按顺序复习。")
    );
    return;
  }
  reviewQuestions.forEach((question) => {
    const row = node("div", "review-question");
    row.append(node("p", "stem", `${question.ordinal}. ${question.stem}`));
    row.append(node("p", "meta", "跨模块综合题"));
    const resultItem = pack.result?.questions?.find((item) => item.question_id === question.id);
    if (resultItem) {
      row.append(
        node(
          "p",
          resultItem.is_correct ? "verdict ok" : "verdict bad",
          resultItem.is_correct ? "回答正确" : "回答错误"
        )
      );
    }
    const link = document.createElement("button");
    link.type = "button";
    link.className = "link";
    link.textContent = "去作答";
    link.addEventListener("click", () => {
      switchTab("questions");
      const target = document.getElementById(`q-${question.id}`);
      if (target) target.scrollIntoView({ block: "start" });
    });
    row.append(link);
    questionBox.append(row);
  });
}

function displayTitle() {
  const sources = pack.sources || [];
  const source = sources.find((item) => item.status === "active") || sources[0];
  const raw = String(source?.display_name || "").trim();
  if (!raw) return "学习计划";
  let name = raw.replace(/\.[^.]+$/, "").trim();
  if (!name) return "学习计划";
  if (name.length > 18) name = `${name.slice(0, 18)}…`;
  return `${name} 学习计划`;
}

function formatScopeDate(value) {
  const text = String(value || "").slice(0, 10);
  const parts = text.split("-");
  if (parts.length !== 3) return text;
  return `${Number(parts[1])} 月 ${Number(parts[2])} 日`;
}

function renderScopeBanner() {
  const box = $("practice-scope");
  if (!box) return;
  if (!practiceScope) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  box.innerHTML = "";
  box.classList.toggle("is-complete", Boolean(practiceScope.completed));
  box.classList.toggle("is-unavailable", !practiceScope.available);
  const title = practiceScope.available
    ? `正在完成：${practiceScope.module_title || "本组练习"} · ${practiceScope.total} 道`
    : `${practiceScope.module_title || "本组练习"} · 暂无可用题目`;
  box.append(node("strong", null, title));
  box.append(node("span", "meta", `原安排 ${formatScopeDate(practiceScope.scheduled_date)}`));
  if (practiceScope.calendar_url) {
    const link = document.createElement("a");
    link.className = "link";
    link.href = practiceScope.calendar_url;
    link.textContent = "返回学习日历";
    box.append(link);
  }
  if (!practiceScope.available) {
    box.append(node("p", "practice-scope-note warn", practiceScope.reason || "本组没有可对应题目。"));
    return;
  }
  if (practiceScope.completed) {
    const note = practiceScope.completed_ahead
      ? "你提前完成了这组练习，原计划日期保持不变。"
      : "这组练习已完成。";
    box.append(node("p", "practice-scope-note ok", note));
    return;
  }
  if (practiceScope.last_attempt_id) {
    box.append(
      node(
        "p",
        "practice-scope-note warn",
        `已交卷，本组还剩 ${practiceScope.remaining} 题；任务仍待完成。`
      )
    );
  } else {
    box.append(node("p", "practice-scope-note", "本组只结算这组题目，不影响其他练习任务。"));
  }
}

function renderHeader() {
  const fullTitle = pack.display_title || "学习计划";
  const title = practiceScope
    ? `${practiceScope.module_title || "练习"} · ${practiceScope.total} 道`
    : topicTitle();
  $("pack-title").textContent = title;
  $("pack-title").title = fullTitle;
  $("pack-meta").textContent = practiceScope
    ? `日程练习 · ${practiceScope.total} 道 · ${practiceScope.completed ? "已完成" : "待完成"}`
    : `${pack.difficulty === "basic" ? "基础" : "巩固"} · ${pack.question_count} 道单选题`;
  renderScopeBanner();
  $("progress-quote").textContent = `进度原文：${pack.progress.raw_text}`;
  const scope = document.createElement("div");
  window.XunzhiRender.handout(scope, [{
    section_type: "scope",
    content: pack.scope,
    citations: [],
  }]);
  $("scope-box").replaceChildren(...scope.childNodes);

  const sourceNames = (pack.sources || []).map((item) => {
    const deleted = item.status !== "active";
    const range = item.kind === "pdf" && item.page_start ? `（第 ${item.page_start}–${item.page_end} 页）` : "";
    return `${item.display_name}${range}${deleted ? "（来源资料已删除）" : ""}`;
  });
  $("source-summary").textContent = sourceNames.length ? `本次资料：${sourceNames.join("、")}` : "";

  const relation = $("relation-box");
  relation.innerHTML = "";
  if (pack.parent_pack_id) {
    const label = document.createElement("p");
    label.className = "hint";
    if (pack.parent_status === "active") {
      label.append("上一轮：");
      const link = document.createElement("a");
      link.href = `/packs/${pack.parent_pack_id}`;
      link.textContent = pack.parent_label || "查看上一轮";
      label.append(link);
    } else {
      label.textContent = "上一轮学习计划已删除，仍可阅读本包内容。";
    }
    relation.append(label);
  }
  if (pack.children?.length) {
    const label = document.createElement("p");
    label.className = "hint";
    label.append("由本包派生的再练：");
    pack.children.forEach((child, index) => {
      if (index) label.append("、");
      const link = document.createElement("a");
      link.href = `/packs/${child.id}`;
      link.textContent = child.display_title;
      label.append(link);
    });
    relation.append(label);
  }
  renderPlanAdoption(pack.plan);
}

function renderPlanAdoption(plan) {
  const box = $("plan-adoption");
  if (!box) return;
  window.XunzhiRender.planAdoption(box, plan || { linked: false }, retryWithPlan);
  box.hidden = false;
}

async function retryWithPlan(versionId) {
  const body = {
    idempotency_key: crypto.randomUUID(),
    sources: (pack.sources || []).map((item) => ({
      source_id: item.source_id,
      page_start: item.page_start,
      page_end: item.page_end,
    })),
    progress_text: pack.progress?.raw_text || "继续复习薄弱考点",
    difficulty: pack.difficulty,
    question_count: pack.question_count,
    consent_ack: true,
    plan_version_id: versionId,
  };
  const response = await fetch("/api/packs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    setStatus(data.error?.message || "按同一计划重试失败", true);
    return;
  }
  setStatus("正在按同一计划重试…");
  for (let index = 0; index < 60; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    const job = await fetch(`/api/jobs/${data.job_id}`).then((item) => item.json());
    if (job.status === "succeeded") {
      location.href = `/packs/${job.pack_id}`;
      return;
    }
    if (["failed", "cancelled", "insufficient_evidence"].includes(job.status)) {
      setStatus(job.error_detail || "生成失败，可再次按同一计划重试", true);
      return;
    }
  }
  setStatus("生成仍在进行，请稍后回到记录页查看", true);
}

function renderQuestions() {
  if (practiceScope && !practiceScope.available) {
    const form = $("question-form");
    form.innerHTML = "";
    form.append(
      node(
        "p",
        "hint warn",
        practiceScope.reason || "本组在本次学习内容中没有可对应题目，无法进入练习。"
      )
    );
    $("submit-btn").hidden = true;
    $("reattempt-btn").hidden = true;
    return;
  }
  const disabled = Boolean(pack.answer_revealed);
  window.XunzhiRender.questions(
    $("question-form"),
    pack.questions,
    pack.draft?.answers || [],
    disabled,
    scheduleDraft,
    { moduleTitles: moduleTitles() }
  );
  updateAnswered();
}

function showResult(result, completedText) {
  $("result-panel").hidden = false;
  window.XunzhiRender.result($("result-body"), result, {
    completedText,
    moduleTitles: moduleTitles(),
  });
  const score = result.score || {};
  if (!completedText && score.total && score.wrong === 0 && score.unanswered === 0) {
    const done = document.createElement("p");
    done.className = "completed";
    done.textContent = "本轮全部答对，可以修改进度后再练。";
    $("result-body").append(done);
  }
  const scope = result.practice_scope || practiceScope;
  if (scopedPractice() && scope) {
    if (scope.completed) {
      $("result-body").append(
        node(
          "p",
          "completed",
          scope.completed_ahead
            ? "你提前完成了这组练习，原计划日期保持不变。"
            : "这组练习已完成。"
        )
      );
    } else if (scope.last_attempt_id) {
      $("result-body").append(
        node(
          "p",
          "hint schedule-warn",
          `本组还剩 ${scope.remaining} 题，任务仍待完成。可以继续做本组或重新作答。`
        )
      );
    }
    $("retry-focus-btn").hidden = true;
    $("reattempt-btn").textContent = scope.completed ? "重做本组" : "继续做本组";
  }
  $("submit-btn").hidden = true;
  $("reattempt-btn").hidden = false;
  const hasWrong = Boolean(result.next_round_suggestion?.available);
  const sourcesMissing = (pack.sources || []).some((item) => item.status !== "active");
  if (hasWrong && !result.completed && !scopedPractice()) {
    $("retry-focus-btn").hidden = false;
    if (sourcesMissing) {
      $("retry-focus-btn").disabled = true;
      $("retry-focus-btn").title = "原资料已删除，需重新选择或上传";
      setStatus("原资料已删除，再练需重新选择或上传；本轮成绩仍可回看。", true);
    }
  } else {
    $("retry-focus-btn").hidden = true;
  }
  if (result.attempt_id) loadMasteryChanges(result.attempt_id);
  if (result.attempt_id) initAssessment(result);
}

let assessmentTimer = null;

function setAssessmentStatus(text, isError = false) {
  const line = $("assessment-status");
  if (!line) return;
  line.textContent = text || "";
  line.classList.toggle("error", Boolean(isError));
}

function assessmentSourceId() {
  const active = (pack.sources || []).find((item) => item.status === "active");
  return (active || (pack.sources || [])[0] || {}).source_id || null;
}

function assessmentButton(text, handler, className = "secondary") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = text;
  button.addEventListener("click", handler);
  return button;
}

const ASSESSMENT_SOURCE_STATUS = {
  ready: "已完成",
  failed: "待重试",
  pending: "生成中",
  missing: "待生成",
};

const ASSESSMENT_SOURCE_TONE = {
  ready: "ok",
  failed: "danger",
  pending: "warn",
  missing: "",
};

function renderAssessmentSourceRows(container, payload, attemptId) {
  const sources = payload.sources || [];
  if (!sources.length) return;
  const box = document.createElement("div");
  box.className = "assessment-sources";
  sources.forEach((source) => {
    const row = document.createElement("div");
    row.className = "assessment-source";
    row.append(
      node(
        "span",
        `badge ${ASSESSMENT_SOURCE_TONE[source.status] || ""}`.trim(),
        ASSESSMENT_SOURCE_STATUS[source.status] || "待生成"
      )
    );
    row.append(node("span", "assessment-source-name", source.source_name || "资料"));
    const counts = source.covered_questions
      ? `覆盖 ${source.covered_questions} 题`
      : "";
    if (counts) row.append(node("span", "hint", counts));
    if (source.status === "ready") {
      const link = document.createElement("a");
      link.className = "link";
      link.href = `/profile?source_id=${encodeURIComponent(source.source_id)}`;
      link.textContent = "查看这份资料的档案";
      row.append(link);
    } else if (source.status === "failed" || source.status === "missing") {
      const failed = source.status === "failed";
      row.append(
        assessmentButton(
          failed ? "重试这份" : "补测这份",
          () => runAssessment(attemptId, failed, source.source_id)
        )
      );
    }
    box.append(row);
  });
  container.append(box);
}

function renderAssessmentDetails(container, payload) {
  (payload.findings || []).forEach((item) => {
    const row = document.createElement("div");
    row.className = "profile-finding";
    const label = item.kind === "possible_misunderstanding" ? "可能的误解（待验证）" : "观察到的表现";
    const meta = document.createElement("p");
    meta.className = "meta";
    meta.textContent = `${item.knowledge_point || ""} · ${label}`;
    const text = document.createElement("p");
    text.textContent = item.judgement || "";
    row.append(meta, text);
    if (item.note) {
      const note = document.createElement("p");
      note.className = "hint";
      note.textContent = item.note;
      row.append(note);
    }
    container.append(row);
  });
  (payload.route_steps || []).forEach((item, index) => {
    const row = document.createElement("p");
    row.className = "hint";
    row.textContent = `${index + 1}. ${item.action_label || item.action}：${item.instruction || ""}（完成标准：${item.completion || ""}）`;
    container.append(row);
  });
  (payload.unknowns || []).forEach((item) => {
    const row = document.createElement("p");
    row.className = "hint";
    row.textContent = `待验证：${item.knowledge_point || ""}——${item.reason || ""}；下一轮：${item.next_check || ""}`;
    container.append(row);
  });
  const pending = payload.coverage?.pending_verification || [];
  pending.slice(0, 4).forEach((item) => {
    const row = document.createElement("p");
    row.className = "hint";
    row.textContent = `待验证：${item.knowledge_point || "这道题"}——${item.reason}`;
    container.append(row);
  });
}

function renderAssessment(snapshot) {
  const panel = $("assessment-panel");
  if (!panel) return;
  panel.hidden = false;
  const body = $("assessment-body");
  const actions = $("assessment-actions");
  body.innerHTML = "";
  actions.innerHTML = "";
  if (!snapshot) {
    setAssessmentStatus("尚未生成测评。");
    return;
  }
  const multi = Boolean(snapshot.multi_source) || (snapshot.sources || []).length > 1;
  const attemptId = snapshot.attempt_id;
  if (snapshot.status === "pending") {
    setAssessmentStatus("正在分析这次练习…");
    pollAssessment(attemptId);
    return;
  }
  if (multi) {
    renderAssessmentSourceRows(body, snapshot, attemptId);
  }
  if (snapshot.status === "failed") {
    setAssessmentStatus(snapshot.failure?.message || "测评失败，可重试", true);
    if (!multi) {
      actions.append(
        assessmentButton("重试测评", () => runAssessment(attemptId, true))
      );
    }
    return;
  }
  if (multi && snapshot.status === "partial") {
    setAssessmentStatus(
      `${snapshot.label || "部分资料已完成"}。已完成的结论可以查看，失败或缺失的资料可单独补测。`
    );
  } else if (multi) {
    setAssessmentStatus(snapshot.label || "本次测评已完成。");
  } else {
    const when = snapshot.completed_at
      ? String(snapshot.completed_at).replace("T", " ").slice(0, 16)
      : "";
    setAssessmentStatus(`本次测评已完成${when ? `（${when}）` : ""}。`);
  }

  let details = body;
  if (multi) {
    details = document.createElement("details");
    details.className = "assessment-conclusions";
    const summary = document.createElement("summary");
    summary.textContent = "查看测评结论";
    details.append(summary);
    body.append(details);
  }
  renderAssessmentDetails(details, snapshot);

  if (!multi) {
    const profileLink = document.createElement("a");
    profileLink.className = "secondary link-button";
    profileLink.href = `/profile${assessmentSourceId() ? `?source_id=${encodeURIComponent(assessmentSourceId())}` : ""}`;
    profileLink.textContent = "查看个人档案";
    if (assessmentSourceId()) {
      actions.append(
        assessmentButton("制定下一轮计划", () => {
          $("next-plan-box").hidden = false;
          window.XunzhiPlanFlow.open({
            sourceId: assessmentSourceId(),
            container: $("next-plan-box"),
            goal: pack.progress?.raw_text || "继续复习薄弱考点",
            questionCount: 5,
          });
        }, "primary")
      );
    }
    actions.append(profileLink);
  } else {
    const profileLink = document.createElement("a");
    profileLink.className = "secondary link-button";
    profileLink.href = "/profile";
    profileLink.textContent = "查看个人档案";
    actions.append(profileLink);
  }
}

async function pollAssessment(attemptId) {
  if (assessmentTimer) clearTimeout(assessmentTimer);
  let attempts = 0;
  const tick = async () => {
    attempts += 1;
    try {
      const response = await fetch(
        `/api/packs/${packId}/assessment?attempt_id=${encodeURIComponent(attemptId)}`
      );
      const data = await response.json();
      if (!response.ok) throw new Error(data.error?.message || "测评状态读取失败");
      if (data.available && data.snapshot && data.snapshot.status !== "pending") {
        renderAssessment(data.snapshot);
        return;
      }
    } catch (error) {
      setAssessmentStatus(error.message, true);
      return;
    }
    if (attempts >= 60) {
      setAssessmentStatus("测评仍在进行，可稍后刷新查看。", true);
      return;
    }
    assessmentTimer = setTimeout(tick, 2000);
  };
  assessmentTimer = setTimeout(tick, 2000);
}

async function runAssessment(attemptId, force, sourceId) {
  setAssessmentStatus("正在分析这次练习…");
  const actions = $("assessment-actions");
  if (actions) actions.innerHTML = "";
  try {
    const response = await fetch(`/api/packs/${packId}/assessment`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        attempt_id: attemptId,
        force: Boolean(force),
        source_id: sourceId || null,
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      setAssessmentStatus(data.error?.message || "测评失败", true);
      if (actions) {
        actions.append(
          assessmentButton(
            sourceId ? "重试这份" : "重试测评",
            () => runAssessment(attemptId, true, sourceId)
          )
        );
      }
      return;
    }
    renderAssessment(data);
  } catch (error) {
    setAssessmentStatus(`测评请求失败：${error.message}`, true);
    if (actions) {
      actions.append(
        assessmentButton(
          sourceId ? "重试这份" : "重试测评",
          () => runAssessment(attemptId, true, sourceId)
        )
      );
    }
  }
}

async function initAssessment(result) {
  const panel = $("assessment-panel");
  if (!panel) return;
  panel.hidden = false;
  if (result.assessment) {
    renderAssessment(result.assessment);
    return;
  }
  await runAssessment(result.attempt_id, false);
}

async function loadMasteryChanges(attemptId) {
  const box = $("mastery-changes");
  if (!box) return;
  box.innerHTML = "";
  try {
    const response = await fetch(
      `/api/packs/${packId}/mastery-changes?attempt_id=${encodeURIComponent(attemptId)}`
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || "掌握变化读取失败");
    window.XunzhiRender.masteryChanges(box, data);
  } catch (err) {
    const line = document.createElement("p");
    line.className = "hint";
    line.textContent = `掌握变化读取失败：${err.message}（本轮分数不受影响）`;
    box.append(line);
  }
}

async function loadFocus() {
  const response = await fetch(`/api/packs/${packId}/review-focus`);
  const data = await response.json();
  const list = $("focus-list");
  list.innerHTML = "";
  if (!response.ok || !data.available) {
    $("retry-status").textContent = data.reason || "本轮无错题，可修改进度后再练";
    return false;
  }
  data.items.forEach((item) => {
    const li = document.createElement("li");
    const input = document.createElement("input");
    input.type = "text";
    input.maxLength = 80;
    input.value = item.edited_text || item.knowledge_point;
    input.dataset.focusId = item.id;
    input.addEventListener("change", async () => {
      const text = input.value.trim();
      if (!text) {
        input.value = item.knowledge_point;
        return;
      }
      const saved = await fetch(`/api/packs/${packId}/review-focus/${item.id}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      });
      $("retry-status").textContent = saved.ok ? "已保存修改" : "修改保存失败";
    });
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "secondary";
    remove.textContent = "删除";
    remove.addEventListener("click", async () => {
      const response_ = await fetch(`/api/packs/${packId}/review-focus/${item.id}`, { method: "DELETE" });
      if (response_.ok) li.remove();
      else $("retry-status").textContent = "删除失败";
    });
    li.append(input, remove);
    list.append(li);
  });
  $("retry-scope").textContent = `沿用：${(pack.sources || []).map((item) => item.display_name).join("、")}`;
  return true;
}

async function openRetry() {
  $("retry-panel").hidden = false;
  $("retry-panel").scrollIntoView({ block: "nearest" });
  const ok = await loadFocus();
  if (!ok) {
    $("retry-generate").disabled = true;
  }
}

async function addFocus() {
  const input = $("focus-input");
  const text = input.value.trim();
  if (!text) return;
  const response = await fetch(`/api/packs/${packId}/review-focus`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ knowledge_point: text, suggestion: "" }),
  });
  const data = await response.json();
  if (!response.ok) {
    $("retry-status").textContent = data.error?.message || "补充失败";
    return;
  }
  input.value = "";
  await loadFocus();
}

async function generateRetry() {
  const inputs = Array.from(document.querySelectorAll("#focus-list input"));
  const focus = inputs.map((item) => item.value.trim()).filter(Boolean);
  if (!focus.length) {
    $("retry-status").textContent = "至少保留 1 条复习重点";
    return;
  }
  if (focus.length > 5) {
    $("retry-status").textContent = "复习重点最多 5 条，请先合并";
    return;
  }
  $("retry-generate").disabled = true;
  $("retry-status").textContent = "正在生成新的学习计划…";
  const body = {
    idempotency_key: crypto.randomUUID(),
    sources: [],
    progress_text: pack.progress.raw_text,
    difficulty: pack.difficulty,
    question_count: Number($("retry-count").value),
    consent_ack: true,
    parent_pack_id: pack.id,
    review_focus: focus,
  };
  const response = await fetch("/api/packs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await response.json();
  if (!response.ok) {
    $("retry-generate").disabled = false;
    $("retry-status").textContent = data.error?.message || "再练生成失败";
    return;
  }
  sessionStorage.setItem("xunzhi_pending_job", data.job_id);
  pollRetry(data.job_id);
}

async function pollRetry(jobId) {
  const response = await fetch(`/api/jobs/${jobId}`);
  const data = await response.json();
  if (!response.ok) {
    $("retry-status").textContent = data.error?.message || "任务读取失败";
    $("retry-generate").disabled = false;
    return;
  }
  if (data.status === "succeeded" && data.pack_id) {
    location.href = `/packs/${data.pack_id}`;
    return;
  }
  if (["failed", "insufficient_evidence", "cancelled"].includes(data.status)) {
    $("retry-status").textContent = data.error?.message || "再练生成失败";
    $("retry-generate").disabled = false;
    return;
  }
  $("retry-status").textContent = "正在生成…";
  setTimeout(() => pollRetry(jobId), 1200);
}

async function submit() {
  const answers = currentAnswers();
  const unanswered = answers.filter((item) => item.selected_index === null).length;
  if (unanswered > 0 && !window.confirm(`还有 ${unanswered} 题未作答，未作答不计入正确率。仍要提交吗？`)) return;
  clearTimeout(draftTimer);
  submitted = true;
  const keyName = scopeTaskId
    ? `xunzhi_submit_${packId}_${scopeTaskId}`
    : `xunzhi_submit_${packId}`;
  let key = sessionStorage.getItem(keyName);
  if (!key) {
    key = crypto.randomUUID();
    sessionStorage.setItem(keyName, key);
  }
  $("submit-btn").disabled = true;
  try {
    const response = await fetch(`/api/packs/${packId}/submit`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        idempotency_key: key,
        answers,
        task_id: scopeTaskId || null,
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      submitted = false;
      setStatus(data.error?.message || "提交失败", true);
      $("submit-btn").disabled = false;
      return;
    }
    if (scopedPractice() && data.practice_scope) {
      practiceScope = data.practice_scope;
      renderScopeBanner();
      if (data.practice_scope.completed) {
        setStatus(
          data.practice_scope.completed_ahead
            ? "你提前完成了这组练习，已记录实际完成时间。"
            : "这组练习已完成。"
        );
      } else {
        setStatus(`已交卷，本组还剩 ${data.practice_scope.remaining} 题，任务仍待完成。`);
      }
    } else {
      setStatus(data.duplicate ? "本轮已提交，显示已保存的结果" : "已提交");
    }
    $("question-form").querySelectorAll("input").forEach((input) => (input.disabled = true));
    showResult(data, data.completed_text || "");
    switchTab("questions");
  } catch (err) {
    submitted = false;
    setStatus(`提交失败：${err.message}`, true);
    $("submit-btn").disabled = false;
  }
}

async function reattempt() {
  const query = scopeTaskId ? `?task_id=${encodeURIComponent(scopeTaskId)}` : "";
  const response = await fetch(`/api/packs/${packId}/reattempt${query}`, { method: "POST" });
  const data = await response.json();
  if (!response.ok) {
    setStatus(data.error?.message || "无法重新作答", true);
    return;
  }
  if (scopeTaskId) {
    sessionStorage.removeItem(`xunzhi_submit_${packId}_${scopeTaskId}`);
  } else {
    sessionStorage.removeItem(`xunzhi_submit_${packId}`);
  }
  location.reload();
}

async function init() {
  try {
    const scopeQuery = scopeTaskId ? `?task_id=${encodeURIComponent(scopeTaskId)}` : "";
    const response = await fetch(`/api/packs/${packId}${scopeQuery}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || "学习计划读取失败");
    pack = data;
    window.__xunzhiPackData = data;
    practiceScope = pack.practice_scope || null;
    submitted = Boolean(pack.answer_revealed);
    const params = new URLSearchParams(window.location.search);
    if (params.get("module_id") || params.get("page_id") || params.get("view")) {
      pack.position = pack.position || {};
      if (params.get("module_id")) pack.position.module_id = params.get("module_id");
      if (params.get("page_id")) pack.position.page_id = params.get("page_id");
      const view = params.get("view");
      if (["handout", "modules", "review", "questions"].includes(view)) {
        pack.position.view = view;
      }
      if (params.get("page_id")) {
        pack.explicit_page_id = params.get("page_id");
        pack.explicit_module_id = params.get("module_id") || null;
      }
    }
    if (pack.is_test_double) {
      $("test-banner").hidden = false;
      $("test-banner").textContent = "测试替身回放：本页内容由确定性模型替身生成，不是真实模型结果。";
    }
    renderHeader();
    const position = pack.position || {};
    const modules = pack.modules || [];
    let initialView = "handout";
    if (modules.length) {
      const moduleIds = new Set(modules.map((item) => item.module_id));
      currentModuleId = moduleIds.has(position.module_id)
        ? position.module_id
        : modules[0].module_id;
      $("tab-handout").textContent = "学习路线";
      $("tab-modules").hidden = false;
      $("tab-review").hidden = true;
      renderRoute();
      renderModuleNav();
      renderModules();
      initialView = position.view || "modules";
      if (initialView === "review") initialView = "modules";
      if (scopedPractice()) initialView = "questions";
      if (pack.answer_revealed && (!position.view || position.view === "questions")) {
        initialView = "questions";
      }
      if (initialView === "review") currentModuleId = "review";
    } else {
      $("tab-handout").textContent = "讲义";
      window.XunzhiRender.handout($("handout"), pack.handout, { hideScope: true });
    }
    renderQuestions();
    if (pack.answer_revealed && pack.result) showResult(pack.result, "");
    restoringPosition = true;
    switchTab(initialView);
    restoringPosition = false;
    if (pack.explicit_page_id && initialView === "modules") {
      window.XunzhiReader?.render(currentModuleId);
    }

    $("tab-handout").addEventListener("click", () => switchTab("handout"));
    $("tab-modules").addEventListener("click", () => switchTab("modules"));
    $("tab-review").addEventListener("click", () => switchTab("review"));
    $("tab-questions").addEventListener("click", () => switchTab("questions"));
    $("continue-practice").addEventListener("click", () => {
      switchTab("questions");
      const first = (pack.questions || []).find((question) => {
        const selected = document.querySelector(`input[name="q-${question.id}"]:checked`);
        return !selected;
      }) || (pack.questions || [])[0];
      const target = first ? document.getElementById(`q-${first.id}`) : null;
      if (target) target.scrollIntoView({ block: "start" });
    });
    $("submit-btn").addEventListener("click", submit);
    $("reattempt-btn").addEventListener("click", reattempt);
    $("retry-focus-btn").addEventListener("click", openRetry);
    $("focus-add-btn").addEventListener("click", addFocus);
    $("retry-generate").addEventListener("click", generateRetry);

    const pending = sessionStorage.getItem("xunzhi_pending_job");
    if (pending) {
      sessionStorage.removeItem("xunzhi_pending_job");
      pollRetry(pending);
    }
  } catch (err) {
    $("pack-title").textContent = "无法打开学习计划";
    $("pack-meta").textContent = err.message;
  }
}

init();