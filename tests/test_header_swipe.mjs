// Exercise the app's actual touch handlers without booting its API streams.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../web/m/app.js", import.meta.url), "utf8");
const block = source.slice(source.indexOf("let drag = null"), source.indexOf("// Typing only rewrites"));

function app(overrides = {}) {
  const handlers = new Map(), navigated = [], clicks = [], animations = [], settled = [], els = {};
  let now = 1000;
  class Element {
    constructor(header = true) { this.header = header; this.scrollLeft = 0; this.parentNode = null; }
    closest() { return this.header ? this : null; }
  }
  const target = new Element();
  // The rendered order intentionally differs from inventory order and contains a group heading.
  const list = { children: [{ _p: { pane_id: "%3" } }, {}, { _p: { pane_id: "%1" } }, { _p: { pane_id: "%2" } }] };
  const drawer = { open: false, offsetWidth: 304, style: { setProperty() {}, removeProperty() {} }, classList: { add: (c) => settled.push(c), remove() {} }, close() { this.open = false; } };
  const sandbox = {
    Element, active: "%1", view: "terminal", page: null, drawer, still: { matches: false },
    WIDE: { matches: false }, innerWidth: 390, ending: new Map(),
    panes: ["%1", "%2", "%3"].map((pane_id) => ({ pane_id })),
    location: { href: "https://example.test/m/#pane=%251&view=terminal" },
    performance: { now: () => now }, viewer: { open: false },
    document: { activeElement: null, querySelector: (sel) => drawer.open && sel.includes("dialog[open]") ? drawer : null }, standalone: () => false,
    setTimeout: (fn) => { fn(); return 0; }, clearTimeout() {}, getComputedStyle: () => ({ transform: "none" }), DOMMatrix: class { m41 = 0; },
    $: (id) => id === "pane-list" ? list : els[id] ??= { style: {}, click: () => { clicks.push(id); if (id === "menu-open") drawer.open = true; }, animate: (frames, options) => animations.push({ id, frames, options }) },
    navigate: (id, view) => { navigated.push([id, view]); sandbox.active = id; sandbox.dropGesture(); },
    addEventListener: (name, handler) => handlers.set(name, [...handlers.get(name) || [], handler]),
    ...overrides,
  };
  vm.createContext(sandbox);
  vm.runInContext(`${block}; this.dropGesture = dropGesture;`, sandbox);
  const emit = (name, x = 200, y = 50, options = {}) => {
    now += 20;
    const point = { clientX: x, clientY: y };
    const event = { target, timeStamp: now, detail: 1, touches: name === "touchend" ? [] : [point], changedTouches: [point],
      prevented: false, stopped: false,
      preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; }, ...options };
    for (const handler of handlers.get(name) || []) handler(event);
    return event;
  };
  const swipe = (dx, dy = 0, x = 200) => { emit("touchstart", x); emit("touchmove", x + dx, 50 + dy); return emit("touchend", x + dx, 50 + dy); };
  return { sandbox, navigated, clicks, animations, settled, drawer, emit, swipe, list, elsewhere: new Element(false), advance: (ms) => { now += ms; },
    shifted: () => ["overview", "terminal"].map((id) => els[id].style.transform) };
}

test("left and right follow rendered session order and keep the selected view", () => {
  const a = app();
  a.swipe(-90);
  a.swipe(90);
  a.swipe(90);
  assert.deepEqual(a.navigated, [["%2", "terminal"], ["%1", "terminal"], ["%3", "terminal"]]);
  assert.deepEqual(a.settled, []); // header swipes never move the drawer
  const summary = app({ view: "summary" });
  summary.swipe(-90);
  assert.deepEqual(summary.navigated, [["%2", "summary"]]);
});

test("list ends and an active session outside the filter do not wrap or jump", () => {
  for (const [active, dx] of [["%3", 90], ["%2", -90], ["%99", -90]]) {
    const a = app({ active });
    a.swipe(dx);
    assert.deepEqual(a.navigated, []);
  }
});

