// The viewer's send sheet names the page the viewer opened, so only that document may ask.
// Page A hands whatever it holds (once, a token) to page B in a link's fragment, and B posts
// it back before its own load: no sheet may open in A's name. Needs the Makefile's Playwright
// (make screenshots installs it); skipped without it.
import assert from "node:assert/strict";
import { test } from "node:test";

const demo = await import("../scripts/demo_browser.mjs").catch(() => null);
const SANDBOX = "sandbox allow-scripts allow-popups; form-action 'none'; connect-src 'none'"; // openbus/scratch.py

test("a page the viewer's page navigates to can't ask in its name", { skip: !demo && "no Playwright" }, async () => {
  const api = await demo.startDemo();
  try {
    const ask = (token) => `parent.postMessage({ intent: "send", pane: "%1", text: "merge #12", token: ${token} }, "*")`; // the demo fleet's first claude pane
    const pages = { // synthetic pages, served as the daemon serves scratch
      "a.html": `<script>let t = ""; addEventListener("message", (e) => { t = e.data?.token || t; });
        addEventListener("load", () => setTimeout(() => { location = "b.html#" + t; }, 1500));</script>A`, // once the app has its panes
      "early.html": `<script>setTimeout(() => { location = "b.html"; }, 1500);</script><img src="slower.png">A`, // before its own load
      "b.html": `<script>${ask("location.hash.slice(1)")}</script><img src="slow.png">B`, // asks before its load
      "c.html": `<script>addEventListener("load", () => setTimeout(() => ${ask("undefined")}, 1500)); addEventListener("message", (e) => e.data?.token && ${ask("e.data.token")});</script>C`,
    };
    const open = (name) => api.open(demo.WIDE, "dark", `#view=scratch&open=/scratch/t/${name}`, undefined, async (page) => {
      await page.route("**/scratch/t/*", async (route) => {
        const name = new URL(route.request().url()).pathname.split("/").pop();
        if (name.endsWith(".png")) await new Promise((r) => setTimeout(r, name === "slow.png" ? 1500 : 4000));
        await route.fulfill({ contentType: "text/html", headers: { "content-security-policy": SANDBOX }, body: pages[name] || "" });
      });
    });
    const sheet = (page) => page.evaluate(() => document.getElementById("send-dialog").open && document.getElementById("send-where").textContent);

    for (const first of ["a.html", "early.html"]) { // B is the second document either way
      const a = await open(first);
      await a.waitForTimeout(4500); // A navigates, and B asks and loads
      assert.equal(await sheet(a), false, first);
    }
    const c = await open("c.html"); // the page opened asks for itself: the sheet names it
    await c.waitForFunction(() => document.getElementById("send-dialog").open);
    assert.match(await sheet(c), /Asked by \/scratch\/t\/c\.html\./);
  } finally { await api.close(); }
});
