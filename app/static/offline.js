let sample = null;
let lastResult = null;

const $ = (id) => document.getElementById(id);

function switchTab(name) {
  const handout = name === "handout";
  $("tab-handout").classList.toggle("active", handout);
  $("tab-questions").classList.toggle("active", !handout);
  $("tab-handout").setAttribute("aria-selected", String(handout));
  $("tab-questions").setAttribute("aria-selected", String(!handout));
  $("view-handout").hidden = !handout;
  $("view-questions").hidden = handout;
}

function currentAnswers() {
  return sample.questions.map((question) => {
    const selected = document.querySelector(`input[name="q-${question.id}"]:checked`);
    return { question_id: question.id, selected_index: selected ? Number(selected.value) : null };
  });
}

function updateAnswered() {
  if (!sample) return;
  const answered = currentAnswers().filter((item) => item.selected_index !== null).length;
  $("offline-answered").textContent = `已作答 ${answered} / ${sample.questions.length}`;
}

function resetQuestionControls() {
  $("offline-submit").disabled = false;
  $("offline-submit").hidden = false;
  $("offline-result").hidden = true;
  $("offline-result-body").innerHTML = "";
  $("offline-retry").hidden = true;
  $("offline-form").querySelectorAll("input").forEach((input) => (input.disabled = false));
}

function renderResult(result) {
  lastResult = result;
  $("offline-result").hidden = false;
  window.XunzhiRender.result($("offline-result-body"), result, {
    completedText: result.completed ? result.completed_text : "",
  });
  $("offline-submit").hidden = true;
  $("offline-submit").disabled = true;
  $("offline-form").querySelectorAll("input").forEach((input) => (input.disabled = true));
  $("offline-retry").hidden = !(result.next_round_suggestion?.available && !result.completed);
}

function renderSample(payload) {
  sample = payload;
  lastResult = payload.result || null;
  $("offline-title").textContent = payload.title;
  $("offline-meta").textContent = `第 ${payload.round_no} 轮 · 原创拓扑图 · 共 ${payload.questions.length} 题`;
  window.XunzhiRender.handout($("offline-handout"), payload.handout);
  const scope = payload.handout.find((section) => section.section_type === "scope");
  window.XunzhiRender.handout($("offline-scope"), scope ? [scope] : []);
  const answers = Object.entries(payload.answers || {}).map(([question_id, selected_index]) => ({
    question_id,
    selected_index,
  }));
  window.XunzhiRender.questions($("offline-form"), payload.questions, answers, payload.submitted, updateAnswered);
  updateAnswered();
  resetQuestionControls();
  if (payload.submitted) {
    if (payload.result) {
      renderResult(payload.result);
    } else {
      $("offline-submit").hidden = true;
      $("offline-form").querySelectorAll("input").forEach((input) => (input.disabled = true));
      if (payload.completed) {
        const done = document.createElement("p");
        done.className = "completed";
        done.textContent = payload.completed_text || "本轮全部答对，已掌握本示例的考点。";
        $("offline-result").hidden = false;
        $("offline-result-body").replaceChildren(done);
      }
    }
  }
}

async function load() {
  const response = await fetch("/api/offline/sample");
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || "离线示例读取失败");
  renderSample(data);
  if (data.submitted && data.result && !data.result.completed) {
    $("offline-status").textContent = "本轮已提交，已恢复判分与解析，可继续按错题考点再练。";
  } else if (data.submitted && data.completed) {
    $("offline-status").textContent = "本轮已全部答对，示例完成。";
  }
}

async function submit() {
  const answers = currentAnswers();
  const unanswered = answers.filter((item) => item.selected_index === null).length;
  if (unanswered > 0 && !window.confirm(`还有 ${unanswered} 题未作答，仍要提交吗？`)) return;
  $("offline-submit").disabled = true;
  try {
    const response = await fetch("/api/offline/attempts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ answers }),
    });
    const data = await response.json();
    if (!response.ok) {
      $("offline-status").textContent = data.error?.message || "提交失败，可重试";
      $("offline-submit").disabled = false;
      return;
    }
    renderResult({ ...data, completed_text: data.completed_text });
    $("offline-status").textContent = data.completed ? "全部答对，示例完成。" : "已提交，可查看解析或按错题考点再练。";
    switchTab("questions");
  } catch (err) {
    $("offline-status").textContent = `提交失败：${err.message}，可重试`;
    $("offline-submit").disabled = false;
  }
}

