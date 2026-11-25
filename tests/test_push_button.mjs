import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../web/push.js", import.meta.url), "utf8").replace(/^export /gm, "");

// Boots the bell with the browser holding `held` (a subscription or null) under
// `permission`, the daemon answering subscribe POSTs from `answers` (true: ok), then taps
// it `taps` times. Returns the label after boot and after each tap, the requests sent and what was announced.
async function bell({ permission = "granted", held = true, answers = [true], taps = 1, grant = "granted", away, dropOnAsk = false, midSave, midUnsubscribe, pendingRead, stuck = false }) {
  const sent = [], said = [], listeners = {};
  let gate, answerRead;
  let sub = held ? { endpoint: "e1", toJSON: () => ({ endpoint: "e1" }), unsubscribe: async () => { if (stuck) throw new Error("stuck"); sub = null; return true; } } : null;
  const fresh = { endpoint: "e2", toJSON: () => ({ endpoint: "e2" }) };
  const button = { classList: { toggle() {} }, setAttribute() {} };
  const Notification = { permission, requestPermission: async () => { if (dropOnAsk) sub = null; return Notification.permission = grant; } };
  const ctx = {
    window: { PushManager: 1, Notification: 1 }, Notification, sessionStorage: null, crypto,
    URLSearchParams, Blob, matchMedia: () => ({ matches: true }), atob,
    Uint8Array, document: { hidden: false, addEventListener: (type, fn) => { listeners[type] = fn; } },
    navigator: { userAgent: "test", serviceWorker: { register: async () => {}, ready: Promise.resolve({
      pushManager: { getSubscription: async () => { const g = gate; gate = null; await g; return sub; }, subscribe: async () => (sub = fresh) } }) } },
    fetch: async (url) => {
      sent.push(url.replace("/api/push/", ""));
      if (url.endsWith("/unsubscribe")) await midUnsubscribe?.(() => { sub = null; }, listeners.visibilitychange);
      else if (url.endsWith("/subscribe") && sent.length > 1) await midSave?.(() => { sub = fresh; }, listeners.visibilitychange, answerRead);
      if (url.endsWith("config")) return { ok: true, json: async () => ({ public_key: "AAAA" }) };
      return { ok: url.endsWith("subscribe") && !url.endsWith("unsubscribe") ? answers.shift() ?? true : true };
    },
  };
  vm.runInNewContext(`${source}; globalThis.setupPush = setupPush;`, ctx);
  await ctx.setupPush(button, (text) => said.push(text));
  const labels = [button.title];
  if (away) { // the user leaves for the OS's settings, changes something there and comes back
    away(Notification, () => { sub = null; }, () => { sub = fresh; });
    await listeners.visibilitychange();
    labels.push(button.title);
  }
  if (pendingRead) { // a return whose read answers only once the tap is under way
    gate = new Promise((r) => { answerRead = r; });
    listeners.visibilitychange();
  }
  for (let i = 0; i < taps; i++) { await button.onclick(); labels.push(button.title); }
  return { labels, sent, said };
}

const OFF = "Notify me when a session needs me", ON = "Turn off notifications", REPAIR = "Reconnect notifications";

test("Off: the tap asks permission, subscribes and saves, then reads On", async () => {
  const { labels, sent } = await bell({ permission: "default", held: false });
  assert.deepEqual(labels, [OFF, ON]);
  assert.deepEqual(sent, ["config", "subscribe"]);
});

test("On: the boot re-post is acknowledged, and the tap turns notifications off", async () => {
  const { labels, sent } = await bell({});
  assert.deepEqual(labels, [ON, OFF]);
  assert.deepEqual(sent, ["subscribe", "unsubscribe"]);
});

test("NeedsRepair: a failed boot re-post reads Reconnect, and the tap re-posts rather than unsubscribing", async () => {
  const { labels, sent } = await bell({ answers: [false, false, true], taps: 2 });
  assert.deepEqual(labels, [REPAIR, REPAIR, ON]);
  assert.deepEqual(sent, ["subscribe", "subscribe", "subscribe"]);
});

