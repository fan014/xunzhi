(function () {
  const $ = (id) => document.getElementById(id);
  let packCache = null;
  let state = { moduleId: null, pages: [], index: 0, anchor: null };
  let noticeText = "";
  const pageByModule = {};

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
  }

  function cleanTitle(value, index = 0) {
    let text = String(value || "").trim();
    text = text
      .replace(/[、,，]\s*\d+\/\d+(?![0-9/])/g, "")
      .replace(/^\d+\/\d+\s*[、,，]?/g, "")
      .replace(/第\s*\d+\s*[-–—]\s*\d+\s*页/g, "")
      .trim();
    if (/^\d{1,3}(?:\.\d{1,3}){3}\/\d{1,2}$/.test(text)) text = `如何理解 ${text}`;
    return text.length >= 2 ? text : `模块 ${index + 1}`;
  }

  function currentPack() {
    if (window.__xunzhiPackData) packCache = window.__xunzhiPackData;
    return packCache;
  }

  function activeModuleId(pack) {
    const active = document.querySelector(".module-nav-item.active");
    if (active?.dataset?.moduleId) return active.dataset.moduleId;
    return (pack.modules || [])[0]?.module_id || null;
  }

  function sectionsOf(module) {
    return (module.handout || []).filter((section) => section.section_type !== "scope");
  }

  function pageFromDeclared(page) {
    const raw = page.visual;
    const visual =
      raw && (raw.type !== "text" || raw.notice) ? raw : null;
    return {
      id: page.page_id,
      title: page.title || "学习页",
      body: page.explanation || "",
      citations: page.citations || [],
      visual,
      page_type: page.page_type || (visual ? visual.type : "text"),
      knowledge_point_ids: page.knowledge_point_ids || [],
      declared: true,
      textOnly: !visual && (page.citations || []).length > 0,
    };
  }

  function citationKey(citation) {
    if (citation.evidence_id) return `evidence:${citation.evidence_id}`;
    return [
      citation.source_id || citation.source_name || "source",
      citation.page_no || "",
      JSON.stringify(citation.boxes || []),
      String(citation.quote || citation.claim || "").slice(0, 80),
    ].join("|");
  }

  function uniqueCitations(citations) {
    const seen = new Set();
    return (citations || []).filter((citation) => {
      const key = citationKey(citation);
      if (seen.has(key)) return false;
      seen.add(key);
      return true;
    });
  }

  function sectionOfPage(page) {
    return {
      id: page.id,
      title: page.title,
      body: page.body || "",
      citations: page.citations || [],
      knowledge_point_ids: page.knowledge_point_ids || [],
    };
  }

  function pageEvidenceKeys(page) {
    return new Set((page.citations || []).map(citationKey));
  }

  function canMergePages(first, second) {
    if (!first || !second || first.recap || second.recap) return false;
    if (first.visual || second.visual) return false;
    if ((first.page_type && first.page_type !== "text") || (second.page_type && second.page_type !== "text")) return false;
    if (!(first.citations || []).length || !(second.citations || []).length) return false;
    const left = pageEvidenceKeys(first);
    return [...pageEvidenceKeys(second)].some((key) => left.has(key));
  }

  function mergePagePair(first, second) {
    const sections = [
      ...(first.sections || [sectionOfPage(first)]),
      ...(second.sections || [sectionOfPage(second)]),
    ];
    return {
      ...first,
      sections,
      merged_ids: [...(first.merged_ids || [first.id]), ...(second.merged_ids || [second.id])],
      citations: uniqueCitations([...(first.citations || []), ...(second.citations || [])]),
      knowledge_point_ids: [
        ...new Set([...(first.knowledge_point_ids || []), ...(second.knowledge_point_ids || [])]),
      ],
      textOnly: true,
      page_type: "text",
      body: `${first.body || ""}

${second.body || ""}`.trim(),
    };
  }

  function mergeAdjacentPages(pages) {
    const result = [];
    pages.forEach((page) => {
      const last = result[result.length - 1];
      if (last && canMergePages(last, page)) {
        result[result.length - 1] = mergePagePair(last, page);
      } else {
        result.push(page);
      }
    });
    return result;
  }

  function presetForModule(module) {
    if (module?.visual_preset && ["switch_forward", "tcp_handshake", "lens_imaging"].includes(module.visual_preset)) {
      return module.visual_preset;
    }
    const text = [module?.title, ...(module?.core_points || []), ...(module?.knowledge_point_ids || [])]
      .filter(Boolean)
      .join(" ");
    if (/交换机/.test(text) && /(MAC|地址表|泛洪)/i.test(text) && /(帧|转发|端口)/.test(text)) {
      return "switch_forward";
    }
    if (/凸透镜/.test(text) && /(物距|像距|成像)/.test(text)) {
      return "lens_imaging";
    }
    if (/TCP/i.test(text) && /(三次握手|SYN|握手)/i.test(text)) {
      return "tcp_handshake";
    }
    return null;
  }

  function applyPresetToPages(module, pages) {
    const preset = presetForModule(module);
    if (!preset) return;
    for (const page of pages) {
      if (page.recap || page.visual) continue;
      const searchable = `${page.title || ""} ${page.body || ""}`;
      if (preset === "switch_forward" && !/(交换机|MAC|转发|泛洪|帧)/i.test(searchable)) continue;
      if (preset === "lens_imaging" && !/(凸透镜|成像|物距|像距)/.test(searchable)) continue;
      if (preset === "tcp_handshake" && !/(TCP|握手|SYN)/i.test(searchable)) continue;
      page.textOnly = false;
      page.visual = {
        type: "step_sequence",
        preset_id: preset,
        title:
          preset === "switch_forward"
            ? "交换机如何决定转发端口"
            : preset === "lens_imaging"
              ? "凸透镜成像：物距决定像的位置"
              : "TCP 三次握手",
      };
      break;
    }
  }

  function buildPages(module, index) {
    const declared = Array.isArray(module.pages)
      ? module.pages.filter((page) => page && page.page_id)
      : [];
    let pages;
    if (declared.length) {
      pages = declared.map(pageFromDeclared);
    } else {
    pages = [];
    const sections = sectionsOf(module);
    sections.forEach((section, sectionIndex) => {
      pages.push({
        id: section.id || `${module.module_id}-p${sectionIndex}`,
        title: section.heading || "知识点讲解",
        body: section.body || "",
        citations: section.citations || [],
        section_type: section.section_type,
        is_extension: Boolean(section.is_extension),
        derived: true,
      });
    });
    if (!pages.length) {
      pages.push({
        id: `intro-${module.module_id}`,
        title: cleanTitle(module.title, index),
        body: module.objective || "本模块暂无详细讲义，可继续查看相关资料或进入练习。",
        citations: [],
        derived: true,
      });
    }
    }
    pages = mergeAdjacentPages(pages);
    applyPresetToPages(module, pages);
    pages.push({ id: `recap-${module.module_id}`, title: "模块回顾", recap: true, citations: [] });
    return pages;
  }

  function sourceBlock(citations) {
    const list = uniqueCitations(citations);
    if (!list.length) return null;
    const details = document.createElement("details");
    details.className = "page-sources";
    details.append(node("summary", null, `页内相关资料（${list.length}）`));
    const box = node("div", "page-source-list");
    list.forEach((citation) => {
      const row = node("div", "page-source-row");
      row.append(node("span", "related-label", citation.label || (citation.page_no ? `第 ${citation.page_no} 页` : "原文")));
      row.append(node("span", "related-claim", String(citation.claim || citation.text_alternative || "").slice(0, 120)));
      const button = document.createElement("button");
      button.type = "button";
      button.className = "chip";
      button.textContent = "查看原文";
      button.addEventListener("click", () => window.XunzhiCitation.open(citation.id, button, citation));
      row.append(button);
      box.append(row);
    });
    details.append(box);
    return details;
  }

  function assetFigure(asset, caption) {
    const figure = document.createElement("figure");
    figure.className = "source-asset";
    figure.append(node("span", "asset-badge", "资料原图"));
    const img = document.createElement("img");
    img.src = asset.asset_url;
    img.alt = asset.alt_text || caption || asset.label || "资料原图";
    img.addEventListener("error", () => {
      figure.classList.add("missing");
      if (!figure.querySelector(".asset-missing")) {
        figure.append(node("p", "hint asset-missing", "该资料图暂不可查看，可阅读右栏文字并继续翻页。"));
      }
    });
    figure.append(img);
    figure.append(
      node("figcaption", "hint", caption || `${asset.source_name || "资料"} · ${asset.label || ""}`)
    );
    return figure;
  }

  function citationForVisual(visual, citations) {
    if (!visual) return null;
    if (visual.citation_id) {
      return (citations || []).find((item) => item.id === visual.citation_id) || null;
    }
    if (visual.source_ref) {
      return (citations || []).find((item) => item.local_ref === visual.source_ref) || null;
    }
    return null;
  }

  function renderLeft(page, module) {
    const box = $("reader-visual");
    box.innerHTML = "";
    if (page.recap) {
      const card = node("div", "recap-card");
      card.append(node("h3", null, "关键判断"));
      const table = document.createElement("table");
      table.className = "recap-table";
      const head = document.createElement("tr");
      head.append(node("th", null, "条件"), node("th", null, "判断或结果"));
      table.append(head);
      recapRules(module)
        .slice(0, 6)
        .forEach((rule) => {
          const row = document.createElement("tr");
          row.append(node("td", null, rule.condition));
          row.append(node("td", null, rule.result));
          table.append(row);
        });
      card.append(table);
      box.append(card);
      return;
    }
    if (page.visual) {
      const wrap = node("div", "module-visual page-visual");
      const handled = window.XunzhiVisual?.renderPage?.(wrap, page.visual, {
        citation: citationForVisual(page.visual, page.citations),
      });
      if (handled && wrap.childNodes.length) {
        box.append(wrap);
        return;
      }
      const fallback = node("div", "reader-source-card");
      fallback.append(
        node(
          "p",
          "hint",
          page.visual.notice || "当前演示暂不可用，可阅读右栏并继续翻页。"
        )
      );
      box.append(fallback);
      return;
    }
    const citations = uniqueCitations(page.citations);
    if (page.declared) {
      const card = node("div", "reader-source-card");
      card.append(node("p", "eyebrow", "本页依据"));
      if (citations.length) {
        const first = citations[0];
        card.append(
          node(
            "p",
            "source-quote",
            String(first.quote || first.text_alternative || first.claim || "").slice(0, 260)
          )
        );
        const button = document.createElement("button");
        button.type = "button";
        button.className = "secondary";
        button.textContent = "查看相关资料";
        button.addEventListener("click", () =>
          window.XunzhiCitation.open(first.id, button, first)
        );
        card.append(button);
      } else {
        card.append(node("p", "hint", "本页是概念讲解，详细依据见右栏。"));
      }
      box.append(card);
      return;
    }
    const assets = citations.filter((citation) => citation.asset_url);
    if (assets.length) {
      const stack = node("div", "source-assets");
      assets.forEach((asset, index) => {
        stack.append(assetFigure(asset, `${asset.source_name || "资料"} · ${asset.label || ""}`));
        if (index < assets.length - 1) stack.append(node("div", "asset-gap"));
      });
      box.append(stack);
      return;
    }
    const card = node("div", "reader-source-card");
    card.append(node("p", "eyebrow", "资料依据"));
    if (citations.length) {
      const first = citations[0];
      card.append(node("p", "source-quote", String(first.quote || first.text_alternative || first.claim || "").slice(0, 260)));
      const button = document.createElement("button");
      button.type = "button";
      button.className = "secondary";
      button.textContent = "查看相关资料";
      button.addEventListener("click", () => window.XunzhiCitation.open(first.id, button, first));
      card.append(button);
    } else {
      card.append(node("p", "hint", "本页是概念解释，暂无单独图片；详细依据见右栏。"));
    }
    box.append(card);
  }

  function recapRules(module) {
    const recap = module.recap && Array.isArray(module.recap.rules) ? module.recap.rules : null;
    if (recap && recap.length) {
      return recap
        .filter((rule) => rule && (rule.condition || rule.result || rule.pitfall))
        .map((rule) => ({
          condition: String(rule.condition || "条件").trim(),
          result: String(rule.result || "结论").trim(),
          pitfall: String(rule.pitfall || "").trim(),
        }));
    }
    const ruleSections = (module.handout || []).filter((section) =>
      /机制|易混淆|判断|规则|结论/.test(section.heading || "")
    );
    if (ruleSections.length) {
      return ruleSections.map((section) => ({
        condition: cleanTitle(section.heading, 0),
        result: String(section.body || "").split(/[。\\n]/)[0].slice(0, 90),
        pitfall: "",
      }));
    }
    return (module.core_points || []).map((point) => ({
      condition: cleanTitle(point, 0),
      result: module.objective || "本模块核心概念",
      pitfall: "",
    }));
  }

  function renderRecap(module, modules) {
    const text = $("reader-text");
    text.innerHTML = "";
    text.append(node("h2", null, "模块回顾"));
    const recap = module.recap || {};
    text.append(
      node("p", "hint", recap.summary || "用「条件 → 判断或结果 → 常见误区」快速回顾，再进入本模块练习。")
    );
    const rules = recapRules(module);
    if (rules.length) {
      const list = document.createElement("ol");
      list.className = "recap-rules";
      rules.forEach((rule) => {
        const item = node("li", null, `${rule.condition} → ${rule.result}`);
        if (rule.pitfall) item.append(node("p", "hint", `常见误区：${rule.pitfall}`));
        list.append(item);
      });
      text.append(list);
    }
    const actions = node("div", "actions");
    const practice = document.createElement("button");
    practice.type = "button";
    practice.className = "primary";
    practice.textContent = "练本模块";
    practice.addEventListener("click", () => practiceModule(module));
    actions.append(practice);
    const last = modules.indexOf(module) === modules.length - 1;
    if (last && (currentPack()?.review?.questions || []).length) {
      const review = document.createElement("button");
      review.type = "button";
      review.className = "secondary";
      review.textContent = "整包综合练习";
      review.addEventListener("click", () => {
        document.getElementById("tab-questions")?.click();
        const first = currentPack().review.questions[0];
        const target = document.getElementById(`q-${first.id}`);
        if (target) target.scrollIntoView({ block: "start" });
      });
      actions.append(review);
    }
    text.append(actions);
    if (last) {
      const summary = node("section", "recap-cross");
      summary.append(node("h3", null, "整包综合回顾"));
      if (recap.cross_module) {
        summary.append(node("p", "hint", recap.cross_module));
      }
      const list = document.createElement("ul");
      modules.forEach((item, index) => {
        const note = (item.recap && item.recap.summary) || "";
        list.append(
          node(
            "li",
            null,
            `${cleanTitle(item.title, index)}：${note || (item.core_points || []).join("、") || "本模块核心概念"}`
          )
        );
      });
      summary.append(list);
      text.append(summary);
    }
    const sources = sourceBlock((module.handout || []).flatMap((section) => section.citations || []));
    if (sources) text.append(sources);
  }

  function practiceModule(module) {
    document.getElementById("tab-questions")?.click();
    const first = ((currentPack()?.questions || []).find((question) => question.module_id === module.module_id)) || null;
    const target = first ? document.getElementById(`q-${first.id}`) : null;
    if (target) target.scrollIntoView({ block: "start" });
  }

  function savePage(page) {
    if (!page) return;
    fetch(`/api/packs/${document.body.dataset.packId}/position`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        module_id: state.moduleId,
        page_id: page.id,
        view: "modules",
      }),
    }).catch(() => {});
  }

  function renderPage() {
    const pack = currentPack();
    if (!pack) return;
    const page = state.pages[state.index];
    if (!page) return;
    const modules = pack.modules || [];
    const module = modules.find((item) => item.module_id === state.moduleId) || modules[0];
    if (!module) return;
    const body = document.querySelector(".reader-body");
    if (body) body.classList.toggle("text-only", Boolean(page.textOnly));
    $("reader-eyebrow").textContent = `模块 ${modules.indexOf(module) + 1} · 学习页`;
    $("reader-title").textContent = page.title;
    $("reader-count").textContent = `${state.index + 1} / ${state.pages.length}`;
    $("reader-page").textContent = `第 ${state.index + 1} / ${state.pages.length} 页`;
    renderLeft(page, module);
    const text = $("reader-text");
    if (page.recap) {
      renderRecap(module, modules);
    } else {
      text.innerHTML = "";
      const eyebrow = node("p", "eyebrow", page.is_extension ? "延伸理解" : "知识点讲解");
      eyebrow.id = `section-${page.sections?.[0]?.id || page.id}`;
      text.append(eyebrow);
      if (page.visual?.notice) {
        text.append(node("p", "hint visual-notice", page.visual.notice));
      }
      if (page.sections?.length) {
        page.sections.forEach((section, sectionIndex) => {
          const sameTitle = section.title === page.title;
          if (!sameTitle) {
            const subhead = node("h3", null, section.title || "知识点");
            subhead.id = `section-${section.id}`;
            text.append(subhead);
          }
          const paragraphs = String(section.body || "").split(/\n{2,}/).filter(Boolean);
          (paragraphs.length ? paragraphs : [section.body || ""]).forEach((paragraph) => {
            text.append(node("p", "body", paragraph));
          });
          if (sectionIndex === 0 && !sameTitle) {
            const eyebrow = text.querySelector(".eyebrow");
            if (eyebrow) eyebrow.textContent = "合并讲解";
          }
        });
      } else {
        const paragraphs = String(page.body || "").split(/\n{2,}/).filter(Boolean);
        (paragraphs.length ? paragraphs : [page.body || ""]).forEach((paragraph) => {
          text.append(node("p", "body", paragraph));
        });
      }
      if (page.knowledge_point_ids?.length) {
        text.append(node("p", "hint", `关联考点：${page.knowledge_point_ids.join("、")}`));
      }
      const sources = sourceBlock(page.citations);
      if (sources) text.append(sources);
    }
    if (noticeText) {
      text.prepend(node("p", "hint reader-notice", noticeText));
    }
    if (state.anchor) {
      const target = document.getElementById(`section-${state.anchor}`);
      if (target) target.scrollIntoView({ block: "start" });
      state.anchor = null;
    }
    $("reader-prev").disabled = state.index === 0;
    $("reader-next").disabled = state.index >= state.pages.length - 1;
  }

  function setNotice(text) {
    noticeText = text || "";
  }

  function render(moduleId) {
    const pack = currentPack();
    if (!pack || !(pack.modules || []).length) return;
    const modules = pack.modules;
    const module = modules.find((item) => item.module_id === moduleId) || modules[0];
    if (!module) return;
    if (moduleId && !modules.some((item) => item.module_id === moduleId)) {
      setNotice("任务对应的模块暂时打不开，已打开第一个可读模块。");
    } else if (!pack.explicit_page_id) {
      setNotice("");
    }
    const explicit = pack.explicit_page_id;
    const explicitModule = pack.explicit_module_id;
    const useExplicit =
      Boolean(explicit) && (!explicitModule || explicitModule === module.module_id);
    if (state.moduleId !== module.module_id || useExplicit) {
      const pages = buildPages(module, modules.indexOf(module));
      const saved = useExplicit
        ? explicit
        : pageByModule[module.module_id]
          || (pack.position?.module_id === module.module_id ? pack.position.page_id : null);
      const index = pages.findIndex(
        (page) => page.id === saved || (page.merged_ids || []).includes(saved)
      );
      state = {
        moduleId: module.module_id,
        pages,
        index: index >= 0 ? index : 0,
        anchor:
          index >= 0 && (pages[index].merged_ids || []).includes(saved) ? saved : null,
      };
      if (useExplicit) {
        delete pack.explicit_page_id;
        delete pack.explicit_module_id;
        setNotice(
          index >= 0
            ? ""
            : "这个模块的回顾页暂时打不开，已回到同模块的讲解页。"
        );
      }
    }
    $("module-list").hidden = true;
    $("module-reader").hidden = false;
    renderPage();
    const currentPage = state.pages[state.index];
    if (currentPage) savePage(currentPage);
  }

  function animateTurn(delta) {
    const reader = $("module-reader");
    if (!reader) return;
    reader.classList.remove("turn-right", "turn-left");
    void reader.offsetWidth;
    reader.classList.add(delta > 0 ? "turn-right" : "turn-left");
    window.setTimeout(() => {
      reader.classList.remove("turn-right", "turn-left");
    }, 220);
  }

  function go(delta) {
    const next = Math.max(0, Math.min(state.pages.length - 1, state.index + delta));
    if (next === state.index) return;
    state.index = next;
    state.anchor = null;
    pageByModule[state.moduleId] = state.pages[next].id;
    animateTurn(delta);
    renderPage();
    savePage(state.pages[next]);
  }

  function boot() {
    const pack = currentPack();
    if (!pack) return setTimeout(boot, 200);
    if (!(pack.modules || []).length) return;
    const initial = activeModuleId(pack);
    render(initial);
    const nav = $("module-nav");
    if (nav) {
      nav.addEventListener("click", (event) => {
        const button = event.target.closest(".module-nav-item");
        if (!button?.dataset.moduleId) return;
        setTimeout(() => render(button.dataset.moduleId), 0);
      });
    }
    $("reader-prev").addEventListener("click", () => go(-1));
    $("reader-next").addEventListener("click", () => go(1));
    document.getElementById("tab-modules")?.addEventListener("click", () => {
      const now = activeModuleId(pack);
      setTimeout(() => render(now), 0);
    });
    document.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight"].includes(event.key)) return;
      if ($("view-modules").hidden) return;
      const target = event.target;
      if (target?.closest?.("input, select, textarea, button, a, [contenteditable='true']")) return;
      event.preventDefault();
      go(event.key === "ArrowRight" ? 1 : -1);
    });
  }

  function currentPageId() {
    const page = state.pages[state.index];
    return page ? page.id : null;
  }

  window.XunzhiReader = { render, go, currentPageId };
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();