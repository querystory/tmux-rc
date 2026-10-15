// Plan limits: each Claude and Codex account's 5h and 7d windows, from /api/usage. The session
// list's strip (a phone's list top, the wide sidebar's foot) draws a small ring card per
// window (renderStrip); the dashboard's card draws a meter per window, each wide one swapping
// its bar for the window's trend, dashed on to the reset at the fitted pace (renderUsage).
// See docs/design/plan-usage.md.
const NAMES = { claude: 'Claude', codex: 'Codex' };
const LOGOS = { claude: '/claude.png', codex: '/openai.svg' };
import { esc } from './pane-model.js';

export function countdown(ms) {
  const m = Math.max(0, Math.round(ms / 60000)), d = Math.floor(m / 1440), h = Math.floor(m / 60) % 24;
  return d ? `${d}d${h}h` : h ? `${h}h${m % 60}m` : `${m}m`; // statusline-compact: 2d12h, 1h8m
}

// The window's samples held to now, the even pace (a faint diagonal from 0% at its start to 100% at
// its reset) and, dashed, the fitted pace from the latest sample on to the reset, or to
// 100% if it gets there first. x spans the window, y 0..100%.
function spark(w, now) {
  const W = 120, H = 24, span = w.resets_at - w.start;
  const at = ([t, p]) => `${((t - w.start) / span * W).toFixed(1)},${(H - Math.min(p, 100) / 100 * H).toFixed(1)}`;
  const held = [Math.min(Math.max(now, w.start), w.resets_at), w.pct]; // as project() holds it
  const end = w.limit_at ? [w.limit_at, 100] : [w.resets_at, w.projected ?? w.pct];
  return `<svg class="usage-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">`
    + `<line x1="0" y1="0.5" x2="${W}" y2="0.5" class="limit"/><line x1="0" y1="${H}" x2="${W}" y2="0" class="limit"/>`
    + `<polyline points="${[[w.start, 0], ...w.samples, held].map(at).join(' ')}"/>`
    + (w.resets_at > now ? `<polyline class="proj" points="${at(held)} ${at(end)}"/>` : '') + '</svg>';
}

// What a window's meter or card says: its level, when it resets or runs out, how old the
// reading is, and the whole sentence for its tooltip.
function reading(w, a, now) {
  const live = w.resets_at > now; // a minute-old reading can hold a window that has since reset
  const when = live ? countdown(w.resets_at - now) : '';
  const full = live && w.limit_at > now ? `full in ${countdown(w.limit_at - now)}` : '';
  const stale = a.as_of ? `as of ${countdown(now - a.as_of)} ago` : ''; // the last read failed: an older reading
  const title = `${who(a)} ${w.window}: ${Math.round(w.pct)}% used${full && `, ${full}`}${when && `, resets in ${when}`}${stale && `, ${stale}`}`
    + (w.projected != null ? `; at this pace ${Math.round(w.projected)}% by the reset` : '');
  return { used: Math.min(100, w.pct), level: w.limit_at || w.pct >= 100 ? 'full' : Math.min(100, w.projected ?? w.pct) >= 90 ? 'warn' : '', when, full, stale, title };
}

// A meter that fills up: the fill is what is used, the faint stretch past it is where the
// fitted pace ends by the reset, and the tick is the even pace (the share of the window
// gone), so a fill past the tick is ahead of it. Wide, the trend says the same over time.
// Both are coloured by the projection's verdict.
function cell(w, a, wide, now) {
  const projected = Math.min(100, w.projected ?? w.pct), { used, level, when, full, stale, title } = reading(w, a, now);
  const note = (full && `<small class="out">${full}</small>`) + (when && `<small>resets ${when}</small>`) + (stale && `<small>${stale}</small>`);
  const pace = w.resets_at && Math.min(100, Math.max(0, (now - w.start) / (w.resets_at - w.start) * 100));
  const bar = `<i class="usage-bar"><i style="width:${projected}%" class="ahead"></i><i style="width:${used}%"></i>`
    + (pace ? `<i class="pace" style="left:${pace.toFixed(1)}%"></i>` : '') + '</i>';
  return `<div class="usage-cell ${level}" title="${esc(title)}">${label(a.provider, w.window)}<b role="meter" aria-label="${esc([NAMES[a.provider], a.acct?.name ?? a.label, w.window].filter(Boolean).join(' '))}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${used}" aria-valuetext="${esc(title)}">${Math.round(w.pct)}%</b><em>used</em>`
    + `${wide && w.resets_at ? spark(w, now) : bar}<div class="usage-foot"><div>${note}</div>${a.chip || ''}</div></div>`;
}

