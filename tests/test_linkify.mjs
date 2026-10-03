import assert from "node:assert/strict";
import { test } from "node:test";
import { linkifyText, renderCaptureLines } from "../web/terminal.js";

const hrefs = (html) => [...html.matchAll(/href="([^"]*)"/g)].map((m) => m[1]);

test("trailing sentence punctuation stays outside the link, exactly once", () => {
  for (const [text, href] of [
    ["see https://example.com/a.html, then", "https://example.com/a.html"],
    ["end https://x.y/z.", "https://x.y/z"],
    ["(https://x.y/z)", "https://x.y/z"],
    ["(https://x.y/z.),", "https://x.y/z"],
    ["**https://x.y/z**", "https://x.y/z"],
    ["https://x.y/z?q=1.", "https://x.y/z?q=1"],
    ["https://x.y/z/", "https://x.y/z/"],
    ["https://x.y/#frag", "https://x.y/#frag"],
  ]) {
    const a = `<a href="${href}" target="_blank" rel="noopener noreferrer">${href}</a>`;
    assert.equal(linkifyText(text), text.replace(href, a), text);
  }
});

test("a wrap-joined URL ending in a comma links without it", () => {
  const capture = "a".repeat(42) + " https://ex.com/very/long/pa\nth/end, " + "x".repeat(59) + "\nshort\n" + "z".repeat(71);
  const lines = renderCaptureLines(capture, {});
  assert.deepEqual(hrefs(lines.join("\n")), ["https://ex.com/very/long/path/end", "https://ex.com/very/long/path/end"]);
  assert.ok(lines[1].startsWith('<a href="https://ex.com/very/long/path/end"') && lines[1].includes("</a>, "), lines[1]);
});
