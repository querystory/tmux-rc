import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../web/sw.js", import.meta.url), "utf8");

// The tap a chat push recorded for the page to pull (live.js), and whether any window had
// been focused by then.
let recorded;

// Runs sw.js against fake windows and taps a notification for `url` (a chat push also names
// its card): what each window saw.
async function tap(url, windows, chat) {
  const listeners = {}, opened = [];
  recorded = undefined;
  const caches = { open: async (name) => ({ put: async (key, response) => {
    recorded = { name, key, ...(await response.json()), focused: windows.some((w) => w.focused) };
  } }) };
  const clients = {
    claim: async () => {},
    matchAll: async () => windows,
    openWindow: async (target) => { opened.push(target); },
  };
  const self = { location: { origin: "https://host" }, addEventListener: (type, fn) => { listeners[type] = fn; } };
  vm.runInNewContext(source, { self, clients, caches, Response, URL, MessageChannel: Channel, setTimeout });
  let done;
  listeners.notificationclick({
    action: "",
    notification: { close() {}, data: { url, chat } },
    waitUntil: (promise) => { done = promise; },
  });
  await done;
  return opened;
}

// A real MessageChannel's open port would keep node running after the tests.
class Channel {
  port1 = {};
  port2 = { postMessage: (data) => this.port1.onmessage?.({ data }) };
}

// `holds`: the card id this window's chat holds, which it answers sw.js's question with.
// A `silent` window is suspended and never answers; a `gone` one closed after matchAll().
const fakeWindow = (url, { navigate = async () => {}, holds, silent, gone } = {}) => {
  const w = { url, posted: [], navigated: [], focused: 0, askedAfterFocus: [] };
  w.postMessage = (message, [port] = []) => {
    if (gone) throw new TypeError("client gone");
    w.posted.push(structuredClone(message)); // into this realm, as a real post
    if (port) w.askedAfterFocus.push(w.focused > 0);
    if (port && !silent) port.postMessage(message.chat === holds);
  };
  w.navigate = async (target) => { w.navigated.push(target); return navigate(target); };
  w.focus = async () => { w.focused += 1; };
  return w;
};

test("an open app is told which pane to open and focused, never navigated", async () => {
  const app = fakeWindow("https://host/m#pane=%251");
  const opened = await tap("/m#pane=%257&from=push", [fakeWindow("https://host/docs/"), app]);
  assert.deepEqual(app.posted, [{ open: "https://host/m#pane=%257&from=push" }]);
  assert.deepEqual(app.navigated, []);
  assert.equal(app.focused, 1);
  assert.deepEqual(opened, []);
});

test("a window that is not the app is navigated; a failure falls back to a new window", async () => {
  const docs = fakeWindow("https://host/docs/", { navigate: async () => { throw new TypeError("no"); } });
  const opened = await tap("/m#pane=%257", [docs]);
  assert.deepEqual(docs.navigated, ["https://host/m#pane=%257"]);
  assert.deepEqual(opened, ["https://host/m#pane=%257"]);
});

test("a cold start opens the app at the pane", async () => {
  assert.deepEqual(await tap("/m#pane=%257", []), ["https://host/m#pane=%257"]);
});

test("a chat tap focuses a lone app window before asking it to bring the card back", async () => {
  const app = fakeWindow("https://host/m", { holds: "c1" });
  assert.deepEqual(await tap("/m", [app], "c1"), []);
  assert.deepEqual(app.posted, [{ chat: "c1" }]); // never told to route, so the list stays put
  assert.deepEqual(app.askedAfterFocus, [true]);
  assert.equal(app.focused, 1);
  assert.deepEqual(app.navigated, []);
});

test("a chat tap is recorded for the page to pull before any window is focused", async () => {
  const start = Date.now();
  await tap("/m", [fakeWindow("https://host/m", { silent: true })], "c1"); // the message is lost
  const { at, ...rest } = recorded;
  assert.deepEqual(rest, { name: "chat-tap", key: "/__tapped-chat", chat: "c1", focused: false });
  assert.ok(at >= start && at <= Date.now());
  await tap("/m", [fakeWindow("https://host/m")]); // a pane's push records nothing
  assert.equal(recorded, undefined);
});

test("with several app windows, only the one holding the card is focused", async () => {
  const other = fakeWindow("https://host/m", { holds: "c2" }), owner = fakeWindow("https://host/m", { holds: "c1" });
  await tap("/m", [other, owner], "c1");
  assert.equal(owner.focused, 1);
  assert.equal(other.focused, 0);
  assert.deepEqual(owner.askedAfterFocus, [false]); // asked once, before the focus
});

test("the owner is focused as soon as it answers, past a suspended or closed window", async () => {
  const gone = fakeWindow("https://host/m", { gone: true }), asleep = fakeWindow("https://host/m", { silent: true });
  const owner = fakeWindow("https://host/m", { holds: "c1" }), start = Date.now();
  await tap("/m", [gone, asleep, owner], "c1");
  assert.ok(Date.now() - start < 500); // not held for the suspended window's timeout
  assert.equal(owner.focused, 1);
  assert.equal(asleep.focused + gone.focused, 0);
});
