// The mock harness: shows a mock's variants of the real /m, built by scripts/mock.mjs into
// data.js as window.MOCK (see docs/deploy/previews.md, "Mocks"). A classic script, because
// scratch pages cannot load modules.
//
// Each frame is the app at exactly the chosen width, at 100%: never squeezed into half the
// screen, where it would show the narrow layout instead of the one under review. So two
// options sit side by side only when both fit, and otherwise ←/→ flips between them. A
// phone shows one option full screen, swiped through.
"use strict";
const PHONE_WIDTH = 390, GAP = 16;

// Runs inside every frame, before the variant's own script: the parts each hand-built mock
// used to re-implement. Stringified, so it must not close over anything.
function bridge() {
  const root = document.documentElement, M = window.MOCK;
  root.classList.toggle("light", M.theme === "light");
  const fit = () => root.style.setProperty("--app-height", `${innerHeight}px`); // the snapshot froze its own
  fit();
  addEventListener("resize", fit);
  // A snapshot keeps a dialog's `open` but not its modality; reopen the modal ones (marked
  // by scripts/mock.mjs) in the top layer, and leave a non-modal one, like the desktop chat
  // column, where it is.
  for (const d of document.querySelectorAll("dialog[data-mock-modal]")) {
    d.close();
    d.showModal();
    d.addEventListener("cancel", (e) => e.preventDefault());
  }
  for (const el of document.querySelectorAll("[data-mock-scroll]")) [el.scrollTop, el.scrollLeft] = el.dataset.mockScroll.split(",").map(Number);
  // Keys and swipes never leave a frame, so hand the harness's own to the page that owns it.
  addEventListener("keydown", (e) => {
    if (["h", "ArrowLeft", "ArrowRight"].includes(e.key) && !e.target.closest("input, textarea, select, [contenteditable]")) parent.postMessage({ mockKey: e.key }, "*");
  });
  let t0;
  addEventListener("touchstart", (e) => { t0 = e.touches[0]; }, { passive: true });
  addEventListener("touchend", (e) => {
    const t = e.changedTouches[0], dx = t.clientX - t0.clientX, dy = t.clientY - t0.clientY;
    if (Math.abs(dx) > 60 && Math.abs(dy) < 45) parent.postMessage({ mockKey: dx < 0 ? "ArrowRight" : "ArrowLeft" }, "*");
  }, { passive: true });
}

const fits = (width, room) => 2 * width + GAP <= room;
// The widest preset that fits the screen, so a first look is the real thing with no scroll.
const widest = (widths, room) => [...widths].sort((a, b) => b - a).find((w) => w <= room) ?? Math.min(...widths);
const inject = (cfg, script) => `window.MOCK = ${JSON.stringify(cfg)};\n(${bridge})();\n${script || ""}`;

// A snapshot as a frame's document: its stylesheet and images inlined, the bridge and the
// variant's script run last, over the finished DOM.
function srcdoc(M, snap, cfg, script) {
  const html = snap.html.replace("</head>", () => `<style>${M.css[snap.css]}</style></head>`)
    .replace("</body>", () => `<script>${inject(cfg, script).replace(/<\/(script)/gi, "<\\/$1")}</script></body>`);
  return `<!doctype html>${html.replace(/@@IMG(\d+)@@/g, (_, i) => M.imgs[i])}`;
}

