import assert from "node:assert/strict";
import { test } from "node:test";
import { sendBlock } from "../web/m/composer.js";

test("a draft that can go has no reason", () => {
  assert.equal(sendBlock({ sending: false, empty: false }), "");
});

test("each block names its reason, an in-flight send first", () => {
  assert.match(sendBlock({ sending: true, available: false, empty: true }), /sending/);
  assert.match(sendBlock({ sending: false, available: false, online: false, empty: false }), /Reconnecting/);
  assert.match(sendBlock({ sending: false, available: false, empty: false }), /not available/);
  assert.match(sendBlock({ sending: false, empty: true }), /Nothing to send/);
  assert.match(sendBlock({ sending: false, secret: true, empty: true }), /password/);
});
