"""Exercise the shipped worklet callback without a microphone or provider connection."""

import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_muted_live_audio_keeps_clock_without_leaking_late_samples():
    script = r"""
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const source = fs.readFileSync(process.argv[1], "utf8");
const start = source.indexOf("    let pending = new Float32Array(0);");
const end = source.indexOf("    source.connect(tap);", start);
assert.ok(start >= 0 && end > start, "capture callback moved; update the extraction");
const sent = [];
const current = { muted: false, listening: true, frameMs: 40,
  ws: {readyState: 1, bufferedAmount: 0, send: (wire) => sent.push(JSON.parse(wire))} };
const tap = {port: {}};
vm.runInNewContext(source.slice(start, end), {
  current, run: current, tap, rate: 16000, CAPTURE_RATE: 16000,
  MIN_FRAME_SAMPLES: 4096, MAX_SOCKET_BACKLOG: 65536, CHAR_CHUNK: 0x8000,
  WebSocket: {OPEN: 1}, btoa: (s) => Buffer.from(s, "binary").toString("base64"),
});
const frame = new Float32Array(320).fill(0.75);
// Half a voiced frame is already buffered when the user mutes.
tap.port.onmessage({data: frame});
assert.equal(sent.length, 0);
current.muted = true;
current.clearPending();
// Queued nonzero worklet samples arrive AFTER mute. They must become silence,
// with the same duration, rather than leak speech or stop the Live clock.
tap.port.onmessage({data: frame});
assert.equal(sent.length, 0);
tap.port.onmessage({data: frame});
assert.equal(sent.length, 1);
const pcm = Buffer.from(sent[0].data, "base64");
assert.equal(pcm.length, 640 * 2);
assert.ok(pcm.every((byte) => byte === 0));
current.muted = false;
tap.port.onmessage({data: new Float32Array(640).fill(0.5)});
assert.ok(Buffer.from(sent[1].data, "base64").some((byte) => byte !== 0));
// Gemini's legacy framing deliberately sends nothing while muted.
current.muted = true;
current.frameMs = null;
current.clearPending();
tap.port.onmessage({data: new Float32Array(4096).fill(0.75)});
assert.equal(sent.length, 2);
"""
    module = Path(__file__).resolve().parents[1] / "web/m/live.js"
    result = subprocess.run(["node", "-e", script, str(module)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_live_audio_background_resume_and_cleanup():
    script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const element = () => ({classList: {toggle() {}}, setAttribute() {}, replaceChildren() {},
  showModal() {}, close() {}, value: '', children: []});
const document = new EventTarget();
document.getElementById = (id) => {
  if (!elements.has(id)) elements.set(id, element());
  return elements.get(id);
};
const window = new EventTarget();
const audioSession = Object.assign(new EventTarget(), {type: 'auto', state: 'active'});
const contexts = [], streams = [], sockets = [], outputs = [], locks = [], timers = new Set();
let initialResumePending = false, initialPlayPending = false;
class Context {
  constructor() {
    this.state = 'suspended'; this.calls = 0; this.sampleRate = 16000;
    this.pending = initialResumePending;
    this.audioWorklet = {addModule: async () => {}}; contexts.push(this);
  }
  async resume() {
    this.calls++;
    if (this.pending) return new Promise(() => {});
    if (this.denied) throw Error('background denied');
    this.state = 'running'; this.onstatechange?.();
  }
  async close() { this.state = 'closed'; }
  createMediaStreamDestination() {
    this.destinationTrack = {stopped: false, stop() {this.stopped = true;}};
    return {stream: {getTracks: () => [this.destinationTrack]}, disconnect() {}};
  }
  createMediaStreamSource() { return {connect() {}, disconnect() {}}; }
  createGain() { return {gain: {}, connect() {}, disconnect() {}}; }
}
class Socket {
  static OPEN = 1;
  constructor() { this.readyState = 1; sockets.push(this); }
  send() {}
  close() { this.readyState = 3; }
}
let initiallyEnded = false;
const navigator = {audioSession, wakeLock: {request: async () => {
  const lock = {released: false, async release() {this.released = true;}};
  locks.push(lock); return lock;
}}, mediaDevices: {getUserMedia: async () => {
  const track = {readyState: initiallyEnded ? 'ended' : 'live', muted: false,
    enabled: true, stopped: false, stop() {this.stopped = true;}};
  const stream = {getTracks: () => [track], getAudioTracks: () => [track]};
  streams.push(stream); return stream;
}}};
const sandbox = {document, window, navigator, AudioContext: Context, WebSocket: Socket,
  AudioWorkletNode: class { constructor() {this.port = {};} connect() {} disconnect() {} },
  Audio: class {
    constructor() {this.paused = true; this.pending = initialPlayPending; outputs.push(this);}
    async play() {
      if (this.pending) return new Promise(() => {});
      this.paused = false; this.onplaying?.();
    }
    pause() {this.paused = true; this.onpause?.();}
  },
  URLSearchParams, location: {protocol: 'https:', host: 'test'},
  localStorage: {getItem() {}, setItem() {}},
  setTimeout: (fn) => {timers.add(fn); return fn;}, clearTimeout: (fn) => timers.delete(fn)};
const source = fs.readFileSync(process.argv[1], 'utf8').replace('export function', 'function');
vm.runInNewContext(source + '\nglobalThis.setup = setupLiveMode;', sandbox);
const live = sandbox.setup({request: async () => {throw Error('offline');}});
const flush = async () => {for (let i = 0; i < 20; i++) await Promise.resolve();};
const status = () => document.getElementById('voice-status').textContent;
let completed = false;
process.once('beforeExit', () => assert.ok(completed, 'lifecycle test left a pending promise'));
(async () => {
  await document.getElementById('voice-start').onclick();
  assert.equal(audioSession.type, 'play-and-record');
  assert.equal(contexts.length, 1); assert.equal(outputs[0].paused, false);
  assert.equal(locks.length, 1); assert.equal(locks[0].released, false);
  sockets[0].onmessage({data: JSON.stringify({type: 'status', status: 'listening'})});
  assert.match(status(), /Listening/);
  outputs[0].pause(); await flush();
  assert.equal(outputs[0].paused, false); assert.match(status(), /Listening/);
  const track = streams[0].getAudioTracks()[0];
  // Background suspension is resumed without releasing the microphone or socket.
  contexts[0].state = 'suspended'; document.hidden = true;
  await locks[0].release(); // The browser releases screen wake locks when hidden.
  document.dispatchEvent(new Event('visibilitychange')); await flush();
  assert.equal(contexts[0].state, 'running');
  assert.equal(track.stopped, false); assert.equal(sockets[0].readyState, 1);
  // A platform denial produces no retry loop and never claims to be listening.
  contexts[0].denied = true; contexts[0].state = 'suspended';
  contexts[0].onstatechange(); await flush();
  const calls = contexts[0].calls; await flush();
  assert.equal(contexts[0].calls, calls); assert.match(status(), /interrupted/);
  assert.equal(locks.length, 1); // No background wake-lock request.
  contexts[0].denied = false; document.hidden = false;
  document.dispatchEvent(new Event('visibilitychange')); await flush();
  assert.match(status(), /Listening/);
  assert.equal(locks.length, 2); assert.equal(locks[1].released, false);
  // WebKit can leave resume pending until a gesture. A tap must still retry.
  contexts[0].pending = true; contexts[0].state = 'suspended';
  document.dispatchEvent(new Event('visibilitychange')); await flush();
  assert.match(status(), /interrupted/);
  contexts[0].pending = false;
  document.getElementById('live-mode').onclick(); await flush();
  assert.equal(contexts[0].state, 'running');
  track.muted = true; track.onmute(); assert.match(status(), /interrupted/);
  track.muted = false; track.onunmute(); await flush(); assert.match(status(), /Listening/);
  document.getElementById('voice-mute').onclick(); await flush();
  assert.equal(track.enabled, false); assert.match(status(), /muted/);
  // Leaving the document explicitly releases all hardware and the audio category.
  window.dispatchEvent(new Event('pagehide')); await flush();
  assert.equal(live.isActive(), false); assert.equal(track.stopped, true);
  assert.equal(audioSession.type, 'auto'); assert.equal(sockets[0].readyState, 3);
  assert.equal(outputs[0].paused, true); assert.equal(outputs[0].srcObject, null);
  assert.equal(contexts[0].destinationTrack.stopped, true);
  assert.ok(locks.every((lock) => lock.released));
  assert.ok(contexts.every((ctx) => ctx.state === 'closed'));
  document.dispatchEvent(new Event('visibilitychange')); await flush();
  assert.equal(streams.length, 1); // No unexpected microphone reacquisition.
  // Unsupported browsers still start; an ended mic cannot leave a Listening label.
  delete navigator.audioSession;
  await document.getElementById('voice-start').onclick();
  streams[1].getAudioTracks()[0].onended(); await flush();
  assert.equal(live.isActive(), false); assert.match(status(), /disconnected/);
  // A track can end before getUserMedia resolves, so no future event arrives.
  initiallyEnded = true; navigator.audioSession = audioSession;
  await document.getElementById('voice-start').onclick(); await flush();
  assert.equal(live.isActive(), false); assert.match(status(), /disconnected/);
  assert.equal(sockets.length, 2); assert.equal(audioSession.type, 'auto');
  assert.ok(contexts.every((ctx) => ctx.state === 'closed'));
  assert.equal(streams[2].getTracks()[0].stopped, true);
  // A never-settling initial resume must not leave startup pending indefinitely.
  initiallyEnded = false; initialResumePending = true;
  document.getElementById('voice-start').onclick(); await flush();
  assert.equal(live.isActive(), true);
  for (const timeout of [...timers]) timeout(); await flush();
  assert.equal(live.isActive(), false); assert.match(status(), /timed out/);
  assert.equal(streams[3].getTracks()[0].stopped, true);
  assert.ok(locks.every((lock) => lock.released));
  // A user retry must finish startup even if its ORIGINAL resume never settles.
  const startup = document.getElementById('voice-start').onclick(); await flush();
  const startingContext = contexts.at(-1); startingContext.pending = false;
  document.getElementById('live-mode').onclick(); await startup; await flush();
  assert.equal(sockets.length, 3); assert.equal(live.isActive(), true);
  document.getElementById('voice-start').onclick();
  initialResumePending = false; initialPlayPending = true;
  const playbackStartup = document.getElementById('voice-start').onclick(); await flush();
  outputs.at(-1).pending = false;
  document.getElementById('live-mode').onclick(); await playbackStartup; await flush();
  assert.equal(sockets.length, 4); assert.equal(live.isActive(), true);
  document.getElementById('voice-start').onclick(); initialPlayPending = false;
  // A wake lock acquired after End must be released, never orphaned.
  initialResumePending = false;
  let resolveLock;
  navigator.wakeLock.request = () => new Promise((resolve) => {resolveLock = resolve;});
  await document.getElementById('voice-start').onclick();
  document.getElementById('voice-start').onclick();
  const lateLock = {released: false, async release() {this.released = true;}};
  resolveLock(lateLock); await flush(); assert.equal(lateLock.released, true);
  completed = true;
})().catch((error) => {console.error(error); process.exitCode = 1;});
"""
    module = Path(__file__).resolve().parents[1] / "web/m/live.js"
    result = subprocess.run(["node", "-e", script, str(module)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
