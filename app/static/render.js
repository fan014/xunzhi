(function () {
  const esc = (text) =>
    String(text ?? "").replace(/[&<>"]/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[ch]));

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
  }

  function citationChip(citation) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chip";
    button.textContent = citation.label || "资料引用";
    button.title = citation.text_alternative || "";
    button.addEventListener("click", () => window.XunzhiCitation.open(citation.id, button, citation));
    return button;
  }

  function citationChips(citations) {
    const box = node("div", "chips");
    (citations || []).forEach((item) => box.append(citationChip(item)));
    return box;
  }

  function handout(container, sections, options = {}) {
    container.innerHTML = "";
    const sectionName = {
      scope: "学习范围",
      key_point: "知识要点",
      pitfall: "易错处",
      visual: "图文解释",
    };
    sections.forEach((section) => {
      if (section.section_type === "scope" && options.hideScope !== false) {
        const scope = node("div", "scope");
        const content = section.content || {};
        if (content.covered?.length) {
          const p = document.createElement("p");
          p.innerHTML = "<strong>本次覆盖：</strong>";
          p.append(content.covered.join("；"));
          scope.append(p);
        }
        if (content.not_covered?.length) {
          const p = document.createElement("p");
          p.innerHTML = "<strong>本次不覆盖：</strong>";
          p.append(content.not_covered.join("；"));
          scope.append(p);
        }
        container.append(scope);
        return;
      }
      const wrap = node("article", "handout-section");
      const heading = node("h3", null, section.heading || sectionName[section.section_type] || "讲解");
      const body = node("div", "body", section.body);
      wrap.append(heading, body);
      if (section.is_extension) wrap.append(node("p", "hint", "延伸理解（无资料直接依据）"));
      if (section.citations?.length && options.hideCitations !== true) wrap.append(citationChips(section.citations));
      container.append(wrap);
    });
  }

  function questions(form, items, answers, disabled, onChange, options = {}) {
    form.innerHTML = "";
    const chosen = {};
    (answers || []).forEach((item) => {
      chosen[item.question_id] = item.selected_index;
    });
    let currentModule = null;
    items.forEach((question) => {
      if (question.module_id && question.module_id !== currentModule && options.moduleTitles) {
        currentModule = question.module_id;
        const title = options.moduleTitles[question.module_id] || (question.module_id === "review" ? "综合复习" : "");
        if (title) {
          const heading = node(
            "div",
            "practice-module",
            `${question.module_id === "review" ? "综合复习" : "本模块练习"} · ${title}`
          );
          form.append(heading);
        }
      }
      const card = node("div", "question");
      card.id = `q-${question.id}`;
      if (question.module_id) card.dataset.module = question.module_id;
      const stem = node("p", "stem", `${question.ordinal}. ${question.stem}`);
      if (question.is_image_based) stem.append(node("span", "muted", " （需看图作答）"));
      const optionsBox = node("div", "options");
      question.options.forEach((option, index) => {
        const label = node("label", "option");
        const radio = document.createElement("input");
        radio.type = "radio";
        radio.name = `q-${question.id}`;
        radio.value = String(index);
        radio.checked = chosen[question.id] === index;
        radio.disabled = disabled;
        radio.addEventListener("change", () => onChange && onChange());
        label.append(radio, node("span", null, `${option.key}. ${option.text}`));
        optionsBox.append(label);
      });
      card.append(stem, optionsBox);
      form.append(card);
    });
  }

  function result(container, payload, options = {}) {
    container.innerHTML = "";
    const score = payload.score || {};
    const scoreBox = node("div", "score");
    [
      ["正确", score.correct],
      ["错误", score.wrong],
      ["未作答", score.unanswered],
      ["共", score.total],
    ].forEach(([label, value]) => {
      const span = document.createElement("span");
      span.append(label + " ", node("b", null, String(value ?? 0)));
      scoreBox.append(span);
    });
    container.append(scoreBox);

    (payload.questions || []).forEach((item) => {
      const row = node("div", "result-row");
      if (item.is_correct === false) row.classList.add("wrong-answer");
      row.append(node("p", "stem", `${item.ordinal}. ${item.stem}`));
      if (item.module_id && options.moduleTitles) {
        const title = options.moduleTitles[item.module_id] || (item.module_id === "review" ? "综合复习" : "");
        if (title) row.append(node("p", "meta", `模块：${title}`));
      }
      let verdict;
      if (item.is_correct === true) verdict = node("p", "verdict ok", "✓ 回答正确");
      else if (item.is_correct === false) verdict = node("p", "verdict bad", "✗ 回答错误");
      else verdict = node("p", "verdict", "— 未作答");
      row.append(verdict);
      const selected = item.options?.[item.selected_index];
      row.append(
        node(
          "p",
          "answer-note",
          selected ? `你的答案：${selected.key}. ${selected.text}` : "你的答案：未作答"
        )
      );
      const correct = item.options?.[item.answer_index];
      if (correct) {
        row.append(node("p", "answer-note", `正确答案：${correct.key}. ${correct.text}`));
      }
      row.append(node("div", "explanation", item.explanation));
      if (item.citations?.length) row.append(citationChips(item.citations));
      container.append(row);
    });
    if (options.completedText) container.append(node("p", "completed", options.completedText));
  }

  function masteryChanges(container, payload) {
    container.innerHTML = "";
    container.append(node("h2", null, "本次掌握变化"));
    (payload.changes || []).forEach((item) => {
      const row = node("div", "mastery-row");
      row.append(node("p", "stem", item.display_name));
      row.append(node("p", "meta", `${item.before.state_label} → ${item.after.state_label}`));
      row.append(node("p", "hint", item.after.decision_reason));
      (item.after.review_targets || []).forEach((ref) => {
        if (!ref.question_id) return;
        const link = document.createElement("a");
        link.href = `#q-${ref.question_id}`;
        link.textContent = "查看对应错题";
        row.append(link);
      });
      container.append(row);
    });
    if (payload.unanswered_points?.length) {
      container.append(
        node(
          "p",
          "hint",
          "本次未作答（不计入答错）：" + payload.unanswered_points.map((item) => item.display_name).join("、")
        )
      );
    }
  }

  function planAdoption(container, plan, onRetry) {
    container.innerHTML = "";
    if (!plan?.linked) {
      container.append(node("h2", null, "实际采用的计划"));
      container.append(node("p", "hint", "本计划未关联学习计划版本；旧计划保留原记录，不补造关联。"));
      return;
    }
    container.append(node("h2", null, "实际采用的计划"));
    container.append(
      node("p", "meta", `计划版本 ${plan.version_id} · 状态 ${plan.version_status || "未知"}`)
    );
    const schedule = plan.schedule || {};
    if (schedule.plan_id) {
      const progress = schedule.progress || {};
      container.append(
        node(
          "p",
          "hint",
          schedule.assigned
            ? `计划进度：${progress.label || "已完成 0 / 0 项"}${
                schedule.schedule?.planned_end_date
                  ? ` · 预计 ${schedule.schedule.planned_end_date} 完成`
                  : ""
              }`
            : "学习计划未安排日程。"
        )
      );
      const link = document.createElement("a");
      link.href = `/calendar?plan_id=${encodeURIComponent(schedule.plan_id)}`;
      link.textContent = schedule.assigned ? "在学习日历中查看" : "安排学习时间";
      container.append(link);
    }
    const delivery = plan.delivery;
    if (!delivery) {
      container.append(node("p", "hint", "未记录交付差异。"));
      return;
    }
    const names = new Map(
      (plan.items || []).map((item) => [item.knowledge_point_id, item.display_name])
    );
    (plan.assessment?.findings || []).forEach((item) => {
      if (item.knowledge_point) names.set(item.knowledge_point_id, item.knowledge_point);
    });
    (plan.assessment?.route_steps || []).forEach((item) => {
      if (item.knowledge_point) names.set(item.knowledge_point_id, item.knowledge_point);
    });
    const summary = node("div", "delivery-summary");
    const planned = node("div", "delivery-cell");
    planned.append(node("span", "meta", "计划题量"), node("b", null, String(delivery.expected?.total ?? "?")));
    const actual = node("div", "delivery-cell");
    actual.append(node("span", "meta", "实际生成"), node("b", null, String(delivery.actual?.total ?? "?")));
    summary.append(planned, actual);
    container.append(summary);
    (delivery.shortfall || []).forEach((item) => {
      const name = names.get(item.knowledge_point_id) || item.knowledge_point_id;
      container.append(
        node(
          "p",
          "hint",
          `${name}：计划 ${item.expected} 道，实际 ${item.actual} 道，差 ${item.missing} 道`
        )
      );
    });
    (delivery.extra || []).forEach((item) => {
      const name = names.get(item.knowledge_point_id) || item.knowledge_point_id;
      container.append(
        node(
          "p",
          "hint",
          `${name}：实际比计划多 ${item.extra} 道`
        )
      );
    });
    (delivery.module_shortfall || []).forEach((item) => {
      container.append(
        node(
          "p",
          "hint",
          `模块「${item.title}」：计划 ${item.expected} 道，实际 ${item.actual} 道，差 ${item.missing} 道`
        )
      );
    });
    if (delivery.review_questions) {
      container.append(node("p", "hint", `其中综合复习题 ${delivery.review_questions} 道。`));
    }
    if (delivery.retry_available && onRetry) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary";
      button.textContent = "按同一计划重试";
      button.addEventListener("click", () => onRetry(plan.version_id));
      container.append(button);
    }
  }

  window.XunzhiRender = {
    handout,
    questions,
    result,
    masteryChanges,
    planAdoption,
    citations: citationChips,
    esc,
  };
})();