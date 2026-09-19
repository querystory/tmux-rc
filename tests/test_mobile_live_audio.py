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
