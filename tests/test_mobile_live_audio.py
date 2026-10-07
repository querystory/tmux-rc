"""Exercise the shipped worklet callback without a microphone or provider connection."""

import json
import os
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


def _run_live(body: str, version: dict | None = None) -> None:
    """Run `body` against web/m/live.js (and the modules it imports) in a stubbed DOM with
    fake audio, microphone, wake lock and sockets; see _HARNESS. `version` is what
    /api/version answers; None is offline."""
    web = Path(__file__).resolve().parents[1] / "web"
    args = [str(web / p) for p in ("m/live.js", "live-close.js", "live-chat.js", "m/composer.js",
                                 "chat-starters.js")]
    markdown, model = (json.dumps((web / p).as_uri())
                       for p in ("chat-markdown.js", "m/pane-model.js"))
    script = ("globalThis.requestAnimationFrame = fn => fn();\n"
              "(async () => { const { appendChatMarkdown } = await import(" + markdown + ");\n"
              "const { since } = await import(" + model + ");\n"
              + _HARNESS + body + "\n})().catch(e => { console.error(e); process.exitCode = 1; });")
    result = subprocess.run(["node", "-e", script, *args],
                            capture_output=True, text=True, timeout=30,
                            env={**os.environ, "LIVE_VERSION": json.dumps(version)})
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_chat_starters_follow_connection_and_conversation_lifecycle():
    _run_live(r"""
(async () => {
  const $ = (id) => document.getElementById(id);
  await live.refresh();
  assert.equal($('chat-starters').hidden, true); // not a voice menu
  $('chat').onclick(); await flush();
  const starters = $('chat-starters'), socket = sockets[0];
  assert.equal(starters.hidden, false);
  assert.ok(starters.children.every(b => b.disabled));
  socket.onmessage({data: JSON.stringify({type:'status', status:'listening'})});
  assert.ok(starters.children.every(b => !b.disabled));
  socket.onmessage({data: JSON.stringify({type:'status', status:'reconnecting'})});
  assert.ok(starters.children.every(b => b.disabled));
  starters.children[0].onclick(); // even a stale/programmatic click sends nothing
  assert.equal(socket.sent.length, 0);
  socket.onmessage({data: JSON.stringify({type:'status', status:'listening'})});
  const segments = sandbox.Composer.prototype.segments;
  sandbox.Composer.prototype.segments = () => [{file:{}}];
  let rejectImage;
  sandbox.createImageBitmap = () => new Promise((resolve, reject) => { rejectImage = reject; });
  const processing = $('voice-compose').onsubmit({preventDefault() {}});
  await flush();
  assert.ok(starters.children.every(b => b.disabled));
  starters.children[0].onclick();
  assert.equal(socket.sent.length, 0); // no second turn during image preparation
  rejectImage(Error('unreadable image')); await processing;
  assert.ok(starters.children.every(b => !b.disabled)); // failure restores the choices
  sandbox.Composer.prototype.segments = segments;
  $('chat-input').textContent = 'Keep this draft';
  starters.children[0].onclick();
  assert.equal(socket.sent.length, 1);
  assert.equal(JSON.parse(socket.sent[0]).action, 'text');
  assert.match(JSON.parse(socket.sent[0]).text, /attention/);
  assert.equal(starters.hidden, true);
  assert.ok(starters.children.every(b => b.disabled));
  starters.children[0].onclick();
  assert.equal(socket.sent.length, 1); // no double send after the accepted tap
  assert.equal($('chat-input').textContent, 'Keep this draft');
  $('voice-close').onclick(); $('chat').onclick(); await flush();
  assert.equal(starters.hidden, true); // restoring is not a new conversation
  $('voice-end').onclick(); $('chat').onclick(); await flush();
  assert.equal(starters.hidden, false); // new conversation, including a model switch
  $('voice-end').onclick();
  assert.equal(starters.hidden, true);
})().catch(error => {console.error(error); process.exitCode = 1;});
""", {"version": "v", "live_enabled": True,
      "live_models": [{"label": "Sonnet", "text": True}]})


