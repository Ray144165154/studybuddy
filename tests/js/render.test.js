// Minimal regression tests for the browser-side rendering helpers.
// Runs under plain node (no browser, no dependencies) and in CI.
const fs = require("fs");
const path = require("path");

const source = fs.readFileSync(path.join(__dirname, "../../src/studybuddy/webui/app.js"), "utf8");
const start = source.indexOf("function escapeHtml");
const end = source.indexOf("// ---------------------------------------------------------------------------\n// state");
if (start < 0 || end < 0) {
  console.error("无法从 app.js 中定位渲染函数");
  process.exit(1);
}
const helpers = source.slice(start, end);
const sandbox = {};
new Function("exports", helpers + "\nexports.renderMarkdown = renderMarkdown; exports.escapeHtml = escapeHtml;")(sandbox);

const { renderMarkdown } = sandbox;
let failures = 0;

function check(name, actual, expected) {
  const ok = actual === expected;
  if (!ok) {
    failures += 1;
    console.error(`✗ ${name}\n  期望: ${JSON.stringify(expected)}\n  实际: ${JSON.stringify(actual)}`);
  } else {
    console.log(`✓ ${name}`);
  }
}

function includes(name, haystack, needle) {
  if (!haystack.includes(needle)) {
    failures += 1;
    console.error(`✗ ${name}\n  未找到: ${needle}\n  实际: ${haystack}`);
  } else {
    console.log(`✓ ${name}`);
  }
}

check("直接文本段落", renderMarkdown("普通一句话"), "<p>普通一句话</p>");
check("引用角标 [1]", renderMarkdown("结论 [1]"), '<p>结论 <span class="cite" data-cite="1">1</span></p>');
check("加粗", renderMarkdown("**重点**"), "<p><b>重点</b></p>");
check("无序列表", renderMarkdown("- 甲\n- 乙"), "<ul><li>甲</li><li>乙</li></ul>");
includes("表格", renderMarkdown("| 甲 | 乙 |\n|---|---|\n| 1 | 2 |"), "<table>");
includes("LaTeX 上下标", renderMarkdown("$X_n$"), "<sub>n</sub>");
includes("LaTeX 黑板体", renderMarkdown("$\\mathbb{R}$"), "ℝ");

// 与 renderAnswer 中一致的 [Wn] 链接化逻辑
const webSources = [{ index: 1, url: "https://example.com/a", title: "示例" }];
const webByIndex = new Map(webSources.map((item) => [item.index, item]));
const withLinks = renderMarkdown("网络说法 [W1] 与 [W2]").replace(/\[W(\d{1,2})\]/g, (match, number) => {
  const item = webByIndex.get(Number(number));
  if (!item) return match;
  return `<a class="cite cite-web" href="${item.url}" target="_blank" rel="noopener noreferrer">W${number}</a>`;
});
includes("[W1] 变成链接", withLinks, 'href="https://example.com/a"');
includes("未知 [W2] 保持原样", withLinks, "[W2]");

if (failures > 0) {
  console.error(`\n${failures} 个检查失败`);
  process.exit(1);
}
console.log("\n全部通过");
