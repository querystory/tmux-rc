// Plan limits: each Claude and Codex account's 5h and 7d windows, from /api/usage, as one
// ring card per window wherever they show (a phone's list, the sidebar, the dashboard's
// card): a percentage and the reset countdown. Trends over time are the usage view's
// (usage-view.js). See docs/design/plan-usage.md.
const NAMES = { claude: 'Claude', codex: 'Codex' };
const LOGOS = { claude: '/claude.png', codex: '/openai.svg' };
import { isRecent } from './pane-model.js';
const esc = s => String(s).replace(/[&<>"]/g, c => `&#${c.charCodeAt(0)};`);

export function countdown(ms) {
  const m = Math.max(0, Math.round(ms / 60000)), d = Math.floor(m / 1440), h = Math.floor(m / 60) % 24;
  return d ? `${d}d${h}h` : h ? `${h}h${m % 60}m` : `${m}m`; // statusline-compact: 2d12h, 1h8m
}

// A small card: a ring that fills with what is used, coloured by the projection's verdict
// (amber when the fitted pace ends at 90% or more, red when it runs out first); then "89% ·
// resets 7h30m", or "· full in 2d12h" in red; then the account and window. Nothing in it
// truncates. The tooltip holds the whole sentence.
function cell(w, a, now) {
  const level = levelOf(w);
  const live = w.resets_at > now; // a minute-old reading can hold a window that has since reset
  const when = live ? countdown(w.resets_at - now) : '';
  const full = live && w.limit_at > now ? `full in ${countdown(w.limit_at - now)}` : '';
  const stale = a.as_of ? `as of ${countdown(now - a.as_of)} ago` : ''; // the last read failed: an older reading
  const note = full ? ` <small class="out">· ${full}</small>` : when ? ` <small>· resets ${when}</small>` : '';
  const title = `${who(a)} ${w.window}: ${Math.round(w.pct)}% used${full && `, ${full}`}${when && `, resets in ${when}`}${stale && `, ${stale}`}`
    + (w.projected != null ? `; at this pace ${Math.round(w.projected)}% by the reset` : '');
  const used = Math.min(100, w.pct);
  return `<div class="usage-cell ${level}" title="${esc(title)}" style="--p:${used}"><i class="usage-ring"></i>`
    + `<b role="meter" aria-label="${esc([NAMES[a.provider], a.acct?.name ?? a.label, w.window].filter(Boolean).join(' '))}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${used}" aria-valuetext="${esc(title)}">${Math.round(w.pct)}%${note}</b>`
    + `${label(a.provider, `${who(a)} · ${w.window}`)}</div>`;
}

// One row of cards rather than a block per account: a weekly-only Codex beside Claude's
// three windows would leave holes. Each card names its provider (icon) and, with several
// accounts, its account.
const who = a => a.label ? `${NAMES[a.provider]} ${a.label}` : NAMES[a.provider];
const label = (provider, text) => `<span><img src="${LOGOS[provider]}" alt="" width="12" height="12">${esc(text)}</span>`;

// Options are named, not positional: a page holding an older copy of this module once read a
// positional flag as `now`, counting every reset from 1970.
export function renderUsage(el, accounts, { now = Date.now() } = {}) {
  el.hidden = !accounts.length;
  const markup = accounts.map(a => a.error ? `<div class="usage-cell none">${label(a.provider, `${who(a)} · ${a.error}`)}</div>`
    : a.windows.map(w => cell(w, a, now)).join('')).join(''); // a plan without a window shows none
  if (el._html !== markup) { el.innerHTML = markup; el._html = markup; }
}

// A reading kept through failed reads, said once under the meters rather than on each: the
// oldest one's age.
export const staleUsage = (accounts, now = Date.now()) => {
  const oldest = Math.min(...accounts.map(a => a.as_of || Infinity));
  return oldest < Infinity ? `<small class="usage-stale">as of ${countdown(now - oldest)} ago</small>` : '';
};

// The phone's Usage setting: "on" is every meter, "off" none, and "auto" only the meters
// that call for a decision, those the server's projection has filling before the reset
// (limit_at) or already past HIGH_PCT; an account with none left, or unreadable, drops out.
export const HIGH_PCT = 70;
export const shownUsage = (accounts, mode) => mode === 'on' ? accounts : mode === 'off' ? []
  : accounts.map(a => ({ ...a, windows: (a.windows || []).filter(w => w.limit_at || w.pct >= HIGH_PCT) })).filter(a => !a.error && a.windows.length);

// Amber where the fitted pace fills the window by its reset, red once it is full or will be
// before: the strip's meters and the pane header's chip both colour by this.
export const levelOf = w => w.limit_at || w.pct >= 100 ? 'full' : Math.min(100, w.projected ?? w.pct) >= 90 ? 'warn' : '';

// The pane header's chip: its account's 5h and 7d, as the strip colours them.
export const inlineUsage = a => (a?.windows || []).filter(w => ['5h', '7d'].includes(w.window))
  .map(w => ` · ${w.window} <b class="${levelOf(w)}">${Math.round(w.pct)}%</b>`).join('');

// Several accounts' strip names only those in use: any of their panes busy or active in the
// last hour, the most busy panes first (ties keep the meters' order). The rest are `idle`,
// for the "+N" by the strip; with none in use, every account shows rather than none.
// `panesOf(meter)` is the panes on a meter's login, across every config dir it covers.
export const IN_USE_SECS = 3600;
export function inUse(meters, panesOf, now = Date.now()) {
  const of = new Map();
  for (const m of meters) if (!of.has(m.acct)) of.set(m.acct, panesOf(m));
  const accts = [...of.keys()], busy = a => of.get(a).filter(p => p.activity !== 'idle').length;
  const used = accts.filter(a => of.get(a).some(p => isRecent(p, now, IN_USE_SECS))).sort((x, y) => busy(y) - busy(x));
  if (!used.length) return { shown: meters, idle: [] };
  return { shown: used.flatMap(a => meters.filter(m => m.acct === a)), idle: accts.filter(a => !used.includes(a)) };
}