async function retry() {
  const focus = lastResult?.wrong_knowledge_points || sample?.result?.wrong_knowledge_points || [];
  if (!focus.length) {
    $("offline-status").textContent = "本轮没有错题，无需再练。";
    return;
  }
  $("offline-retry").disabled = true;
  try {
    const response = await fetch("/api/offline/retry", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ focus_ids: focus }),
    });
    const data = await response.json();
    if (!response.ok) {
      $("offline-status").textContent = data.error?.message || "再练加载失败，可重试";
      return;
    }
    lastResult = null;
    const refreshed = await fetch("/api/offline/sample");
    const payload = await refreshed.json();
    if (!refreshed.ok) {
      $("offline-status").textContent = payload.error?.message || "再练轮次读取失败";
      return;
    }
    renderSample(payload);
    $("offline-submit").disabled = false;
    $("offline-status").textContent = `已按考点进入第 ${data.round_no} 轮，可继续作答。`;
    switchTab("questions");
  } catch (err) {
    $("offline-status").textContent = `再练加载失败：${err.message}，可重试`;
  } finally {
    $("offline-retry").disabled = false;
  }
}

async function reset() {
  const ok = window.confirm("重置离线示例？示例会回到初始未作答状态，不影响真实学习记录。");
  if (!ok) return;
  const response = await fetch("/api/offline/reset", { method: "POST" });
  if (!response.ok) {
    $("offline-status").textContent = "重置失败，可重试";
    return;
  }
  lastResult = null;
  await load();
  $("offline-submit").disabled = false;
  $("offline-status").textContent = "示例已重置，可重新体验。";
  switchTab("questions");
}

$("tab-handout").addEventListener("click", () => switchTab("handout"));
$("tab-questions").addEventListener("click", () => switchTab("questions"));
$("offline-submit").addEventListener("click", submit);
$("offline-retry").addEventListener("click", retry);
$("offline-reset").addEventListener("click", reset);
$("offline-form").addEventListener("change", updateAnswered);
load().catch((err) => {
  $("offline-title").textContent = "无法打开离线示例";
  $("offline-status").textContent = err.message;
});
function renderOfflinePlan(branch, question, plan) {
  const box = $("offline-dx-result");
  box.innerHTML = "";
  const label = document.createElement("p");
  label.className = "meta";
  label.textContent = `分支 ${branch.toUpperCase()} · 离线示例`;
  box.append(label);
  if (question) {
    const title = document.createElement("h3");
    title.textContent = question.stem;
    box.append(title);
    (question.options || []).forEach((option) => {
      const line = document.createElement("p");
      line.className = "hint";
      line.textContent = `${option.key}. ${option.text}`;
      box.append(line);
    });
  }
  if (plan) {
    const heading = document.createElement("h3");
    heading.textContent = "建议计划（仅离线预览）";
    box.append(heading);
    (plan.items || []).forEach((item) => {
      const line = document.createElement("p");
      line.className = "adjustment-row";
      line.textContent = `${item.display_name}：${item.count} 题`;
      box.append(line);
    });
    if (plan.excluded?.length) {
      const line = document.createElement("p");
      line.className = "hint";
      line.textContent = `本次排除：${plan.excluded.join("、")}`;
      box.append(line);
    }
  }
}

async function runOfflineBranch(branch) {
  const response = await fetch("/api/offline/diagnosis/answer", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ branch }),
  });
  const data = await response.json();
  if (!response.ok) {
    $("offline-dx-result").textContent = data.error?.message || "离线诊断失败";
    return;
  }
  renderOfflinePlan(data.branch, data.question, data.plan);
}

async function loadOfflineDiagnosis() {
  const response = await fetch("/api/offline/diagnosis/sample");
  const data = await response.json();
  if (!response.ok) return;
  if (data.saved) {
    renderOfflinePlan(data.saved.branch, null, data.saved.plan);
  }
}

async function resetOfflineDiagnosis() {
  await fetch("/api/offline/diagnosis/reset", { method: "POST" });
  $("offline-dx-result").innerHTML = "";
}

$("offline-dx-a").addEventListener("click", () => runOfflineBranch("a"));
$("offline-dx-b").addEventListener("click", () => runOfflineBranch("b"));
$("offline-dx-reset").addEventListener("click", resetOfflineDiagnosis);
loadOfflineDiagnosis();