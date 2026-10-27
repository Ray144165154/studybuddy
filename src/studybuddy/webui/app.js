"use strict";

// ---------------------------------------------------------------------------
// tiny helpers
// ---------------------------------------------------------------------------

const $ = (id) => document.getElementById(id);

async function api(path, payload) {
  const options = payload
    ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }
    : { method: "GET" };
  const response = await fetch(path, options);
  const data = await response.json().catch(() => ({ error: "服务器返回了非 JSON 内容" }));
  if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
  return data;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function escapeHtml(text) {
  return text.replace(/[&<>"']/g, (ch) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
}

// Deliberately small: headings, lists, bold, code, citations. Anything fancier
// belongs in a library, not in a 200-line UI.
function renderMarkdown(text) {
  const lines = escapeHtml(text).split("\n");
  let html = "";
  let list = null;
  for (const raw of lines) {
    const line = raw.trimEnd();
    if (!line.trim()) { if (list) { html += `</${list}>`; list = null; } continue; }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) { if (list) { html += `</${list}>`; list = null; } html += `<h3>${heading[2]}</h3>`; continue; }
    const item = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
    if (item) {
      if (!list) { list = "ul"; html += "<ul>"; }
      html += `<li>${inline(item[1])}</li>`;
      continue;
    }
    if (list) { html += `</${list}>`; list = null; }
    html += `<p>${inline(line)}</p>`;
  }
  if (list) html += `</${list}>`;
  return html;
}

function inline(text) {
  return text
    .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`(.+?)`/g, "<code>$1</code>")
    .replace(/\[(\d{1,2})\]/g, '<span class="cite" data-cite="$1">$1</span>');
}

// ---------------------------------------------------------------------------
// state
// ---------------------------------------------------------------------------

let STATE = null;

function renderStatus() {
  const status = $("status");
  if (!STATE) { status.textContent = ""; return; }
  const parts = [`${STATE.stats.books} 本教材`, `${STATE.stats.chunks} 个片段`];
  if (STATE.has_embeddings) parts.push("语义检索已建索引");
  parts.push(STATE.llm_ready ? STATE.model : "未配置模型");
  status.textContent = parts.join(" · ");
  $("book-count").textContent = String(STATE.stats.books);
}

function renderBooks() {
  const list = $("books");
  list.innerHTML = "";
  if (!STATE || !STATE.books.length) {
    list.append(el("li", "meta", "还没有教材，点下面的按钮导入。"));
    return;
  }
  for (const book of STATE.books) {
    const item = el("li");
    item.append(el("div", "title", book.title));
    item.append(el("div", "meta", `${book.pages} 页 · ${book.chunks} 片段${book.course ? " · " + book.course : ""}`));
    const row = el("div", "row");
    const quiz = el("button", "btn tiny", "出题");
    quiz.onclick = () => runQuiz(book.title);
    const outline = el("button", "btn tiny", "提纲");
    outline.onclick = () => runSummary(book.title);
    row.append(quiz, outline);
    item.append(row);
    list.append(item);
  }
}

async function refresh() {
  STATE = await api("/api/state");
  renderStatus();
  renderBooks();
}

// ---------------------------------------------------------------------------
// chat
// ---------------------------------------------------------------------------

function clearEmpty() {
  const empty = document.querySelector(".messages .empty");
  if (empty) empty.remove();
}

function addUser(text) {
  clearEmpty();
  const bubble = el("div", "bubble user");
  bubble.textContent = text;
  $("messages").append(bubble);
  return bubble;
}

function addAssistantCard() {
  const card = el("div", "bubble assistant");
  $("messages").append(card);
  return card;
}

function scrollDown() {
  const messages = $("messages");
  messages.scrollTop = messages.scrollHeight;
}

async function ask(question) {
  if (!STATE || !STATE.stats.books) {
    alert("先导入一本教材");
    return;
  }
  addUser(question);
  const card = addAssistantCard();
  card.innerHTML = '<p class="foot">检索教材中…</p>';
  scrollDown();

  const send = $("send");
  send.disabled = true;
  try {
    const payload = {
      question,
      rewrite: $("rewrite").checked,
      semantic: $("semantic").checked,
    };
    const data = await api("/api/ask", payload);
    renderAnswer(card, data);
  } catch (error) {
    card.innerHTML = "";
    card.append(el("div", "warn", `出错: ${error.message}`));
  } finally {
    send.disabled = false;
    scrollDown();
  }
}

