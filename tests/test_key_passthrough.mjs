import assert from "node:assert/strict";
import { test } from "node:test";
import { tmuxKey, inputQueue } from "../web/m/keys.js";

const key = (k, mods = {}) => tmuxKey({ key: k, code: "", ctrlKey: false, altKey: false, shiftKey: false, metaKey: false, isComposing: false, getModifierState: () => false, ...mods }, mods.selected, mods.mac ?? false);
const named = (keys) => ({ keys, literal: false });
const tick = () => new Promise((r) => setImmediate(r));

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
  assert.deepEqual(key("w", { ctrlKey: true, mac: true }), named("C-w")); // a Mac's browser uses Cmd-W
  assert.deepEqual(key("с", { ctrlKey: true, code: "KeyC" }), named("C-c")); // Cyrillic layout
  assert.deepEqual(key("b", { altKey: true }), named("M-b"));
  assert.deepEqual(key(" ", { altKey: true }), named("M-Space"));
  assert.deepEqual(key("∫", { altKey: true, mac: true }), { keys: "∫", literal: true }); // Mac Option
  assert.deepEqual(key("@", { altKey: true, code: "KeyL", mac: true }), { keys: "@", literal: true }); // German Mac
  assert.deepEqual(key("@", { ctrlKey: true, altKey: true, getModifierState: (m) => m === "AltGraph" }), { keys: "@", literal: true });
});

test("the browser keeps its own chords", () => {
  for (const [k, mods] of [["c", { metaKey: true }], ["v", { metaKey: true }], ["I", { ctrlKey: true, shiftKey: true }], ["V", { ctrlKey: true, shiftKey: true }], ["Delete", { ctrlKey: true, shiftKey: true }], ["F5", { ctrlKey: true, shiftKey: true }], ["Tab", { ctrlKey: true }], ["w", { ctrlKey: true }], ["t", { ctrlKey: true }], ["n", { ctrlKey: true }], ["Escape", { ctrlKey: true }], ["F4", { altKey: true }], ["PageUp", { ctrlKey: true }], ["c", { ctrlKey: true, selected: true }], ["с", { ctrlKey: true, code: "KeyC", selected: true }], ["Shift", { shiftKey: true }], ["Dead"], ["a", { isComposing: true }]]) {
    assert.equal(key(k, mods), null, `${k} ${JSON.stringify(mods)}`);
  }
});

test("keys go out in order, one at a time, with text typed meanwhile joined", async () => {
  const sent = [];
  let release;
  const push = inputQueue((op) => { sent.push(op); return new Promise((r) => { release = r; }); });
  push({ pane: "%1", keys: "l", literal: true });
  push({ pane: "%1", keys: "s", literal: true });
  push({ pane: "%1", keys: " -a", literal: true });
  push({ pane: "%1", keys: "Enter", literal: false });
  push({ pane: "%1", keys: "x", literal: true });
  assert.equal(sent.length, 1);
  for (let i = 0; i < 3; i++) { release(true); await tick(); }
  release(true); await tick();
  assert.deepEqual(sent.map((op) => op.keys), ["l", "s -a", "Enter", "x"]);
});

test("panes are independent: no joining, no waiting, and a failure drops only its own", async () => {
  const sent = [], release = {};
  const push = inputQueue((op) => { sent.push(op.keys); return new Promise((r) => { release[op.keys] = r; }); });
  push({ pane: "%1", keys: "a", literal: true });
  push({ pane: "%1", keys: "b", literal: true });
  push({ pane: "%1", keys: "Enter", literal: false });
  push({ pane: "%2", keys: "c", literal: true }); // not held behind %1's request
  assert.deepEqual(sent, ["a", "c"]);
  release.a(false); release.c(true); await tick();
  push({ pane: "%2", keys: "d", literal: true });
  assert.deepEqual(sent, ["a", "c", "d"]);
});

test("a held key's repeats wait for the queue to drain rather than piling up", async () => {
  const sent = [];
  let release;
  const push = inputQueue((op) => { sent.push(op); return new Promise((r) => { release = r; }); });
  push({ pane: "%1", keys: "BSpace", literal: false });
  for (let i = 0; i < 20; i++) push({ pane: "%1", keys: "BSpace", literal: false, repeat: true });
  release(true); await tick();
  release(true); await tick();
  assert.equal(sent.length, 2);
});

test("jobs take their turn in the same order as keys, both ways", async () => {
  const log = [];
  let release;
  const push = inputQueue((op) => { log.push(op.keys); return new Promise((r) => { release = r; }); });
  push({ pane: "%1", keys: "a", literal: true });
  let finish;
  const job = push.run("%1", () => { log.push("compose"); return new Promise((r) => { finish = r; }); });
  push({ pane: "%1", keys: "b", literal: true }); // typed after Submit
  release(true); await tick();
  assert.deepEqual(log, ["a", "compose"]);
  finish("ok"); assert.equal(await job, "ok"); await tick();
  assert.deepEqual(log, ["a", "compose", "b"]);
});

test("a failed keystroke drops the jobs queued behind it", async () => {
  let release;
  const push = inputQueue(() => new Promise((r) => { release = r; }));
  push({ pane: "%1", keys: "a", literal: true });
  let ran = false;
  const job = push.run("%1", async () => { ran = true; });
  release(false);
  await assert.rejects(job);
  assert.equal(ran, false);
});

test("a failed job drops the keys queued behind it", async () => {
  const sent = [];
  const push = inputQueue(async (op) => { sent.push(op.keys); return true; });
  let fail;
  const job = push.run("%1", () => new Promise((_, reject) => { fail = reject; }));
  push({ pane: "%1", keys: "a", literal: true });
  fail(new Error("compose failed"));
  await assert.rejects(job); await tick();
  assert.deepEqual(sent, []);
});
