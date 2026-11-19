"""Input re-reads its own pane ahead of the full pass (Watcher.request_reparse).

A real tmux on a private socket, nobody else's server: busy panes whose screens move
every tick, so each full pass parses all of them one slow model call after another,
and one quiet pane the test types into. The card must show the typed line well before
a full pass could have reached it."""

import asyncio
import shutil
import time

import pytest

from openbus import tmux
from openbus import watcher as W
from tests.test_new_session import sock  # noqa: F401  (a fixture: a private tmux socket)

PARSE = 0.3  # one model call
BUSY = 24  # panes that re-parse every pass: a pass is BUSY * PARSE / TICK_WORKERS


@pytest.mark.skipif(not shutil.which("tmux"), reason="tmux is not installed")
@pytest.mark.usefixtures("sock")
def test_input_reparses_its_pane_before_the_full_pass(monkeypatch):
    real = W.classify

    def slow(pane, text, **kw):
        time.sleep(PARSE)
        return {**real(pane, text, **kw), "parse_ok": True, "probe": "pong" in text}

    monkeypatch.setattr(W, "classify", slow)
    target = tmux.run("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", "t", "cat")
    for _ in range(BUSY):
        tmux.run("new-window", "-d", "-t", "t:",
                 "while :; do head -c9 /dev/urandom | base64; sleep 0.05; done")
    full_pass = BUSY * PARSE / W.TICK_WORKERS

    def card():
        return next((s for s in w.states if s.get("pane_id") == target), {})

    async def until(ready):
        deadline = time.monotonic() + 30
        while not ready():
            assert time.monotonic() < deadline, card()
            await asyncio.sleep(0.02)

    async def run():
        w.start()
        await until(lambda: card().get("parsed_at"))  # booted and read once
        await asyncio.sleep(full_pass / 2)  # mid-pass, as a tap usually lands
        tmux.send_keys(target, "pong", enter=True)
        sent = time.monotonic()
        w.note_input(target)  # what every send route does after delivery
        await until(lambda: card().get("probe"))
        latency = time.monotonic() - sent
        await w.stop()
        return latency

    w = W.Watcher(target=None, use_llm=False)
    latency = asyncio.run(run())
    print(f"tap-to-card {latency:.2f}s, full pass {full_pass:.2f}s")
    assert latency < full_pass / 2