function renderAnswer(card, data) {
  card.innerHTML = "";
  const body = el("div", "answer");
  body.innerHTML = renderMarkdown(data.text || "（模型没有返回内容）");
  card.append(body);

  if (data.warnings && data.warnings.length) {
    card.append(el("div", "warn", data.warnings.join(" / ")));
  }

  if (data.memories && data.memories.length) {
    const remember = el("div", "remember");
    remember.innerHTML = "<b>我记得：</b>" + escapeHtml(data.memories.slice(0, 3).join("；"));
    card.append(remember);
  }

  if (data.sources && data.sources.length) {
    const sources = el("div", "sources");
    sources.append(el("div", "label", "来源"));
    const list = el("ol");
    for (const source of data.sources) {
      const item = el("li");
      const where = source.section
        ? `《${source.book}》第${source.page}页 · ${source.section}`
        : `《${source.book}》第${source.page}页`;
      item.append(el("span", "where", where));
      if (source.unreliable) item.append(el("span", "badge", "公式可能失真"));
      item.title = source.snippet || "";
      list.append(item);
    }
    sources.append(list);
    card.append(sources);

    body.querySelectorAll(".cite").forEach((node) => {
      node.onclick = () => {
        const index = Number(node.dataset.cite) - 1;
        const target = list.children[index];
        if (target) target.scrollIntoView({ block: "nearest", behavior: "smooth" });
      };
    });
  }

  if (data.usage) {
    card.append(el("div", "foot", `${data.usage.total_tokens} tokens`));
  }
}

// ---------------------------------------------------------------------------
// quick actions
// ---------------------------------------------------------------------------

async function runQuiz(book) {
  clearEmpty();
  const card = addAssistantCard();
  card.innerHTML = '<p class="foot">出题中…</p>';
  scrollDown();
  try {
    const data = await api("/api/quiz", { book, count: 3 });
    card.innerHTML = "";
    card.append(el("div", "foot", `自测 · ${data.topic || book}`));
    const wrap = el("div", "answer");
    card.append(wrap);
    if (!data.questions.length) {
      wrap.append(el("p", "foot", "没有生成出题目，换一本书或稍后再试。"));
      return;
    }
    data.questions.forEach((question, index) => {
      const item = el("div", "quiz-q");
      item.append(el("div", "q", `${index + 1}. ${question.question}`));
      const answer = el("div", "a", question.answer);
      item.append(answer);
      const toggle = el("button", "btn tiny", "看答案");
      toggle.onclick = () => {
        item.classList.toggle("open");
        toggle.textContent = item.classList.contains("open") ? "收起" : "看答案";
      };
      item.append(toggle);
      wrap.append(item);
    });
  } catch (error) {
    card.innerHTML = "";
    card.append(el("div", "warn", `出错: ${error.message}`));
  }
  scrollDown();
}

async function runSummary(book) {
  clearEmpty();
  const card = addAssistantCard();
  card.innerHTML = '<p class="foot">整理提纲中…</p>';
  scrollDown();
  try {
    const data = await api("/api/summary", { book });
    card.innerHTML = "";
    const body = el("div", "answer");
    body.innerHTML = renderMarkdown(data.text || "");
    card.append(body);
  } catch (error) {
    card.innerHTML = "";
    card.append(el("div", "warn", `出错: ${error.message}`));
  }
  scrollDown();
}

// ---------------------------------------------------------------------------
// memory drawer
// ---------------------------------------------------------------------------

async function openMemory() {
  $("drawer").classList.add("open");
  $("scrim").hidden = false;
  const body = $("memory-body");
  body.innerHTML = "<p class='foot'>加载中…</p>";
  try {
    const data = await api("/api/memory");
    renderMemory(body, data);
  } catch (error) {
    body.innerHTML = "";
    body.append(el("div", "warn", error.message));
  }
}

function closeMemory() {
  $("drawer").classList.remove("open");
  $("scrim").hidden = true;
}

