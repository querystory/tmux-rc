"""Live Mode recordings, on synthetic tones: what the phone sent lands in mic.wav at the
rate it was captured, every frame's fate in frames.jsonl, and nothing breaks the session."""

import asyncio
import base64
import itertools
import json
import math
import struct
import wave

import pytest

import openbus.live as L
from openbus import gpt_live, live_record


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("TMUXRC_LIVE_RECORD", "1")
    return tmp_path / "tmux-rc/live-audio"


def tone(ms: int, rate: int = 16000) -> bytes:
    n = rate * ms // 1000
    return struct.pack(f"<{n}h", *(int(8000 * math.sin(i / 10)) for i in range(n)))


def lines(path):
    return [json.loads(x) for x in path.read_text().splitlines()]


class _WS:
    def __init__(self, *msgs):
        self.msgs = list(msgs)
        self.sent = []

    async def receive_json(self):
        return self.msgs.pop(0)

    async def send_json(self, m):
        self.sent.append(m)


class _Session:
    def __init__(self):
        self.audio = []

    async def send_audio(self, pcm):
        self.audio.append(pcm)


def test_a_session_records_what_the_phone_sent_and_each_frame(state):
    meter = L._Meter("abcdef0123456789", None, gpt_live.ENTRY)
    meter.rec = live_record.start(meter.session, gpt_live.ENTRY)
    frame = tone(40)
    b64 = base64.b64encode(frame).decode()
    session = _Session()
    ws = _WS(*[{"action": "audio", "data": b64}] * 24,
             {"action": "audio", "data": "A"},
             {"action": "audio", "data": b64, "lost_ms": 120},
             {"action": "stop"})
    asyncio.run(L._forward_client(ws, session, meter))
    asyncio.run(L._transcript(ws, meter, "user", "hello there", start_ms=0, end_ms=400))
    meter.rec.model(tone(100, 24000), 24000)
    meter.rec.event("end", reason="stop")
    meter.rec.close()

    [folder] = state.iterdir()
    assert folder.name.endswith("-abcdef01-1")
    assert session.audio == [frame] * 25  # forwarding unchanged
    with wave.open(str(folder / "mic.wav")) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        assert w.getnframes() == 25 * 640  # one second of 40 ms frames: plays at speed
        assert w.readframes(640) == frame
    with wave.open(str(folder / "model.wav")) as w:
        assert (w.getframerate(), w.getnframes()) == (24000, 2400)
    frames = lines(folder / "frames.jsonl")
    assert len(frames) == 26 and frames[0]["bytes"] == 1280 and frames[0]["forwarded"]
    assert frames[24] == {**frames[24], "forwarded": False, "dropped": "undecodable"}
    assert frames[25]["client_lost_ms"] == 120
    assert all(a["t_ms"] <= b["t_ms"] for a, b in itertools.pairwise(frames))
    events = lines(folder / "events.jsonl")
    assert [e["kind"] for e in events] == ["start", "transcript", "end"]
    assert events[0]["model"] == gpt_live.MODEL
    assert events[1] | {"role": "user", "text": "hello there", "end_ms": 400} == events[1]


def test_audio_during_a_reconnect_is_recorded_as_dropped(state):
    meter = L._Meter("s1", None, gpt_live.ENTRY)
    meter.rec = live_record.start("s1", gpt_live.ENTRY)
    ws = _WS({"action": "audio", "data": base64.b64encode(tone(40)).decode()},
             {"action": "stop"})
    assert asyncio.run(L._hold(ws, 5, meter)) is False
    meter.rec.close()
    [folder] = state.iterdir()
    assert lines(folder / "frames.jsonl")[0]["dropped"] == "reconnecting"


def test_off_records_nothing(state, monkeypatch):
    monkeypatch.setenv("TMUXRC_LIVE_RECORD", "0")
    rec = live_record.start("s1", gpt_live.ENTRY)
    rec.mic(tone(40))
    rec.close()
    assert not state.exists()


def test_old_sessions_are_pruned_by_count_and_size(state, monkeypatch):
    state.mkdir(parents=True)
    for i in range(5):
        (state / f"2026010{i}-000000-x-1").mkdir()
        (state / f"2026010{i}-000000-x-1" / "mic.wav").write_bytes(b"\0" * 100)
    monkeypatch.setattr(live_record, "KEEP", 4)
    live_record.start("new", gpt_live.ENTRY).close()
    assert sorted(d.name[:8] for d in state.iterdir())[:3] == ["20260102", "20260103", "20260104"]
    [new] = [d for d in state.iterdir() if not d.name.startswith("2026010")]
    monkeypatch.setattr(live_record, "MAX_BYTES", (new / "events.jsonl").stat().st_size + 100)
    live_record.start("new", gpt_live.ENTRY).close()
    assert len([d for d in state.iterdir() if not d.name.startswith("2026010")]) == 2
    old = [d.name for d in state.iterdir() if d.name.startswith("2026010")]
    assert old == ["20260104-000000-x-1"]


def test_a_failing_disk_never_breaks_the_session(state, caplog):
    rec = live_record.start("s1", gpt_live.ENTRY)
    rec.close()  # the events file is gone: every later write lands on a dead recorder
    rec.mic(tone(40))
    rec = live_record.Recorder(state / "missing")
    rec.mic(tone(40))  # cannot open: logged once, then a no-op
    rec.event("end")
    assert rec.folder is None
    assert caplog.text.count("recording to") == 1
