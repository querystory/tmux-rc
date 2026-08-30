"""send_keys must chunk large literals under tmux's ~16KB message cap.

A 30KB pasted transcript in ONE send-keys made tmux refuse the command
(CalledProcessError -> 500 to the phone). Chunks must cover the text exactly,
in order, each under the cap, with the Enter still last."""
import threading
import time

import openbus.tmux as T


def test_large_literal_is_chunked_in_order(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "_run", lambda argv: calls.append(argv))
    text = "x" * (T._SEND_CHUNK_BYTES * 2 + 123)
    T.send_keys("%1", text, enter=True, literal=True)
    lits = [c[-1] for c in calls if "-l" in c]
    assert "".join(lits) == text
    assert all(len(x.encode()) <= T._SEND_CHUNK_BYTES for x in lits)
    assert len(lits) == 3
    assert calls[-1] == ["send-keys", "-t", "%1", "Enter"]


def test_non_ascii_chunks_bounded_by_bytes(monkeypatch):
    # the tmux cap is in BYTES: 4-byte emoji must not ride a char-counted chunk past it,
    # and no chunk may split a code point (join+equality proves clean boundaries)
    calls = []
    monkeypatch.setattr(T, "_run", lambda argv: calls.append(argv))
    # leading "a" misaligns the byte grid so a naive slice would land mid-emoji
    text = "a" + "🚀" * (T._SEND_CHUNK_BYTES // 2) + "é"  # ~2x the cap in bytes
    T.send_keys("%1", text, enter=False)
    lits = [c[-1] for c in calls if "-l" in c]
    assert "".join(lits) == text
    assert all(len(x.encode()) <= T._SEND_CHUNK_BYTES for x in lits)
    assert len(lits) == 3


def test_small_literal_one_call(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "_run", lambda argv: calls.append(argv))
    T.send_keys("%1", "yes", enter=False)
    assert calls == [["send-keys", "-t", "%1", "-l", "yes"]]


def test_empty_literal_still_sends(monkeypatch):
    # an empty text with enter=True is "press Return" — the empty -l call is harmless
    # and the Enter must still go
    calls = []
    monkeypatch.setattr(T, "_run", lambda argv: calls.append(argv))
    T.send_keys("%1", "", enter=True)
    assert calls[-1] == ["send-keys", "-t", "%1", "Enter"]


def test_concurrent_sends_do_not_interleave(monkeypatch):
    # one logical send is now several tmux commands (chunks + Enter); concurrent callers
    # (asyncio.to_thread in live.py, parallel HTTP handlers) must not interleave mid-paste
    calls = []
    monkeypatch.setattr(
        T, "_run", lambda argv: (time.sleep(0.002), calls.append(argv)) and None
    )
    ts = [
        threading.Thread(target=T.send_keys, args=("%1", ch * (T._SEND_CHUNK_BYTES * 3)))
        for ch in "ab"
    ]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(calls) == 8  # 2 sends x (3 chunks + Enter)
    for block in (calls[:4], calls[4:]):  # each send's calls must be contiguous
        assert len({c[-1][0] for c in block[:3]}) == 1  # all chunks from the same send
        assert block[3] == ["send-keys", "-t", "%1", "Enter"]


def test_key_names_never_chunk(monkeypatch):
    calls = []
    monkeypatch.setattr(T, "_run", lambda argv: calls.append(argv))
    T.send_keys("%1", "C-c", enter=False, literal=False)
    assert calls == [["send-keys", "-t", "%1", "C-c"]]