test("taps, short drags and vertical or diagonal scrolling do not navigate", () => {
  for (const [dx, dy] of [[0, 0], [-40, 0], [-20, 100], [-90, 60]]) {
    const a = app();
    a.swipe(dx, dy);
    assert.deepEqual(a.navigated, []);
  }
  const a = app();
  a.emit("touchstart");
  assert.equal(a.emit("touchmove", 195, 75).prevented, false);
  a.emit("touchmove", 100, 50); // changing direction after a scroll does not steal it
  a.emit("touchend", 100, 50);
  assert.deepEqual(a.navigated, []);
});

test("desktop, content, screen edges and open overlays leave swipes alone", () => {
  for (const overrides of [{ WIDE: { matches: true } }, { active: null }, { document: { querySelector: () => ({ open: true }) } }]) {
    const a = app(overrides);
    a.swipe(-90);
    assert.deepEqual(a.navigated, []);
  }
  for (const x of [10, 380]) {
    const a = app();
    a.swipe(x === 10 ? 90 : -90, 0, x);
    assert.deepEqual(a.navigated, []);
  }
  const a = app();
  a.emit("touchstart", 200, 50, { target: a.elsewhere });
  a.emit("touchend", 100);
  assert.deepEqual(a.navigated, []);
});

test("a PWA's left-edge swipe still goes Back", () => {
  const a = app({ standalone: () => true });
  a.swipe(90, 0, 10);
  assert.deepEqual(a.navigated, []);
  assert.deepEqual(a.clicks, ["back"]);
});

test("cancel, a second finger and navigation during a gesture cancel the swipe", () => {
  for (const cancel of [
    (a) => a.emit("touchcancel"),
    (a) => a.emit("touchstart", 200, 50, { touches: [{ clientX: 200, clientY: 50 }, { clientX: 220, clientY: 50 }] }),
    (a) => { a.sandbox.location.href += "&q=moon"; },
    (a) => a.sandbox.dropGesture(),
    (a) => { a.sandbox.WIDE.matches = true; },
  ]) {
    const a = app();
    a.emit("touchstart");
    a.emit("touchmove", 110);
    cancel(a);
    a.emit("touchend", 110);
    assert.deepEqual(a.navigated, []);
  }
});

test("a poll cannot reorder destinations under the finger or open a vanished pane", () => {
  const a = app();
  a.emit("touchstart");
  a.list.children.reverse();
  a.emit("touchend", 110);
  assert.deepEqual(a.navigated, [["%2", "terminal"]]);
  for (const remove of [
    (b) => { b.sandbox.panes = b.sandbox.panes.filter((p) => p.pane_id !== "%2"); },
    (b) => b.sandbox.ending.set("%2", {}),
  ]) {
    const b = app();
    b.emit("touchstart");
    remove(b);
    b.emit("touchend", 110);
    assert.deepEqual(b.navigated, []);
  }
});

test("ending panes are skipped when starting a gesture", () => {
  const a = app({ active: "%3", ending: new Map([["%1", {}]]) });
  a.swipe(-90);
  assert.deepEqual(a.navigated, [["%2", "terminal"]]);
});

test("horizontal swipes suppress accidental header clicks while taps and keyboard activation work", () => {
  const a = app();
  assert.equal(a.emit("click").prevented, false);
  a.emit("touchstart");
  assert.equal(a.emit("touchmove", 110).prevented, true);
  a.emit("touchend", 110);
  const accidental = a.emit("click");
  assert.equal(accidental.prevented, true);
  assert.equal(accidental.stopped, true);
  assert.equal(a.emit("click", 200, 50, { detail: 0 }).prevented, false);
  assert.equal(a.emit("click", 200, 50, { target: a.elsewhere }).prevented, false);
  a.advance(500);
  assert.equal(a.emit("click").prevented, false);
  a.swipe(-90);
  a.emit("touchstart");
  a.emit("touchend");
  assert.equal(a.emit("click").prevented, false); // a new tap need not wait for the timeout
});

