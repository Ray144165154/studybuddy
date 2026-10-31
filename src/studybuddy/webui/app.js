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
  let table = null;

  const closeList = () => { if (list) { html += `</${list}>`; list = null; } };
  const closeTable = () => {
    if (!table) return;
    const [head, ...rows] = table;
    html += "<table><thead><tr>";
    for (const cell of head) html += `<th>${inline(cell)}</th>`;
    html += "</tr></thead><tbody>";
    for (const row of rows) {
      html += "<tr>";
      for (const cell of row) html += `<td>${inline(cell)}</td>`;
      html += "</tr>";
    }
    html += "</tbody></table>";
    table = null;
  };

  const splitRow = (line) =>
    line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map((cell) => cell.trim());

  for (const raw of lines) {
    const line = raw.trimEnd();

    if (!line.trim()) { closeList(); closeTable(); continue; }

    if (line.trim().startsWith("|")) {
      closeList();
      const cells = splitRow(line);
      const isSeparator = cells.every((cell) => /^:?-{2,}:?$/.test(cell));
      if (isSeparator) continue;
      if (!table) table = [];
      table.push(cells);
      continue;
    }
    closeTable();

    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) { closeList(); html += `<h3>${inline(heading[2])}</h3>`; continue; }

    const item = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)$/);
    if (item) {
      if (!list) { list = "ul"; html += "<ul>"; }
      html += `<li>${inline(item[1])}</li>`;
      continue;
    }

    closeList();
    html += `<p>${inline(line)}</p>`;
  }
  closeList();
  closeTable();
  return html;
}


// ---------------------------------------------------------------------------
// minimal LaTeX rendering
//
// The model answers in LaTeX ($\sigma$, \mathbb{R}, X_n, \frac{a}{b}). Shipping
// KaTeX would add ~1MB to a local-first tool, so this covers the notation that
// actually shows up in probability/calculus textbooks and leaves anything exotic
// recognisable rather than broken.
// ---------------------------------------------------------------------------

const MATH_SYMBOLS = {
  // 希腊字母
  alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", varepsilon: "ε",
  zeta: "ζ", eta: "η", theta: "θ", iota: "ι", kappa: "κ", lambda: "λ", mu: "μ",
  nu: "ν", xi: "ξ", pi: "π", rho: "ρ", sigma: "σ", tau: "τ", upsilon: "υ",
  phi: "φ", varphi: "φ", chi: "χ", psi: "ψ", omega: "ω",
  Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π",
  Sigma: "Σ", Phi: "Φ", Psi: "Ψ", Omega: "Ω",
  // 关系与运算
  le: "≤", leq: "≤", ge: "≥", geq: "≥", ne: "≠", neq: "≠", approx: "≈",
  equiv: "≡", sim: "∼", propto: "∝", ll: "≪", gg: "≫",
  times: "×", div: "÷", cdot: "·", pm: "±", mp: "∓",
  to: "→", rightarrow: "→", leftarrow: "←", mapsto: "↦", implies: "⇒",
  iff: "⇔", in: "∈", notin: "∉", ni: "∋", subset: "⊂", subseteq: "⊆",
  supset: "⊃", supseteq: "⊇", cup: "∪", cap: "∩", setminus: "∖",
  emptyset: "∅", varnothing: "∅", forall: "∀", exists: "∃", neg: "¬",
  infty: "∞", partial: "∂", nabla: "∇", sum: "∑", prod: "∏", int: "∫",
  oint: "∮", sqrt: "√", angle: "∠", perp: "⊥", parallel: "∥",
  cdots: "⋯", ldots: "…", dots: "…", quad: " ", qquad: "  ",
  // 黑板粗体
  "mathbb{R}": "ℝ", "mathbb{N}": "ℕ", "mathbb{Z}": "ℤ", "mathbb{Q}": "ℚ",
  "mathbb{C}": "ℂ", "mathbb{E}": "𝔼", "mathbb{P}": "ℙ",
};

