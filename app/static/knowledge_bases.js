const $ = (id) => document.getElementById(id);

function setStatus(text, isError = false) {
  const line = $("kb-list-status");
  line.textContent = text || "";
  line.classList.toggle("error", Boolean(isError));
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined) element.textContent = text;
  return element;
}

async function load() {
  const list = $("kb-list");
  list.innerHTML = "";
  try {
    const response = await fetch("/api/knowledge-bases");
    const data = await response.json();
    if (!response.ok) throw new Error(data.error?.message || "知识库读取失败");
    const items = data.items || [];
    $("kb-list-empty").hidden = items.length > 0;
    const limits = data.limits || {};
    $("kb-list-meta").textContent = limits.kb_max_pages
      ? `单库上限：约 ${limits.kb_max_pages} 页 / ${limits.kb_max_sources} 份资料`
      : "";
    items.forEach((item) => {
      const card = node("article", "kb-card");
      const head = node("div", "record-head");
      const title = document.createElement("a");
      title.href = `/knowledge-bases/${item.id}`;
      title.className = "record-title";
      title.textContent = item.name;
      head.append(title);
      head.append(
        node(
          "span",
          `badge ${item.status === "ready" ? "ok" : item.status === "failed" ? "danger" : "warn"}`,
          item.status_label
        )
      );
      card.append(head);
      if (item.description) card.append(node("p", "hint", item.description));
      card.append(
        node(
          "p",
          "meta",
          `${item.item_count} 份资料 · ${item.total_pages} 页 · ${item.ready_count} 份可学习${
            item.failed_count ? ` · ${item.failed_count} 份失败` : ""
          }`
        )
      );
      const actions = node("div", "actions");
      const open = document.createElement("a");
      open.className = "secondary link-button";
      open.href = `/knowledge-bases/${item.id}`;
      open.textContent = "打开";
      const remove = node("button", "link", "删除");
      remove.type = "button";
      remove.addEventListener("click", async () => {
        if (!window.confirm(`删除知识库「${item.name}」？只会解除资料关联，原文件不会被删除。`)) return;
        const response = await fetch(`/api/knowledge-bases/${item.id}`, {
          method: "DELETE",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ confirm: true }),
        });
        const data = await response.json();
        if (!response.ok) {
          setStatus(data.error?.message || "删除失败", true);
          return;
        }
        load();
      });
      actions.append(open, remove);
      card.append(actions);
      list.append(card);
    });
  } catch (error) {
    setStatus(error.message, true);
    $("kb-list-empty").hidden = false;
  }
}

$("kb-create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const name = $("kb-name").value.trim();
  if (!name) return;
  const response = await fetch("/api/knowledge-bases", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name, description: $("kb-desc").value.trim() }),
  });
  const data = await response.json();
  if (!response.ok) {
    setStatus(data.error?.message || "新建失败", true);
    return;
  }
  setStatus(`已新建「${data.name}」`);
  $("kb-name").value = "";
  $("kb-desc").value = "";
  location.href = `/knowledge-bases/${data.id}`;
});

load();