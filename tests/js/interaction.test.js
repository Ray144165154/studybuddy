// Exercise real form handlers with a small DOM stub; no browser or network needed.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const { test } = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../../src/studybuddy/webui/app.js"), "utf8");

function setup() {
  const nodes = new Map();
  const alerts = [];
  const document = {
    querySelectorAll: () => [],
    getElementById(id) {
      if (!nodes.has(id)) nodes.set(id, {
        value: "", disabled: false, checked: false, textContent: "", handlers: {},
        addEventListener(type, handler) { this.handlers[type] = handler; },
        showModal() { this.open = true; },
        close() { this.open = false; },
        requestSubmit() { return this.handlers.submit?.({ preventDefault() {} }); },
      });
      return nodes.get(id);
    },
  };
  const context = vm.createContext({ document, alert: (text) => alerts.push(text) });
  // Skip only the initial network refresh; all form wiring and ask logic run.
  vm.runInContext(source.slice(0, source.lastIndexOf("refresh().catch(")), context);
  vm.runInContext(`
    STATE = { stats: { books: 1 }, llm_ready: true, model: "test", base_url: "https://example.com/v1" };
    addUser = () => {};
    addAssistantCard = () => ({ append() {}, innerHTML: "" });
    scrollDown = () => {};
    renderAnswer = () => {};
    clearEmpty = () => {};
    el = () => ({});
    api = async () => ({});
  `, context);
  const get = (id) => document.getElementById(id);
  get("remember").checked = true;
  get("ask-form").requestSubmit = () => get("ask-form").handlers.submit({ preventDefault() {} });
  return { context, get, alerts, run: (code) => vm.runInContext(code, context) };
}

test("IME confirmation and Shift+Enter do not submit; Enter submits once", async () => {
  const { get } = setup();
  let submissions = 0;
  let prevented = 0;
  get("ask-form").requestSubmit = () => { submissions += 1; };
  const keydown = get("question").handlers.keydown;
  for (const extra of [{ isComposing: true }, { keyCode: 229 }, { shiftKey: true }]) {
    keydown({ key: "Enter", preventDefault() { prevented += 1; }, ...extra });
  }
  assert.equal(submissions, 0);
  assert.equal(prevented, 0);
  keydown({ key: "Enter", preventDefault() { prevented += 1; } });
  assert.equal(submissions, 1);
  assert.equal(prevented, 1);
});

test("missing books or model preserves the draft", async () => {
  for (const state of ["STATE.stats.books = 0", "STATE.llm_ready = false"]) {
    const { get, run } = setup();
    run(state);
    get("question").value = " 什么是条件概率？ ";
    await get("ask-form").requestSubmit();
    assert.equal(get("question").value, " 什么是条件概率？ ");
  }
});

test("pending request prevents duplicate submissions and preserves the next draft", async () => {
  const { get, context, run } = setup();
  let requests = 0;
  let finish;
  context.request = () => { requests += 1; return new Promise((resolve) => { finish = resolve; }); };
  run("api = request");
  get("question").value = "第一个问题";
  const pending = get("ask-form").requestSubmit();
  assert.equal(get("question").value, "");
  assert.equal(get("send").disabled, true);
  get("question").value = "第二个问题";
  await get("ask-form").requestSubmit();
  assert.equal(requests, 1);
  assert.equal(get("question").value, "第二个问题");
  finish({});
  await pending;
  assert.equal(get("send").disabled, false);
});

test("failed request restores the question without overwriting a new draft", async () => {
  for (const nextDraft of ["", "新的草稿"]) {
    const { get, context, run } = setup();
    let fail;
    context.request = () => new Promise((resolve, reject) => { fail = reject; });
    run("api = request");
    get("question").value = "原来的问题";
    const pending = get("ask-form").requestSubmit();
    get("question").value = nextDraft;
    fail(new Error("模型暂时不可用"));
    await pending;
    assert.equal(get("question").value, nextDraft || "原来的问题");
    assert.equal(get("send").disabled, false);
  }
});