function renderMemory(root, data) {
  root.innerHTML = "";

  const profile = el("div", "mem-section");
  profile.append(el("h3", "画像"));
  const p = el("div");
  p.innerHTML = data.profile_summary
    ? escapeHtml(data.profile_summary)
    : "<span class='foot'>还没有设置。用命令行 <code>studybuddy profile</code> 或直接提问，助手会逐渐记住你。</span>";
  profile.append(p);
  root.append(profile);

  const facts = el("div", "mem-section");
  facts.append(el("h3", `记住的事（${data.facts.length}）`));
  if (!data.facts.length) facts.append(el("div", "foot", "还没有。多问几次，它会记下你的薄弱点和偏好。"));
  const factList = el("ul");
  for (const fact of data.facts) {
    const item = el("li");
    item.innerHTML = `<span class="tag">${escapeHtml(fact.kind)}</span>${escapeHtml(fact.text)}`;
    factList.append(item);
  }
  facts.append(factList);
  root.append(facts);

  const topics = el("div", "mem-section");
  topics.append(el("h3", `跟进的话题（${data.topics.length}）`));
  const topicList = el("ul");
  for (const topic of data.topics) {
    const item = el("li");
    item.textContent = `${topic.name} · 掌握度 ${Math.round(topic.mastery * 100)}% · 见 ${topic.seen} 次`;
    topicList.append(item);
  }
  topics.append(topicList);
  root.append(topics);

  const due = el("div", "mem-section");
  due.append(el("h3", "今天该复习"));
  if (!data.due.length) {
    due.append(el("div", "foot", "没有到期的复习任务。👍"));
  } else {
    for (const name of data.due.slice(0, 5)) {
      const row = el("div", "due");
      row.append(el("span", "", name));
      const ok = el("button", "btn tiny", "记住了");
      ok.onclick = async () => {
        await api("/api/grade", { topic: name, correct: true });
        openMemory();
      };
      const bad = el("button", "btn tiny", "没记住");
      bad.onclick = async () => {
        await api("/api/grade", { topic: name, correct: false });
        openMemory();
      };
      row.append(ok, bad);
      due.append(row);
    }
  }
  root.append(due);

  const clear = el("button", "btn ghost", "清空记忆");
  clear.onclick = async () => {
    if (!confirm("清空后薄弱点、话题和历史都会删除（画像保留），确定？")) return;
    await api("/api/memory/clear", {});
    openMemory();
  };
  root.append(clear);
}

// ---------------------------------------------------------------------------
// upload + settings
// ---------------------------------------------------------------------------

async function uploadFile(file) {
  const status = $("status");
  status.textContent = `导入 ${file.name}…`;
  const base64 = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result).split(",")[1]);
    reader.onerror = () => reject(new Error("读取文件失败"));
    reader.readAsDataURL(file);
  });

  try {
    await api("/api/add-book", {
      name: file.name,
      data: base64,
      course: $("course").value.trim(),
    });
    $("course").value = "";
    await refresh();
    status.textContent = `已导入 ${file.name}`;
    setTimeout(renderStatus, 2500);
  } catch (error) {
    alert(`导入失败: ${error.message}`);
    renderStatus();
  }
}

function openSettings() {
  if (STATE) {
    $("set-model").value = STATE.model || "";
    $("set-base").value = STATE.base_url || "";
  }
  $("settings").showModal();
}

async function saveSettings() {
  const payload = {
    model: $("set-model").value.trim(),
    base_url: $("set-base").value.trim(),
  };
  const key = $("set-key").value.trim();
  if (key) payload.api_key = key;
  await api("/api/config", payload);
  $("set-key").value = "";
  await refresh();
}

// ---------------------------------------------------------------------------
// wiring
// ---------------------------------------------------------------------------

$("ask-form").addEventListener("submit", (event) => {
  event.preventDefault();
  const question = $("question").value.trim();
  if (!question) return;
  $("question").value = "";
  ask(question);
});

$("question").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    $("ask-form").requestSubmit();
  }
});

$("file").addEventListener("change", (event) => {
  const file = event.target.files[0];
  if (file) uploadFile(file);
  event.target.value = "";
});

$("btn-memory").onclick = openMemory;
$("btn-close-drawer").onclick = closeMemory;
$("scrim").onclick = closeMemory;
$("btn-settings").onclick = openSettings;
$("btn-save-settings").onclick = (event) => {
  event.preventDefault();
  saveSettings().then(() => $("settings").close());
};

refresh().catch((error) => {
  $("status").textContent = `无法连接后端: ${error.message}`;
});
