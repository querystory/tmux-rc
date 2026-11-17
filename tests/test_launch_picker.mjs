// With several Claude accounts, Teleport first shows the account step. Switching the session
// to New session… hides Teleport, so the step must go too: the agents come back, and Start
// can no longer post a teleport with no session. Needs the Makefile's Playwright; skipped without it.
import assert from "node:assert/strict";
import { test } from "node:test";

const demo = await import("../scripts/demo_browser.mjs").catch(() => null);

test("another session cancels a pending account step", { skip: !demo && "no Playwright" }, async () => {
  const api = await demo.startDemo(undefined, { TMUXRC_DEMO_ACCOUNTS: "2" });
  try {
    const page = await api.open(demo.WIDE, "dark");
    await page.evaluate(() => document.getElementById("new-window").click());
    await page.waitForSelector("#launch-dialog .acct-picker", { state: "attached" });
    await page.fill("#teleport-id", "session_01ExampleSeedPlanner");
    await page.evaluate(() => document.getElementById("teleport-go").click());
    const shown = () => page.evaluate(() => ({ agents: !document.getElementById("launch-choices").hidden,
      step: !document.querySelector("#launch-dialog .acct-picker").hidden }));
    assert.deepEqual(await shown(), { agents: false, step: true });
    await page.selectOption("#launch-session", "");
    assert.deepEqual(await shown(), { agents: true, step: false });
  } finally { await api.close(); }
});
