// Activity on a phone's dashboard: full once its slot is reached and still full at the bottom
// (folding there shrank the page and looped), back to the strip after a jump to the top or a
// subpage. Needs the Makefile's Playwright (make screenshots installs it); skipped without it.
import assert from "node:assert/strict";
import { test } from "node:test";

const demo = await import("../scripts/demo_browser.mjs").catch(() => null);

test("Activity folds only while its slot is below the dashboard", { skip: !demo && "no Playwright" }, async () => {
  const api = await demo.startDemo();
  try {
    const page = await api.open({ width: 375, height: 700 }, "dark", "#view=dashboard");
    await page.waitForSelector(".atlas-pulse.compact");
    const folded = (on) => page.waitForFunction((on) => document.querySelector(".atlas-pulse").classList.contains("compact") === on, on);
    const scroll = (to) => page.evaluate((to) => { const l = document.getElementById("landing"); l.scrollTop = to ?? l.scrollHeight; }, to);
    const flips = await page.evaluateHandle(() => { const n = { count: 0 };
      new MutationObserver(() => n.count++).observe(document.querySelector(".atlas-pulse"), { attributeFilter: ["class"] }); return n; });
    for (let i = 0; i < 10; i++) { await scroll(); await page.waitForTimeout(100); }
    await folded(false);
    await page.waitForTimeout(1000);
    await scroll(); // the grown page's bottom, with the slot above the dashboard
    await page.waitForTimeout(500);
    assert.ok(await page.evaluate(() => document.querySelector(".atlas-pulse").getBoundingClientRect().top < document.getElementById("landing").getBoundingClientRect().top));
    assert.equal(await flips.evaluate((n) => n.count), 1); // unfolded once, then stayed
    await scroll(0); // a jump over the whole page, crossing the slot between two frames
    await folded(true);
    await scroll();
    await folded(false);
    await page.waitForTimeout(1000);
    await scroll(); // the slot above the dashboard again, so hiding it changes nothing else
    await page.evaluate(() => { location.hash = "#view=usage"; });
    await folded(true); // a subpage hides the slot
  } finally { await api.close(); }
});
