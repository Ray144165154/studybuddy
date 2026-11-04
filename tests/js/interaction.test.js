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
    el = () => ({});
    api = async () => ({});
  `, context);
  const get = (id) => document.getElementById(id);
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
