const $ = (id) => document.getElementById(id);

function node(tag, className, text) {
  const item = document.createElement(tag);
  if (className) item.className = className;
  if (text !== undefined) item.textContent = text;
  return item;
}

function setStatus(text, isError = false) {
  const line = $("profile-status");
  line.textContent = text || "";
  line.classList.toggle("error", Boolean(isError));
}

const MASTERY_LABEL = {
  needs_reinforce: "需巩固",
  consolidating: "正在巩固",
  stable: "相对稳定",
  pending: "待评估",
};

const MASTERY_TONE = {
  needs_reinforce: "danger",
  consolidating: "warn",
  stable: "ok",
  pending: "",
};

function findingsFor(snapshot, pointId) {
  return (snapshot.findings || []).filter((item) => item.knowledge_point_id === pointId);
}

function focusPoints(snapshot) {
  const points = (snapshot.points || []).filter((item) => item.mastery);
  const withFindings = points.filter((item) => findingsFor(snapshot, item.knowledge_point_id).length);
  const others = points.filter((item) => !withFindings.includes(item));
  return [...withFindings, ...others].sort((a, b) => {
    const rank = { needs_reinforce: 0, consolidating: 1, pending: 2, stable: 3 };
    return (rank[a.mastery?.state] ?? 4) - (rank[b.mastery?.state] ?? 4);
  });
}

function renderFocus(box, snapshot) {
  const points = focusPoints(snapshot);
  if (!points.length) return;
  box.append(node("h3", null, "需要优先巩固"));

  points.forEach((point) => {
    const mastery = point.mastery || {};
    const findings = findingsFor(snapshot, point.knowledge_point_id);
    const row = node("div", "profile-focus");
    const name = node("span", "focus-name", point.display_name || point.knowledge_point_id);
    const badge = node(
      "span",
      `badge ${MASTERY_TONE[mastery.state] || ""}`.trim(),
      MASTERY_LABEL[mastery.state] || mastery.state || "待评估"
    );
    row.append(name, badge);
    box.append(row);

    if (!findings.length && mastery.decision_reason) {
      box.append(node("p", "hint", mastery.decision_reason));
    }

    findings.forEach((item) => {
      const card = node("div", "profile-finding");
      const isPossible = item.kind === "possible_misunderstanding";
      card.append(
        node(
          "p",
          "meta",
          isPossible ? "可能的误解（待验证）" : "观察到的表现"
        )
      );
      card.append(node("p", null, item.judgement || ""));
      if (item.note) card.append(node("p", "hint", item.note));
      card.append(node("p", "hint", `依据：${(item.evidence_ids || []).length} 条作答证据`));
      box.append(card);
    });
  });
}

function optionLabel(value) {
  if (!value) return "—";
  if (typeof value === "string") return value;
  if (typeof value === "object") {
    const text = value.text || value.label || "";
    return value.key ? `${value.key}. ${text}` : text || "—";
  }
  return String(value);
}

function renderEvidence(box, snapshot) {
  const wrong = (snapshot.evidence || []).filter((item) => item.result === "wrong");
  if (!wrong.length) return;
  box.append(node("h3", null, "错题依据"));
  wrong.slice(0, 6).forEach((item, index) => {
    const row = node("div", "profile-evidence");
    row.append(node("p", "stem", `${index + 1}. ${item.stem}`));
    const meta = node("p", "meta");
    meta.append(
      `你的答案：${optionLabel(item.selected_text)} · 正确答案：${optionLabel(item.correct_text)}`
    );
    if (item.knowledge_point) meta.append(` · ${item.knowledge_point}`);
    row.append(meta);
    const tags = node("p", "inline-actions");
    tags.append(node("span", "badge danger", "答错"));
    if (item.is_original_redo) tags.append(node("span", "badge", "重做原题"));
    if (item.is_new_question) tags.append(node("span", "badge accent", "同考点新题"));
    if (item.pack_id) {
      const link = document.createElement("a");
      link.href = `/packs/${item.pack_id}`;
      link.textContent = "查看原题";
      link.className = "link";
      tags.append(link);
    }
    row.append(tags);
    box.append(row);
  });
}