_HARNESS = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = new Map();
const thumbs = []; // every image put ahead of a transcript row's text
const element = () => {
  const classes = new Set();
  const node = Object.assign(new EventTarget(), {
    classList: {add: (c) => classes.add(c), contains: (c) => classes.has(c),
      toggle: (c, on = !classes.has(c)) => on ? classes.add(c) : classes.delete(c)},
    setAttribute() {}, insertAdjacentHTML() {}, querySelector() {}, remove() {},
    showModal() {node.open = node.modal = true;}, show() {node.open = true; node.modal = false;},
    focus() {},
    close() {node.open = false; node.dispatchEvent(new Event('close'));},
    before(...nodes) {thumbs.push(...nodes.map((image) => image.src));},
    remove() {
      node.parent?.children.splice(node.parent.children.indexOf(node), 1); node.parent = null;
    },
    insertBefore(child, ref) {
      child.remove(); child.parent = node;
      node.children.splice(ref ? node.children.indexOf(ref) : node.children.length, 0, child);
    },
    replaceChildren(...children) {
      [...node.children].forEach((c) => c.remove()); node.append(...children);
    },
    append(...children) {children.forEach((child) => node.insertBefore(child, null));},
    value: '', textContent: '', dataset: {}, children: []});
  Object.defineProperty(node, 'isConnected', {get: () => !!node.parent});
  Object.defineProperty(node, 'previousElementSibling',
    {get: () => node.parent?.children[node.parent.children.indexOf(node) - 1]});
  Object.defineProperty(node, 'firstChild', {get: () => node.children[0]});
  Object.defineProperty(node, 'lastChild', {get: () => node.children.at(-1)});
  Object.defineProperty(node, 'lastElementChild', {get: () => node.children.at(-1)});
  return node;
};
const document = new EventTarget();
document.createElement = element;
document.body = element();
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
  constructor(url) { this.url = url; this.readyState = 1; this.sent = []; sockets.push(this); }
  send(data) { this.sent.push(data); }
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
  appendChatMarkdown, since,
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
const strip = (path) =>
  fs.readFileSync(path, 'utf8').replace(/^import .*\n/gm, '').replace(/^export /gm, '');
const source = process.argv.slice(1).reverse().map(strip).join('\n');
vm.runInNewContext(source + '\nglobalThis.setup = setupLiveMode; globalThis.Composer = Composer;',
  sandbox);
const version = JSON.parse(process.env.LIVE_VERSION || 'null');
const wide = Object.assign(new EventTarget(), {matches: false}); // app.js's WIDE query
const opened = []; // panes app.js was asked to navigate to
const live = sandbox.setup({licon: (name) => name, wide, open: (id) => opened.push(id),
  request: async () => { if (!version) throw Error('offline'); return version; }});
