const $ = (id) => document.getElementById(id);

function setStatus(text) {
  const line = $("record-empty");
  if (line && text) line.textContent = text;
}

async function loadPacks() {
  const list = $("record-list");
  list.innerHTML = "";
  try {
    const response = await fetch("/api/packs?limit=50");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || "记录读取失败");
    $("record-empty").hidden = data.items.length > 0;
    data.items.forEach((pack) => {
      const li = document.createElement("li");
      const left = document.createElement("div");
      const title = document.createElement("div");
      title.className = "record-title";
      title.textContent = pack.display_title;
      const meta = document.createElement("div");
      meta.className = "meta";
      const schedule = pack.schedule || {};
      const scheduleLabel = schedule.assigned
        ? schedule.progress?.label || "已安排日程"
        : "未安排日程";
      meta.textContent = `${pack.created_at.replace("T", " ").slice(0, 16)} · ${pack.source_count} 份资料 · ${pack.question_count} 题 · ${pack.difficulty === "basic" ? "基础" : "巩固"} · ${pack.attempt_status} · ${scheduleLabel}`;
      const excerpt = document.createElement("div");
      excerpt.className = "hint";
      excerpt.textContent = pack.progress_excerpt;
      left.append(title, meta, excerpt);

      const right = document.createElement("div");
      right.className = "record-actions";
      if (pack.plan_id) {
        const calendar = document.createElement("a");
        calendar.className = "secondary link-button";
        calendar.href = `/calendar?plan_id=${encodeURIComponent(pack.plan_id)}`;
        calendar.textContent = "日历";
        right.append(calendar);
      }
      const open = document.createElement("a");
      open.className = "secondary link-button";
      open.href = `/packs/${pack.id}`;
      open.textContent = "打开";
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "secondary";
      remove.textContent = "删除";
      remove.addEventListener("click", () => deletePack(pack));
      right.append(open, remove);
      li.append(left, right);
      list.append(li);
    });
  } catch (err) {
    $("record-empty").hidden = false;
    setStatus(err.message);
  }
}

async function deletePack(pack) {
  const ok = window.confirm(
    `确定删除「${pack.display_title}」？\n\n将删除该学习计划的讲义、题目与作答记录；原始资料不受影响。删除不可撤销。`
  );
  if (!ok) return;
  const response = await fetch(`/api/packs/${pack.id}`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: true }),
  });
  if (!response.ok) {
    const data = await response.json();
    setStatus(data.error?.message || "删除失败");
    return;
  }
  await loadPacks();
  await loadSources();
}

async function loadSources() {
  const list = $("source-list");
  list.innerHTML = "";
  try {
    const response = await fetch("/api/sources");
    const data = await response.json();
    if (!response.ok) return;
    $("source-empty").hidden = data.items.length > 0;
    data.items.forEach((item) => {
      const li = document.createElement("li");
      const left = document.createElement("div");
      const title = document.createElement("div");
      title.textContent = item.display_name;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `${item.kind === "pdf" ? "PDF" : item.kind === "text" ? "文本" : "图片"} · 被 ${item.referenced_pack_count} 个学习计划引用`;
      left.append(title, meta);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "secondary";
      remove.textContent = "删除资料";
      remove.addEventListener("click", () => deleteSource(item));
      li.append(left, remove);
      list.append(li);
    });
  } catch (_) {}
}

async function deleteSource(source) {
  const impact = await fetch(`/api/sources/${source.id}/impact`);
  const info = await impact.json();
  if (!impact.ok) {
    setStatus(info.error?.message || "影响范围读取失败");
    return;
  }
  const packText = info.packs.length
    ? "\n\n引用的学习计划：\n" + info.packs.map((pack) => `· ${pack.title}（${pack.created_at.slice(0, 10)}）`).join("\n")
    : "";
  const ok = window.confirm(
    `这份资料被 ${info.referenced_pack_count} 个学习计划引用。${packText}\n\n删除后将无法再从这些学习计划定位原文或查看图片预览；历史讲义与成绩仍保留。取消不会修改任何数据。`
  );
  if (!ok) return;
  const response = await fetch(`/api/sources/${source.id}`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: true }),
  });
  if (!response.ok) {
    const data = await response.json();
    setStatus(data.error?.message || "删除失败");
    return;
  }
  await loadSources();
  await loadPacks();
}