function boot(M) {
  const $ = (s) => document.querySelector(s);
  const store = (k, v) => { try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch { /* sandboxed: no storage */ } return v; };
  const key = (k) => `mock:${location.pathname}:${k}`;
  const phone = matchMedia("(max-width: 699px)");
  const q = new URLSearchParams(location.hash.slice(1));
  const names = M.variants.map((v) => v.name);
  const s = {
    v: [q.get("v") || names[0], q.get("b") || names[1] || names[0]],
    view: q.get("view") || M.views[0].name,
    width: +q.get("w") || widest(M.widths, innerWidth),
    ab: q.get("ab") === "1",
    theme: q.get("theme") || store(key("theme")) || (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark"),
    hidden: q.has("hide") ? q.get("hide") === "1" : store(key("hidden")) === "1",
  };
  const variant = (name) => M.variants.find((v) => v.name === name) || M.variants[0];
  const script = async (file) => !file ? "" : M.live ? (await fetch(`../${file}`)).text() : M.scripts[file];
  // A frame's snapshot is its own script (scripts/mock.mjs), fetched the first time it shows.
  const loadSnap = (k) => M.snaps[k] || new Promise((resolve) => document.head.append(Object.assign(document.createElement("script"),
    { src: `snaps/${k.replaceAll("/", "-")}.js`, onload: () => resolve(M.snaps[k]), onerror: () => resolve() })));
  const seg = (el, items, pressed, pick) => {
    el.replaceChildren(...items.map(([value, label, title]) => Object.assign(document.createElement("button"), {
      textContent: label, title: title || "", ariaPressed: String(value === pressed), onclick: () => pick(value) })));
  };

  async function frame(name, i, width) {
    const v = variant(name), cfg = { variant: v.name, view: s.view, theme: s.theme, width };
    const fig = document.createElement("figure"), cap = document.createElement("figcaption");
    const iframe = document.createElement("iframe");
    if (i === 1) { // the second slot picks its own option; the first follows the bar
      const sel = Object.assign(document.createElement("select"), { ariaLabel: "Second option" });
      sel.innerHTML = M.variants.map((x) => `<option value="${x.name}">${x.label}</option>`).join("");
      sel.value = v.name;
      sel.onchange = () => set({ v: [s.v[0], sel.value] });
      cap.append(sel, ` ${v.description || ""}`);
    } else cap.textContent = `${v.label}: ${v.description || ""}`;
    iframe.style.width = phone.matches ? "100%" : `${width}px`;
    iframe.title = v.label;
    fig.append(cap, iframe);
    const text = await script(v.script);
    if (M.live) {
      iframe.onload = async () => { // the real app: wait for its boot, take the view's steps, then patch it
        const w = iframe.contentWindow, doc = iframe.contentDocument;
        const until = async (ok) => { while (!ok()) await new Promise((r) => setTimeout(r, 100)); };
        const view = M.views.find((x) => x.name === s.view);
        await until(() => doc.documentElement.classList.contains("booted"));
        for (const sel of view.click || []) { await until(() => doc.querySelector(sel) && !doc.querySelector(sel).disabled); doc.querySelector(sel).click(); }
        if (view.wait) await until(() => doc.querySelector(view.wait));
        w.eval(inject(cfg, text));
      };
      iframe.src = `/m/${M.views.find((x) => x.name === s.view).hash || ""}`; // a chat view needs the canned socket: snapshots only
    } else {
      const snap = await loadSnap(`${s.view}/${s.theme}/${width}`);
      iframe.srcdoc = snap ? srcdoc(M, snap, cfg, text) : `<p style="font:14px sans-serif">No ${width}px snapshot of this view: rebuild the mock.</p>`;
    }
    return fig;
  }

  const sideBySide = () => !phone.matches && s.ab && fits(s.width, innerWidth);
  let shown, renders = 0;
  async function render() {
    const mine = ++renders; // frames load lazily: only the latest render may fill the stage
    const width = phone.matches ? PHONE_WIDTH : s.width, two = shown = sideBySide();
    document.body.classList.toggle("phone", phone.matches);
    document.body.classList.toggle("hidden", s.hidden);
    document.documentElement.classList.toggle("light", s.theme === "light");
    seg($("#variants"), M.variants.map((v) => [v.name, phone.matches && v.short || v.label, v.description]), s.v[0], (v) => set({ v: [v, s.v[1]] }));
    seg($("#views"), M.views.length > 1 ? M.views.map((v) => [v.name, v.label]) : [], s.view, (view) => set({ view }));
    seg($("#widths"), M.widths.map((w) => [w, String(w)]), s.width, (w) => set({ width: w }));
    for (const b of document.querySelectorAll("#bar [aria-pressed=true]")) b.scrollIntoView({ block: "nearest", inline: "nearest" }); // a phone's rows scroll
    $("#ab").disabled = !fits(width, innerWidth);
    $("#ab").ariaPressed = String(two);
    $("#theme").textContent = s.theme === "light" ? "Dark" : "Light";
    $("#desc").textContent = variant(s.v[0]).description || "";
    $("#pill").hidden = !s.hidden;
    $("#pill").textContent = `${variant(s.v[0]).label} · h`;
    const frames = await Promise.all((two ? s.v : s.v.slice(0, 1)).map((v, i) => frame(v, i, width)));
    if (mine !== renders) return;
    $("#stage").replaceChildren(...frames);
    writeHash();
  }
  function set(change) {
    Object.assign(s, change);
    store(key("theme"), s.theme);
    store(key("hidden"), s.hidden ? "1" : "0");
    render();
  }
  const step = (d) => set({ v: [names[(names.indexOf(s.v[0]) + d + names.length) % names.length], s.v[1]] });
  const onKey = (k) => k === "h" ? set({ hidden: !s.hidden }) : k === "ArrowLeft" ? step(-1) : k === "ArrowRight" ? step(1) : null;
  addEventListener("message", (e) => onKey(e.data?.mockKey));
  addEventListener("keydown", (e) => { if (!e.target.closest("select")) onKey(e.key); });
  // The hash carries the whole state, so a link (or a reload, where storage is sandboxed off) reopens it.
  function writeHash() {
    const hash = new URLSearchParams({ v: s.v[0], b: s.v[1], view: s.view, w: s.width, ab: +s.ab, theme: s.theme, hide: +s.hidden, note: +$("#note").open });
    try { history.replaceState(null, "", `#${hash}`); } catch { location.replace(`#${hash}`); } // an opaque origin may refuse the first
  }
  // A modal dialog: it takes focus, traps it, closes on Escape and hands focus back to Note.
  $("#note").onclose = () => { $("#note-btn").ariaExpanded = "false"; writeHash(); };
  $("#note-btn").onclick = () => { $("#note").showModal(); $("#note-btn").ariaExpanded = "true"; writeHash(); };
  $("#note-body").innerHTML = M.note || "";
  $("#note-btn").hidden = !M.note;
  $("#note-close").onclick = () => $("#note").close();
  $("#ab").onclick = () => set({ ab: !s.ab });
  $("#theme").onclick = () => set({ theme: s.theme === "light" ? "dark" : "light" });
  $("#hide").onclick = $("#pill").onclick = () => set({ hidden: !s.hidden });
  $("#title").textContent = document.title = M.title;
  if (M.note && q.get("note") === "1") $("#note-btn").click();
  phone.addEventListener("change", render);
  addEventListener("resize", () => { if (sideBySide() !== shown) render(); });
  render();
}

if (typeof document !== "undefined") boot(window.MOCK);
else globalThis.mockHarness = { fits, widest, srcdoc }; // tests/test_mock_harness.mjs
