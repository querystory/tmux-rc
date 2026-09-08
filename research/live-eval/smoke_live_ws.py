"""End-to-end Live Mode smoke against a REAL provider, through the daemon's own WebSocket.

Speaks one command (OpenAI TTS → 16 kHz PCM, exactly the frames the browser would send)
into /api/live-mode with a fake watcher and a mocked send_keys, then checks the full loop
round-tripped: connect → ambient [tmux update] with NO reply → user speech → tool call →
tool result → follow-up → metered cost under the entry's rate card. Nothing touches tmux.
Usage: TMUXRC_LIVE_MODELS='[…]' smoke_live_ws.py <label>     (keys from ~/.config/tmux-rc/openai.env)"""
import asyncio, base64, json, os, struct, sys, time, urllib.request
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("TMUXRC_LIVE_MODE", "1")
from fastapi.testclient import TestClient  # noqa: E402
from daemon import live, llm, server, telemetry, tmux  # noqa: E402  (server import loads .env + the key file)

LABEL = sys.argv[1] if len(sys.argv) > 1 else "GPT 2.1 mini"
UTTERANCE = "Tell the work pane yes."


class Watcher:
    """Two panes; the active one has a pending yes/no question. One state change fires ~1s
    in so the context-updater sends an ambient update before anyone speaks."""
    snapshots = {"%1": [{"id": "s1", "text": "Run the tests? 1) yes 2) no", "ts": 1.0}],
                 "%2": [{"id": "s2", "text": "$ ", "ts": 1.0}]}
    v = 1

    def digest(self):
        return [{"pane_id": "%1", "label": "work", "window_index": "1", "tool": "claude", "activity": "waiting",
                 "tmux_active": True, "headline": "asking whether to run tests", "summary": None,
                 "question": "Run the tests? 1) yes 2) no", "history": []},
                {"pane_id": "%2", "label": "shell", "window_index": "2", "tool": "shell", "activity": "idle",
                 "tmux_active": False, "headline": None, "summary": None, "question": None, "history": []}]

    def state_version(self): return self.v
    async def wait_for_state_change(self, since, timeout):
        if self.v == 1:
            await asyncio.sleep(1.0); self.v = 2
        else:
            await asyncio.sleep(timeout)
        return self.v
    def request_reparse(self, pane_id): pass


def tts_pcm16k(text: str) -> bytes:
    """OpenAI TTS → 24 kHz PCM16 → 16 kHz by linear interpolation (the browser's capture rate)."""
    req = urllib.request.Request("https://api.openai.com/v1/audio/speech", method="POST",
        headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"], "Content-Type": "application/json"},
        data=json.dumps({"model": "gpt-4o-mini-tts", "input": text, "voice": "alloy", "response_format": "pcm"}).encode())
    pcm24 = urllib.request.urlopen(req, timeout=60).read()
    x = struct.unpack(f"<{len(pcm24) // 2}h", pcm24[: len(pcm24) & ~1]); n = len(x); out = []
    for j in range(n * 2 // 3):
        pos = j * 1.5; i = int(pos); a = x[i]; b = x[i + 1] if i + 1 < n else a
        out.append(int(a + (b - a) * (pos - i)))
    return struct.pack(f"<{len(out)}h", *out)


def main():
    server.app.state.watcher = Watcher()
    typed, emitted, folded = [], [], {}
    tmux.send_keys = lambda *a: typed.append(a)          # never touch a real pane
    telemetry.emit_action = lambda **k: None
    telemetry.emit_live_turn = lambda **k: emitted.append(k)
    llm.record_live_usage = lambda **k: folded.update(k)
    live.telemetry, live.tmux, live.llm = telemetry, tmux, llm

    speech = tts_pcm16k(UTTERANCE) + b"\0" * (16000 * 2 * 2)  # + 2s silence so VAD sees the end
    frames = [speech[i:i + 8192] for i in range(0, len(speech), 8192)]  # 4096 samples, like the browser
    log = []; t0 = time.monotonic()
    c = TestClient(server.app)
    with c.websocket_connect(f"/api/live-mode?model={LABEL}") as ws:
        def pump(until, budget):
            while time.monotonic() - t0 < budget:
                m = ws.receive_json(); log.append((round(time.monotonic() - t0, 2), m))
                if until(m): return m
            return None
        assert pump(lambda m: m.get("status") == "listening", 20), "never listening"
        time.sleep(3.0)  # let the ambient [tmux update] land — nothing should come back
        for f in frames:
            ws.send_json({"action": "audio", "data": base64.b64encode(f).decode()}); time.sleep(0.05)
        t_speech = time.monotonic() - t0
        pump(lambda m: m["type"] == "typed", 45)
        pump(lambda m: m["type"] == "turn_complete" and any(x[1]["type"] == "typed" for x in log), 25)
        ws.send_json({"action": "stop"})
        try:
            while True: log.append((round(time.monotonic() - t0, 2), ws.receive_json()))
        except Exception:
            pass

    kinds = [m["type"] if m["type"] != "status" else "status:" + m["status"] for _, m in log]
    pre_speech = [k for (t, m), k in zip(log, kinds) if t < t_speech and k not in ("status:connecting", "status:listening")]
    first_typed = next((t for t, m in log if m["type"] == "typed"), None)
    print(f"model label: {LABEL}")
    print(f"events: {kinds}")
    print(f"before speech (ambient update must be silent): {pre_speech or 'nothing'}")
    print(f"transcripts: {[m['text'] for _, m in log if m['type'] == 'transcript']}")
    print(f"typed via mocked send_keys: {typed}")
    print(f"latency speech-end → typed: {first_typed and round(first_typed - t_speech, 2)} s")
    print(f"meter final: turns={emitted[-1]['turns'] if emitted else None} in={folded.get('in_tokens')} "
          f"out={folded.get('out_tokens')} cost=${folded.get('cost', 0):.4f} model={emitted[-1]['model'] if emitted else None}")
    ok = bool(typed) and not pre_speech and folded.get("cost", 0) > 0 and any(k == "turn_complete" for k in kinds)
    print("PASS" if ok else "FAIL")


if __name__ == "__main__":
    main()
