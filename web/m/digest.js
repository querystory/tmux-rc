// The dashboard's Digest card: what a job outside the daemon wrote this morning into the
// scratch page digest-daily (its summary.json), as a verdict, one picture of where the effort
// went (a strip: width is a theme's share, colour its state) and what needs you. No numbers.
import { since, esc } from "./pane-model.js"; // relative, so node's tests can load this module

const STATES = { "Live on main": "main", "Live on your machine, not on main": "held", "In progress": "", "Stuck": "stuck" };
const STALE = 36 * 3600e3; // a daily digest this old has missed a morning
const link = (href) => /^(https?:\/\/|\/)/.test(href) ? esc(href) : "#"; // an agent wrote the file

export function digestHtml(s, now = Date.now()) {
  const at = Date.parse(s.generated), asks = s.needs_you || [], themes = [...s.themes || []].sort((a, b) => b.share - a.share);
  const mark = (t, grow) => `<i class="digest-state ${STATES[t.state] ?? ""}"${grow ? ` style="flex-grow:${Number(t.share) || 0}"` : ""}></i>`;
  return `<p class="digest-verdict">${esc(s.verdict)}</p>`
    + `<div class="digest-strip" role="img" aria-label="${esc(themes.map((t) => `${t.name}: ${t.state}`).join(". "))}">${themes.map((t) => mark(t, true)).join("")}</div>`
    + themes.slice(0, 3).map((t) => `<div class="digest-theme">${mark(t)}<span>${esc(t.name)}</span><span class="muted">${esc(t.state)}</span></div>`).join("")
    + (asks.length ? `<div class="digest-asks"><p class="muted">Needs you</p>${asks.map((n) => `<a href="${link(n.href)}" target="_blank" rel="noopener">${esc(n.text)}</a>`).join("")}`
      + `${s.more ? `<p class="muted">${Number(s.more)} more ›</p>` : ""}</div>` : "")
    + '<p class="muted">'
    + (now - at > STALE ? `Out of date: last written ${since({ state_since: at / 1000 }, now)} ago` : `as of ${new Date(at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })}`) + "</p>";
}
