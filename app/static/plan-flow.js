(function () {
  const $ = (id) => document.getElementById(id);

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
  }

  function basisLine(data) {
    if (data.assessment_applied && data.assessment?.completed_at) {
      return `依据：个人学习档案（${String(data.assessment.completed_at).replace("T", " ").slice(0, 16)} 的练习）`;
    }
    return "依据：现有掌握规则（暂无匹配当前作答的有效测评，仍可生成基础建议）";
  }

  function renderAdjustment(box, item, assessment) {
    const row = node("div", "plan-row");
    row.append(node("div", "plan-name", item.display_name || item.knowledge_point_id));
    const previous = item.previous_question_count ?? 0;
    const current = item.current_question_count ?? item.suggested_question_count ?? 0;
    row.append(node("div", "plan-count", `上次 ${previous} 道 → 本次 ${current} 道`));
    if (item.reason) row.append(node("p", "plan-reason", item.reason));
    const detail = item.assessment || {};
    if (detail.judgement) {
      row.append(node("p", "plan-reason", `档案判断：${detail.judgement}`));
    }
    if (detail.route_instruction) {
      const route = node("p", "plan-reason", `本轮安排：${detail.route_instruction}`);
      if (assessment?.pack_id) {
        const link = document.createElement("a");
        link.href = `/packs/${assessment.pack_id}`;
        link.textContent = "查看对应练习";
        route.append(" ");
        route.append(link);
      }
      row.append(route);
    }
    if (detail.route_completion) {
      row.append(node("p", "plan-reason", `完成标准：${detail.route_completion}`));
    }
    box.append(row);
  }

  async function confirmAndGenerate(plan, version, status) {
    status.textContent = "正在确认计划…";
    const confirmed = await fetch(`/api/plans/${plan.id}/versions/${version.id}/confirm`, {
      method: "POST",
    });
    const confirmBody = await confirmed.json();
    if (!confirmed.ok) {
      status.textContent = confirmBody.error?.message || "计划确认失败";
      status.classList.add("error");
      return;
    }
    const sources = (version.sources || []).map((item) => ({
      source_id: item.source_id,
      page_start: item.page_start,
      page_end: item.page_end,
    }));
    status.textContent = "正在生成新的学习计划…";
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
      status.textContent = job.error?.message || "生成失败";
      status.classList.add("error");
      return;
    }
    for (let index = 0; index < 60; index += 1) {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      const detail = await fetch(`/api/jobs/${job.job_id}`).then((item) => item.json());
      if (detail.status === "succeeded" && detail.pack_id) {
        location.href = `/packs/${detail.pack_id}`;
        return;
      }
      if (["failed", "cancelled", "insufficient_evidence"].includes(detail.status)) {
        status.textContent = detail.error?.message || detail.error_detail || "生成失败，可按同一计划重试";
        status.classList.add("error");
        return;
      }
    }
    status.textContent = "生成仍在进行，请稍后到学习记录查看";
  }

  async function open(options) {
    const { sourceId, container, goal, questionCount } = options;
    if (!container) return;
    container.hidden = false;
    container.innerHTML = "";
    container.scrollIntoView({ behavior: "smooth", block: "nearest" });
    const heading = node("h3", null, "下一轮学习计划（待确认）");
    container.append(heading);
    const status = node("p", "status", "正在读取个人档案并生成建议…");
    container.append(status);
    const response = await fetch(`/api/sources/${sourceId}/next-plan`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        goal: goal || "继续复习薄弱考点",
        question_count: questionCount || 5,
        points: [],
      }),
    });
    const data = await response.json();
    if (!response.ok) {
      status.textContent = data.error?.message || "建议计划生成失败";
      status.classList.add("error");
      return;
    }
    const version = data.version;
    container.innerHTML = "";
    container.append(node("h3", null, "下一轮学习计划（待确认）"));
    container.append(node("p", "meta", `共 ${version.question_count} 题 · ${data.reused ? "复用已有草稿" : "新建议"}`));
    container.append(node("p", "hint", basisLine(data)));
    const modules = version.modules || [];
    if (modules.length) {
      modules.forEach((module, index) => {
        const card = node("div", "plan-module");
        const head = node("div", "plan-module-head");
        head.append(node("span", "plan-ordinal", `模块 ${index + 1}`));
        head.append(node("strong", null, module.title || module.module_id));
        head.append(node("span", "badge accent", `${module.suggested_question_count || 0} 道`));
        card.append(head);
        if (module.objective) card.append(node("p", "hint", `学习目标：${module.objective}`));
        if (module.core_points?.length) {
          card.append(node("p", "hint", `核心考点：${module.core_points.join("、")}`));
        }
        if (module.rationale) card.append(node("p", "plan-reason", `安排理由：${module.rationale}`));
        container.append(card);
      });
    }
    const adjustments = version.adjustment || [];
    const rows = adjustments.length ? adjustments : version.items || [];
    rows.forEach((item) => renderAdjustment(container, item, data.assessment));
    (data.missing || []).forEach((item) => container.append(node("p", "hint", `待补齐：${item}`)));
    (data.excluded_points || []).forEach((item) => {
      container.append(node("p", "hint", `本轮不安排：${item.knowledge_point_id}（${item.reason}）`));
    });
    if (data.unplaced_points?.length) {
      container.append(node("p", "hint", "题量不足，本轮未安排部分考点；可增加题量或调整目标。"));
    }
    const confirm = document.createElement("button");
    confirm.type = "button";
    confirm.className = "primary";
    confirm.textContent = "确认并按此计划生成";
    confirm.addEventListener("click", () => {
      confirm.disabled = true;
      confirmAndGenerate(data.plan, version, status);
    });
    container.append(confirm);
    status.textContent = "";
    container.append(status);
  }

  window.XunzhiPlanFlow = { open };
})();