test("reopening settings removes an unsaved key", () => {
  const { get, run } = setup();
  get("set-key").value = "sk-unsaved";
  run("openSettings()");
  assert.equal(get("set-key").value, "");
  assert.equal(get("settings").open, true);
});

test("settings cancel is not the implicit Enter target", () => {
  const html = fs.readFileSync(path.join(__dirname, "../../src/studybuddy/webui/index.html"), "utf8");
  const cancel = html.match(/<button[^>]*id="btn-cancel-settings"[^>]*>/);
  assert.ok(cancel, "cancel button should keep its id");
  assert.match(cancel[0], /type="button"/);
  assert.match(html, /id="btn-save-settings"[^>]*value="default"/);
});

test("Enter in a settings field saves and closes", async () => {
  const { context, get, run } = setup();
  context.saves = 0;
  run("saveSettings = async () => { saves += 1; }");
  get("settings").open = true;
  const prevented = [];
  get("settings").handlers.keydown({
    key: "Enter",
    isComposing: false,
    target: { tagName: "INPUT" },
    preventDefault() { prevented.push(true); },
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(context.saves, 1);
  assert.equal(prevented.length, 1);
  assert.equal(get("settings").open, false);
});

test("questions send the selected memory mode", async () => {
  for (const remember of [false, true]) {
    const { get, context, run } = setup();
    let payload;
    context.request = async (path, body) => { payload = body; return {}; };
    run("api = request");
    get("remember").checked = remember;
    get("question").value = "条件概率";
    await get("ask-form").requestSubmit();
    assert.equal(payload.remember, remember);
  }
});

test("quick actions share the request guard and always release it", async () => {
  for (const action of ["runQuiz", "runSummary"]) {
    for (const fail of [false, true]) {
      const { get, context, run } = setup();
      let requests = 0;
      let finish;
      let requestPath;
      context.request = (path, body) => {
        requests += 1;
        requestPath = path;
        assert.equal(body.book, "unique-id");
        return new Promise((resolve, reject) => {
          finish = () => fail ? reject(new Error("unavailable")) : resolve({ questions: [], text: "提纲" });
        });
      };
      run("api = request; el = () => ({ append() {} })");
      get("question").value = "保留草稿";
      const pending = run(`${action}("unique-id")`);
      assert.equal(get("send").disabled, true);
      await run('runQuiz("another-id")');
      await run('runSummary("another-id")');
      await get("ask-form").requestSubmit();
      assert.equal(requests, 1);
      assert.equal(requestPath, action === "runQuiz" ? "/api/quiz" : "/api/summary");
      assert.equal(get("question").value, "保留草稿");
      finish();
      await pending;
      assert.equal(get("send").disabled, false);
    }
  }
});

test("quick actions require model configuration", async () => {
  const { get, context, run } = setup();
  context.request = () => { assert.fail("must not call the model"); };
  run('api = request; STATE.llm_ready = false');
  await run('runQuiz("unique-id")');
  await run('runSummary("unique-id")');
  assert.equal(get("settings").open, true);
  assert.equal(get("send").disabled, false);
});

test("book actions use IDs even when two books have the same title", () => {
  const { get, context, run } = setup();
  const items = [];
  get("books").append = (node) => items.push(node);
  context.makeNode = (tag, className, text) => ({
    tag, className, text, children: [], append(...nodes) { this.children.push(...nodes); },
  });
  run(`
    el = makeNode;
    STATE.books = [{ id: "first", title: "高数" }, { id: "second", title: "高数" }];
    renderBooks();
    runQuiz = (id) => { selectedQuiz = id; };
    runSummary = (id) => { selectedSummary = id; };
  `);
  items[1].children[2].children[0].onclick();
  items[1].children[2].children[1].onclick();
  assert.equal(run("selectedQuiz"), "second");
  assert.equal(run("selectedSummary"), "second");
});