function renderRoute(box, snapshot) {
  const routes = snapshot.route_steps || [];
  if (routes.length) {
    box.append(node("h3", null, "推荐学习路线"));
    routes.forEach((item, index) => {
      const row = node("div", "profile-step");
      row.append(
        node(
          "p",
          "meta",
          `第 ${index + 1} 步 · ${item.action_label || item.action} · ${item.knowledge_point || ""}`
        )
      );
      row.append(node("p", null, item.instruction || ""));
      if (item.completion) row.append(node("p", "hint", `完成标准：${item.completion}`));
      box.append(row);
    });
  }
  const unknowns = snapshot.unknowns || [];
  if (unknowns.length) {
    box.append(node("h3", null, "还需要验证"));
    unknowns.forEach((item) => {
      box.append(
        node(
          "p",
          "hint",
          `${item.knowledge_point || ""}：${item.reason}；下一轮：${item.next_check}`
        )
      );
    });
  }
}

function renderSnapshot(snapshot) {
  const card = node("section", "panel profile-card");
  card.append(node("h2", null, snapshot.source_name || "资料"));

  const when = snapshot.completed_at
    ? String(snapshot.completed_at).replace("T", " ").slice(0, 16)
    : "时间未知";
  const meta = node("p", "profile-meta");
  meta.append(`最近有效测评：${when}`);
  if (snapshot.pack_id) {
    meta.append(" · 对应练习：");
    const link = document.createElement("a");
    link.href = `/packs/${snapshot.pack_id}`;
    link.textContent = "查看本次练习";
    meta.append(link);
  }
  card.append(meta);

  renderFocus(card, snapshot);
  renderEvidence(card, snapshot);
  renderRoute(card, snapshot);

  const actions = node("div", "actions profile-plan");
  const planButton = document.createElement("button");
  planButton.type = "button";
  planButton.className = "primary";
  planButton.textContent = "制定下一轮计划";
  const planBox = node("div", "profile-plan");
  planBox.hidden = true;
  planButton.addEventListener("click", () =>
    window.XunzhiPlanFlow.open({
      sourceId: snapshot.source_id,
      container: planBox,
      goal: "继续复习薄弱考点",
      questionCount: 5,
    })
  );
  actions.append(planButton);
  card.append(actions);
  card.append(planBox);

  const details = document.createElement("details");
  details.className = "profile-history";
  details.append(
    node(
      "summary",
      null,
      `历史与规则：${(snapshot.history || []).length || 1} 次有效测评`
    )
  );
  const history = snapshot.history || [];
  const latest = history.find((item) => item.id === snapshot.id);
  if (latest?.completed_at) {
    details.append(
      node("p", "hint", `本次快照：${String(latest.completed_at).replace("T", " ").slice(0, 16)}`)
    );
  }
  details.append(node("p", "hint", `测评规则版本：${snapshot.rule_version || "未知"}`));
  if (snapshot.evidence_fingerprint) {
    details.append(node("p", "hint", `证据指纹：${snapshot.evidence_fingerprint.slice(0, 18)}…`));
  }
  card.append(details);
  return card;
}

async function load() {
  const list = $("profile-list");
  const empty = $("profile-empty");
  list.innerHTML = "";
  const sourceId = new URLSearchParams(window.location.search).get("source_id");
  try {
    const response = await fetch(
      `/api/learner-profile${sourceId ? `?source_id=${encodeURIComponent(sourceId)}` : ""}`
    );
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || "档案读取失败");
    setStatus("");
    const items = data.items || [];
    empty.hidden = items.length > 0;
    items.forEach((item) => list.append(renderSnapshot(item)));
  } catch (error) {
    empty.hidden = false;
    setStatus(error.message, true);
  }
}

load();