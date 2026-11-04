import assert from 'node:assert/strict';
import { test } from 'node:test';
import { scopeHistory } from '../web/m/fleet-history.js';
const zero = [0, 0, 0, 0, 0, 0, 0], busy = [0, 1, 0, 0, 0, 0, 0];
const orchard = { session: 'orchard', tool: 'claude', n: busy, foreground: busy, background: busy };
const garden = { session: 'garden', tool: 'shell', n: busy, foreground: zero, background: zero };
const sample = { source: 'daemon', n: [0, 2, 0, 0, 0, 0, 0], foreground: busy,
  background: null, groups: [orchard, garden], worker_gap_sessions: null };

test('initial unavailable source preserves a known unrelated session and pane counts', () => {
  const [scoped] = scopeHistory({session: 'orchard'}, [sample]);
  assert.deepEqual(scoped.n, busy);
  assert.deepEqual(scoped.background, busy);
  assert.equal(scopeHistory({}, [sample])[0].background, null);
  assert.equal(scopeHistory({tools: ['claude']}, [sample])[0].background, null);
  assert.equal(scopeHistory({session: 'orchard', tools: ['codex']}, [sample])[0].background, null);
});

test('expired empty roster is unmeasured for its session even without matching tool groups', () => {
  const stale = {...sample, worker_gap_sessions: ['garden'], groups: [orchard, {...garden, background: null}]};
  assert.deepEqual(scopeHistory({session: 'orchard'}, [stale])[0].background, busy);
  const [missing] = scopeHistory({session: 'garden', tools: ['codex']}, [stale]);
  assert.deepEqual(missing.n, zero);
  assert.equal(missing.background, null);
  assert.deepEqual(scopeHistory({session: 'orchard', tools: ['codex']}, [stale])[0].background, zero);
});

test('fresh empty inventory and legacy unmeasured background retain their meanings', () => {
  const fresh = {...sample, background: zero}; delete fresh.worker_gap_sessions;
  assert.deepEqual(scopeHistory({session: 'garden'}, [fresh])[0].background, zero);
  const old = {...fresh, groups: [{...orchard, background: null}]};
  assert.equal(scopeHistory({session: 'orchard'}, [old])[0].background, null);
});


test('custom harness tool filter retains its independent worker history', () => {
  const custom = {session: 'garden', tool: 'garden-runner', n: zero,
    foreground: zero, background: busy};
  const complete = {source: 'daemon', n: busy, foreground: busy,
    background: [0, 2, 0, 0, 0, 0, 0], groups: [orchard, custom]};
  const [filtered] = scopeHistory({tools: ['garden-runner']}, [complete]);
  assert.deepEqual(filtered.groups, [custom]);
  assert.deepEqual(filtered.n, zero);
  assert.deepEqual(filtered.background, busy);
});
