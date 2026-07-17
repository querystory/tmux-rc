"""FastAPI app: serves the PWA and the state/answer API.

Endpoints:
  GET  /api/state                 -> list of raw parser-JSON dicts (list-shaped for M2)
  GET  /api/panes/{id}/snapshots  -> recent snapshot ids + timestamps
  GET  /api/panes/{id}/snapshots/{snap} -> raw captured text of one snapshot
  POST /api/panes/{id}/send       -> inject keys / answer a prompt
  GET  /                          -> PWA (static)
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

# Load .env BEFORE importing the watcher/llm/telemetry chain — those read config from
# os.environ at import time (model, GOOGLE_CLOUD_PROJECT, OTEL endpoint). Without this, a
# launch that didn't inherit the env (e.g. a stray `make dev`) silently loses Vertex creds
# and every parse fails. Real environment vars still win over .env (override=False).
from dotenv import find_dotenv, load_dotenv

# Prefer the repo-root .env next to the package (the dev/run-from-checkout case); if that
# doesn't exist (e.g. installed as a wheel and launched elsewhere), fall back to the
# usual upward search from cwd. Either way, real env vars still win (override=False).
_repo_env = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(_repo_env if _repo_env.exists() else find_dotenv(usecwd=True))

from fastapi import FastAPI, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import PlainTextResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from . import tmux  # noqa: E402
from .watcher import Watcher  # noqa: E402

# One standard, human-readable log format for ALL loggers (uvicorn included — main()
# passes log_config=None so its loggers propagate here instead of using its own):
# timestamp, level, logger name. Previously nothing configured logging, so module
# loggers fell through to Python's bare lastResort handler — unprefixed lines, and
# anything below WARNING silently invisible (which pushed routine lines to WARNING just
# to be seen). Import-time, not main(): under --reload the worker process re-imports
# this module but never calls main(). basicConfig is a no-op if root is already set up.
logging.basicConfig(
    level=os.environ.get("TMUXRC_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
# Chatty third-party libraries log a line per LLM call at INFO (httpx: every Vertex
# POST; google_genai: an "AFC is enabled" banner). That's ~2 lines per parse of pure
# noise drowning our own signal — pin them to WARNING. EXCEPT under DEBUG: those same
# loggers are the ones you need when debugging the Vertex/HTTP path, so an explicit
# TMUXRC_LOG_LEVEL=DEBUG unmutes everything.
if os.environ.get("TMUXRC_LOG_LEVEL", "INFO").upper() != "DEBUG":
    for _noisy in ("httpx", "httpcore", "google_genai"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
# Uploaded images land here so the agent can read them by path. Kept out of the repo.
IMG_DIR = Path(tempfile.gettempdir()) / "tmux-rc-images"
_EXT = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


class SendBody(BaseModel):
    keys: str
    enter: bool = True
    literal: bool = True  # False ⇒ keys is a tmux key-name (Escape, Up, C-c)


# Audit lines ride the standard root config above (INFO). Routine records, not warnings.
_audit_log = logging.getLogger("daemon.server.audit")

# Key CONTENT in the audit trail is on by default (the operator asked for exactly this
# visibility) but can be switched off: keys typed via the phone can include no-echo
# secrets (sudo/ssh passwords) that nothing else in the system captures — pane capture
# never sees unechoed input — and a forwarded journal would persist them. Set
# TMUXRC_AUDIT_KEYS=0 to log actions without key content (local log AND telemetry).
_AUDIT_KEYS = os.environ.get("TMUXRC_AUDIT_KEYS") != "0"


def _audit(
    request: Request,
    action: str,
    pane_id: str,
    detail: str = "",
    keys: str | None = None,
    outcome: str = "ok",
) -> None:
    """One line per state-CHANGING request, with WHO — answers "what is making changes
    to my terminals?". Also emitted as an OTel record so the trail is queryable next to
    the parse telemetry.

    Trust model for WHO: X-Tunnel-User is honored only from loopback peers — the
    tunnel-client connects from localhost, and the relay validated the identity via IAP
    and strips spoofed inbound copies (qsi-automation#525). From any OTHER peer the
    header is an unauthenticated LAN client's claim, so it is logged as a claim rather
    than as the actor — which makes spoof attempts themselves visible in the trail.

    `outcome` records what actually happened ("ok", "rejected: ...", "error: ..."), so
    a forensic reader can distinguish completed actions from refused/failed attempts."""
    peer = request.client.host if request.client else "?"
    claimed = request.headers.get("x-tunnel-user")
    if claimed and peer in ("127.0.0.1", "::1"):
        actor = claimed
        # Untrusted forensics breadcrumb: the relay forwards Cloud Run's XFF chain,
        # whose first hop is the real browser IP. Annotation only — never the actor.
        if xff := request.headers.get("x-forwarded-for"):
            actor = f"{claimed} [via {xff.split(',')[0].strip()[:45]}]"
    elif claimed:
        actor = f"local:{peer} claiming {claimed[:60]!r}"
    else:
        actor = f"local:{peer}"
    shown = f" keys={keys[:80]!r}" if keys is not None and _AUDIT_KEYS else ""
    _audit_log.info(
        "AUDIT %s pane=%s by %s%s%s%s",
        action,
        pane_id,
        actor,
        f" {detail}" if detail else "",
        shown,
        "" if outcome == "ok" else f" [{outcome}]",
    )
    try:
        from . import telemetry

        telemetry.emit_action(
            action=action,
            pane_uid=f"{tmux.server_uid()}:{pane_id}",
            actor=actor[:200],
            detail=detail or None,
            keys=keys if _AUDIT_KEYS else None,
            outcome=outcome,
        )
    except Exception:  # noqa: BLE001 - audit telemetry must never break the request
        logger.debug("audit emit failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    target = os.environ.get("TMUXRC_TARGET")
    use_llm = os.environ.get("TMUXRC_NO_LLM") != "1"
    app.state.watcher = Watcher(target=target, use_llm=use_llm)
    app.state.watcher.start()
    yield
    await app.state.watcher.stop()


app = FastAPI(title="tmux-rc", lifespan=lifespan)


@app.middleware("http")
async def no_cache(request, call_next):
    """Never let the browser cache anything. StaticFiles sends an ETag but no
    Cache-Control, so phones serve stale JS/HTML heuristically and edits never appear.
    Force no-store on every response — fine for a live dev tool on the LAN."""
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    for h in ("etag", "last-modified"):
        if h in resp.headers:
            del resp.headers[h]
    return resp


@app.get("/api/version")
def get_version():
    """Hash of the web assets, so the client can reload itself when they change
    (see app.js). Cheap to recompute per call — the web dir is tiny."""
    import hashlib

    h = hashlib.md5()
    for p in sorted(WEB_DIR.glob("*")):
        if p.is_file():
            h.update(p.name.encode())
            h.update(str(p.stat().st_mtime_ns).encode())
    return {"version": h.hexdigest()}


@app.get("/api/state")
def get_state():
    from .llm import last_error, usage_totals

    w = app.state.watcher
    return {
        "stale": w.is_stale(),
        "llm_error": last_error[
            "msg"
        ],  # transient; UI shows it subtly, not a big banner
        "usage": usage_totals(),  # running tokens/cost/calls/errors for the top-bar readout
        "prefix": tmux.prefix_key(),  # auto-detected tmux prefix, so the phone button matches
        "panes": w.states,
    }


@app.get("/api/digest")
def get_digest():
    """Per-pane state + recent history in one GET — the endpoint for agents/scripts.
    /api/state is shaped for the phone (only NEW events per parse; the phone refetches
    the server-side event log on demand); this returns the whole picture in one shot:
    headline, activity, idle time,
    pending question, the LLM idle-summary, and the recent timestamped event history."""
    return {"panes": app.state.watcher.digest()}


@app.get("/api/panes/{pane_id}/events")
def list_events(pane_id: str):
    """The pane's activity-log cache (bootstrap-seeded history + live events). The
    phone fetches this instead of accumulating client-side, so a page reload doesn't
    start the feed from zero. In-memory, not persisted (tmux is the state).
    states[].events_seq (a monotonic append counter) signals when to refetch. See
    docs/design/activity-log.md."""
    # Snapshot copy: the watcher mutates this list from its worker thread (to_thread),
    # so serializing the live object could race a concurrent extend/trim.
    return list(app.state.watcher.events_log.get(pane_id, []))


@app.get("/api/panes/{pane_id}/snapshots")
def list_snapshots(pane_id: str):
    hist = app.state.watcher.snapshots.get(pane_id, [])
    return [{"id": s["id"], "ts": s["ts"]} for s in hist]


@app.get("/api/panes/{pane_id}/snapshots/{snap_id}", response_class=PlainTextResponse)
def get_snapshot(pane_id: str, snap_id: str):
    text = app.state.watcher.snapshot_text(pane_id, snap_id)
    if text is None:
        raise HTTPException(404, "snapshot not found")
    return text


@app.post("/api/panes/{pane_id}/send")
def send(pane_id: str, body: SendBody, request: Request):
    detail = f"enter={body.enter} literal={body.literal}"
    if tmux.find_pane(pane_id) is None:
        # Refused attempts are audited too — probing for pane ids is exactly the
        # traffic a forensic reader wants to see.
        _audit(
            request,
            "send_keys",
            pane_id,
            detail,
            body.keys,
            outcome="rejected: pane not found",
        )
        raise HTTPException(404, "pane not found")
    try:
        tmux.send_keys(pane_id, body.keys, enter=body.enter, literal=body.literal)
    except Exception as e:
        _audit(
            request, "send_keys", pane_id, detail, body.keys, outcome=f"error: {e}"[:80]
        )
        raise
    _audit(request, "send_keys", pane_id, detail, body.keys)
    return {"ok": True}


@app.post("/api/panes/{pane_id}/select")
def select(pane_id: str, request: Request):
    """Focus this pane in tmux itself — tapping a card on the phone follows on host."""
    if tmux.find_pane(pane_id) is None:
        _audit(request, "select_pane", pane_id, outcome="rejected: pane not found")
        raise HTTPException(404, "pane not found")
    try:
        tmux.select_pane(pane_id)
    except Exception as e:
        _audit(request, "select_pane", pane_id, outcome=f"error: {e}"[:80])
        raise
    _audit(request, "select_pane", pane_id)
    return {"ok": True}


@app.post("/api/panes/{pane_id}/image")
async def send_image(pane_id: str, file: UploadFile, request: Request):
    """Attach an image by typing the staged file's PATH into the pane (no Enter, so it
    joins whatever the user is composing). Agents read files from disk — Claude Code
    treats an image path in the prompt as an attachment. The old clipboard+Ctrl-V route
    died whenever the session's clipboard did (Xwayland restarts rotate the auth cookie;
    wl-copy holders wedge) — and failed SILENTLY, returning 200 with nothing pasted.
    A path on disk has no such failure mode."""
    if tmux.find_pane(pane_id) is None:
        _audit(request, "paste_image", pane_id, outcome="rejected: pane not found")
        raise HTTPException(404, "pane not found")
    mime = file.content_type or "image/png"
    if mime not in _EXT:
        _audit(
            request,
            "paste_image",
            pane_id,
            detail=mime,
            outcome="rejected: unsupported type",
        )
        raise HTTPException(415, f"unsupported image type: {mime}")
    data = await file.read()
    detail = f"{mime} {len(data)}B"

    # Stage to disk, prune stale stagings (the pane reads the file right after the
    # paste; a day of slack covers "answer later" without growing /tmp forever).
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(IMG_DIR, 0o700)  # /tmp is shared: don't let umask leave stagings listable
    cutoff = time.time() - 86400
    for old in IMG_DIR.iterdir():
        try:  # regular files only; tolerate races with concurrent prunes
            if old.is_file() and old.stat().st_mtime < cutoff:
                old.unlink(missing_ok=True)
        except OSError:
            continue
    fd, path = tempfile.mkstemp(prefix="img-", suffix=_EXT[mime], dir=IMG_DIR)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)

    # Trailing space keeps the path a clean token next to typed text / later pastes.
    tmux.send_keys(pane_id, f"{path} ", enter=False, literal=True)
    _audit(request, "paste_image", pane_id, detail)
    return {"ok": True, "path": path, "bytes": len(data)}


# PWA static files last so /api/* wins. html=True serves index.html at /.
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def main() -> None:
    import uvicorn

    # Reload watches the package source and restarts the process on edits (resetting
    # the watcher's in-memory cache — safe, tmux is the source of truth and state
    # rebuilds within a couple ticks). ON by default; set TMUXRC_RELOAD=0 to disable.
    reload = os.environ.get("TMUXRC_RELOAD", "1") != "0"
    # proxy_headers=False: uvicorn's default rewrites request.client from
    # X-Forwarded-For on loopback connections — and the tunnel relay forwards Cloud
    # Run's XFF, so legit tunnel requests LOOKED like they came from the relay's IP and
    # the audit trust gate (loopback-only) refused their identity. The direct TCP peer
    # is what the trust model needs.
    # log_config=None: don't install uvicorn's own handlers/formatters — its loggers
    # (uvicorn.access etc.) then propagate to root and share the timestamped format above.
    uvicorn.run(
        "daemon.server:app" if reload else app,
        proxy_headers=False,
        log_config=None,
        host=os.environ.get("TMUXRC_HOST", "0.0.0.0"),
        port=int(os.environ.get("TMUXRC_PORT", "8080")),
        reload=reload,
        reload_dirs=["daemon"] if reload else None,
    )


if __name__ == "__main__":
    main()
