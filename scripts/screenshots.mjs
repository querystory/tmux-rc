// Deterministic screenshots of /m against the demo fleet (scripts/demo_fleet.py).
//
//   node scripts/screenshots.mjs <out-dir> [tree]     (or: make screenshots)
//
// Boots scripts/demo_server.py from `tree` (default: this checkout) and shoots SHOTS below.
// Same tree, same machine => same bytes: scripts/demo_browser.mjs pins everything a render
// could vary on.
import { mkdirSync, writeFileSync } from "node:fs";
import path from "node:path";
import { PHONE, WIDE, chat, settle, startDemo } from "./demo_browser.mjs";

const [out = ".screenshots", tree = process.cwd()] = process.argv.slice(2);

// name, viewport, colour scheme, URL hash, and an optional step before the shot.
const SHOTS = [
  ["hero", WIDE, "light", "#pane=%259"],
  ["wide-pane-dark", WIDE, "dark", "#pane=%259"],
  ["wide-needs-you", WIDE, "light", "#pane=%254"],
  ["wide-dashboard", WIDE, "light", "#view=dashboard"],
  ["wide-dashboard-dark", WIDE, "dark", "#view=dashboard"],
  ["wide-heatmap", WIDE, "light", "#view=dashboard", heatCard],
  ["wide-heatmap-view", WIDE, "dark", "#view=heat", heatCell],
  ["wide-usage-detail", WIDE, "dark", "#view=usage"],
  ["wide-chat", WIDE, "light", "#pane=%259", chat("Which panes need me?")],
  ["wide-subagents", WIDE, "dark", "#pane=%259", showSubagents],
  ["wide-open-loops", WIDE, "dark", "#view=loops", showLoops],
  ["wide-sessions", WIDE, "light", "#view=dashboard", reach(".atlas-multiples")],
  ["mobile-open-loops", PHONE, "light", "#view=loops", showLoops],
  ["mobile-loops-card", PHONE, "dark", "#view=dashboard", reach("#loops-card .loop-count")],
  ["mobile-sessions", PHONE, "dark", "#view=dashboard", reach(".atlas-multiples")],
  ["wide-activity", WIDE, "light", "#view=dashboard", reach(".atlas-pulse", true)],
  ["mobile-activity", PHONE, "dark", "#view=dashboard", reach(".atlas-pulse", true)],
  ["mobile-list", PHONE, "light", ""],
  ["mobile-list-dark", PHONE, "dark", ""],
  ["mobile-more-menu", PHONE, "light", "", (page) => page.click("#more-button")],
  ["mobile-usage", PHONE, "light", "", (page) => page.click("#usage")],
  ["mobile-usage-detail", PHONE, "dark", "#view=usage"],
  ["mobile-heatmap", PHONE, "light", "#view=dashboard", heatCard],
  ["mobile-pane", PHONE, "light", "#pane=%259"],
  ["mobile-needs-you", PHONE, "light", "#pane=%254"],
  ["mobile-question-items", PHONE, "light", "#pane=%2546"],
  ["mobile-menu", PHONE, "dark", "#pane=%2540"],
  ["mobile-terminal", PHONE, "light", "#pane=%259&view=terminal"],
  ["mobile-password", PHONE, "dark", "#pane=%2529&view=terminal"],
  ["mobile-pane-menu", PHONE, "dark", "#pane=%259", (page) => page.click("#pane-menu-button")],
  ["mobile-expunge", PHONE, "light", "#pane=%259", confirmExpunge],
  ["mobile-shell-menu", PHONE, "dark", "#pane=%256", (page) => page.click("#pane-menu-button")],
  ["mobile-chat-open", PHONE, "light", "", chat("Let's go back to window 1", "#voice-log .voice-open button")],
  ["mobile-chat-consent", PHONE, "light", "", chat("Tell e2e triage to rerun it headed", "#voice-log .propose .open")],
  ["mobile-chat-resume", PHONE, "light", "", chat("Resume the checkout session")],
  ["mobile-chat-minimized", PHONE, "light", "", async (page) => { await chat("Which panes need me?")(page); await page.click("#voice-close"); }],
  ["mobile-no-tmux", PHONE, "light", "", noTmux],
  ["wide-new-session", WIDE, "light", "", async (page) => { await noTmux(page); await page.click(".start-session:not([hidden])"); }],
];

