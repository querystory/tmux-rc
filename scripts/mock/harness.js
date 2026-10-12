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
  // A swipe that starts on something that scrolls sideways (the usage strip, the keys row)
  // belongs to the app.
  const scrollsX = (el) => !!el?.closest && (el.scrollWidth > el.clientWidth && /auto|scroll/.test(getComputedStyle(el).overflowX) || scrollsX(el.parentElement));
  let t0;
  addEventListener("touchstart", (e) => { t0 = scrollsX(e.target) ? null : e.touches[0]; }, { passive: true });
  addEventListener("touchend", (e) => {
    if (!t0) return;
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
    // A phone starts on the app alone, controls folded into the pill.
    hidden: (q.get("hide") ?? store(key("hidden")) ?? (phone.matches ? "1" : "0")) === "1",
    state: Object.fromEntries((M.states || []).map((st) => [st.name, q.get(`s.${st.name}`) || st.options[0][0]])),
  };
  let once = {}; // state a frame asked for its next render only (a toast, a sheet left open)
  const variant = (name) => M.variants.find((v) => v.name === name) || M.variants[0];
  const script = async (file) => !file ? "" : M.live ? (await fetch(`../${file}`)).text() : M.scripts[file];
  // A frame's snapshot is its own script (scripts/mock.mjs), fetched the first time it shows.
  const loadSnap = (k) => M.snaps[k] || new Promise((resolve) => document.head.append(Object.assign(document.createElement("script"),
    { src: `snaps/${k.replaceAll("/", "-")}.js`, onload: () => resolve(M.snaps[k]), onerror: () => resolve() })));
  // A row of buttons on a desktop; on a phone, a select, so every group fits on one row.
  const seg = (el, items, pressed, pick, label) => {
    if (!phone.matches || !items.length) {
      return el.replaceChildren(...items.map(([value, text, title]) => Object.assign(document.createElement("button"), {
        textContent: text, title: title || "", ariaPressed: String(value === pressed), onclick: () => pick(value) })));
    }
    const sel = Object.assign(document.createElement("select"), { ariaLabel: label, onchange: () => pick(items[sel.selectedIndex][0]) });
    sel.append(...items.map(([value, text]) => new Option(text, value, false, value === pressed)));
    el.replaceChildren(sel);
  };

  async function frame(name, i, width, extra) {
    const v = variant(name), cfg = { variant: v.name, view: s.view, theme: s.theme, width, state: { ...s.state, ...extra }, fragments: M.fragments };
    const fig = document.createElement("figure"), cap = document.createElement("figcaption");
    const iframe = document.createElement("iframe");
    if (i === 1) { // the second slot picks its own option; the first follows the bar
      const sel = Object.assign(document.createElement("select"), { ariaLabel: "Second option" });
      sel.append(...M.variants.map((x) => new Option(x.label, x.name, false, x.name === v.name)));
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
        const until = async (ok, what) => { // as long as a snapshot build would wait, then say what never came
          for (const end = Date.now() + 10000; !ok(); await new Promise((r) => setTimeout(r, 100))) {
            if (Date.now() > end) throw new Error(`mock-live: ${what} not reached in 10s`);
          }
        };
        const view = M.views.find((x) => x.name === s.view);
        await until(() => doc.documentElement.classList.contains("booted"), "app boot");
        const shown = (sel) => doc.querySelector(sel)?.checkVisibility() && !doc.querySelector(sel).disabled; // as Playwright's actionability
        for (const sel of view.click || []) { await until(() => shown(sel), `visible ${sel}`); doc.querySelector(sel).click(); }
        if (view.wait) await until(() => shown(view.wait), `visible ${view.wait}`);
        w.eval(inject(cfg, text));
      };
      store("tmuxrc-theme", s.theme); // same origin as the app: it boots in the harness's theme, icon and all
      iframe.src = `/m/${M.views.find((x) => x.name === s.view).hash || ""}`; // a chat view needs the canned socket: snapshots only
    } else {
      const snap = await loadSnap(`${s.view}/${s.theme}/${width}`);
      iframe.srcdoc = snap ? srcdoc(M, snap, cfg, text) : `<p style="font:14px sans-serif">No ${width}px snapshot of this view: rebuild the mock.</p>`;
    }
    return fig;
  }

  const sideBySide = () => !phone.matches && s.ab && M.variants.length > 1 && fits(s.width, innerWidth);
  let shown, renders = 0;
  async function render() {
    const mine = ++renders; // frames load lazily: only the latest render may fill the stage
    const width = phone.matches ? PHONE_WIDTH : s.width, two = shown = sideBySide(), extra = once;
    once = {};
    document.body.classList.toggle("phone", phone.matches);
    document.body.classList.toggle("hidden", s.hidden);
    document.documentElement.classList.toggle("light", s.theme === "light");
    seg($("#variants"), M.variants.length > 1 ? M.variants.map((v) => [v.name, phone.matches && v.short || v.label, v.description]) : [], s.v[0], (v) => set({ v: [v, s.v[1]] }), "Option");
    seg($("#views"), M.views.length > 1 ? M.views.map((v) => [v.name, v.label]) : [], s.view, (view) => set({ view }), "View");
    $("#states").replaceChildren(...(M.states || []).map((st) => {
      const el = Object.assign(document.createElement("span"), { className: "seg", title: st.label });
      seg(el, st.options, s.state[st.name], (value) => set({ state: { ...s.state, [st.name]: value } }), st.label);
      return el;
    }));
    seg($("#widths"), M.widths.map((w) => [w, String(w)]), s.width, (w) => set({ width: w }));
    $("#ab").hidden = M.variants.length < 2;
    $("#ab").disabled = !fits(width, innerWidth);
    $("#ab").ariaPressed = String(two);
    $("#theme").textContent = s.theme === "light" ? "Dark" : "Light";
    $("#note-desc").textContent = `${variant(s.v[0]).label}: ${variant(s.v[0]).description || ""}`;
    $("#pill").hidden = !s.hidden;
    $("#pill").textContent = `${variant(s.v[0]).label} · h`;
    const frames = await Promise.all((two ? s.v : s.v.slice(0, 1)).map((v, i) => frame(v, i, width, extra)));
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
  // A frame can move the harness itself, so a prototype's own buttons navigate between views:
  // { mock: { view, theme, state, once } }.
  const go = ({ view, theme, state, once: next }) => {
    once = { ...next };
    set({ view: M.views.some((x) => x.name === view) ? view : s.view, theme: theme || s.theme, state: { ...s.state, ...state } });
  };
  addEventListener("message", (e) => e.data?.mock ? go(e.data.mock) : onKey(e.data?.mockKey));
  addEventListener("keydown", (e) => { if (!e.target.closest("select")) onKey(e.key); });
  // The hash carries the whole state, so a link (or a reload, where storage is sandboxed off) reopens it.
  function writeHash() {
    const hash = new URLSearchParams({ v: s.v[0], b: s.v[1], view: s.view, w: s.width, ab: +s.ab, theme: s.theme, hide: +s.hidden, note: +$("#note").open,
      ...Object.fromEntries(Object.entries(s.state).map(([k, v]) => [`s.${k}`, v])) });
    try { history.replaceState(null, "", `#${hash}`); } catch { location.replace(`#${hash}`); } // an opaque origin may refuse the first
  }
  // A modal dialog: it takes focus, traps it, closes on Escape and hands focus back to Note.
  $("#note").onclose = () => { $("#note-btn").ariaExpanded = "false"; writeHash(); };
  $("#note-btn").onclick = () => { $("#note").showModal(); $("#note-btn").ariaExpanded = "true"; writeHash(); };
  $("#note-body").innerHTML = M.note || "";
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
