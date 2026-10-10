// Plan limits: each Claude and Codex account's 5h and 7d windows, from /api/usage. A phone
// gets a percentage, a bar and the reset countdown; the wide sidebar swaps each bar for the
// window's trend, dashed on to the reset at the fitted pace. See docs/design/plan-usage.md.
const NAMES = { claude: 'Claude', codex: 'Codex' };
const LOGOS = { claude: '/claude.png', codex: '/openai.svg' };
const esc = s => String(s).replace(/[&<>"]/g, c => `&#${c.charCodeAt(0)};`);

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
    + `<polyline class="proj" points="${at(held)} ${at(end)}"/></svg>`;
}

// A clock time for the detail view: the weekday as well once it is not today.
const at = (t, now) => new Date(t).toLocaleString([], { ...(new Date(t).toDateString() !== new Date(now).toDateString() && { weekday: 'short' }), hour: 'numeric', minute: '2-digit' });

// A meter that fills up: the fill is what is used, the faint stretch past it is where the
// fitted pace ends by the reset, and the tick is the even pace (the share of the window
// gone), so a fill past the tick is ahead of it. Wide, the trend says the same over time.
// Both are coloured by the projection's verdict.
function cell(w, a, wide, detail, now) {
  const projected = Math.min(100, w.projected ?? w.pct), used = Math.min(100, w.pct);
  const level = w.limit_at || w.pct >= 100 ? 'full' : projected >= 90 ? 'warn' : '';
  const when = w.resets_at ? countdown(w.resets_at - now) : '';
  const full = w.limit_at ? `full in ${countdown(w.limit_at - now)}` : '';
  const note = (full && `<small class="out">${full}${detail ? `, ${at(w.limit_at, now)}` : ''}</small>`)
    + (when && `<small>resets ${when}${detail ? `, ${at(w.resets_at, now)}` : ''}</small>`)
    + (detail && w.projected != null ? `<small>at this pace ${Math.round(w.projected)}% by the reset</small>` : '');
  const title = `${who(a)} ${w.window}: ${Math.round(w.pct)}% used${full && `, ${full}`}${when && `, resets in ${when}`}`
    + (w.projected != null ? `; at this pace ${Math.round(w.projected)}% by the reset` : '');
  const pace = w.resets_at && Math.min(100, Math.max(0, (now - w.start) / (w.resets_at - w.start) * 100));
  const bar = `<i class="usage-bar"><i style="width:${projected}%" class="ahead"></i><i style="width:${used}%"></i>`
    + (pace ? `<i class="pace" style="left:${pace.toFixed(1)}%"></i>` : '') + '</i>';
  return `<div class="usage-cell ${level}" title="${esc(title)}" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${used}" aria-valuetext="${esc(title)}">${label(a.provider, [w.window, a.label].filter(Boolean).join(' '))}<b>${Math.round(w.pct)}%</b><em>used</em>`
    + `${wide && w.resets_at ? spark(w, now) : bar}${note}</div>`;
}

// One flat grid of meters, two to a row, rather than a block per account: a weekly-only
// Codex beside Claude's three windows would leave holes. So each label carries its
// provider's icon, and after the window (so a narrow cell truncates the name, not the
// window) the account's short name when a provider has several. Accounts come
// sorted by provider, windows by name (5h, 7d, then per-model "7d Fable").
const who = a => a.label ? `${NAMES[a.provider]} ${a.label}` : NAMES[a.provider];
const label = (provider, text) => `<span><img src="${LOGOS[provider]}" alt="${NAMES[provider]}" width="14" height="14">${esc(text)}</span>`;

// Wide draws each window's trend instead of its bar; detail (the dashboard's expanded
// panel) adds the clock times behind each countdown and the pace's forecast. Options are
// named, not positional: a page holding an older copy of this module once read a
// positional `detail` flag as `now`, counting every reset from 1970 and drawing each trend
// back to its window's start.
export function renderUsage(el, accounts, { wide = false, detail = false, now = Date.now() } = {}) {
  el.hidden = !accounts.length;
  const markup = accounts.map(a => a.error ? `<div class="usage-cell none">${label(a.provider, [a.label, a.error].filter(Boolean).join(': '))}</div>`
    : a.windows.map(w => cell(w, a, wide, detail, now)).join('')).join(''); // a plan without a window shows none
  if (el._html !== markup) { el.innerHTML = markup; el._html = markup; }
}

// The phone's Usage setting: "on" is every meter, "off" none, and "auto" only the meters
// that call for a decision, those the server's projection has filling before the reset
// (limit_at) or already past HIGH_PCT; an account with none left, or unreadable, drops out.
export const HIGH_PCT = 70;
export const shownUsage = (accounts, mode) => mode === 'on' ? accounts : mode === 'off' ? []
  : accounts.map(a => ({ ...a, windows: (a.windows || []).filter(w => w.limit_at || w.pct >= HIGH_PCT) })).filter(a => !a.error && a.windows.length);

// The account a pane draws on, named only when its provider has several to tell apart.
export const paneAccount = (accounts, paneId) => accounts.find(a => a.label && a.panes.includes(paneId))?.label;
