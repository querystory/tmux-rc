import assert from "node:assert/strict";
import { test } from "node:test";
import { renderChatMarkdown, appendChatMarkdown } from "../web/chat-markdown.js";

test("assistant replies render emphasis, headings, lists, tables and code", () => {
  const html = renderChatMarkdown("## Ready\n\n**Merge:**\n\n- PR 286\n- PR 287\n\n1. Test\n2. Review\n\n`inline`\n\n```js\nconst x = '<tag>';\n```\n\n| PR | State |\n| --- | --- |\n| 286 | Ready |");
  for (const tag of ["h2", "strong", "ul", "ol", "code", "pre", "table"]) assert.ok(html.includes(`<${tag}`), tag);
  assert.ok(html.includes("&lt;tag&gt;"));
});

test("raw HTML and hostile link schemes cannot execute", () => {
  const html = renderChatMarkdown('<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n[bad](javascript:alert%281%29) [encoded](jav&#x61;script:alert%281%29) [data](data:text/html,hi) [file](file:///etc/passwd)');
  assert.ok(html.includes("&lt;script&gt;"));
  assert.ok(!/<script|<img|href=/i.test(html));
});

test("PR links and bare web URLs open safely; images never fetch remotely", () => {
  const html = renderChatMarkdown('[PR](https://github.com/org/repo/pull/286) https://example.com\n\n![Screenshot](https://example.com/tracker.png)');
  assert.ok(html.includes('href="https://github.com/org/repo/pull/286"'));
  assert.ok(html.includes('href="https://example.com"'));
  assert.ok(html.includes('target="_blank"'));
  assert.ok(html.includes('rel="noopener noreferrer"'));
  assert.ok(!html.includes("<img"));
  assert.ok(html.includes("Screenshot"));
});

test("streamed chunks retain source delimiters and stay independent per bubble", () => {
  const previous = globalThis.requestAnimationFrame, frames = [];
  globalThis.requestAnimationFrame = (fn) => frames.push(fn);
  const node = () => ({ classList: { add() {} }, innerHTML: "" });
  const a = node(), b = node();
  try {
    let rendered = 0;
    appendChatMarkdown(a, "**Ready");
    appendChatMarkdown(b, "Other reply");
    appendChatMarkdown(a, " to merge**\n\n- PR 286", () => rendered++);
    assert.equal(frames.length, 2); // one per bubble, not one per delta
    assert.equal(a.innerHTML, "");
    frames.splice(0).forEach((fn) => fn());
    assert.equal(rendered, 1);
    assert.ok(a.innerHTML.includes("<strong>Ready to merge</strong>"));
    assert.ok(a.innerHTML.includes("<li>PR 286</li>"));
    assert.ok(!a.innerHTML.includes("Other reply"));
    assert.ok(!b.innerHTML.includes("Ready"));
    for (let i = 0; i < 2000; i++) appendChatMarkdown(a, " token");
    assert.equal(frames.length, 1);
    a.isConnected = false;
    frames[0](); // a closed conversation must not repaint or scroll its successor
    assert.ok(!a.innerHTML.includes(" token"));
  } finally { globalThis.requestAnimationFrame = previous; }
});
