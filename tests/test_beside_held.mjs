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

// Loads one after another: each holds the raw rows `besideHeld` hands back, as loadEarlier
// does, so a URL that began rows before the edge still links whole when the next chunk lands.
test("a URL rejoined by one load survives the next, either way", () => {
  const url = [`${"x".repeat(30)} https://ex.com/${"a".repeat(14)}`, "b".repeat(60), `${"c".repeat(20)} tail`];
  const other = ["short", "z".repeat(61), "row"];
  const one = render([...url, ...other].join("\n"));
  const append = (shown, tail, rows) => {
    const r = besideHeld(rows, tail, true, false);
    return { lines: [...shown.slice(0, shown.length - r.again.length - 1), ...r.again.reverse(), ...r.lines], tail: r.raw };
  };
  let s = { lines: render(url.slice(0, 2).join("\n")), tail: url.slice(0, 2).join("\n") };
  s = append(s.lines, s.tail, url[2]);
  s = append(s.lines, s.tail, other.join("\n"));
  assert.deepEqual(s.lines, one);
  // An earlier chunk prepended, then one appended against the rows held at the bottom.
  const top = besideHeld(url[0], url.slice(1, 3).join("\n"), false, false);
  const shown = [...top.lines, ...top.again];
  assert.equal(top.raw, url.join("\n")); // every row: loadEarlier holds it at both ends then
  const next = besideHeld(other.join("\n"), top.raw, true, false);
  assert.deepEqual([...shown.slice(0, shown.length - next.again.length - 1), ...next.again.reverse(), ...next.lines], one);
});
