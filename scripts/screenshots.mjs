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
const HD = { width: 1920, height: 1080 };

// name, viewport, colour scheme, URL hash, and an optional step before the shot.
const SHOTS = [
  ["hero", WIDE, "light", "#pane=%259"],
  ["website-desktop", WIDE, "light", "#pane=%259"],
  ["website-chat", WIDE, "light", "#pane=%259", chat("Tell e2e triage to rerun it headed", "#voice-log .propose .open")],
  ["website-chat-phone", PHONE, "light", "", chat("Tell e2e triage to rerun it headed", "#voice-log .propose .open")],
  ["website-voice", WIDE, "light", "#pane=%259", voice],
  ["website-voice-phone", PHONE, "light", "", voice],
  ["website-usage", WIDE, "light", "#pane=%259", undefined, "#usage"],
  ["website-dashboard", WIDE, "light", "#view=dashboard"],
  ["website-atlas", WIDE, "light", "#view=dashboard", (page) => page.locator(".atlas-map").scrollIntoViewIfNeeded()],
  ["wide-pane-dark", WIDE, "dark", "#pane=%259"],
  ["wide-needs-you", WIDE, "light", "#pane=%254"],
  ["wide-layout-menu", WIDE, "light", "#pane=%259", (page) => page.click("#review-layout-picker summary")],
  ["wide-dashboard", WIDE, "light", "#view=dashboard"],
  ["wide-dashboard-dark", WIDE, "dark", "#view=dashboard"],
  ["wide-heatmap", WIDE, "light", "#view=dashboard", heatCard],
  ["wide-heatmap-view", WIDE, "dark", "#view=heat", heatCell],
  ["wide-usage-detail", WIDE, "dark", "#view=usage"],
  ["wide-usage-7d", WIDE, "light", "#view=usage", pickRange("7d")],
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
  ["mobile-settings", PHONE, "light", "", (page) => page.click("#settings-open")],
  ["mobile-usage", PHONE, "light", "", (page) => page.click("#usage")],
  ["mobile-usage-detail", PHONE, "dark", "#view=usage"],
  ["mobile-usage-weekly", PHONE, "light", "#view=usage", pickRange("Weekly")],
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
  // The sidebar (docs/design/sidebar.md): Chat open from the head, Settings beside it, and the rail.
  ["wide-places", WIDE, "light", "#pane=%259", (page) => page.click("#chat")],
  ["wide-settings", WIDE, "dark", "#view=dashboard", (page) => page.click("#settings-open")],
  ["wide-rail", WIDE, "light", "#pane=%259", async (page) => { await page.click("#collapse"); await page.hover("#side-list .sb-rail >> nth=2"); await page.locator("#side-hover").waitFor(); }],
  ["wide-new-session", WIDE, "light", "", async (page) => { await noTmux(page); await page.click(".start-session:not([hidden])"); }],
];

