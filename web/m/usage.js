// Plan limits: each Claude and Codex account's 5h and 7d windows, from /api/usage. A phone
// gets a percentage, a bar and the reset countdown; the wide sidebar swaps each bar for the
// window's trend, dashed on to the reset at the fitted pace. See docs/design/plan-usage.md.
const WINDOWS = ['5h', '7d'];
const NAMES = { claude: 'Claude', codex: 'Codex' };
const LOGOS = { claude: '/claude.png', codex: '/openai.svg' };
const esc = s => String(s).replace(/[&<>"]/g, c => `&#${c.charCodeAt(0)};`);

export function countdown(ms) {
  const m = Math.max(0, Math.round(ms / 60000)), d = Math.floor(m / 1440), h = Math.floor(m / 60) % 24;
  return d ? `${d}d${h}h` : h ? `${h}h${m % 60}m` : `${m}m`; // statusline-compact: 2d12h, 1h8m
}

// The window's samples and, dashed, the fitted pace from the latest one to the reset, or to
// 100% if it gets there first. x spans the window, y 0..100%.
function spark(w) {
  const W = 120, H = 24, span = w.resets_at - w.start;
  const at = ([t, p]) => `${((t - w.start) / span * W).toFixed(1)},${(H - Math.min(p, 100) / 100 * H).toFixed(1)}`;
  const last = w.samples[w.samples.length - 1] || [Date.now(), w.pct];
  const end = w.limit_at ? [w.limit_at, 100] : [w.resets_at, w.projected ?? w.pct];
  return `<svg class="usage-spark" viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">`
    + `<line x1="0" y1="0.5" x2="${W}" y2="0.5" class="limit"/>`
    + `<polyline points="${[[w.start, 0], ...w.samples].map(at).join(' ')}"/>`
    + `<polyline class="proj" points="${at(last)} ${at(end)}"/></svg>`;
}

function cell(w, name, provider, wide, now) {
  if (!w) return `<div class="usage-cell none" title="${esc(`${NAMES[provider]} has no ${name} limit on this plan`)}"><span>${name}</span><b>—</b></div>`;
  const level = w.pct >= 90 ? 'full' : w.limit_at ? 'warn' : '';
  const when = w.resets_at ? countdown(w.resets_at - now) : '';
  const note = w.limit_at ? `full in ${countdown(w.limit_at - now)}` : when;
  const title = `${NAMES[provider]} ${name}: ${Math.round(w.pct)}% used${when && `, resets in ${when}`}`
    + (w.projected != null ? `; at this pace ${Math.round(w.projected)}% by the reset` : '');
  const graph = wide && w.resets_at ? spark(w) : `<i class="usage-bar"><i style="width:${Math.min(100, w.pct)}%"></i></i>`;
  return `<div class="usage-cell ${level}" title="${esc(title)}"><span>${name}</span><b>${Math.round(w.pct)}%</b><small>${esc(note)}</small>${graph}</div>`;
}

export function renderUsage(el, accounts, wide, now = Date.now()) {
  el.hidden = !accounts.length;
  const markup = accounts.map(a => {
    const name = a.label || NAMES[a.provider], by = Object.fromEntries(a.windows.map(w => [w.window, w]));
    const cells = a.error ? `<div class="usage-cell none wide">${esc(a.error)}</div>`
      : WINDOWS.map(n => cell(by[n], n, a.provider, wide, now)).join('');
    return `<div class="usage-account"><span class="usage-name"><img src="${LOGOS[a.provider]}" alt="" width="14" height="14">${esc(name)}</span>${cells}</div>`;
  }).join('');
  if (el._html !== markup) { el.innerHTML = markup; el._html = markup; }
}

// The account a pane draws on, named only when its provider has several to tell apart.
export const paneAccount = (accounts, paneId) => accounts.find(a => a.label && a.panes.includes(paneId))?.label;
