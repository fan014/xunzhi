const kbId = document.body.dataset.kbId;
const $ = (id) => document.getElementById(id);
let base = null;
let timer = null;

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

function setStatus(target, text, isError = false) {
  const line = $(target);
  if (!line) return;
  line.textContent = text || "";
  line.classList.toggle("error", Boolean(isError));
}

function itemStatusTone(status) {
  return status === "ready" ? "ok" : status === "failed" ? "danger" : "warn";
}

function renderHeader() {
  $("kb-title").textContent = base.name;
  $("kb-crumb").textContent = base.name;
  $("kb-description").textContent = base.description || "";
  $("kb-summary").textContent = `${base.item_count} 份资料 · ${base.ready_count} 份可学习 · ${base.total_pages} 页 · ${base.status_label}`;
}

function actionButton(text, handler, className = "link") {
  const button = node("button", className, text);
  button.type = "button";
  button.addEventListener("click", handler);
  return button;
}

function renderItems() {
  const box = $("kb-items");
  box.innerHTML = "";
  const items = base.items || [];
  $("kb-items-empty").hidden = items.length > 0;
  $("kb-item-meta").textContent = `${items.length} 份资料，共 ${base.total_pages} 页`;
  items.forEach((item) => {
    const card = node("article", "kb-item");
    const head = node("div", "record-head");
    const title = node("strong", "record-title", item.display_name || "未命名资料");
    head.append(title);
    head.append(node("span", `badge ${itemStatusTone(item.status)}`, item.status_label));
    card.append(head);
    card.append(
      node(
        "p",
        "meta",
        `${item.kind || "资料"} · ${item.page_count || 0} 页 · 版本 ${item.version_no}${
          item.source_available ? "" : " · 原文件已删除"
        }`
      )
    );
    if (item.note) card.append(node("p", "hint", `笔记：${item.note}`));
    if (item.status === "failed") {
      card.append(
        node(
          "p",
          "hint",
          `整理失败：${item.error_message || item.error_code || "未知原因"}${
            item.usable ? "。本次学习仍使用原版本，可重新上传替换。" : ""
          }`
        )
      );
    }
    const chapters = item.chapters || [];
    if (chapters.length) {
      const list = node("div", "chapter-list");
      list.append(node("span", "chapter-head", "按章节开始学习"));
      chapters.forEach((chapter) => {
        const button = node("button", "chapter", chapter.label);
        button.type = "button";
        button.addEventListener("click", () => {
          const params = new URLSearchParams({
            kb: kbId,
            source: item.source_id,
            start: String(chapter.page_start),
            end: String(chapter.page_end),
          });
          window.location.href = `/?${params.toString()}`;
        });
        list.append(button);
      });
      card.append(list);
    }
    const actions = node("div", "actions");
    actions.append(
      actionButton("改名", async () => {
        const name = window.prompt(
          "在本知识库中的显示名（不影响原资料名和其他知识库）",
          item.display_name || ""
        );
        if (name === null) return;
        await patchItem(item.id, { display_name: name });
      })
    );
    actions.append(
      actionButton("笔记", async () => {
        const note = window.prompt("纯文本笔记", item.note || "");
        if (note === null) return;
        await patchItem(item.id, { note });
      })
    );
    actions.append(
      actionButton("替换文件", () => replaceItem(item.id))
    );
    actions.append(
      actionButton("移除", async () => {
        if (!window.confirm("从本知识库移除这份资料？只解除关联，原文件和其他库继续可用。")) return;
        const response = await fetch(`/api/knowledge-bases/${kbId}/items/${item.id}`, {
          method: "DELETE",
        });
        const data = await response.json();
        if (!response.ok) {
          setStatus("kb-status", data.error?.message || "移除失败", true);
          return;
        }
        await load();
      })
    );
    card.append(actions);
    box.append(card);
  });
}