function mathTokens(expr) {
  // 先处理带参数的命令，再处理单符号命令
  let out = expr;
  out = out.replace(/\\(?:text|mathrm|mathit|mathbf|operatorname)\{([^{}]*)\}/g, "$1");
  out = out.replace(/\\(?:mathbb|mathcal|mathscr|mathfrak)\{([A-Za-z])\}/g, (m, letter) => {
    const blackboard = { R: "ℝ", N: "ℕ", Z: "ℤ", Q: "ℚ", C: "ℂ" };
    return blackboard[letter] || letter; // 避免补充平面字形（𝒜 会显示成方框）
  });
  out = out.replace(/\\sqrt\{([^{}]*)\}/g, "√($1)");
  out = out.replace(/\\frac\{([^{}]*)\}\{([^{}]*)\}/g, "($1)/($2)");
  out = out.replace(/\\([A-Za-z]+)/g, (m, name) => (MATH_SYMBOLS[name] !== undefined ? MATH_SYMBOLS[name] : name));
  out = out.replace(/\\[,;:! ]/g, " ").replace(/[{}]/g, "").replace(/\\/g, "");
  return out;
}

function mathToHtml(expr) {
  let html = mathTokens(expr);
  // 上下标：先整体替换，再处理单字符
  html = html.replace(/\^\{([^{}]*)\}/g, "<sup>$1</sup>").replace(/_\{([^{}]*)\}/g, "<sub>$1</sub>");
  html = html.replace(/\^([A-Za-z0-9+\-])/g, "<sup>$1</sup>").replace(/_([A-Za-z0-9+\-])/g, "<sub>$1</sub>");
  return `<span class="math">${html}</span>`;
}

function renderMath(text) {
  return text
    .replace(/\$\$([\s\S]+?)\$\$/g, (m, expr) => mathToHtml(expr))
    .replace(/\$([^$\n]+?)\$/g, (m, expr) => mathToHtml(expr));
}

