// Exercise the upload error path without a browser or external dependencies.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const source = fs.readFileSync(path.join(__dirname, "../../src/studybuddy/webui/app.js"), "utf8");
const uploadSource = source.slice(source.indexOf("async function uploadFile("), source.indexOf("function openSettings("));

async function checkReadFailure(event, message) {
  const status = { textContent: "" };
  const alerts = [];
  let requests = 0;
  let resets = 0;
  class FailedReader {
    readAsDataURL() {
      this[event]();
    }
  }
  const upload = new Function("$", "FileReader", "api", "alert", "renderStatus", `${uploadSource}; return uploadFile;`)(
    () => status,
    FailedReader,
    () => { requests += 1; },
    (text) => alerts.push(text),
    () => { resets += 1; status.textContent = "就绪"; },
  );
  await upload({ name: "book.pdf", size: 100 });
  assert.equal(requests, 0);
  assert.equal(resets, 1);
  assert.equal(status.textContent, "就绪");
  assert.equal(alerts.length, 1);
  assert.ok(alerts[0].includes(message));
}

(async () => {
  await checkReadFailure("onerror", "读取文件失败");
  await checkReadFailure("onabort", "文件读取已取消");
  console.log("上传读取失败与取消处理通过");
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
