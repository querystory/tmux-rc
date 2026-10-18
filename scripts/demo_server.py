"""Serve the real tmux-rc app against the fictional fleet in scripts/demo_fleet.py.

    uv run python -m scripts.demo_server [port]

This is openbus.server's own FastAPI app — its routes, static mount, middleware and
History.query — with three things swapped out: the watcher (a fixed fleet instead of
tmux), the history database (a seeded temp file instead of ~/.local/state, plan limits
included, so no real account is read or polled), and every mutation (answered
{"ok": true} without running a handler). tmux itself is unplugged
at the subprocess seam, so nothing served from here can read or touch a real pane.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
import time
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from types import SimpleNamespace

# Before openbus.server loads .env: an existing variable wins over the file, so an empty
# one keeps a checkout's real telemetry endpoint from receiving demo traffic and its own
# launcher menu (labels, icons) and keyword map out of the shots — empty means the shipped
# defaults. The agent-history root points nowhere so New session's directory suggestions
# never carry real paths. Scratch's mount is decided at import, so its dir must exist now
# (install() fills it).
os.environ.update(OTEL_EXPORTER_OTLP_ENDPOINT="", TMUXRC_LAUNCHERS="", TMUXRC_WORKSTREAMS="",
                  AGENT_HISTORY_DIR="/nonexistent-demo-agent-history",
                  TMUXRC_SCRATCH_DIR=tempfile.mkdtemp(prefix="tmux-rc-demo-scratch-"))

import uvicorn
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.routing import Mount

from openbus import live, live_providers, server, tmux

try:  # absent on a base that predates plan limits: screenshots_diff runs these scripts there
    from openbus.plan_usage import PlanUsage
except ImportError:
    PlanUsage = None
try:  # likewise for agent accounts
    from openbus import accounts
except ImportError:
    accounts = None
try:  # likewise for open loops
    from openbus.open_loops import OpenLoops
except ImportError:
    OpenLoops = None
try:  # likewise for reboot restore
    from openbus import restore
except ImportError:
    restore = None

from . import demo_fleet as demo


def _no_tmux(args, *_a, **_kw):
    raise subprocess.CalledProcessError(1, ["tmux", *args], stderr="demo: no tmux")


FLEET = {p["pane_id"]: p for p in demo.fleet()}
# The add-account shots: once one opens the sign-in window, its pane joins the fleet and the
# new account signs in a few seconds later (demo_fleet.accounts).
SIGN_IN = demo.sign_in_pane() if hasattr(demo, "sign_in_pane") else None
_signing_in: dict[str, float] = {}
# The two-account screenshot state (scripts/screenshots.mjs); unset, one account each.
TWO_ACCOUNTS = os.environ.get("TMUXRC_DEMO_ACCOUNTS") == "2"


def _capture(pane_id, **_):
    if SIGN_IN and pane_id == SIGN_IN["pane_id"]:
        return demo.capture(SIGN_IN)
    if pane_id not in FLEET:
        _no_tmux(["capture-pane", "-t", pane_id])
    return demo.capture(FLEET[pane_id])


class DemoWatcher:
    """The slice of openbus.watcher.Watcher the HTTP routes read, over a frozen fleet."""

    def __init__(self):
        self.events_log = demo.events_log()
        self.snapshots = {}
        self.tmux_running = True

    @property
    def states(self):
        return list(FLEET.values()) + ([SIGN_IN] if _signing_in else [])

    def state_version(self): return 1
    def pane_birth(self, _pane_id): return None
    def is_stale(self): return False
    def booted(self): return True
    def snapshot_text(self, *_): return None
    def note_live_poll(self, *_): pass
    def label_for(self, pane_id): return pane_id
    def tool_for(self, _pane_id): return None

    async def wait_for_state_change(self, since, timeout):  # noqa: ASYNC109 - mirrors Watcher
        await asyncio.sleep(timeout)  # nothing ever changes
        return since


class DemoHistory(demo.History):
    def query(self, *args, **kwargs):
        now = kwargs.get("now")  # a `before` pages back from NOW
        now = demo.NOW if now is None else min(now, demo.NOW)
        return super().query(*args, **{**kwargs, "now": now})


def _expunge_demo(root: Path):
    """server._pane_session over a fictional Claude session in a temp config dir, so
    Expunge's confirmation has files to count. The POST itself never runs (read_only)."""
    from openbus import expunge  # a base tree that predates Expunge has no such module

    sid = "5f3a9c1e-7b2d-4e8a-9c6f-0d1e2f3a4b5c"
    project = root / "projects" / "-src-example-org-storefront"
    for path in (project / f"{sid}.jsonl", project / sid / "subagents" / "agent-1.jsonl",
                 root / "file-history" / sid / "a1b2c3@v1",
                 root / "session-env" / sid / "hook-0.sh",
                 root / "history.jsonl"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    session = expunge.Session("claude", sid, root, root.parent / "agent-history", 0, "")
    return lambda *_a: ("0", session, expunge.targets(session))


@asynccontextmanager
async def lifespan(app):
    with tempfile.TemporaryDirectory(prefix="tmux-rc-demo-") as tmp:
        if hasattr(server, "_pane_session"):
            server._pane_session = _expunge_demo(Path(tmp) / "claude")  # noqa: SLF001
        app.state.history = demo.seed_history(DemoHistory(Path(tmp) / "history.sqlite3"))
        app.state.watcher, app.state.push = DemoWatcher(), None
        if PlanUsage:
            app.state.usage = demo.seed_usage(PlanUsage(app.state.history), TWO_ACCOUNTS)
            app.state.usage.report = partial(app.state.usage.report, demo.NOW)
        if OpenLoops:
            app.state.loops = demo.seed_loops(OpenLoops())
            app.state.loops.report = partial(app.state.loops.report, now=demo.NOW)
        if restore and hasattr(demo, "seed_restore"):
            demo.seed_restore(app.state.history)
        yield


def _since_sign_in() -> float | None:
    return time.time() - _signing_in["at"] if _signing_in else None


def install() -> None:
    """Swap the live seams for the demo ones. Separate from main() so a test can use it."""
    tmux._run = _no_tmux  # noqa: SLF001 - the one seam every tmux call goes through
    if accounts:  # the fleet's own accounts, never a scan of this machine's config dirs
        accounts.report = lambda *_: {**demo.accounts(TWO_ACCOUNTS, _since_sign_in()),
                                      "shared": accounts.SHARED}
    tmux.capture_pane = _capture
    tmux.capture_live = lambda pane_id: (_capture(pane_id), 0, "120")  # no history to page back
    if restore and hasattr(demo, "restore_seams"):
        for name, seam in demo.restore_seams().items():
            setattr(restore, name, seam)
    # A fixed model menu, so the chat button shows on every machine whatever keys it holds;
    # the socket itself is dropped (the screenshot browser stubs it), so no provider is called.
    server.DOCS_MOUNTED = True  # the Docs link shows as on a real deploy, built or not
    if hasattr(demo, "seed_scratch") and hasattr(server, "scratch"):  # the fleet's previews
        demo.seed_scratch(Path(os.environ["TMUXRC_SCRATCH_DIR"]))
        server.scratch.agent_history = SimpleNamespace(
            writers=lambda _pattern, due: {n: demo.SCRATCH_WRITERS[n] for n in due
                                           if n in demo.SCRATCH_WRITERS},
            get_many=lambda _ids: {}, reconcile=lambda: None)
        server.scratch_sidecars.browser = lambda: None  # thumbnails are seeded, never shot
    live.enabled = lambda: True
    live.offered = lambda: [live_providers.LiveModel("Demo chat", "demo", "anthropic")]
    live_providers.models = list
    server.app.router.routes[:] = [r for r in server.app.router.routes
                                   if getattr(r, "path", "") not in (  # this host's own data
                                       "/api/live-mode", "/api/export", "/api/export/parts")]
    server.app.router.lifespan_context = lifespan
    # make mock-live: a mock's folder at /mock/, ahead of the "/" catch-all, so its harness
    # is same-origin with /m and can patch the running app (scripts/mock.mjs).
    if mock := os.environ.get("TMUXRC_MOCK_DIR"):
        server.app.router.routes.insert(0, Mount("/mock", StaticFiles(directory=mock, html=True)))

    @server.app.middleware("http")
    async def read_only(request, call_next):
        if request.method in ("GET", "HEAD"):
            return await call_next(request)
        if request.url.path == "/api/accounts" and request.method == "POST":  # the add flow
            return JSONResponse({"dir": (await request.json())["dir"], "existed": False})
        if request.url.path == "/api/accounts" and request.method == "PUT":  # Remove: undo it
            _signing_in.clear()
        if request.url.path == "/api/accounts/login" and SIGN_IN:
            _signing_in.setdefault("at", time.time())
            return JSONResponse({"ok": True, "pane_id": SIGN_IN["pane_id"], "pid": "1"})
        return JSONResponse({"ok": True})


def main() -> None:
    install()
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18039
    uvicorn.run(server.app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