test("a first enable the daemon refuses leaves the subscription as NeedsRepair", async () => {
  const { labels } = await bell({ permission: "default", held: false, answers: [false] });
  assert.deepEqual(labels, [OFF, REPAIR]);
});

test("Orphaned: permission revoked in the OS reads Off, and the tap turns notifications back on", async () => {
  const { labels, sent } = await bell({ permission: "default" }); // reset in the OS, so it can be asked again
  assert.deepEqual(labels, [OFF, ON]);
  assert.deepEqual(sent, ["subscribe"]); // the held subscription is kept and saved, never unsubscribed
});

test("a permission reset in the OS shows on return, and the tap replaces the subscription the browser dropped", async () => {
  const { labels, sent } = await bell({ away: (n, drop) => { n.permission = "default"; drop(); } });
  assert.deepEqual(labels, [ON, OFF, ON]);
  assert.deepEqual(sent, ["subscribe", "config", "subscribe"]);
});

test("Orphaned whose subscription the browser drops as permission returns subscribes afresh", async () => {
  const { labels, sent } = await bell({ permission: "default", dropOnAsk: true });
  assert.deepEqual(labels, [OFF, ON]);
  assert.deepEqual(sent, ["config", "subscribe"]);
});

test("a subscription replaced elsewhere and not yet saved shows Reconnect on return, not On", async () => {
  const { labels, sent } = await bell({ away: (_n, _drop, replace) => replace() });
  assert.deepEqual(labels, [ON, REPAIR, ON]);
  assert.deepEqual(sent, ["subscribe", "subscribe"]); // the tap saves the replacement; nothing unsubscribes
});

test("a subscription replaced while a save is out is not taken as saved", async () => {
  const { labels } = await bell({ answers: [false, true], midSave: async (replace, resume) => { replace(); await resume(); } });
  assert.deepEqual(labels, [REPAIR, REPAIR]); // e1 was saved, e2 never was
});

test("a return mid-tap leaves the tap acting on the subscription it started with", async () => {
  const { labels, said } = await bell({ midUnsubscribe: async (drop, resume) => { drop(); await resume(); } });
  assert.deepEqual(labels, [ON, OFF]);
  assert.equal(said.at(-1), "Notifications turned off");
});

test("a return read still out when the tap starts cannot change what the tap saves", async () => {
  const { labels } = await bell({ answers: [false, true], pendingRead: true,
    midSave: async (replace, _resume, answerRead) => { replace(); answerRead(); await null; } });
  assert.deepEqual(labels, [REPAIR, REPAIR]); // e1 was saved; the e2 that read returned never was
});

test("a browser that fails to unsubscribe after the daemon dropped it reads Reconnect, not On", async () => {
  const { labels } = await bell({ stuck: true });
  assert.deepEqual(labels, [ON, REPAIR]);
});