// A host after a reboot, with no tmux server at all. The demo fleet always has panes, so
// its state is emptied on the way to the page.
async function noTmux(page) {
  // No timeout: the fetch may be a 25s long poll. A failure (the context closing under a
  // held poll) is dropped, not thrown — an unhandled one would end the whole run.
  await page.route(/\/api\/state/, async (route) => {
    try {
      const state = await (await route.fetch({ timeout: 0 })).json();
      await route.fulfill({ json: { ...state, panes: [], tmux_running: false } });
    } catch { /* page gone */ }
  });
  await page.reload();
  await page.waitForSelector(".start-session:not([hidden])");
}


// Bring a dashboard node to the top once it, and the Open loops card above it (which arrives
// after the page), have drawn; a base that predates either shoots the page as it is. `end`
// scrolls to the page's foot instead (Activity, which grows from its strip there).
function reach(selector, end = false) {
  return async (page) => {
    for (const node of ["#loops-card .loop-count", selector]) await page.locator(node).first().waitFor({ timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(500); // the panels above settle their height first
    await page.locator(selector).first().evaluate((el, end) => {
      const pane = document.getElementById("landing"); // scroll the dashboard, never the page
      pane.scrollTop = end ? pane.scrollHeight : pane.scrollTop + el.getBoundingClientRect().top - pane.getBoundingClientRect().top - 16;
    }, end).catch(() => {});
    if (!end) return;
    // The strip grows into the chart, which lengthens the page: follow the foot until it holds.
    for (let last = -1, i = 0; i < 10; i++) {
      await page.waitForTimeout(300);
      const top = await page.evaluate(() => { const pane = document.getElementById("landing"); pane.scrollTop = pane.scrollHeight; return pane.scrollTop; });
      if (top === last) break;
      last = top;
    }
  };
}
// The Open loops page, once its lanes arrive.
async function showLoops(page) {
  await page.locator("#landing-loops summary").first().waitFor({ timeout: 5000 }).catch(() => {});
}

// The Sub-agents toggle is on by default; show its lines under activity cards too.
async function showSubagents(page) {
  await page.locator(".sb-group", { hasText: "Working" }).locator(".sb-icon").click();
  await page.locator(".sb-card", { hasText: "terraform plan review" }).scrollIntoViewIfNeeded();
}

// The heatmap card at the dashboard's foot, once the page above it has its height.
async function heatCard(page) {
  // The word cloud above lays out at random, and at the page's foot it is in frame.
  await page.addStyleTag({ content: ".atlas-cloud { visibility: hidden; }" });
  await page.waitForSelector(".atlas-heat-card .heat-grid");
  await page.waitForTimeout(500); // the panels above settle their height first
  await page.locator(".atlas-heat-card").scrollIntoViewIfNeeded();
}
// Older days page in while the foot is near; a shot waits until the day count holds still.
async function heatSettled(page) {
  let days;
  for (let i = 0; i < 20; i++) {
    await page.waitForSelector("#dash-page .heat-foot:not([aria-busy])");
    await page.waitForTimeout(400);
    const now = await page.locator("#dash-page .heat-day").count();
    if (now === days) return;
    days = now;
  }
}
// The heatmap's own view, one cell's tip open.
async function heatCell(page) {
  await heatSettled(page);
  await page.locator("#dash-page .heat-grid .heat-cell").nth(24 + 14).click(); // yesterday, 14:00
}

async function confirmExpunge(page) {
  await page.click("#pane-menu-button");
  await page.click("#expunge-pane");
  await page.waitForSelector("#expunge-dialog[open]");
}




mkdirSync(out, { recursive: true });
const demo = await startDemo(tree);
try {
  for (const [name, viewport, colorScheme, hash, step] of SHOTS) {
    const page = await demo.open(viewport, colorScheme, hash);
    // A step written against one UI may not fit another; shoot what is there either way.
    await step?.(page).catch((e) => console.warn(`${name}: step failed, shooting as is: ${e.message}`));
    await page.evaluate(() => document.fonts.ready);
    writeFileSync(path.join(out, `${name}.png`), await settle(page, name));
    await page.context().close();
    console.log(`shot ${name}`);
  }
} finally {
  await demo.close();
}
