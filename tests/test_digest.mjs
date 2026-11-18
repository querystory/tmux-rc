import assert from "node:assert/strict";
import { test } from "node:test";
import { digestHtml } from "../web/m/digest.js";

const NOW = Date.parse("2026-06-16T15:30:00Z");
const summary = {
  generated: "2026-06-16T06:30:00Z", verdict: "Refunds are ready. <b>Checkout</b> is stuck.", more: 2,
  needs_you: [{ text: "Refunds can merge", href: "https://github.com/example-org/shop-api/pull/413" }, { text: "Run it", href: "javascript:alert(1)" }],
  themes: [{ name: "Notes", state: "Live on main", share: 0.1 }, { name: "Refunds", state: "Live on your machine, not on main", share: 0.5 },
    { name: "Checkout", state: "Stuck", share: 0.25 }, { name: "Money", state: "In progress", share: 0.15 }],
};

test("the card: the verdict, a strip by share and state, the largest themes named, the asks", () => {
  const html = digestHtml(summary, NOW);
  assert.match(html, /<p class="digest-verdict">Refunds are ready\. &#60;b&#62;Checkout/); // an agent wrote it: text, never markup
  assert.deepEqual([...html.matchAll(/class="digest-state (\w*)" style="flex-grow:([\d.]+)"/g)].map((m) => m.slice(1)),
    [["held", "0.5"], ["stuck", "0.25"], ["", "0.15"], ["main", "0.1"]]); // largest first
  assert.deepEqual([...html.matchAll(/<span>(\w+)<\/span>/g)].map((m) => m[1]), ["Refunds", "Checkout", "Money"]);
  assert.match(html, /href="https:\/\/github\.com\/example-org\/shop-api\/pull\/413"[^>]*>Refunds can merge</);
  assert.match(html, /href="#"[^>]*>Run it</); // no script URLs
  assert.match(html, /2 more ›/);
  assert.match(html, /as of \d+:30/);
});

test("a summary that missed a morning says so, and one with no asks has no asks", () => {
  const html = digestHtml({ ...summary, needs_you: [], generated: "2026-06-14T06:30:00Z" }, NOW);
  assert.match(html, /Out of date: last written 2d ago/);
  assert.doesNotMatch(html, /Needs you|more ›/);
});
