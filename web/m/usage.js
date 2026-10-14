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
// 100% if it gets there first. x spans the window, y 0..100%. An ended window has no pace to fit.
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

// A clock time for the detail view: the weekday as well once it is not today, and the date
// once the weekday could name another week.
const at = (t, now) => new Date(t).toLocaleString([], { ...(new Date(t).toDateString() !== new Date(now).toDateString() && { weekday: 'short' }),
  ...(Math.abs(now - t) > 6 * 864e5 && { month: 'short', day: 'numeric' }), hour: 'numeric', minute: '2-digit' });

// A meter that fills up: the fill is what is used, the faint stretch past it is where the
// fitted pace ends by the reset, and the tick is the even pace (the share of the window
// gone), so a fill past the tick is ahead of it. Wide, the trend says the same over time.
// Both are coloured by the projection's verdict. An ended window (the usage view's history)
// says how it ended instead: where it stopped, or when it hit the limit, and its dates.
function cell(w, nav, a, wide, detail, now) {
  const projected = Math.min(100, w.projected ?? w.pct), used = Math.min(100, w.pct);
  const level = w.limit_at || w.pct >= 100 ? 'full' : projected >= 90 ? 'warn' : '';
  const ended = w.resets_at <= now, span = ended ? `${at(w.start, now)} – ${at(w.resets_at, now)}` : '';
  const when = w.resets_at && !ended ? countdown(w.resets_at - now) : '';
  const full = !w.limit_at ? '' : ended ? `hit the limit ${at(w.limit_at, now)}` : `full in ${countdown(w.limit_at - now)}`;
  const stale = a.as_of && !ended ? `as of ${countdown(now - a.as_of)} ago` : ''; // the last read failed: an older reading
  const note = (full && `<small class="out">${full}${detail && !ended ? `, ${at(w.limit_at, now)}` : ''}</small>`)
    + (ended && !full ? `<small>ended at ${Math.round(w.pct)}%</small>` : '') + (span && `<small>${span}</small>`)
    + (when && `<small>resets ${when}${detail ? `, ${at(w.resets_at, now)}` : ''}</small>`)
    + (detail && w.projected != null ? `<small>at this pace ${Math.round(w.projected)}% by the reset</small>` : '')
    + (stale && `<small>${stale}</small>`);
  const title = `${who(a)} ${w.window}${span && `, ${span}`}: ${Math.round(w.pct)}% used${full && `, ${full}`}${when && `, resets in ${when}`}${stale && `, ${stale}`}`
    + (w.projected != null ? `; at this pace ${Math.round(w.projected)}% by the reset` : '');
  const pace = w.resets_at && Math.min(100, Math.max(0, (now - w.start) / (w.resets_at - w.start) * 100));
  const bar = `<i class="usage-bar"><i style="width:${projected}%" class="ahead"></i><i style="width:${used}%"></i>`
    + (pace ? `<i class="pace" style="left:${pace.toFixed(1)}%"></i>` : '') + '</i>';
  // The meter is the percentage, not the cell, whose arrows a meter's role would hide.
  return `<div class="usage-cell ${level}" title="${esc(title)}">${label(a.provider, w.window)}<b role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${used}" aria-valuetext="${esc(title)}">${Math.round(w.pct)}%</b><em>used</em>${nav}`
    + `${wide && w.resets_at ? spark(w, now) : bar}<div class="usage-foot"><div>${note}</div>${a.chip || ''}</div></div>`;
}

// One flat grid of meters, two to a row, rather than a block per account: a weekly-only
// Codex beside Claude's three windows would leave holes. So each label carries its
// provider's icon, and when a provider has several accounts the meter's foot carries the
// account's chip (accounts.js), across from the reset. Accounts come sorted by provider,
// windows by name (5h, 7d, then per-model "7d Fable").
const who = a => a.label ? `${NAMES[a.provider]} ${a.label}` : NAMES[a.provider];
const label = (provider, text) => `<span><img src="${LOGOS[provider]}" alt="${NAMES[provider]}" width="14" height="14">${esc(text)}</span>`;

