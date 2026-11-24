(function () {
  let lastTrigger = null;
  let onKeydown = null;
  let zoom = 1;

  function close() {
    const overlay = document.getElementById("citation-overlay");
    if (!overlay) return;
    overlay.hidden = true;
    overlay.setAttribute("aria-hidden", "true");
    document.removeEventListener("keydown", onKeydown);
    if (lastTrigger && typeof lastTrigger.focus === "function") lastTrigger.focus();
  }

  function renderBoxes(container, boxes) {
    if (!boxes || !boxes.length) return;
    boxes.forEach((box) => {
      const [x0, y0, x1, y1] = box;
      const div = document.createElement("div");
      div.className = "box";
      div.style.left = `${x0 * 100}%`;
      div.style.top = `${y0 * 100}%`;
      div.style.width = `${(x1 - x0) * 100}%`;
      div.style.height = `${(y1 - y0) * 100}%`;
      container.append(div);
    });
  }

  function note(text, className = "muted") {
    const p = document.createElement("p");
    p.className = className;
    p.textContent = text;
    return p;
  }

  function quote(text) {
    const div = document.createElement("div");
    div.className = "quote";
    div.textContent = text || "（无摘要）";
    return div;
  }

  function alternativeLabel(payload) {
    return payload.text_alternative_source === "claim" ? "引用说明" : "文字替代";
  }

  function render(payload) {
    const body = document.getElementById("citation-body");
    const title = document.getElementById("citation-title");
    title.textContent = `${payload.source_name || "资料引用"} · ${payload.label || ""}`;
    body.innerHTML = "";
    zoom = 1;

    if (payload.status === "source_deleted") {
      body.append(note("来源资料已删除，以下为引用时保存的摘要，无法再打开原文或预览。"));
      body.append(quote(payload.quote || payload.text_alternative));
      return;
    }
    if (payload.status === "locator_invalid") {
      body.append(note("引用定位失败，已保留摘要，不打开空白页。", "error-text"));
      body.append(quote(payload.quote || payload.text_alternative));
      return;
    }
    if (payload.status === "asset_missing" || payload.status === "source_missing") {
      body.append(note(payload.message || "引用页图或媒体文件缺失，仅保留文字摘要。", "error-text"));
      if (payload.quote) body.append(quote(payload.quote));
      body.append(note(`${alternativeLabel(payload)}：${payload.text_alternative || "无"}`));
      return;
    }
    if (payload.asset_url) {
      const wrap = document.createElement("div");
      wrap.className = "locator-image";
      const img = document.createElement("img");
      img.alt = payload.text_alternative || "资料图像";
      img.src = payload.asset_url;
      img.addEventListener("error", () => {
        wrap.replaceWith(note("图片加载失败，已保留文字说明。", "error-text"));
        if (payload.quote) body.append(quote(payload.quote));
        body.append(note(`${alternativeLabel(payload)}：${payload.text_alternative || "无"}`));
      });
      wrap.append(img);
      renderBoxes(wrap, payload.boxes);
      const controls = document.createElement("div");
      controls.className = "image-controls";
      const out = document.createElement("button");
      out.type = "button";
      out.className = "secondary";
      out.textContent = "缩小";
      out.addEventListener("click", () => {
        zoom = Math.max(0.5, zoom - 0.25);
        wrap.style.width = `${zoom * 100}%`;
      });
      const inBtn = document.createElement("button");
      inBtn.type = "button";
      inBtn.className = "secondary";
      inBtn.textContent = "放大";
      inBtn.addEventListener("click", () => {
        zoom = Math.min(3, zoom + 0.25);
        wrap.style.width = `${zoom * 100}%`;
      });
      controls.append(out, inBtn);
      if (payload.boxes?.length) {
        controls.append(note(`区域 1 / 共 ${payload.boxes.length} 处`));
      } else {
        controls.append(note(payload.asset_kind === "page_image" ? "整页（无局部区域）" : "整图（无局部区域）"));
      }
      body.append(wrap, controls);
      if (payload.scanned) body.append(note("扫描页 · 按图像定位"));
    }
    if (payload.page_no) {
      body.append(note(`第 ${payload.page_no} 页${payload.page_count ? ` / 共 ${payload.page_count} 页` : ""}`));
    }
    if (payload.context_before) body.append(note(`前文：${payload.context_before}`));
    if (payload.quote) body.append(quote(payload.quote));
    if (payload.context_after) body.append(note(`后文：${payload.context_after}`));
    body.append(note(`${alternativeLabel(payload)}：${payload.text_alternative || "无"}`));
  }

  async function open(citationId, trigger, localPayload) {
    lastTrigger = trigger || document.activeElement;
    const overlay = document.getElementById("citation-overlay");
    const body = document.getElementById("citation-body");
    if (!overlay || !body) return;
    overlay.hidden = false;
    overlay.setAttribute("aria-hidden", "false");
    body.textContent = "正在读取引用…";
    document.getElementById("citation-close").focus();
    onKeydown = (event) => {
      if (event.key === "Escape") close();
    };
    document.addEventListener("keydown", onKeydown);

    try {
      let data = null;
      if (localPayload && localPayload.is_offline) {
        data = localPayload;
      } else if (localPayload && localPayload.status) {
        data = localPayload;
      } else {
        const response = await fetch(`/api/citations/${encodeURIComponent(citationId)}`);
        data = await response.json();
        if (!response.ok) throw new Error(data.error?.message || "引用读取失败");
      }
      render(data);
    } catch (err) {
      body.textContent = err.message;
    }
  }

  window.XunzhiCitation = { open, close };
  document.addEventListener("DOMContentLoaded", () => {
    const button = document.getElementById("citation-close");
    if (button) button.addEventListener("click", close);
  });
})();