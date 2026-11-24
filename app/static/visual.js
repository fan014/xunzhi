(function () {
  const SVG_NS = "http://www.w3.org/2000/svg";

  function node(tag, className, text) {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
  }

  function svgNode(tag, attrs = {}) {
    const item = document.createElementNS(SVG_NS, tag);
    Object.entries(attrs).forEach(([key, value]) => item.setAttribute(key, String(value)));
    return item;
  }

  const SWITCH_SPEC = {
    kind: "switch_forward",
    title: "交换机如何决定转发端口",
    nodes: [
      { id: "pcA", label: "PC-A", x: 70, y: 70, kind: "host" },
      { id: "sw", label: "SW1", x: 300, y: 150, kind: "switch" },
      { id: "pcB", label: "PC-B", x: 540, y: 70, kind: "host" },
      { id: "pcC", label: "PC-C", x: 540, y: 230, kind: "host" },
    ],
    edges: [
      { id: "a", from: "pcA", to: "sw", label: "Gi0/1" },
      { id: "b", from: "sw", to: "pcB", label: "Gi0/2" },
      { id: "c", from: "sw", to: "pcC", label: "Gi0/3" },
    ],
    options: [
      { id: "gi1", text: "Gi0/1（连接 PC-A）" },
      { id: "gi2", text: "Gi0/2（连接 PC-B）" },
      { id: "gi3", text: "Gi0/3（连接 PC-C）" },
      { id: "flood", text: "除入端口外的所有端口" },
    ],
    modes: {
      known: {
        label: "已知目的 MAC",
        correctOptionId: "gi2",
        steps: [
          {
            text: "PC-A 发出帧，帧从 Gi0/1 端口进入 SW1。",
            nodes: ["pcA", "sw"],
            edges: ["a"],
          },
          {
            text: "SW1 读取帧的目的 MAC 地址，在 MAC 地址表中查找对应端口。",
            nodes: ["sw"],
            edges: ["a"],
          },
          {
            text: "表中记录目的 MAC 连接在 Gi0/2，SW1 只从 Gi0/2 转发给 PC-B。",
            nodes: ["sw", "pcB"],
            edges: ["a", "b"],
          },
        ],
      },
      unknown: {
        label: "未知目的 MAC",
        correctOptionId: "flood",
        steps: [
          {
            text: "PC-A 发出帧，帧仍然从 Gi0/1 端口进入 SW1。",
            nodes: ["pcA", "sw"],
            edges: ["a"],
          },
          {
            text: "目的 MAC 不在 MAC 地址表中，交换机不能确定唯一出口，进入泛洪。",
            nodes: ["sw"],
            edges: ["a"],
          },
          {
            text: "泛洪从除 Gi0/1 外的所有端口发出：Gi0/2 到 PC-B，Gi0/3 到 PC-C；无关主机会丢弃。",
            nodes: ["sw", "pcB", "pcC"],
            edges: ["a", "b", "c"],
          },
        ],
      },
    },
  };

  const TCP_SPEC = {
    kind: "tcp_handshake",
    title: "TCP 三次握手",
    nodes: [
      { id: "client", label: "客户端", x: 90, y: 140, kind: "host" },
      { id: "server", label: "服务器", x: 520, y: 140, kind: "host" },
    ],
    edges: [{ id: "link", from: "client", to: "server", label: "" }],
    modes: {
      handshake: {
        label: "建立连接",
        correctOptionId: "handshake",
        steps: [
          {
            text: "客户端发送 SYN，带上自己的初始序号，表示请求建立连接。",
            nodes: ["client", "server"],
            edges: ["link"],
          },
          {
            text: "服务器回复 SYN-ACK：确认客户端序号，同时带上自己的初始序号。",
            nodes: ["client", "server"],
            edges: ["link"],
          },
          {
            text: "客户端发送 ACK 确认服务器序号，双方的发送与接收方向都进入已建立状态。",
            nodes: ["client", "server"],
            edges: ["link"],
          },
        ],
      },
    },
  };

  const LENS_SPEC = {
    kind: "lens_imaging",
    title: "凸透镜成像：物距决定像的位置、正倒和大小",
    question: "先判断：当前物距下，像的位置和大小是什么？",
    placeholder: "请选择成像结果",
    nodes: [
      { id: "object", label: "物体", x: 90, y: 150, kind: "host" },
      { id: "lens", label: "凸透镜", x: 310, y: 150, kind: "switch" },
      { id: "image", label: "像", x: 530, y: 150, kind: "host" },
    ],
    edges: [
      { id: "obj_lens", from: "object", to: "lens", label: "物距 u" },
      { id: "lens_image", from: "lens", to: "image", label: "像距 v" },
    ],
    options: [
      { id: "between_f_2f_small", text: "像在一倍与两倍焦距之间，倒立缩小实像" },
      { id: "beyond_2f_big", text: "像在两倍焦距之外，倒立放大实像" },
      { id: "same_side_virtual", text: "像与物体同侧，正立放大虚像" },
    ],
    modes: {
      beyond_2f: {
        label: "物距大于 2f",
        correctOptionId: "between_f_2f_small",
        steps: [
          {
            text: "物体在二倍焦距之外，平行于主光轴的光线经凸透镜后过焦点。",
            nodes: ["object", "lens"],
            edges: ["obj_lens"],
          },
          {
            text: "另一条过光心的光线方向不变，两条折射光线在透镜另一侧相交。",
            nodes: ["object", "lens", "image"],
            edges: ["obj_lens", "lens_image"],
          },
          {
            text: "交点落在 f 与 2f 之间，形成倒立、缩小的实像。",
            nodes: ["lens", "image"],
            edges: ["lens_image"],
          },
        ],
      },
      between_f_2f: {
        label: "物距在 f 与 2f 之间",
        correctOptionId: "beyond_2f_big",
        steps: [
          {
            text: "物体在一倍焦距与两倍焦距之间，折射光线仍会在透镜另一侧相交。",
            nodes: ["object", "lens"],
            edges: ["obj_lens"],
          },
          {
            text: "交点比物体更远离透镜，落在二倍焦距之外。",
            nodes: ["object", "lens", "image"],
            edges: ["obj_lens", "lens_image"],
          },
          {
            text: "此时形成倒立、放大的实像，像距大于二倍焦距。",
            nodes: ["lens", "image"],
            edges: ["lens_image"],
          },
        ],
      },
    },
  };

  const PRESETS = {
    switch_forward: SWITCH_SPEC,
    tcp_handshake: TCP_SPEC,
    lens_imaging: LENS_SPEC,
  };

  function layoutNodes(nodes) {
    const total = nodes.length;
    const cols = Math.min(4, Math.max(1, total));
    const rows = Math.ceil(total / cols);
    return nodes.map((item, index) => {
      const row = Math.floor(index / cols);
      const rowItems = Math.min(cols, total - row * cols);
      const col = index % cols;
      const x = rowItems <= 1 ? 300 : 90 + (col * 440) / (rowItems - 1);
      const y = rows <= 1 ? 150 : 95 + row * 120;
      return { ...item, x, y };
    });
  }

  function normalizeSpec(visual) {
    const nodes = layoutNodes(
      (visual.nodes || []).map((item) => ({
        id: item.id,
        label: item.label || item.id,
        kind: item.kind || "process",
        state: item.state || "",
      }))
    );
    const edges = (visual.edges || []).map((item) => ({
      id: item.id,
      from: item.source,
      to: item.target,
      label: item.label || "",
    }));
    const options = (visual.options || []).map((item) => ({ id: item.id, text: item.text || item.id }));
    const modeEntries = (visual.modes || []).filter((mode) => mode && mode.id);
    const modes = {};
    if (modeEntries.length) {
      modeEntries.forEach((mode) => {
        modes[mode.id] = {
          label: mode.label || mode.id,
          correctOptionId: mode.correct_option_id || visual.correct_option_id || "",
          steps: (mode.steps || []).map((step) => ({
            text: step.text || "",
            nodes: step.active_node_ids || [],
            edges: step.active_edge_ids || [],
            states: step.node_states || {},
          })),
        };
      });
    } else {
      modes.default = {
        label: visual.title || "演示",
        correctOptionId: visual.correct_option_id || "",
        steps: (visual.steps || []).map((step) => ({
          text: step.text || "",
          nodes: step.active_node_ids || [],
          edges: step.active_edge_ids || [],
          states: step.node_states || {},
        })),
      };
    }
    return {
      kind: visual.preset_id || "page_visual",
      title: visual.title || "分步演示",
      caption: visual.caption || "",
      altText: visual.alt_text || "",
      nodes,
      edges,
      options,
      modes,
    };
  }

  function renderStage(spec, state) {
    const svg = svgNode("svg", {
      viewBox: "0 0 620 300",
      role: "img",
      "aria-label": spec.title,
    });
    const edgeLayer = svgNode("g", { class: "visual-edges" });
    const nodeLayer = svgNode("g", { class: "visual-nodes" });
    const points = {};
    spec.nodes.forEach((item) => {
      points[item.id] = item;
    });
    spec.edges.forEach((edge) => {
      const from = points[edge.from];
      const to = points[edge.to];
      if (!from || !to) return;
      const line = svgNode("line", {
        x1: from.x,
        y1: from.y,
        x2: to.x,
        y2: to.y,
        class: "visual-edge",
        "data-edge": edge.id,
      });
      edgeLayer.append(line);
      if (edge.label) {
        const label = svgNode("text", {
          x: (from.x + to.x) / 2,
          y: (from.y + to.y) / 2 - 8,
          class: "visual-edge-label",
        });
        label.textContent = edge.label;
        edgeLayer.append(label);
      }
    });
    spec.nodes.forEach((item) => {
      const group = svgNode("g", {
        class: `visual-node ${item.kind || "host"}`,
        "data-node": item.id,
        transform: `translate(${item.x},${item.y})`,
      });
      if (item.kind === "switch") {
        group.append(
          svgNode("rect", { x: -55, y: -32, width: 110, height: 64, rx: 10, class: "visual-box" })
        );
      } else {
        group.append(
          svgNode("rect", { x: -46, y: -28, width: 92, height: 56, rx: 8, class: "visual-box" })
        );
      }
      const text = svgNode("text", { x: 0, y: 4, class: "visual-label", "text-anchor": "middle" });
      text.textContent = item.label;
      group.append(text);
      if (item.state) {
        const stateText = svgNode("text", {
          x: 0,
          y: 48,
          class: "visual-state",
          "text-anchor": "middle",
        });
        stateText.textContent = item.state;
        group.append(stateText);
      }
      nodeLayer.append(group);
    });
    svg.append(edgeLayer, nodeLayer);
    return svg;
  }

  function renderComparison(container, visual) {
    const wrap = node("section", "teaching-visual comparison-visual");
    const titleRow = node("div", "visual-title");
    titleRow.append(node("h3", null, visual.title || "条件对比"));
    titleRow.append(node("span", "visual-badge", "教学示意"));
    wrap.append(titleRow);
    if (visual.caption) wrap.append(node("p", "hint", visual.caption));
    const grid = node("div", "comparison-grid");
    (visual.cards || []).forEach((card) => {
      const article = node("article", "comparison-card");
      article.append(node("h4", null, card.title || "条件"));
      const rows = [
        ["条件", card.condition],
        ["例子", card.example],
        ["结果", card.result],
      ];
      rows.forEach(([label, value]) => {
        if (!String(value || "").trim()) return;
        const row = node("div", "comparison-row");
        row.append(node("span", "comparison-label", label));
        row.append(node("span", "comparison-value", value));
        article.append(row);
      });
      grid.append(article);
    });
    wrap.append(grid);
    if (visual.notice) wrap.append(node("p", "hint", visual.notice));
    container.append(wrap);
    return true;
  }

  function renderSourceAsset(container, visual, options = {}) {
    const citation = options.citation;
    const url = visual.asset_url || citation?.asset_url || "";
    if (!url) {
      container.append(node("p", "hint", visual.notice || "该资料图暂不可查看，可阅读右栏文字并继续翻页。"));
      return true;
    }
    const figure = document.createElement("figure");
    figure.className = "source-asset page-source-asset";
    figure.append(node("span", "asset-badge", "资料原图"));
    const frame = node("div", "source-asset-frame");
    const img = document.createElement("img");
    img.src = url;
    img.alt = visual.alt_text || visual.caption || "资料原图";
    img.loading = "lazy";
    const sourceBoxes = Array.isArray(visual.boxes) ? visual.boxes : citation?.boxes;
    const boxes = Array.isArray(sourceBoxes) ? sourceBoxes.filter((box) => Array.isArray(box) && box.length === 4) : [];
    if (!boxes.length) {
      const fallback = node("div", "reader-source-card");
      fallback.append(node("p", "hint", "这张资料图没有可用区域框，已从主阅读区收起；右侧讲解和引用仍可核对。"));
      if (citation) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "chip";
        button.textContent = "查看原图";
        button.addEventListener("click", () => window.XunzhiCitation.open(citation.id, button, citation));
        fallback.append(button);
      }
      container.append(fallback);
      return true;
    }
    let crop = null;
    if (boxes.length) {
      const pad = 0.04;
      const x0 = Math.max(0, Math.min(...boxes.map((box) => box[0])) - pad);
      const y0 = Math.max(0, Math.min(...boxes.map((box) => box[1])) - pad);
      const x1 = Math.min(1, Math.max(...boxes.map((box) => box[2])) + pad);
      const y1 = Math.min(1, Math.max(...boxes.map((box) => box[3])) + pad);
      if (x1 - x0 > 0.01 && y1 - y0 > 0.01 && x1 - x0 < 0.96) {
        crop = { x0, y0, x1, y1 };
      }
    }
    const applyCrop = () => {
      if (!crop || !img.naturalWidth || !img.naturalHeight) return;
      const width = crop.x1 - crop.x0;
      const height = crop.y1 - crop.y0;
      frame.classList.add("cropped");
      frame.style.aspectRatio = `${width * img.naturalWidth} / ${height * img.naturalHeight}`;
      img.style.width = `${100 / width}%`;
      img.style.left = `${-crop.x0 * 100 / width}%`;
      img.style.top = `${-crop.y0 * 100 / height}%`;
    };
    img.addEventListener("load", applyCrop);
    img.addEventListener("error", () => {
      figure.classList.add("missing");
      frame.classList.add("missing");
      if (!figure.querySelector(".asset-missing")) {
        figure.append(node("p", "hint asset-missing", "该资料图暂不可查看，可阅读右栏文字并继续翻页。"));
      }
    });
    frame.append(img);
    figure.append(frame);
    const controls = node("div", "asset-controls");
    if (crop) {
      const zoom = document.createElement("button");
      zoom.type = "button";
      zoom.className = "secondary";
      zoom.textContent = "放大";
      zoom.addEventListener("click", () => {
        const full = frame.classList.toggle("show-full");
        zoom.textContent = full ? "回到区域" : "放大";
      });
      controls.append(zoom);
      figure.append(node("p", "hint", "已按可靠区域裁剪显示；放大可查看完整原图。"));
    }
    const original = document.createElement("button");
    original.type = "button";
    original.className = "chip";
    original.textContent = "查看原图";
    original.addEventListener("click", () => {
      if (citation) window.XunzhiCitation.open(citation.id, original, citation);
      else window.open(url, "_blank", "noopener");
    });
    controls.append(original);
    if (citation) {
      const source = document.createElement("button");
      source.type = "button";
      source.className = "chip";
      source.textContent = "查看原文";
      source.addEventListener("click", () => window.XunzhiCitation.open(citation.id, source, citation));
      controls.append(source);
    }
    figure.append(controls);
    const sourceName = visual.source_name || "资料";
    const label = visual.label || (visual.page_no ? `第 ${visual.page_no} 页` : "");
    figure.append(node("figcaption", "hint", visual.caption || `${sourceName}${label ? ` · ${label}` : ""}`));
    container.append(figure);
    return true;
  }

  function renderStepSpec(container, spec, options = {}) {
    const state = { mode: Object.keys(spec.modes)[0], step: 0 };
    const wrap = node("section", "teaching-visual");
    const titleRow = node("div", "visual-title");
    titleRow.append(node("h3", null, spec.title));
    titleRow.append(node("span", "visual-badge", options.badge || "教学示意"));
    wrap.append(titleRow);
    if (spec.caption) wrap.append(node("p", "hint", spec.caption));

    const modeBox = node("div", "visual-modes");
    const modeKeys = Object.keys(spec.modes);
    if (modeKeys.length > 1) {
      modeKeys.forEach((key) => {
        const mode = spec.modes[key];
        const button = document.createElement("button");
        button.type = "button";
        button.className = "visual-mode";
        button.textContent = mode.label;
        button.dataset.mode = key;
        button.addEventListener("click", () => {
          state.mode = key;
          state.step = 0;
          update();
        });
        modeBox.append(button);
      });
      wrap.append(modeBox);
    } else {
      modeBox.hidden = true;
    }

    const stage = node("div", "visual-stage");
    const svg = renderStage(spec, state);
    stage.append(svg);
    wrap.append(stage);

    const stepText = node("p", "visual-step-text");
    stepText.setAttribute("aria-live", "polite");
    wrap.append(stepText);

    const quiz = node("div", "visual-quiz");
    const quizQuestion = node(
      "p",
      "visual-quiz-title",
      spec.question || "先判断：当前条件下的结果是什么？"
    );
    const select = document.createElement("select");
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = spec.placeholder || "请选择判断结果";
    select.append(placeholder);
    (spec.options || []).forEach((item) => {
      const option = document.createElement("option");
      option.value = item.id;
      option.textContent = item.text;
      select.append(option);
    });
    const check = document.createElement("button");
    check.type = "button";
    check.className = "secondary";
    check.textContent = "核对判断";
    const feedback = node("p", "visual-feedback");
    feedback.setAttribute("aria-live", "polite");
    check.addEventListener("click", () => {
      const mode = spec.modes[state.mode];
      if (!select.value) {
        feedback.textContent = "请先选择一个结果，再核对判断。";
        feedback.classList.remove("ok");
        feedback.classList.add("bad");
        return;
      }
      const correct = select.value === mode.correctOptionId;
      const correctText = (spec.options || []).find(
        (item) => item.id === mode.correctOptionId
      )?.text || "";
      feedback.textContent = correct
        ? `判断正确：${correctText}。`
        : `再想一想：当前条件下应是 ${correctText}。`;
      feedback.classList.toggle("ok", correct);
      feedback.classList.toggle("bad", !correct);
    });
    quiz.append(quizQuestion, select, check, feedback);
    if (!spec.options?.length || !spec.modes[state.mode].correctOptionId) quiz.hidden = true;
    wrap.append(quiz);

    const controls = node("div", "visual-controls");
    const prev = document.createElement("button");
    prev.type = "button";
    prev.className = "secondary";
    prev.textContent = "上一步";
    const next = document.createElement("button");
    next.type = "button";
    next.className = "secondary";
    next.textContent = "下一步";
    const replay = document.createElement("button");
    replay.type = "button";
    replay.className = "link";
    replay.textContent = "重播";
    prev.addEventListener("click", () => {
      state.step = Math.max(0, state.step - 1);
      update();
    });
    next.addEventListener("click", () => {
      state.step = Math.min(spec.modes[state.mode].steps.length - 1, state.step + 1);
      update();
    });
    replay.addEventListener("click", () => {
      state.step = 0;
      update();
    });
    controls.append(prev, next, replay);
    wrap.append(controls);

    function update() {
      const mode = spec.modes[state.mode];
      const step = mode.steps[state.step] || { text: "", nodes: [], edges: [], states: {} };
      stepText.textContent = step.text;
      modeBox.querySelectorAll("button").forEach((button) => {
        button.classList.toggle("active", button.dataset.mode === state.mode);
      });
      stage.querySelectorAll("[data-node]").forEach((item) => {
        item.classList.toggle("active", step.nodes.includes(item.dataset.node));
        const stateText = (step.states || {})[item.dataset.node];
        const label = item.querySelector(".visual-state");
        if (stateText && label) label.textContent = stateText;
      });
      stage.querySelectorAll("[data-edge]").forEach((item) => {
        item.classList.toggle("active", step.edges.includes(item.dataset.edge));
      });
      prev.disabled = state.step === 0;
      next.disabled = state.step >= mode.steps.length - 1;
      feedback.textContent = "";
      feedback.classList.remove("ok", "bad");
      select.value = "";
    }

    update();
    container.append(wrap);
    return true;
  }

  function renderPage(container, visual, options = {}) {
    if (!visual || !visual.type) return false;
    if (visual.type === "source_asset") {
      return renderSourceAsset(container, visual, options);
    }
    if (visual.type === "comparison") {
      return renderComparison(container, visual);
    }
    if (visual.type === "step_sequence") {
      const preset = visual.preset_id ? PRESETS[visual.preset_id] : null;
      const spec = preset || normalizeSpec(visual);
      if (!spec.modes || !Object.keys(spec.modes).length) {
        container.append(node("p", "hint", visual.notice || "当前演示暂不可用，可阅读右栏并继续翻页。"));
        return true;
      }
      return renderStepSpec(container, spec);
    }
    return false;
  }

  function render(container, module) {
    const presetId = module && module.visual_preset;
    if (presetId && PRESETS[presetId]) {
      return renderPage(container, { type: "step_sequence", preset_id: presetId });
    }
    return false;
  }

  window.XunzhiVisual = { render, renderPage, presets: PRESETS };
})();