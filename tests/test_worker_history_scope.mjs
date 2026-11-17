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

const boundCustom = {session: 'garden', tool: 'garden-runner', n: zero,
  foreground: zero, background: zero,
  bound_workers: [{state: 1, pane_session: 'garden', pane_tool: 'shell'}]};
const boundSample = {source: 'daemon', n: busy, foreground: zero, background: zero,
  groups: [garden, boundCustom]};

test('pane-bound history counts under its harness only when its pane is excluded', () => {
  const [custom] = scopeHistory({tools: ['garden-runner']}, [boundSample]);
  assert.deepEqual(custom.n, zero);
  assert.deepEqual(custom.background, busy);
  assert.deepEqual(custom.groups.map(g => g.background), [busy]); // the per-tool breakdown has it too
  for (const scope of [{}, {tools: ['shell']}, {tools: ['shell', 'garden-runner']}, {session: 'garden'}]) {
    const [included] = scopeHistory(scope, [boundSample]);
    assert.deepEqual(included.n, busy);
    assert.deepEqual(included.background, zero);
  }
  assert.deepEqual(boundSample.background, zero);
  assert.deepEqual(boundCustom.background, zero);
});

test('pane-bound history uses both pane session and tool when deduplicating', () => {
  const crossSession = {...boundSample, groups: [{...garden, session: 'orchard'},
    {...boundCustom, bound_workers: [{state: 1, pane_session: 'orchard', pane_tool: 'shell'}]}]};
  const [controller] = scopeHistory({session: 'garden'}, [crossSession]);
  assert.deepEqual(controller.n, zero);
  assert.deepEqual(controller.background, busy);
  const [pane] = scopeHistory({session: 'orchard'}, [crossSession]);
  assert.deepEqual(pane.n, busy);
  assert.deepEqual(pane.background, zero);
  assert.deepEqual(scopeHistory({tools: ['shell', 'garden-runner']}, [crossSession])[0].background, zero);
  const [unrelated] = scopeHistory({session: 'meadow', tools: ['garden-runner']}, [crossSession]);
  assert.deepEqual(unrelated.n, zero);
  assert.deepEqual(unrelated.background, zero);
});

test('pane-bound history preserves source coverage gaps and legacy observations', () => {
  const stale = {...boundSample, background: null, worker_gap_sessions: ['garden']};
  assert.equal(scopeHistory({tools: ['garden-runner']}, [stale])[0].background, null);
  const unmeasured = {...boundSample, groups: [garden, {...boundCustom, background: null}]};
  assert.equal(scopeHistory({tools: ['garden-runner']}, [unmeasured])[0].background, null);
  const legacy = {...boundSample, groups: [garden]};
  assert.deepEqual(scopeHistory({tools: ['garden-runner']}, [legacy])[0].n, zero);
  assert.deepEqual(scopeHistory({tools: ['garden-runner']}, [legacy])[0].background, zero);
});