// The same bell inside the native shell: the push plugin as the shell's bridge exposes it,
// answering register() with a token as its AppDelegate words it. No service worker exists.
async function shellBell({ on = false, receive = "granted", taps = 1, tapped, apns = true, status = 200, returns = 0 }) {
  const sent = [], opened = [], said = [], seen = {}, listeners = {}, kept = new Map(on ? [["tmuxrc-native-push", "1"]] : []);
  const plugin = {
    addListener: (name, fn) => { listeners[name] = fn; },
    register: async () => { queueMicrotask(() => listeners.registration({ value: "development:ab12" })); },
    checkPermissions: async () => ({ receive }), requestPermissions: async () => ({ receive: receive = "granted" }),
  };
  const store = { getItem: (k) => kept.get(k) ?? null, setItem: (k, v) => kept.set(k, v), removeItem: (k) => kept.delete(k) };
  const button = { classList: { toggle() {} }, setAttribute() {} };
  const ctx = {
    window: {}, navigator: { userAgent: "iPhone" }, matchMedia: () => ({ matches: false }), URL,
    location: { origin: "https://tmux.example.com" }, document: { hidden: false, addEventListener: (type, fn) => { seen[type] = fn; } },
    fetch: async (url, { body } = {}) => {
      if (url.endsWith("config")) return { ok: true, json: async () => ({ public_key: "AAAA", native: apns ? ["apns"] : [] }) };
      sent.push([url.replace("/api/push/", ""), JSON.parse(body)]); return { ok: status === 200, status };
    },
  };
  vm.runInNewContext(`${source}; globalThis.setupPush = setupPush; globalThis.nativePush = nativePush;`, ctx);
  await ctx.setupPush(button, (text) => said.push(text), "", ctx.nativePush(plugin, store, (...link) => opened.push(link)));
  const labels = [button.title];
  for (let i = 0; i < taps; i++) { await button.onclick(); labels.push(button.title); }
  for (let i = 0; i < returns; i++) { await seen.visibilitychange(); labels.push(button.title); }
  if (tapped) listeners.pushNotificationActionPerformed(tapped);
  return { labels, sent, opened, said };
}

test("native shell: turning on posts the device token, and turning off names the same device", async () => {
  const { labels, sent } = await shellBell({ receive: "prompt", taps: 2 });
  assert.deepEqual(labels, [OFF, ON, OFF]);
  assert.deepEqual(sent, [["subscribe", { kind: "apns", token: "ab12", env: "development" }], ["unsubscribe", { kind: "apns", token: "ab12" }]]);
});

test("native shell: a device left on re-posts its token at boot, and one never turned on posts nothing", async () => {
  assert.deepEqual((await shellBell({ on: true, taps: 0 })).sent, [["subscribe", { kind: "apns", token: "ab12", env: "development" }]]);
  assert.deepEqual((await shellBell({ taps: 0 })).sent, []);
});

test("native shell: a tapped notification opens its link on the daemon's origin only", async () => {
  const tap = (url, actionId = "tap") => shellBell({ taps: 0, tapped: { actionId, notification: { data: { url } } } });
  assert.deepEqual((await tap("/m#pane=%251")).opened, [["https://tmux.example.com/m#pane=%251", undefined]]);
  const chat = await shellBell({ taps: 0, tapped: { actionId: "tap", notification: { data: { url: "/m", chat: "card" } } } });
  assert.deepEqual(chat.opened, [["https://tmux.example.com/m", "card"]]); // the page brings that chat card back
  assert.deepEqual((await tap("https://elsewhere.example.com/m")).opened, []);
  assert.deepEqual((await tap("/m#pane=%251", "answer:0")).opened, []); // answered natively, nothing to open
});

test("native shell: a daemon with no APNs key says so, and the device is left off", async () => {
  const { labels, sent, said } = await shellBell({ receive: "prompt", apns: false });
  assert.deepEqual(labels, [OFF, OFF]);
  assert.deepEqual([sent, said], [[], ["This daemon is not set up to notify the app"]]);
});

test("native shell: a daemon that refuses the token is asked once, and the device reads Off from then on", async () => {
  const { labels, sent } = await shellBell({ on: true, status: 400, taps: 0, returns: 2 });
  assert.deepEqual(labels, [OFF, OFF, OFF]);
  assert.deepEqual(sent.map(([what]) => what), ["subscribe"]); // no retry on return, and none on the next boot: it is off
});

test("native shell: a daemon that fails otherwise keeps the device for the next boot to re-post", async () => {
  const { labels, sent } = await shellBell({ on: true, status: 502, taps: 0, returns: 1 });
  assert.deepEqual(labels, [REPAIR, REPAIR]);
  assert.deepEqual(sent.map(([what]) => what), ["subscribe"]);
});
