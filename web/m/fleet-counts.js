import { needsYou, isRunning, AGENT_TOOLS } from './pane-model.js';

export const STATES = ['Needs you', 'Running', 'Idle', 'Unknown', 'Compacting', 'Waiting', 'Quota blocked'];
export const stateOf = (p) => needsYou(p) ? 0 : p.activity === 'compacting' ? 4 : p.activity === 'waiting' ? 5 : p.activity === 'quota_blocked' ? 6 : isRunning(p) ? 1 : p.activity === 'idle' ? 2 : 3;

export function liveCounts(panes, workers = [], metric = 'all') {
  const counts = STATES.map(() => 0);
  panes.forEach(p => {
    counts[stateOf(p)]++;
    // Omitted means none parsed (the agent's own count still applies); null means unmeasured.
    const subs = 'subagents' in p ? p.subagents : [];
    if (metric !== 'all' || !AGENT_TOOLS.has(p.tool) || !Array.isArray(subs)) return;
    const busy = counts[1] + counts[4];
    subs.forEach(a => { if (a && a.state !== 'done') counts[stateOf({ activity: a.state, waiting_on: a.waiting_on || 'external' })]++; });
    counts[1] += Math.max(0, (p.agents || 0) - (counts[1] + counts[4] - busy));
  });
  const paneIds = new Set(panes.map(p => p.pane_id));
  if (metric === 'all') workers.forEach(w => {
    if ((!w.pane_id || !paneIds.has(w.pane_id)) && w.activity !== 'done') counts[stateOf(w)]++;
  });
  return counts;
}
