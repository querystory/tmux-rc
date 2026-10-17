import assert from "node:assert/strict";
import { test } from "node:test";
import { besideHeld, renderCaptureLines } from "../web/terminal.js";

const render = (text) => renderCaptureLines(text, { color: true });
// The held chunk's lines as rendered again beside the new one, then the new chunk's lines.
function shown(above, below, gap) {
  const { lines, again } = gap ? besideHeld(below, above, true, false) : besideHeld(above, below, false, false);
  return gap ? [...again.reverse(), ...lines] : [...lines, ...again];
}
const hrefs = (lines) => lines.map((l) => l.match(/href="([^"]*)"/)?.[1]);

// A URL a TUI wrapped with its own newline, cut at a chunk edge: each side, rendered against
// the other, links the whole href, as one capture of the same rows does, in its colors too.
for (const color of ["", "\x1b[31m"]) {
  test(`a hard-wrapped URL across a chunk edge stays one link${color && " in color"}`, () => {
    const above = `${color}${"a".repeat(42)} https://ex.com/very/long/pa`;
    const below = `${color}th/continues/here/${"x".repeat(41)}\nshort\n${"z".repeat(71)}`;
    const one = render(besideHeld(below, above, true, false).raw);
    assert.match(one[1], /href="https:\/\/ex.com\/very\/long\/path\/continues/);
    assert.deepEqual(shown(above, below, true), one);
    assert.deepEqual(shown(above, below, false), one);
  });
}

// Over three lines, the held fragments away from the edge change too, and `again` has them.
test("a URL wrapped over several lines relinks every held fragment", () => {
  const lines = [`${"x".repeat(30)} https://ex.com/${"a".repeat(14)}`, "b".repeat(60), `${"c".repeat(20)} tail`, "short"];
  const one = render(lines.join("\n")), href = `https://ex.com/${"a".repeat(14)}${"b".repeat(60)}${"c".repeat(20)}`;
  assert.deepEqual(hrefs(one).slice(0, 3), [href, href, href]);
  assert.deepEqual(shown(lines.slice(0, 2).join("\n"), lines.slice(2).join("\n"), true), one);
  assert.deepEqual(shown(lines[0], lines.slice(1).join("\n"), false), one);
});