function inline(text) {
  return renderMath(
    text
      .replace(/\*\*(.+?)\*\*/g, "<b>$1</b>")
    .replace(/`(.+?)`/g, "<code>$1</code>")
      .replace(/\[(\d{1,2})\]/g, '<span class="cite" data-cite="$1">$1</span>')
  );
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

function chips(questions) {
  const wrap = el("div", "chips");
  for (const question of questions) {
    const chip = el("button", "chip", question);
    chip.onclick = () => {
      $("question").value = question;
      $("ask-form").requestSubmit();
    };
    wrap.append(chip);
  }
  return wrap;
}

function renderWelcome() {
  const messages = $("messages");
  messages.innerHTML = "";
  if (!STATE) return;

  const needsModel = !STATE.llm_ready;
  const needsBook = STATE.stats.books === 0;

  if (!needsModel && !needsBook) {
    const hero = el("div", "empty");
    hero.append(el("h1", "", "问你的教材"));
    hero.append(el("p", "", "回答会标出页码，方便翻书核对。"));
    if (STATE.suggestions && STATE.suggestions.length) {
      hero.append(el("div", "foot", "试试这些问题："));
      hero.append(chips(STATE.suggestions));
    }
    messages.append(hero);
    return;
  }

  const card = el("div", "onboarding");
  card.append(el("h1", "", "三步开始用"));
  card.append(el("p", "sub", "教材只在本机解析；只有被检索到的片段会发给模型。"));

  const steps = [
    {
      done: !needsModel,
      title: "接入模型",
      body: "选择服务商，填写该服务商的模型 ID、接口地址和 API Key。聊天网站账号或订阅不能代替 API Key；重启后需重新填写 Key。",
      action: "去配置",
      onAction: openSettings,
    },
    {
      done: !needsBook,
      title: "导入教材",
      body: "PDF 教材（中英文都行）。扫描版需要先 OCR，再导入。",
      action: "选择 PDF",
      onAction: () => $("file").click(),
    },
    {
      done: false,
      title: "开始提问",
      body: "先问“第一章主要讲什么？”。点击 [1] 核对原文片段；页码是 PDF 从封面起的页序，可能与书上印刷页码不同。",
      action: null,
      onAction: null,
    },
  ];

  steps.forEach((step, index) => {
    const row = el("div", "step" + (step.done ? " done" : ""));
    const mark = el("span", "step-mark", step.done ? "✓" : String(index + 1));
    row.append(mark);
    const body = el("div", "step-body");
    body.append(el("div", "step-title", step.title));
    body.append(el("div", "step-desc", step.body));
    row.append(body);
    if (step.action) {
      const button = el("button", "btn tiny", step.action);
      button.onclick = step.onAction;
      row.append(button);
    }
    card.append(row);
  });

  if (!STATE.writable) {
    card.append(
      el(
        "div",
        "warn",
        "数据目录不可写：" +
          STATE.root +
          "。可以查看教材和提问，但导入教材、保存记忆会失败。请检查目录权限，或换一个数据目录（--root）。",
      ),
    );
  }

  card.append(el("div", "foot", "卡住了？点右上角「帮助」，或在终端运行 studybuddy doctor。"));
  messages.append(card);
}

async function refresh() {
  STATE = await api("/api/state");
  renderStatus();
  renderBooks();
  $("data-root").textContent = STATE.root;
  if (!$("messages").querySelector(".bubble")) renderWelcome();
  const webBox = $("web");
  if (!STATE.web_ready) {
    webBox.disabled = true;
    webBox.checked = false;
    $("web-wrap").title = "未配置搜索服务（默认使用 DuckDuckGo；也可设 Tavily/Serper 的 key）";
  }
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
  if (!STATE.llm_ready) {
    $("question").value = question;
    openSettings();
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
      web: $("web").checked && !$("web").disabled,
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

// Safari can swallow programmatic navigation from an injected anchor; when the
// popup is blocked, fall back to copying the URL so the user can still get there.
function openExternal(event, url) {
  event.preventDefault();
  let opened = null;
  try {
    opened = window.open(url, "_blank", "noopener,noreferrer");
  } catch (error) {
    opened = null;
  }
  if (!opened) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(url).catch(() => {});
    }
    alert("浏览器拦截了新窗口，链接已尝试复制到剪贴板：\n\n" + url);
  }
}

function copyLink(url) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(url).then(
      () => alert("链接已复制：\n" + url),
      () => prompt("复制下面的链接：", url),
    );
  } else {
    prompt("复制下面的链接：", url);
  }
}

function domainOf(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch (error) {
    return "";
  }
}

