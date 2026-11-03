import { STATES } from './fleet-counts.js';
const sum = rows => rows.some(row => row == null) ? null : STATES.map((_, i) => rows.reduce((n, row) => n + (row[i] || 0), 0));

// Preserve independent pane evidence while applying source gaps only to their known sessions.
export function scopeHistory(scope, samples) {
  if (!scope.tools?.length && !scope.session) return samples;
  return samples.map(s => {
    if (s.n === null) return s;
    const groups = (s.groups || []).filter(g => (!scope.tools?.length || scope.tools.includes(g.tool)) && (!scope.session || g.session === scope.session));
    // A partial log reconstruction cannot prove a filtered fleet was empty. Keep
    // its timestamp as a gap, including at either end of the selected range.
    if (!s.groups || (s.source === 'logs' && !groups.length)) return { ...s, n: null, groups: [], source: 'gap' };
    const missing = Object.hasOwn(s, 'worker_gap_sessions') && (!scope.session || s.worker_gap_sessions?.includes(scope.session) || (s.worker_gap_sessions === null && !groups.length));
    return { ...s, groups, n: groups.reduce((counts, g) => counts.map((n, i) => n + (g.n[i] || 0)), STATES.map(() => 0)),
      foreground: s.foreground == null && !groups.length ? null : sum(groups.map(g => g.foreground)),
      background: missing ? null : Object.hasOwn(s, 'worker_gap_sessions')
        ? sum(groups.map(g => g.background))
        : s.background == null && !groups.length ? null : sum(groups.map(g => g.background)) };
  });
}