// The same fleet with a second Claude account (TMUXRC_DEMO_ACCOUNTS=2, demo_fleet.PERSONAL):
// badges, the strip by urgency, grouped Plan usage, the picker, the filter and Accounts….
// Everything above is the one-account state, which must look as it did before accounts.
const TWO_ACCOUNTS = [
  ["accounts-wide-dashboard", WIDE, "light", "#view=dashboard", two()],
  ["accounts-hd-dashboard", HD, "dark", "#view=dashboard", two()],
  ["accounts-wide-pane", WIDE, "light", "#pane=%2513", two()],
  ["accounts-wide-new-window", WIDE, "dark", "", two(async (page) => { await page.click("#new-window"); await page.click('.acct-pick [data-dir="~/.claude-personal"]'); })],
  ["accounts-wide-usage-tall", WIDE, "dark", "#pane=%2513", two(async (page) => { // the sidebar strip dragged to two rows
    const handle = page.locator("#usage-handle");
    if (!await handle.count()) return; // a base without the seam (boundingBox would wait for it)
    await handle.waitFor(); // visible: shown with the meters, which may follow the badges
    const r = await handle.boundingBox();
    await page.mouse.move(r.x + 100, r.y + 4); await page.mouse.down(); await page.mouse.move(r.x + 100, r.y - 110, { steps: 4 }); await page.mouse.up();
  })],
  ["accounts-mobile-list", PHONE, "light", "", two()],
  ["accounts-mobile-list-dark", PHONE, "dark", "", two()],
  ["accounts-mobile-settings", PHONE, "light", "", two((page) => page.click("#settings-open"))],
  ["accounts-mobile-filtered", PHONE, "dark", "", two(async (page) => { await page.click('#list-nav [data-filter="working"]'); await page.click('#acct-filter [data-acct="claude-personal"]'); })],
  ["accounts-wide-filter", WIDE, "light", "", two((page) => page.click("#side-filter-picker summary"))],
  ["accounts-wide-rail", WIDE, "dark", "#pane=%2513", two((page) => page.click("#collapse"))],
  ["accounts-wide-filtered", WIDE, "dark", "", two(async (page) => { await page.click("#side-filter-picker summary"); await page.click('#side-filter-picker [data-v="working"]'); await page.click('#acct-filter [data-acct="claude"]'); await page.click("#side-filter-picker summary"); })],
  ["accounts-mobile-dashboard", PHONE, "light", "#view=dashboard", two((page) => page.locator("#usage-panel").scrollIntoViewIfNeeded())],
  ["accounts-mobile-pane", PHONE, "light", "#pane=%2513", two()],
  ["accounts-mobile-pane-menu", PHONE, "dark", "#pane=%2513", moveMenu()],
  ["accounts-mobile-move", PHONE, "light", "#pane=%2513", moveMenu("#move-pane"), "#move-dialog"],
  ["accounts-mobile-moved", PHONE, "light", "#pane=%2513", moveMenu("#move-pane", "Exit the old pane"), "#move-dialog"],
  ["accounts-mobile-new-window", PHONE, "light", "", two(async (page) => { await page.click("#new-window"); await page.waitForSelector(".acct-pick"); })],
  ["accounts-mobile-consent", PHONE, "light", "", two(chat("Tell e2e triage to rerun it headed", "#voice-log .propose .acct-badge"))],
  ["accounts-mobile-accounts", PHONE, "dark", "", two(async (page) => { await page.click("#settings-open"); await page.click("#accounts-open"); })],
  ["accounts-mobile-add", PHONE, "light", "", two(async (page) => { await page.click("#settings-open"); await page.click("#accounts-open"); await page.click("#acct-dialog [data-add]"); await page.fill("#acct-name", "Side project"); })],
  // Adding "Demo", step by step, each cropped to its sheet (docs/design/agent-accounts.md).
  // Last, and in this order: opening the sign-in window changes the demo server's state.
  ["accounts-add-1-new-window", PHONE, "light", "", two(async (page) => { await page.click("#new-window"); await page.waitForSelector(".acct-pick"); }), "#launch-dialog"],
  ["accounts-add-2-name", PHONE, "light", "", two(addFlow("name")), "#acct-dialog"],
  ["accounts-add-3-share", PHONE, "light", "", two(addFlow("share")), "#acct-dialog"],
  ["accounts-add-4-sign-in", PHONE, "light", "", two(addFlow("login")), "#acct-dialog"],
  ["accounts-add-5-waiting", PHONE, "light", "", two(addFlow("wait")), "#acct-dialog"],
  ["accounts-add-5-sign-in-pane", PHONE, "dark", "#pane=%2590&view=terminal", two()],
  ["accounts-add-6-done", PHONE, "light", "", two(addFlow("done")), "#acct-dialog"],
  ["accounts-add-7-list", PHONE, "light", "", two((page) => page.fill("#search", "sign in"))],
];
// The add flow up to `until`: name, share, login (its step before the window opens), wait
// (the window open, signing in) or done.
function addFlow(until) {
  return async (page) => {
    await page.click("#settings-open"); await page.click("#accounts-open");
    const added = page.locator('#acct-dialog [data-remove="~/.claude-demo"]'); // by an earlier shot
    if (await added.count()) { // remove it, and wait for the list to come back without it
      await added.click(); await page.click("#acct-dialog [data-remove-ok]");
      await page.locator("#acct-dialog [data-remove-ok]").waitFor({ state: "detached" });
    }
    await page.click("#acct-dialog [data-add]");
    await page.fill("#acct-name", "Demo");
    if (until === "name") return;
    await page.click('#acct-dialog [data-goto="share"]');
    if (until === "share") return;
    await page.click('#acct-dialog [data-create="link"]'); await page.waitForSelector("#acct-session");
    if (until === "login") return;
    await page.click("#acct-dialog [data-login]"); await page.waitForSelector(".acct-wait");
    if (until === "wait") return;
    await page.waitForSelector("#acct-make-default", { timeout: 20000 });
  };
}
// The pane's "…" menu once the pane's account has loaded (the list's badges are hidden
// behind the pane on a phone, so two() can't wait on those), then `item` in it. A base
// from before accounts, or before the item, is shot as it is.
function moveMenu(item, done) {
  return async (page) => {
    if (!await page.locator('link[href="/m/accounts.js"]').count() || (item && !await page.locator(item).count())) return;
    await page.locator("#pane-location .acct-badge").waitFor();
    await page.click("#pane-menu-button");
    if (item) { await page.click(item); await page.waitForSelector("#move-dialog[open]"); }
    if (done) { await page.click("#move-choices button"); await page.getByText(done).waitFor(); } // the demo's move succeeds
  };
}