// One flat grid of meters, two to a row, rather than a block per account: a weekly-only
// Codex beside Claude's three windows would leave holes. So each label carries its
// provider's icon, and when a provider has several accounts the meter's foot carries the
// account's chip (accounts.js), across from the reset. Accounts come sorted by provider,
// windows by name (5h, 7d, then per-model "7d Fable").
const who = a => a.label ? `${NAMES[a.provider]} ${a.label}` : NAMES[a.provider];
const label = (provider, text) => `<span><img src="${LOGOS[provider]}" alt="${NAMES[provider]}" width="14" height="14">${esc(text)}</span>`;

// Wide draws each window's trend instead of its bar. Options are named, not positional: a
// page holding an older copy of this module once read a positional flag as `now`, counting
// every reset from 1970 and drawing each trend back to its window's start.
export function renderUsage(el, accounts, options = {}) {
  const { wide = false, now = Date.now() } = options;
  el.hidden = !accounts.length;
  const markup = accounts.map(a => a.error ? `<div class="usage-cell none">${label(a.provider, a.error)}${a.chip || ''}</div>`
    : a.windows.map(w => cell(w, a, wide, now)).join('')).join(''); // a plan without a window shows none
  if (el._html !== markup) { el.innerHTML = markup; el._html = markup; }
}

// The session list's strip: one small card per window (the tab bar mock's modern pass), a ring
// that fills with what is used, "89% · resets 7h30m" (or "· full in 2d12h" in red), then the
// account by the rows' chip, or with one account the provider by its logo, and the window.
// Nothing in it truncates; the strip swipes, and the next card peeks in at its edge.
const logo = provider => `<img src="${LOGOS[provider]}" alt="" width="12" height="12">`;
function card(w, a, now) {
  const { used, level, when, full, title } = reading(w, a, now);
  const note = full ? ` <small class="out">· ${full}</small>` : when ? ` <small>· resets ${when}</small>` : '';
  return `<div class="usage-card ${level}" title="${esc(title)}" style="--p:${used}"><i class="usage-ring"></i>`
    + `<b role="meter" aria-label="${esc([NAMES[a.provider], a.acct?.name ?? a.label, w.window].filter(Boolean).join(' '))}" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${used}" aria-valuetext="${esc(title)}">${Math.round(w.pct)}%${note}</b>`
    + `<span>${a.chip || `${logo(a.provider)}${esc(who(a))}`} · ${esc(w.window)}</span></div>`;
}
export function renderStrip(el, accounts, { now = Date.now() } = {}) {
  el.hidden = !accounts.length;
  const markup = accounts.map(a => a.error ? `<div class="usage-card none"><span>${a.chip || `${logo(a.provider)}${esc(who(a))}`} · ${esc(a.error)}</span></div>`
    : a.windows.map(w => card(w, a, now)).join('')).join('');
  if (el._html !== markup) { el.innerHTML = markup; el._html = markup; }
}
// A reading kept through failed reads, said once by the strip rather than on each card: the
// oldest one's age.
export const staleUsage = (accounts, now = Date.now()) => {
  const oldest = Math.min(...accounts.map(a => a.as_of || Infinity));
  return oldest < Infinity ? `<small class="usage-stale">as of ${countdown(now - oldest)} ago</small>` : '';
};

// One entry per meter: already full first, then full soonest, then the highest pace at the
// reset; unreadable last.
const urgency = w => !w ? -1 : w.pct >= 100 ? Infinity : w.limit_at ? 1e15 - w.limit_at : w.projected ?? w.pct;
export const byUrgency = list => list.flatMap(a => a.windows?.length ? a.windows.map(w => ({ ...a, windows: [w] })) : [a])
  .sort((a, b) => urgency(b.windows?.[0]) - urgency(a.windows?.[0]));
// The phone's Usage setting: "on" is every meter, "off" none, and "auto" the meters worth a
// glance, those the server's projection has filling before the reset (limit_at) or already
// past HIGH_PCT, with the rest folded into the strip's "+N" (moreUsage); with none, the most
// urgent one still shows, so the strip never stands empty. `list` is one meter per entry,
// most urgent first (byUrgency).
export const HIGH_PCT = 50;
export function shownUsage(list, mode) {
  if (mode !== 'auto') return { shown: mode === 'on' ? list : [], quiet: [] };
  const hot = list.filter(a => a.windows?.some(w => w.limit_at || w.pct >= HIGH_PCT)), shown = hot.length ? hot : list.slice(0, 1);
  return { shown, quiet: list.filter(a => !shown.includes(a)) };
}
// The strip's last card: the quiet meters as "+N", named in its tooltip; a tap opens Plan usage.
export function moreUsage(quiet) {
  const names = quiet.flatMap(a => a.error ? [who(a)] : a.windows.map(w => `${who(a)} ${w.window} ${Math.round(w.pct)}%`));
  return names.length ? `<button type="button" id="usage-more" title="${esc(`Quiet: ${names.join(', ')}`)}">+${names.length}</button>` : '';
}
