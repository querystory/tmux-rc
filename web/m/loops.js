// Open loops on the dashboard: Waiting on you, Moving and Dropped, by workstream, from
// /api/open-loops (rendered server-side from cached GitHub and git state). Panes blocked
// on you stay in the dashboard's own Needs you list above, so they are not repeated here.
// A row's pane buttons open the window that owns it. See docs/design/open-loops.md.
import { since } from "/m/pane-model.js";

const esc = s => String(s ?? "").replace(/[&<>"]/g, c => `&#${c.charCodeAt(0)};`);
const LANES = { waiting: "Waiting on you", moving: "Moving", dropped: "Dropped" };
export const REASONS = {
  review_requested: "your review is requested", approved: "approved and green, not merged",
  mergeable: "green, no review required", no_reviewer: "no reviewer requested",
  checks_failed: "checks failing", base_merged: "base merged: retarget it",
  conflicts: "conflicts with its base", stale: "no activity in 3 days",
  idle_dirty: "idle a day over uncommitted changes", no_pane: "no pane",
};
const ref = i => `${i.repo.split("/").pop()}#${i.number}`;
const ago = (t, now) => t ? `${since({ state_since: t }, now)} ago` : ""; // the list's own age format

function panes(list, licon) {
  return list.length ? list.map(p => `<button type="button" class="loop-pane" data-pane="${esc(p.pane_id)}">`
    + `${licon("terminal", 14)}${esc(p.label || `${p.session}:${p.window_index}`)}</button>`).join("")
    : '<span class="loop-orphan">no pane</span>';
}

function row(i, licon, now) {
  const why = esc(i.reasons.map(r => REASONS[r] || r).join(" · "));
  if (i.kind === "pr") {
    return `<div class="loop-row"><a href="${esc(i.url)}" target="_blank" rel="noopener">${esc(i.title)}</a>`
      + `<span class="m">${esc(ref(i))}${i.draft ? " · draft" : ""} · ${why} · ${ago(i.at, now)}</span>`
      + `<span class="loop-panes">${panes(i.panes, licon)}</span></div>`;
  }
  if (i.kind === "pane") {
    return `<div class="loop-row"><span class="loop-panes">${panes([i.pane], licon)}</span>`
      + `<span class="m">${why} · ${i.dirty} changed files · ${ago(i.at, now)}</span></div>`;
  }
  const held = [i.dirty && `${i.dirty} uncommitted`, i.unpushed && `${i.unpushed} unpushed`].filter(Boolean);
  return `<div class="loop-row"><span class="t">${esc(i.path.split("/").slice(-2).join("/"))}</span>`
    + `<span class="m">${esc([i.branch || "detached", ...held, why].join(" · "))} · ${ago(i.at, now)}</span></div>`;
}

// Moving is one line per workstream: enough to confirm delegated work progresses.
function moved(items, now) {
  const count = k => items.filter(i => i.kind === k).length;
  const by = [...new Set(items.filter(i => i.kind === "reviewed").map(i => i.by))];
  return `<span class="m">${esc([count("merged") && `${count("merged")} merged`, count("committed") && `${count("committed")} with new commits`,
    by.length && `reviewed by ${by.join(", ")}`].filter(Boolean).join(" · "))} · ${ago(items[0].at, now)}</span>`;
}

// A workstream of one PR is just that PR: its heading would repeat the row's title.
const solo = g => g.items.length === 1 && g.items[0].title === g.workstream?.name;

export function renderLoops(el, report, open, licon, now = Date.now()) {
  const lanes = report?.lanes;
  el.hidden = !lanes;
  if (!lanes) return;
  const groups = lane => lanes[lane].map(g => ({ ...g, items: g.items.filter(i => !i.reasons?.includes("needs_you")) }))
    .filter(g => g.items.length);
  const notes = [report.fetched_at ? `GitHub as of ${ago(report.fetched_at, now)}` : "GitHub not read yet",
    report.error && "last refresh failed", report.older_open_prs && `${report.older_open_prs} older open PRs not shown`,
    report.older_worktrees && `${report.older_worktrees} older worktrees not shown`];
  // A lane the viewer folded stays folded when a minute's tick redraws the ages.
  const open0 = [...el.querySelectorAll(".loop-lane")].map(d => d.open);
  const markup = Object.entries(LANES).map(([lane, title], k) => {
    const list = groups(lane), n = list.reduce((sum, g) => sum + g.items.length, 0);
    return `<details class="loop-lane" ${open0[k] ?? lane !== "moving" ? "open" : ""}><summary><h3>${title}</h3><span class="m">${n}</span></summary>`
      + (list.map(g => `<section class="loop-ws">${lane !== "moving" && solo(g) ? "" : `<h4>${esc(g.workstream?.name || "Ungrouped")}</h4>`}`
        + (lane === "moving" ? moved(g.items, now) : g.items.map(i => row(i, licon, now)).join("")) + "</section>").join("")
        || '<p class="m">Nothing here.</p>') + "</details>";
  }).join("") + `<p class="m loop-notes">${esc(notes.filter(Boolean).join(" · "))}</p>`;
  if (el._html !== markup) { el.innerHTML = markup; el._html = markup; }
  el.onclick = e => { const b = e.target.closest("[data-pane]"); if (b) open(b.dataset.pane); };
}