test("the view follows the finger: 1:1 to the threshold, a third past it or without a session that way, capped", () => {
  const a = app();
  a.emit("touchstart");
  a.emit("touchmove", 160);
  assert.deepEqual(a.shifted(), ["translateX(-40px)", "translateX(-40px)"]);
  a.emit("touchmove", 110);
  assert.deepEqual(a.shifted(), ["translateX(-70px)", "translateX(-70px)"]);
  a.emit("touchmove", -300);
  assert.deepEqual(a.shifted(), ["translateX(-120px)", "translateX(-120px)"]);
  a.emit("touchmove", 290); // back the other way: a session lies there too
  assert.deepEqual(a.shifted(), ["translateX(70px)", "translateX(70px)"]);
  const last = app({ active: "%2" });
  last.emit("touchstart");
  last.emit("touchmove", 110);
  assert.deepEqual(last.shifted(), ["translateX(-30px)", "translateX(-30px)"]);
});

test("past the threshold the view slides out and the next pane's in; short of it, it springs back", () => {
  const a = app();
  a.emit("touchstart");
  a.emit("touchmove", 110);
  a.emit("touchend", 110);
  assert.deepEqual(a.navigated, [["%2", "terminal"]]);
  assert.deepEqual(a.shifted(), ["", ""]);
  const [out] = a.animations;
  assert.equal(a.animations.length, 2);
  assert.deepEqual([...out.frames].map((f) => [f.transform, f.opacity]), [["translateX(-70px)", 1], ["translateX(-390px)", 0], ["translateX(390px)", 0], ["none", 1]]);
  assert.equal(out.options.duration, 200);
  const b = app();
  b.emit("touchstart");
  b.emit("touchmove", 160);
  b.emit("touchend", 160);
  assert.deepEqual(b.navigated, []);
  assert.deepEqual(b.shifted(), ["", ""]);
  assert.deepEqual(structuredClone(b.animations[0].frames), [{ transform: "translateX(-40px)" }, { transform: "none" }]);
  const quiet = app({ still: { matches: true } });
  quiet.swipe(-90);
  assert.deepEqual(quiet.navigated, [["%2", "terminal"]]);
  assert.ok(quiet.animations.every((x) => x.options.duration === 0));
});

test("an installed app's left-edge swipe on the list opens the drawer and nothing else", () => {
  const a = app({ standalone: () => true, active: null });
  a.swipe(90, 0, 8);
  assert.equal(a.drawer.open, true);
  assert.deepEqual(a.clicks, ["menu-open"]);
  assert.deepEqual(a.navigated, []);
  a.swipe(-200, 0, 250); // dragged shut again, still without a Back
  assert.equal(a.drawer.open, false);
  assert.deepEqual(a.clicks, ["menu-open"]);
  const open = app({ standalone: () => true });
  open.drawer.open = true;
  open.swipe(90, 0, 8); // over an open drawer the edge drags it, never presses Back under it
  assert.deepEqual(open.clicks, []);
  assert.equal(open.drawer.open, true);
});

test("a swipe that starts vertical at the edge is a scroll: neither the drawer nor Back", () => {
  const a = app({ standalone: () => true, active: null });
  a.emit("touchstart", 8);
  a.emit("touchmove", 8, 80);
  a.emit("touchmove", 120, 80);
  a.emit("touchend", 120, 80);
  assert.equal(a.drawer.open, false);
  assert.deepEqual(a.clicks, []);
});

test("where the browser has a page to go back to, the edge is its alone", () => {
  for (const active of [null, "%1"]) {
    const a = app({ standalone: () => true, active, navigation: { canGoBack: true } });
    a.swipe(90, 0, 8);
    assert.equal(a.drawer.open, false);
    assert.deepEqual(a.clicks, []);
    assert.deepEqual(a.navigated, []);
  }
  const first = app({ standalone: () => true, navigation: { canGoBack: false } }); // the app's first screen: Back is ours
  first.swipe(90, 0, 8);
  assert.deepEqual(first.clicks, ["back"]);
});