function renderAnswer(card, data) {
  card.innerHTML = "";
  const body = el("div", "answer");

  // The answer cites web results as [W1]; turn those into links to the source.
  const webByIndex = new Map((data.web_sources || []).map((item) => [item.index, item]));
  const html = renderMarkdown(data.text || "（模型没有返回内容）").replace(
    /\[W(\d{1,2})\]/g,
    (match, number) => {
      const item = webByIndex.get(Number(number));
      if (!item) return match;
      const url = escapeHtml(item.url);
      return (
        `<a class="cite cite-web" data-web="${number}" href="${url}" ` +
        `target="_blank" rel="noopener noreferrer" title="${url}">W${number}</a>`
      );
    },
  );
  body.innerHTML = html;
  body.querySelectorAll(".cite-web").forEach((node) => {
    const item = webByIndex.get(Number(node.dataset.web));
    if (item) node.addEventListener("click", (event) => openExternal(event, item.url));
  });
  card.append(body);

  if (data.warnings && data.warnings.length) {
    card.append(el("div", "warn", data.warnings.join(" / ")));
  }

  if (data.memories && data.memories.length) {
    const remember = el("div", "remember");
    remember.innerHTML = "<b>我记得：</b>" + escapeHtml(data.memories.slice(0, 3).join("；"));
    card.append(remember);
  }

  if (data.web_sources && data.web_sources.length) {
    const web = el("div", "sources");
    web.append(el("div", "label", "网络来源（非教材内容，点击在新标签页打开）"));
    const list = el("ol");
    for (const item of data.web_sources) {
      const li = el("li");
      const link = document.createElement("a");
      link.href = item.url;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      link.title = item.url;
      link.textContent = item.title || item.url;
      li.append(el("span", "badge-web", "W" + item.index), link);
      const domain = domainOf(item.url);
      if (domain) li.append(el("span", "where", "  " + domain));
      const copy = el("button", "btn tiny copy-link", "复制");
      copy.title = item.url;
      copy.onclick = () => copyLink(item.url);
      li.append(copy);
      li.append(el("div", "url-line", item.url));
      list.append(li);
    }
    web.append(list);
    card.append(web);
  }

  if (data.sources && data.sources.length) {
    const sources = el("div", "sources");
    sources.append(el("div", "label", "检索到的教材片段（引用编号不代表内容已经核实）"));
    const list = el("ol");
    for (const source of data.sources) {
      const item = el("li");
      const where = source.section
        ? `《${source.book}》PDF 第${source.page}页 · ${source.section}`
        : `《${source.book}》PDF 第${source.page}页`;
      item.append(el("span", "where", where));
      if (source.unreliable) item.append(el("span", "badge", "公式可能失真"));
      item.append(el("p", "foot", source.snippet || "无可显示的原文片段"));
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

  if (data.usage || data.elapsed) {
    const parts = [];
    if (data.elapsed) parts.push(`${data.elapsed} 秒`);
    if (data.usage) parts.push(`${data.usage.total_tokens} tokens`);
    if (data.sources) parts.push(`${data.sources.length} 个教材片段`);
    if (data.web_sources && data.web_sources.length) parts.push(`${data.web_sources.length} 条网络来源`);
    card.append(el("div", "foot", parts.join(" · ")));
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
  if (file.size > 80 * 1024 * 1024) {
    status.textContent = "文件太大（上限 80MB）";
    return;
  }
  status.textContent = `导入 ${file.name}…`;
  try {
    const base64 = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",")[1]);
      reader.onerror = () => reject(new Error("读取文件失败，请重新选择文件"));
      reader.onabort = () => reject(new Error("文件读取已取消"));
      reader.readAsDataURL(file);
    });
    const result = await api("/api/add-book", {
      name: file.name,
      data: base64,
      course: $("course").value.trim(),
    });
    $("course").value = "";
    await refresh();
    const warnings = result.book?.warnings || [];
    status.textContent = `已导入 ${file.name}`;
    if (warnings.length) alert(`教材已导入，但内容不完整：\n${warnings.join("\n")}`);
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
  $("settings-error").textContent = "";
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

$("btn-help").onclick = () => $("help").showModal();
$("btn-close-help").onclick = () => $("help").close();
$("btn-memory").onclick = openMemory;
$("btn-close-drawer").onclick = closeMemory;
$("scrim").onclick = closeMemory;
document.querySelectorAll(".presets button").forEach((button) => {
  button.onclick = () => {
    $("set-model").value = button.dataset.model;
    $("set-base").value = button.dataset.base;
    $("set-key").value = button.dataset.base.startsWith("http://127.0.0.1:") ? "not-needed" : "";
    $("set-key").focus();
  };
});

$("btn-settings").onclick = openSettings;
$("btn-save-settings").onclick = (event) => {
  event.preventDefault();
  saveSettings().then(() => $("settings").close()).catch((error) => {
    $("settings-error").textContent = error.message;
  });
};

refresh().catch((error) => {
  $("status").textContent = `无法连接后端: ${error.message}`;
});