async function patchItem(itemId, payload) {
  const response = await fetch(`/api/knowledge-bases/${kbId}/items/${itemId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    setStatus("kb-status", data.error?.message || "修改失败", true);
    return;
  }
  base = data;
  renderHeader();
  renderItems();
}

function replaceItem(itemId) {
  const input = document.createElement("input");
  input.type = "file";
  input.accept = ".pdf,.txt,.md,.png,.jpg,.jpeg,.webp";
  input.addEventListener("change", async () => {
    if (!input.files?.length) return;
    const form = new FormData();
    form.append("file", input.files[0]);
    setStatus("kb-add-status", "正在替换并整理新版本…");
    const response = await fetch(`/api/knowledge-bases/${kbId}/items/${itemId}/replace`, {
      method: "POST",
      body: form,
    });
    const data = await response.json();
    if (!response.ok) {
      setStatus("kb-add-status", data.error?.message || "替换失败", true);
      return;
    }
    base = data;
    renderHeader();
    renderItems();
    schedulePoll();
  });
  input.click();
}

async function load() {
  clearTimeout(timer);
  try {
    const response = await fetch(`/api/knowledge-bases/${kbId}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || "知识库读取失败");
    base = data;
    renderHeader();
    renderItems();
    if ((base.items || []).some((item) => ["pending", "processing"].includes(item.status))) {
      schedulePoll();
    }
  } catch (error) {
    setStatus("kb-status", error.message, true);
  }
}

function schedulePoll() {
  clearTimeout(timer);
  timer = setTimeout(load, 2000);
}

async function loadExistingSources() {
  const select = $("kb-existing-source");
  const response = await fetch("/api/sources");
  const data = await response.json();
  if (!response.ok) return;
  select.innerHTML = '<option value="">选择已上传资料</option>';
  (data.items || []).forEach((item) => {
    const option = document.createElement("option");
    option.value = item.id;
    option.textContent = `${item.display_name} · ${item.kind}`;
    select.append(option);
  });
}

async function uploadFiles(files) {
  if (!files?.length) return;
  const form = new FormData();
  Array.from(files).forEach((file) => form.append("files", file, file.name));
  setStatus("kb-add-status", "正在上传，整理会在后台继续…");
  const response = await fetch(`/api/knowledge-bases/${kbId}/items/upload`, {
    method: "POST",
    body: form,
  });
  const data = await response.json();
  if (!response.ok) {
    setStatus("kb-add-status", data.error?.message || "上传失败", true);
    return;
  }
  base = data;
  renderHeader();
  renderItems();
  const errors = data.upload_errors || [];
  setStatus(
    "kb-add-status",
    errors.length ? `部分失败：${errors.map((item) => `${item.name} ${item.message}`).join("；")}` : "已加入，正在后台整理",
    errors.length > 0
  );
  schedulePoll();
}

$("kb-dropzone").addEventListener("click", () => {
  const input = $("kb-files");
  input.value = "";
  input.click();
});
$("kb-dropzone").addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    $("kb-files").click();
  }
});
$("kb-files").addEventListener("change", () => uploadFiles($("kb-files").files));
$("kb-add-existing").addEventListener("click", async () => {
  const sourceId = $("kb-existing-source").value;
  if (!sourceId) return;
  const response = await fetch(`/api/knowledge-bases/${kbId}/items`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ source_id: sourceId }),
  });
  const data = await response.json();
  if (!response.ok) {
    setStatus("kb-add-status", data.error?.message || "加入失败", true);
    return;
  }
  base = data;
  renderHeader();
  renderItems();
  setStatus("kb-add-status", "已加入已有资料");
});
$("kb-edit").addEventListener("click", async () => {
  const name = window.prompt("知识库名称", base.name);
  if (name === null) return;
  const description = window.prompt("知识库简介", base.description || "");
  if (description === null) return;
  const response = await fetch(`/api/knowledge-bases/${kbId}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description }),
  });
  const data = await response.json();
  if (!response.ok) {
    setStatus("kb-status", data.error?.message || "保存失败", true);
    return;
  }
  base = data;
  renderHeader();
});
$("kb-delete").addEventListener("click", async () => {
  if (!window.confirm(`删除知识库「${base.name}」？只解除关联，原文件不会被删除。`)) return;
  const response = await fetch(`/api/knowledge-bases/${kbId}`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ confirm: true }),
  });
  const data = await response.json();
  if (!response.ok) {
    setStatus("kb-status", data.error?.message || "删除失败", true);
    return;
  }
  location.href = "/knowledge-bases";
});

load();
loadExistingSources();