// Once the accounts have arrived (after the panes), the step. A base from before accounts
// (no accounts.js preload) is shot as it is; on any other, a missing badge fails the shot.
function two(step) {
  return async (page) => {
    if (!await page.locator('link[href="/m/accounts.js"]').count()) return;
    // Attached, not visible: a phone's dashboard has its badged rows hidden.
    await page.locator(".acct-badge").first().waitFor({ state: "attached" })
      .catch((e) => { throw Object.assign(new Error(`no account badge: ${e.message}`), { loud: true }); });
    await step?.(page);
  };
}

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

// The sub-agents count's switch is on by default; show their lines under activity cards too.
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

// The usage view at another range, or (on a phone) another window's chart.
function pickRange(label) {
  return (page) => page.locator(".usage-view .fleet-seg button", { hasText: label }).first().click();
}

async function voice(page) {
  await page.click("#live-mode");
  await page.locator("#voice-models button").click();
  await page.waitForFunction(() => document.getElementById("voice-status")?.textContent.startsWith("Listening /"));
  await page.waitForSelector("#voice-log .voice-open button");
}

// Fictional speech and a follow-up about work the user just directed. The browser's
// synthetic microphone stays active; no recording, provider, or real pane is involved.
function stubVoice(socket) {
  socket.send(JSON.stringify({ type: "status", status: "listening" }));
  let spoken = false;
  socket.onMessage((raw) => {
    if (spoken || JSON.parse(raw).action !== "audio") return;
    spoken = true;
    for (const frame of [
      { type: "transcript", role: "user", text: "Tell e2e triage to rerun the checkout test with tracing.", new_segment: true },
      { type: "transcript", role: "model", text: "Sent. I'll keep an eye on that rerun.", new_segment: true },
      { type: "typed", label: "e2e triage", pane_id: "%9", text: "Rerun the checkout test with tracing.", submitted: true },
      { type: "turn_complete" },
      { type: "transcript", role: "model", text: "The rerun reproduced the checkout failure. e2e triage is tracing the iframe timing now.", new_segment: true },
      { type: "open_pane", pane_id: "%9", label: "e2e triage" },
      { type: "turn_complete" },
    ]) socket.send(JSON.stringify(frame));
  });
}

mkdirSync(out, { recursive: true });
// One demo per account state: the demo fleet's accounts are fixed when it starts.
const demos = [];
try {
  demos.push([SHOTS, await startDemo(tree)]); // each pushed as it starts, so a later failure closes it
  demos.push([TWO_ACCOUNTS, await startDemo(tree, { TMUXRC_DEMO_ACCOUNTS: "2" })]);
  for (const [shots, demo] of demos) for (const [name, viewport, colorScheme, hash, step, crop] of shots) {
    const speaking = name === "website-voice" || name === "website-voice-phone";
    const page = await demo.open(viewport, colorScheme, hash, speaking ? stubVoice : undefined, async (page) => {
      if (speaking) await page.route("**/api/version", async (route) => {
        const response = await route.fetch(), data = await response.json();
        data.live_models.push({ label: "Demo voice", hint: "Fictional conversation", text: false });
        await route.fulfill({ response, json: data });
      });
      // The landing page shows a quieter fictional fleet so the compact session and
      // sub-agent rows fit below one actionable card. Other shots keep all states. As in
      // noTmux: no timeout on what may be a long poll, and a page gone under it is dropped.
      if (name.startsWith("website-")) await page.route("**/api/state*", async (route) => {
        try {
          const response = await route.fetch({ timeout: 0 }), data = await response.json();
          data.panes = data.panes.filter((p) => p.waiting_on !== "user" || p.title === "api contract diff");
          await route.fulfill({ response, json: data });
        } catch { /* page gone */ }
      });
    });
    // A step written against one UI may not fit another; shoot what is there, unless it says otherwise.
    await step?.(page).catch((e) => { if (e.loud) throw e; console.warn(`${name}: step failed, shooting as is: ${e.message}`); });
    await page.evaluate(() => document.fonts.ready);
    // `crop`: just that sheet, when it is open (a base without it is shot whole).
    const target = crop && await page.locator(crop).isVisible() ? page.locator(crop) : page;
    writeFileSync(path.join(out, `${name}.png`), await settle(page, name, () => target.screenshot()));
    await page.unrouteAll({ behavior: "ignoreErrors" });
    await page.context().close();
    console.log(`shot ${name}`);
  }
} finally {
  for (const [, demo] of demos) await demo.close();
}
