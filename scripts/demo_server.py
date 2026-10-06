"""Serve the real tmux-rc app against the fictional fleet in scripts/demo_fleet.py.

    uv run python -m scripts.demo_server [port]

This is openbus.server's own FastAPI app — its routes, static mount, middleware and
History.query — with three things swapped out: the watcher (a fixed fleet instead of
tmux), the history database (a seeded temp file instead of ~/.local/state), and every
mutation (answered {"ok": true} without running a handler). tmux itself is unplugged
at the subprocess seam, so nothing served from here can read or touch a real pane.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

# Before openbus.server loads .env: an existing variable wins over the file, so an empty
# one keeps a checkout's real telemetry endpoint from receiving demo traffic and its own
# launcher menu (labels, icons) out of the shots — empty means the shipped defaults.
os.environ.update(OTEL_EXPORTER_OTLP_ENDPOINT="", TMUXRC_LAUNCHERS="")

import uvicorn
from fastapi.responses import JSONResponse

from openbus import live, live_providers, server, tmux

from . import demo_fleet as demo


def _no_tmux(args, *_a, **_kw):
    raise subprocess.CalledProcessError(1, ["tmux", *args], stderr="demo: no tmux")


FLEET = {p["pane_id"]: p for p in demo.fleet()}


def _capture(pane_id, **_):
    if pane_id not in FLEET:
        _no_tmux(["capture-pane", "-t", pane_id])
    return demo.capture(FLEET[pane_id])


class DemoWatcher:
    """The slice of openbus.watcher.Watcher the HTTP routes read, over a frozen fleet."""

    def __init__(self):
        self.states = list(FLEET.values())
        self.events_log = demo.events_log()
        self.snapshots = {}

    def state_version(self): return 1
    def is_stale(self): return False
    def booted(self): return True
    def snapshot_text(self, *_): return None
    def note_live_poll(self, *_): pass
    def label_for(self, pane_id): return pane_id
    def tool_for(self, _pane_id): return None
    def pane_birth(self, _pane_id): return None
    def question_generation(self, _pane_id): return 0

    async def wait_for_state_change(self, since, timeout):  # noqa: ASYNC109 - mirrors Watcher
        await asyncio.sleep(timeout)  # nothing ever changes
        return since


class DemoHistory(demo.History):
    def query(self, *args, **kwargs):
        return super().query(*args, **{**kwargs, "now": demo.NOW})


@asynccontextmanager
async def lifespan(app):
    with tempfile.TemporaryDirectory(prefix="tmux-rc-demo-") as tmp:
        app.state.history = demo.seed_history(DemoHistory(Path(tmp) / "history.sqlite3"))
        app.state.watcher, app.state.push = DemoWatcher(), None
        yield


def install() -> None:
    """Swap the live seams for the demo ones. Separate from main() so a test can use it."""
    tmux._run = _no_tmux  # noqa: SLF001 - the one seam every tmux call goes through
    tmux.capture_pane = _capture
    # A fixed model menu, so the chat button shows on every machine whatever keys it holds;
    # the socket itself is dropped (the screenshot browser stubs it), so no provider is called.
    server.DOCS_MOUNTED = True  # the Docs link shows as on a real deploy, built or not
    live.enabled = lambda: True
    live.offered = lambda: [live_providers.LiveModel("Demo chat", "demo", "anthropic")]
    live_providers.models = list
    server.app.router.routes[:] = [r for r in server.app.router.routes
                                   if getattr(r, "path", "") != "/api/live-mode"]
    server.app.router.lifespan_context = lifespan

    @server.app.middleware("http")
    async def read_only(request, call_next):
        if request.method in ("GET", "HEAD"):
            return await call_next(request)
        return JSONResponse({"ok": True})


def main() -> None:
    install()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18039
    uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
