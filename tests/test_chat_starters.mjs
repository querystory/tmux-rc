import assert from "node:assert/strict";
import { test } from "node:test";
import { CHAT_STARTERS, chatStarters } from "../web/chat-starters.js";

test("starter buttons send read-only questions over the normal text contract", () => {
  const previous = globalThis.document;
  globalThis.document = { createElement: () => ({}) };
  try {
    const sent = [], container = { children: [], append(button) { this.children.push(button); } };
    const paint = chatStarters(container, (frame) => sent.push(frame));
    paint({ visible: true, connected: false });
    assert.equal(container.hidden, false);
    assert.ok(container.children.every((button) => button.disabled));
    paint({ visible: true, connected: true });
    for (const [index, button] of container.children.entries()) {
      assert.equal(button.type, "button");
      assert.equal(button.textContent, CHAT_STARTERS[index][0]);
      assert.equal(button.disabled, false);
      button.onclick();
      assert.deepEqual(sent[index], { action: "text", text: CHAT_STARTERS[index][1], images: [] });
    }
    paint({ visible: false, connected: true });
    assert.equal(container.hidden, true);
    assert.ok(container.children.every((button) => button.disabled));
  } finally { globalThis.document = previous; }
});
