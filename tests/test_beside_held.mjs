import assert from "node:assert/strict";
import { test } from "node:test";
import { besideHeld, renderCaptureLines } from "../web/terminal.js";

const render = (text) => renderCaptureLines(text, { color: true });
// What the live view shows once a chunk lands beside the held rows (app.js loadEarlier): the
// rows as shown, the edge replaced and the rows in `again` redrawn, then the new chunk.
function landed(shown, r, gap) {
  return gap ? [...shown.slice(0, shown.length - 1 - r.again.length), ...[...r.again].reverse(), ...r.lines]
    : [...r.lines, ...r.again, ...shown.slice(1 + r.again.length)];
}
const shown = (above, below, gap) => gap ? landed(render(above), besideHeld(below, above, true, false), true)
  : landed(render(below), besideHeld(above, below, false, false), false);
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
  const append = (s, rows) => { const r = besideHeld(rows, s.tail, true, false); return { lines: landed(s.lines, r, true), tail: r.raw }; };
  let s = { lines: render(url.slice(0, 2).join("\n")), tail: url.slice(0, 2).join("\n") };
  s = append(s, url[2]);
  s = append(s, other.join("\n"));
  assert.deepEqual(s.lines, one);
  // An earlier chunk prepended, then one appended against the rows held at the bottom.
  const top = besideHeld(url[0], url.slice(1, 3).join("\n"), false, false);
  assert.equal(top.raw, url.join("\n")); // every row: loadEarlier holds it at both ends then
  const next = besideHeld(other.join("\n"), top.raw, true, false);
  assert.deepEqual(landed(landed(render(url.slice(1, 3).join("\n")), top, false), next, true), one);
});

// Only the URL crossing the edge is redrawn: rows it does not run over stay as shown, so a
// window of held rows that would render them differently (less width evidence) leaves them be.
test("rows the edge's URL does not run over are left as shown", () => {
  const held = [`${"x".repeat(30)} https://ex.com/${"a".repeat(14)}`, "b".repeat(60), `<${"y".repeat(58)}>`, "z".repeat(60)];
  assert.deepEqual(besideHeld("short", held.join("\n"), true, false).again, []);
  assert.deepEqual(besideHeld(held.join("\n"), "short", false, false).again, []);
});

// The rows held at an end are all of them, a blank first row too.
test("the held raw rows keep a blank first row", () => {
  assert.equal(besideHeld("\nL11", null, true, false).raw, "\nL11");
  assert.equal(besideHeld("L1\n", null, false, false).raw, "L1\n");
});