async function loadContinueLearning() {
  const list = $("continue-list");
  const empty = $("continue-empty");
  if (!list) return;
  list.innerHTML = "";
  try {
    const response = await fetch("/api/sources");
    const data = await response.json();
    if (!response.ok) return;
    const active = data.items.filter((item) => item.status === "active");
    for (const source of active) {
      const context = await fetch(`/api/sources/${source.id}/learning-context`).then((item) => item.json());
      if (!context.can_continue) continue;
      const li = document.createElement("li");
      const left = document.createElement("div");
      const title = document.createElement("div");
      title.className = "record-title";
      title.textContent = source.display_name;
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = `${context.has_confirmed_plan ? "已有已确认计划" : "可生成首个建议"} · ${context.mastery.length} 个考点`;
      left.append(title, meta);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary";
      button.textContent = context.direct_eligible ? "直接查看建议计划" : "继续学习";
      button.addEventListener("click", () =>
        context.direct_eligible ? openNextPlan(source.id) : startDiagnosis(source.id)
      );
      li.append(left, button);
      const profile = document.createElement("a");
      profile.className = "link";
      profile.href = `/profile?source_id=${encodeURIComponent(source.id)}`;
      profile.textContent = "查看个人档案";
      li.append(profile);
      if (context.direct_eligible) {
        const diagnose = document.createElement("button");
        diagnose.type = "button";
        diagnose.className = "link";
        diagnose.textContent = "补充诊断";
        diagnose.addEventListener("click", () => startDiagnosis(source.id));
        li.append(diagnose);
      }
      list.append(li);
    }
    empty.hidden = list.children.length > 0;
  } catch (_) {
    empty.hidden = false;
  }
}

async function startDiagnosis(sourceId) {
  const response = await fetch(`/api/sources/${sourceId}/diagnosis/sessions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      idempotency_key: crypto.randomUUID(),
      goal: "补上上次的薄弱点",
      question_count: 3,
    }),
  });
  const data = await response.json();
  if (!response.ok) {
    setStatus(data.error?.message || "诊断创建失败");
    return;
  }
  location.href = `/diagnosis/${data.session.id}`;
}

async function openNextPlan(sourceId) {
  const panel = $("continue-draft");
  if (!panel) return;
  window.XunzhiPlanFlow.open({
    sourceId,
    container: panel,
    goal: "继续复习薄弱考点",
    questionCount: 3,
  });
}

async function confirmAndGenerate(data, version) {
  const confirmed = await fetch(`/api/plans/${data.plan.id}/versions/${version.id}/confirm`, {
    method: "POST",
  });
  const confirmBody = await confirmed.json();
  if (!confirmed.ok) {
    setStatus(confirmBody.error?.message || "计划确认失败");
    return;
  }
  const sources = (version.sources || []).map((item) => ({
    source_id: item.source_id,
    page_start: item.page_start,
    page_end: item.page_end,
  }));
  const created = await fetch("/api/packs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      idempotency_key: crypto.randomUUID(),
      sources,
      progress_text: "继续复习薄弱考点",
      difficulty: "consolidate",
      question_count: version.question_count,
      consent_ack: true,
      plan_version_id: version.id,
    }),
  });
  const job = await created.json();
  if (!created.ok) {
    setStatus(job.error?.message || "生成失败");
    return;
  }
  for (let index = 0; index < 60; index += 1) {
    await new Promise((resolve) => setTimeout(resolve, 1000));
    const status = await fetch(`/api/jobs/${job.job_id}`).then((item) => item.json());
    if (status.status === "succeeded") {
      location.href = `/packs/${status.pack_id}`;
      return;
    }
    if (["failed", "cancelled", "insufficient_evidence"].includes(status.status)) {
      setStatus(status.error_detail || "生成失败，可按同一计划重试");
      return;
    }
  }
  setStatus("生成仍在进行，请稍后到学习记录查看");
}

async function openConfirmedPlanFromQuery() {
  const params = new URLSearchParams(window.location.search);
  const planId = params.get("plan_id");
  const versionId = params.get("version_id");
  if (!planId || !versionId) return;
  const panel = $("continue-draft");
  if (!panel) return;
  panel.hidden = false;
  panel.innerHTML = "";
  const response = await fetch(`/api/plans/${encodeURIComponent(planId)}`);
  const data = await response.json();
  if (!response.ok) {
    panel.textContent = "已确认计划不可用";
    return;
  }
  const version = (data.versions || []).find((item) => item.id === versionId);
  if (!version || version.status !== "confirmed") {
    panel.textContent = "已确认计划不可用";
    return;
  }
  const heading = document.createElement("h3");
  heading.textContent = "已确认计划";
  panel.append(heading);
  const title = document.createElement("p");
  title.className = "record-title";
  title.textContent = data.title || "学习计划";
  panel.append(title);
  const meta = document.createElement("p");
  meta.className = "meta";
  meta.textContent = `已确认计划版本 ${version.id} · 共 ${version.question_count} 题`;
  panel.append(meta);
  (version.items || []).forEach((item) => {
    const line = document.createElement("p");
    line.className = "adjustment-row";
    line.textContent = `${item.display_name}：${item.suggested_question_count} 题`;
    panel.append(line);
  });
}

loadPacks();
loadSources();
loadContinueLearning();
openConfirmedPlanFromQuery();