// The usage view steps each meter back through its ended windows (w.past, oldest first;
// only /api/usage?past=1 sends them) with arrows, or a sideways swipe on its chart. The
// window being read is kept per meter on the element by its reset, not by how far back it
// is, so a refresh across a reset still shows it; none kept is the current window.
const position = (el, k) => {
  const list = el._list[k], i = list.findIndex(w => w.resets_at === el._back[k]);
  return i < 0 ? list.length - 1 : i;
};

function stepper(el, a, w, icon) {
  const k = `${a.provider} ${a.dirs} ${w.window}`, list = el._list[k] = [...w.past, w], at = position(el, k);
  const button = (step, text, off) => `<button type="button" data-step="${step}" aria-label="${text} ${esc(w.window)} window"${off ? ' disabled' : ''}>${icon('chevron', 16)}</button>`;
  return [list[at], `<span class="usage-nav" data-k="${esc(k)}">${button(1, 'Earlier', !at)}${button(-1, 'Later', at === list.length - 1)}</span>`];
}

function steppable(el) {
  if (el._back) return;
  el._back = {}; el._list = {};
  const step = (nav, by) => {
    const k = nav.dataset.k, list = el._list[k], at = Math.min(list.length - 1, Math.max(0, position(el, k) - by));
    el._back[k] = at < list.length - 1 ? list[at].resets_at : undefined;
    el._redraw();
    return [...el.querySelectorAll('.usage-nav')].find(n => n.dataset.k === k);
  };
  el.addEventListener('click', e => {
    const b = e.target.closest('.usage-nav button');
    if (b) step(b.parentNode, +b.dataset.step)?.querySelector(`[data-step="${b.dataset.step}"]:not(:disabled), button:not(:disabled)`)?.focus();
  });
  let from = null;
  el.addEventListener('pointerdown', e => { from = e.target.closest('.usage-spark') && e; });
  el.addEventListener('pointerup', e => {
    const dx = from ? e.clientX - from.clientX : 0, nav = from?.target.closest('.usage-cell').querySelector('.usage-nav');
    if (nav && Math.abs(dx) > 40 && Math.abs(dx) > 2 * Math.abs(e.clientY - from.clientY)) step(nav, dx > 0 ? 1 : -1);
    from = null;
  });
}

// Wide draws each window's trend instead of its bar; detail (the dashboard's expanded
// panel) adds the clock times behind each countdown and the pace's forecast, and with
// `icon` (app.js's), the arrows back through past windows. Options are
// named, not positional: a page holding an older copy of this module once read a
// positional `detail` flag as `now`, counting every reset from 1970 and drawing each trend
// back to its window's start.
export function renderUsage(el, accounts, options = {}) {
  const { wide = false, detail = false, now = Date.now(), icon } = options;
  el.hidden = !accounts.length;
  if (icon) { steppable(el); el._redraw = () => renderUsage(el, accounts, options); }
  const markup = accounts.map(a => a.error ? `<div class="usage-cell none">${label(a.provider, a.error)}${a.chip || ''}</div>`
    : a.windows.map(w => cell(...(icon && w.past?.length ? stepper(el, a, w, icon) : [w, '']), a, wide, detail, now)).join('')).join(''); // a plan without a window shows none
  if (el._html !== markup) { el.innerHTML = markup; el._html = markup; }
}

// The phone's Usage setting: "on" is every meter, "off" none, and "auto" only the meters
// that call for a decision, those the server's projection has filling before the reset
// (limit_at) or already past HIGH_PCT; an account with none left, or unreadable, drops out.
export const HIGH_PCT = 70;
export const shownUsage = (accounts, mode) => mode === 'on' ? accounts : mode === 'off' ? []
  : accounts.map(a => ({ ...a, windows: (a.windows || []).filter(w => w.limit_at || w.pct >= HIGH_PCT) })).filter(a => !a.error && a.windows.length);
