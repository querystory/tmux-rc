import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../web/push.js", import.meta.url), "utf8").replace(/^export /gm, "");

// Boots the bell with the browser holding `held` (a subscription or null) under
// `permission`, the daemon answering subscribe POSTs from `answers` (true: ok), then taps
// it `taps` times. Returns the label after boot and after each tap, and the requests sent.
async function bell({ permission = "granted", held = true, answers = [true], taps = 1, grant = "granted", away, dropOnAsk = false }) {
  const sent = [], listeners = {};
  let sub = held ? { endpoint: "e1", toJSON: () => ({ endpoint: "e1" }), unsubscribe: async () => { sub = null; return true; } } : null;
  const fresh = { endpoint: "e2", toJSON: () => ({ endpoint: "e2" }) };
  const button = { classList: { toggle() {} }, setAttribute() {} };
  const Notification = { permission, requestPermission: async () => { if (dropOnAsk) sub = null; return Notification.permission = grant; } };
  const ctx = {
    window: { PushManager: 1, Notification: 1 }, Notification, sessionStorage: null, crypto,
    URLSearchParams, Blob, matchMedia: () => ({ matches: true }), atob,
    Uint8Array, document: { hidden: false, addEventListener: (type, fn) => { listeners[type] = fn; } },
    navigator: { userAgent: "test", serviceWorker: { register: async () => {}, ready: Promise.resolve({
      pushManager: { getSubscription: async () => sub, subscribe: async () => (sub = fresh) } }) } },
    fetch: async (url) => {
      sent.push(url.replace("/api/push/", ""));
      if (url.endsWith("config")) return { ok: true, json: async () => ({ public_key: "AAAA" }) };
      return { ok: url.endsWith("subscribe") && !url.endsWith("unsubscribe") ? answers.shift() ?? true : true };
    },
  };
  vm.runInNewContext(`${source}; globalThis.setupPush = setupPush;`, ctx);
  await ctx.setupPush(button, () => {});
  const labels = [button.title];
  if (away) { // the user leaves for the OS's settings, changes something there and comes back
    away(Notification, () => { sub = null; });
    await listeners.visibilitychange();
    labels.push(button.title);
  }
  for (let i = 0; i < taps; i++) { await button.onclick(); labels.push(button.title); }
  return { labels, sent };
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
