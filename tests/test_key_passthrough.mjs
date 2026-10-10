import assert from "node:assert/strict";
import { test } from "node:test";
import { tmuxKey, keyStream } from "../web/m/keys.js";

const key = (k, mods = {}) => tmuxKey({ key: k, code: "", ctrlKey: false, altKey: false, shiftKey: false, metaKey: false, isComposing: false, getModifierState: () => false, ...mods }, mods.selected);
const named = (keys) => ({ keys, literal: false });

test("named keys map to tmux names, modifiers as prefixes", () => {
  assert.deepEqual(key("ArrowUp"), named("Up"));
  assert.deepEqual(key("PageDown"), named("NPage"));
  assert.deepEqual(key("Backspace"), named("BSpace"));
  assert.deepEqual(key("Escape"), named("Escape"));
  assert.deepEqual(key("F5"), named("F5"));
  assert.deepEqual(key("ArrowLeft", { shiftKey: true }), named("S-Left"));
  assert.deepEqual(key("ArrowRight", { ctrlKey: true, altKey: true }), named("C-M-Right"));
  assert.deepEqual(key("Tab", { shiftKey: true }), named("BTab"));
  assert.deepEqual(key("Backspace", { ctrlKey: true }), named("C-w"));
});

test("printable keys are literal text, Ctrl and Alt chords are named", () => {
  assert.deepEqual(key("a"), { keys: "a", literal: true });
  assert.deepEqual(key("é"), { keys: "é", literal: true });
  assert.deepEqual(key("c", { ctrlKey: true }), named("C-c"));
  assert.deepEqual(key("R", { ctrlKey: true }), named("C-r"));
  assert.deepEqual(key(" ", { ctrlKey: true }), named("C-Space"));
  assert.deepEqual(key("v", { ctrlKey: true }), named("C-v"));
  assert.deepEqual(key("с", { ctrlKey: true, code: "KeyC" }), named("C-c")); // Cyrillic layout
  assert.deepEqual(key("b", { altKey: true }), named("M-b"));
  assert.deepEqual(key("∫", { altKey: true }), { keys: "∫", literal: true }); // Mac Option
  assert.deepEqual(key("@", { ctrlKey: true, altKey: true, getModifierState: (m) => m === "AltGraph" }), { keys: "@", literal: true });
});

test("the browser keeps its own chords", () => {
  for (const [k, mods] of [["c", { metaKey: true }], ["v", { metaKey: true }], ["I", { ctrlKey: true, shiftKey: true }], ["V", { ctrlKey: true, shiftKey: true }], ["Delete", { ctrlKey: true, shiftKey: true }], ["F5", { ctrlKey: true, shiftKey: true }], ["Tab", { ctrlKey: true }], ["Escape", { ctrlKey: true }], ["PageUp", { ctrlKey: true }], ["c", { ctrlKey: true, selected: true }], ["с", { ctrlKey: true, code: "KeyC", selected: true }], ["Shift", { shiftKey: true }], ["Dead"], ["a", { isComposing: true }]]) {
    assert.equal(key(k, mods), null, `${k} ${JSON.stringify(mods)}`);
  }
});

test("keys go out in order, one at a time, with text typed meanwhile joined", async () => {
  const sent = [];
  let release;
  const push = keyStream((op) => { sent.push(op); return new Promise((r) => { release = r; }); });
  const first = push({ pane: "%1", keys: "l", literal: true });
  push({ pane: "%1", keys: "s", literal: true });
  push({ pane: "%1", keys: " -a", literal: true });
  push({ pane: "%1", keys: "Enter", literal: false });
  push({ pane: "%1", keys: "x", literal: true });
  assert.equal(sent.length, 1);
  for (let i = 0; i < 3; i++) { release(true); await new Promise((r) => setImmediate(r)); }
  release(true); await first;
  assert.deepEqual(sent.map((op) => op.keys), ["l", "s -a", "Enter", "x"]);
});

test("text for another pane is not joined, and a failure drops the rest", async () => {
  const sent = [];
  let release;
  const push = keyStream((op) => { sent.push(op); return new Promise((r) => { release = r; }); });
  const first = push({ pane: "%1", keys: "a", literal: true });
  push({ pane: "%1", keys: "b", literal: true });
  push({ pane: "%2", keys: "c", literal: true });
  release(true); await new Promise((r) => setImmediate(r));
  release(false); await first;
  assert.deepEqual(sent.map((op) => op.keys), ["a", "b"]);
});

test("a held key's repeats wait for the queue to drain rather than piling up", async () => {
  const sent = [];
  let release;
  const push = keyStream((op) => { sent.push(op); return new Promise((r) => { release = r; }); });
  const first = push({ pane: "%1", keys: "BSpace", literal: false });
  for (let i = 0; i < 20; i++) push({ pane: "%1", keys: "BSpace", literal: false, repeat: true });
  release(true); await new Promise((r) => setImmediate(r));
  release(true); await first;
  assert.equal(sent.length, 2);
});

test("drained settles only once every queued key is out", async () => {
  let release;
  const push = keyStream(() => new Promise((r) => { release = r; }));
  let done = false;
  await push.drained(); // idle: settles at once
  push({ pane: "%1", keys: "a", literal: true });
  push({ pane: "%1", keys: "Enter", literal: false });
  push.drained().then(() => { done = true; });
  release(true); await new Promise((r) => setImmediate(r));
  assert.equal(done, false);
  release(true); await new Promise((r) => setImmediate(r));
  assert.equal(done, true);
});