const flush = async () => {for (let i = 0; i < 20; i++) await Promise.resolve();};
const status = () => document.getElementById('voice-status').textContent;
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_live_audio_background_resume_and_cleanup():
    _run_live(r"""
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
  // Remote status and socket loss cannot hide the local resume instruction.
  for (const remote of ['connecting', 'reconnecting', 'listening']) {
    sockets[0].onmessage({data: JSON.stringify({type: 'status', status: remote})});
    assert.match(status(), /interrupted/);
  }
  sockets[0].onclose({code: 1006}); assert.match(status(), /interrupted/);
  track.muted = false; track.onunmute(); await flush();
  assert.match(status(), /Connection lost. Reconnecting/);
  sockets[0].onmessage({data: JSON.stringify({type: 'status', status: 'listening'})});
  assert.match(status(), /Listening/);
  document.getElementById('voice-mute').onclick(); await flush();
  assert.equal(track.enabled, false); assert.match(status(), /muted/);
  // A refusal of the reconnect handshake is definitive: shown, never retried.
  sockets[0].onclose({code: 1008, reason: 'Live model not available'});
  assert.equal(live.isActive(), false); assert.match(status(), /not available/);
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
""")


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_wide_chat_docks_toggles_and_moves_across_the_breakpoint():
    _run_live(r"""
(async () => {
  const $ = (id) => document.getElementById(id), dialog = $('voice-dialog');
  const escape = () => dialog.dispatchEvent(Object.assign(new Event('keydown'), {key: 'Escape'}));
  await live.refresh(); wide.matches = true;
  $('chat').onclick(); await flush();
  assert.equal(dialog.open, true); assert.equal(dialog.modal, false); // docked, not modal
  $('chat').onclick(); assert.equal(dialog.open, false); // the button toggles a docked panel
  assert.equal(live.isActive(), true); // closing it only minimizes
  $('chat').onclick(); escape(); assert.equal(dialog.open, false);
  $('chat').onclick();
  wide.matches = false; wide.dispatchEvent(new Event('change'));
  assert.equal(dialog.open, true); assert.equal(dialog.modal, true); // same session, now a sheet
  assert.equal(sockets.length, 1);
  wide.matches = true; wide.dispatchEvent(new Event('change'));
  assert.equal(dialog.modal, false); assert.equal(sockets.length, 1);
  completed = true;
})().catch((error) => {console.error(error); process.exitCode = 1;});
""", {"version": "v", "live_enabled": True,
      "live_models": [{"label": "Sonnet", "text": True}]})


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_chat_opens_a_text_session_without_the_mic_minimizes_and_sends_images():
    _run_live(r"""
(async () => {
  navigator.mediaDevices.getUserMedia = async () => { throw Error('the mic was asked for'); };
  const $ = (id) => document.getElementById(id), dialog = $('voice-dialog');
  await live.refresh();
  assert.equal($('chat').hidden, false);
  // The chat icon goes straight to a text session on the chat model: no picker, no mic.
  $('chat').onclick(); await flush();
  assert.equal(dialog.open, true); assert.equal(live.isActive(), true);
  assert.equal($('voice-models').hidden, true); assert.equal($('voice-title').textContent, 'Chat');
  assert.equal(contexts.length, 0); assert.equal(sockets.length, 1);
  assert.match(sockets[0].url, /[?&]mode=text/); assert.match(sockets[0].url, /[?&]model=Sonnet/);
  sockets[0].onmessage({data: JSON.stringify({type: 'status', status: 'listening'})});
  assert.equal(status(), 'Connected');
  // Minimized, the conversation keeps running; a reply dots the bubble and a consent
  // card counts on it.
  const [bubble] = document.body.children, badge = bubble.children[1];
  assert.equal(bubble.hidden, true);
  $('voice-close').onclick();
  assert.equal(dialog.open, false); assert.equal(bubble.hidden, false);
  assert.equal(live.isActive(), true); assert.equal(sockets[0].readyState, 1);
  sockets[0].onmessage({data: JSON.stringify({type: 'transcript', role: 'model', text: 'hi'})});
  assert.equal(bubble.classList.contains('unread'), true);
  sockets[0].onmessage({data: JSON.stringify({type: 'propose', id: 'p1', text: 'Send to work'})});
  assert.equal(badge.textContent, 1);
  sockets[0].onmessage({data: JSON.stringify({type: 'decided', id: 'p1', ok: true})});
  assert.equal(badge.textContent, '');
  bubble.onclick(); assert.equal(dialog.open, true); assert.equal(bubble.hidden, true);
  // A card's Open goes to its pane without answering it: still pending on the bubble,
  // and still pending in the log when the user comes back to approve.
  sockets[0].onmessage({data: JSON.stringify(
    {type: 'propose', id: 'p3', text: 'Send to work', pane_id: '%1'})});
  const card = document.getElementById('voice-log').children.at(-1), actions = card.lastChild;
  actions.children.find((b) => b.className === 'open').onclick();
  assert.deepEqual(opened, ['%1']); assert.equal(dialog.open, false);
  assert.equal(badge.textContent, 1); assert.equal(sockets[0].sent.length, 0);
  bubble.onclick(); assert.equal(card.lastChild, actions);
  assert.equal(card.firstChild.textContent, 'Wants to act');
  sockets[0].onmessage({data: JSON.stringify({type: 'decided', id: 'p3', ok: false})});
  // Tapping chat again brings the running conversation back rather than starting another.
  $('chat').onclick(); await flush();
  assert.equal(sockets.length, 1);
  // A pasted image rides the typed turn as a JPEG part, and its thumbnail lands on the
  // turn's echo; a refused turn consumes its own thumbnails, not the next turn's.
  sandbox.Composer.prototype.segments = () => [{text: 'look '}, {file: {type: 'image/png'}}];
  sandbox.createImageBitmap = async () => ({width: 3000, height: 1000, close() {}});
  const make = document.createElement;
  document.createElement = (tag) => tag !== 'canvas' ? make(tag) : Object.assign(make(tag), {
    getContext: () => ({fillRect() {}, drawImage() {}}),
    toDataURL: () => 'data:image/jpeg;base64,QUJD'});
  await $('voice-compose').onsubmit({preventDefault() {}}); await flush();
  sockets[0].onmessage({data: JSON.stringify({type: 'error', message: 'Too long', refused: true})});
  await $('voice-compose').onsubmit({preventDefault() {}}); await flush();
  assert.deepEqual(JSON.parse(sockets[0].sent.at(-1)),
    {action: 'text', text: 'look', images: [{mime: 'image/jpeg', data: 'QUJD'}]});
  sockets[0].onmessage({data: JSON.stringify(
    {type: 'transcript', role: 'user', text: 'look', new_segment: true, images: 1})});
  assert.deepEqual(thumbs, ['data:image/jpeg;base64,QUJD']);
  completed = true;
})().catch((error) => {console.error(error); process.exitCode = 1;});
""", {"version": "v", "live_enabled": True,
      "live_models": [{"label": "Gemini", "hint": "voice"}, {"label": "Sonnet", "text": True}]})


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_resume_card_names_the_session_past_its_title():
    _run_live(r"""
(async () => {
  await live.refresh();
  document.getElementById('chat').onclick(); await flush();
  const last = new Date(Date.now() - 6 * 86400e3).toISOString();
  sockets[0].onmessage({data: JSON.stringify({type: 'propose', id: 'p1', text: 'Resume dreamforce',
    session: {tool: 'codex', cwd: '~/src/df', last_active: last, id: 'ab12cd34'}})});
  const text = document.getElementById('voice-log').children.at(-1).children[1];
  assert.match(text.lastChild.textContent, /^codex · ~\/src\/df\n.+ · 6d ago · ab12cd34$/);
})().catch(error => {console.error(error); process.exitCode = 1;});
""", {"version": "v", "live_enabled": True, "live_models": [{"label": "Sonnet", "text": True}]})


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_chat_shows_a_working_row_until_each_turn_is_answered():
    _run_live(r"""
(async () => {
  const $ = (id) => document.getElementById(id), log = $('voice-log');
  await live.refresh();
  $('chat').onclick(); await flush();
  const socket = sockets[0], [bubble] = document.body.children;
  const say = (message) => socket.onmessage({data: JSON.stringify(message)});
  const roles = () => log.children.map((row) => row.dataset.role || 'typing');
  const send = (text) => {
    sandbox.Composer.prototype.segments = () => [{text}];
    $('voice-compose').onsubmit({preventDefault() {}});
  };
  say({type: 'status', status: 'listening'});
  send('hello');
  assert.deepEqual(roles(), ['typing']); // at once, before the echo
  say({type: 'transcript', role: 'user', text: 'hello', new_segment: true});
  assert.deepEqual(roles(), ['user', 'typing']); // rows arriving meanwhile go above it
  // A consent card means the model waits on the user: no dots until it is decided.
  say({type: 'propose', id: 'p1', text: 'Send to work'});
  assert.deepEqual(roles(), ['user', 'propose']);
  say({type: 'decided', id: 'p1', ok: true});
  assert.deepEqual(roles(), ['user', 'propose', 'typing']);
  // Minimized, the bubble pulses while the reply is out, and says so beside unread.
  $('voice-close').onclick();
  assert.equal(bubble.classList.contains('working'), true);
  say({type: 'typed', label: 'work', pane_id: '%1', submitted: true, text: 'ls'});
  assert.deepEqual(roles(), ['user', 'propose', 'typed', 'typing']);
  assert.match(bubble.title, /new messages, responding/);
  // A reply can come ahead of more work in the same turn: the row stays until it completes.
  say({type: 'transcript', role: 'model', text: 'Done.'});
  assert.deepEqual(roles(), ['user', 'propose', 'typed', 'model', 'typing']);
  assert.equal(bubble.classList.contains('working'), true);
  say({type: 'turn_complete'});
  assert.deepEqual(roles(), ['user', 'propose', 'typed', 'model']);
  assert.equal(bubble.classList.contains('working'), false);
  assert.match(bubble.title, /new messages/);
  // Sends are not blocked while one is out: the daemon queues them, so the dots stay up
  // until the last is answered, through a refusal of one past the queue's room.
  bubble.onclick();
  send('one');
  send('two');
  send('three');
  assert.equal(socket.sent.length, 4);
  say({type: 'error', refused: true, message: 'Still answering earlier turns; not sent'});
  assert.equal(roles().at(-1), 'typing');
  say({type: 'transcript', role: 'model', text: 'One.'}); say({type: 'turn_complete'});
  assert.equal(roles().at(-1), 'typing'); // the second turn is still out
  // The row is not a transcript row: the log still keeps 40 of those under it.
  for (let i = 0; i < 45; i++) say({type: 'typed', label: 'w', pane_id: '%1', text: 'x'});
  assert.equal(log.children.length, 41); assert.equal(roles().at(-1), 'typing');
  // Any other error ends the session's turns, and so the dots.
  say({type: 'error', message: 'live session failed'});
  assert.equal(roles().at(-1), 'error');
  assert.ok(!roles().includes('typing'));
  // A dropped connection takes the queued turns with it, and ending clears the row.
  send('four');
  assert.equal(roles().at(-1), 'typing');
  say({type: 'status', status: 'reconnecting'});
  assert.ok(!roles().includes('typing'));
  say({type: 'status', status: 'listening'});
  // Typing instead of answering a card supersedes it: the card says so, and the dots
  // return for the new message rather than it sitting queued behind the card.
  say({type: 'propose', id: 'p2', text: 'Send to work'});
  send('never mind');
  assert.equal(roles().at(-1), 'propose');
  say({type: 'decided', id: 'p2', ok: null});
  const card = log.children.filter((row) => row.dataset.role === 'propose').at(-1);
  assert.equal(card.firstChild.textContent, 'Cancelled — you sent a new message');
  assert.equal(roles().at(-1), 'typing');
  send('five');
  $('voice-end').onclick();
  assert.ok(!roles().includes('typing'));
  assert.equal(bubble.classList.contains('working'), false);
})().catch(error => {console.error(error); process.exitCode = 1;});
""", {"version": "v", "live_enabled": True,
      "live_models": [{"label": "Sonnet", "text": True}]})
