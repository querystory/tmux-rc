"""FastAPI app: serves the PWA and the state/answer API.

Endpoints:
  GET  /api/state                 -> list of raw parser-JSON dicts (list-shaped for M2)
  GET  /api/panes/{id}/snapshots  -> recent snapshot ids + timestamps
  GET  /api/panes/{id}/snapshots/{snap} -> raw captured text of one snapshot
  POST /api/panes/{id}/send       -> inject keys / answer a prompt
  GET  /                          -> PWA (static)
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import subprocess
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

# Load .env BEFORE importing the watcher/llm/telemetry chain — those read config from
# os.environ at import time (model, GOOGLE_CLOUD_PROJECT, OTEL endpoint). Without this, a
# launch that didn't inherit the env (e.g. a stray `make dev`) silently loses Vertex creds
# and every parse fails. Real environment vars still win over .env (override=False).
from dotenv import find_dotenv, load_dotenv

# One spelling of the package dir and the dir above it, reused for .env / web/ / docs
# resolution below. In a source checkout the parent is the repo root; in an installed
# wheel it's site-packages. Which one we're in is decided by asset existence, not this
# path alone (see WEB_DIR).
_PKG_DIR = Path(__file__).resolve().parent  # .../daemon (checkout or site-packages)
_REPO_ROOT = _PKG_DIR.parent

# Prefer the repo-root .env next to the package (the dev/run-from-checkout case); if that
# doesn't exist (e.g. installed as a wheel and launched elsewhere), fall back to the
# usual upward search from cwd. Either way, real env vars still win (override=False).
_repo_env = _REPO_ROOT / ".env"
load_dotenv(_repo_env if _repo_env.exists() else find_dotenv(usecwd=True))

# Networks with an advertised-but-dead IPv6 route (common behind home routers) hang any
# client that walks AAAA records serially — the Vertex Live websocket handshake times out
# before an A record is ever tried. Sorting IPv4 first is harmless where v6 works and
# unbreaks all daemon egress (Vertex, OTLP) where it doesn't. TMUXRC_PREFER_IPV4=0 opts
# out for the mirror-image network (working v6, broken v4).
if os.environ.get("TMUXRC_PREFER_IPV4", "1") != "0":
    _getaddrinfo = socket.getaddrinfo
    socket.getaddrinfo = lambda *a, **kw: sorted(
        _getaddrinfo(*a, **kw), key=lambda info: info[0] != socket.AF_INET
    )

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

# Key on the actual asset, not a marker file: the repo-root web/ exists only in a source
# checkout, since the wheel carries the UI bundled at daemon/web/ instead. Its presence is
# therefore an unambiguous "running from a checkout" signal — a stray pyproject.toml beside
# the package in a shared venv (or a `pip install --target` into such a dir) can't fake it.
# Prefer the checkout copy so edits are served live (and /api/version's hash changes, so the
# client self-reloads); fall back to the bundled copy when installed. Same asset-existence
# predicate as the .env lookup above. Caveat: a *non-editable* `pip install .` from a
# checkout has no daemon/-adjacent web/ and serves the bundled install-time snapshot, so
# later edits to that checkout's web/ won't show — use an editable install or uvx for live
# edits. _FROM_CHECKOUT reuses this predicate to gate reload in main().
_repo_web = _REPO_ROOT / "web"
_FROM_CHECKOUT = _repo_web.is_dir()
WEB_DIR = _repo_web if _FROM_CHECKOUT else _PKG_DIR / "web"
# Uploaded images land here so the agent can read them by path. Kept out of the repo.
IMG_DIR = Path(tempfile.gettempdir()) / "tmux-rc-images"
IMG_MAX_BYTES = (
    20 * 2**20
)  # generous for phone photos; blocks memory-ballooning uploads
# A client-error report is a handful of short structural fields plus one capped message;
# anything larger is malformed/hostile, so reject before parsing (fields are re-capped in
# telemetry too — this bounds the READ so a huge body can't balloon memory).
CLIENT_ERROR_MAX_BYTES = 8 * 2**10
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


class NewWindowBody(BaseModel):
    session: str
    launcher: str  # label of a configured launcher — never a raw command


# Agent launchers offered by the dock's "+" menu. Configurable so a fleet can offer
# model/provider variants ("Claude (Fable)" → `claude --model fable`); the phone sends
# back only the LABEL and the daemon looks the command up here, so the HTTP surface
# can't be asked to run arbitrary strings. `icon` names one of the web app's built-in
# tool logos (claude/codex/gemini/shell) or any image URL it serves.
# TMUXRC_LAUNCHERS: inline JSON list, or a path to a JSON file containing one.
_DEFAULT_LAUNCHERS = [
    {"label": "Claude", "command": "claude", "icon": "claude"},
    {"label": "Codex", "command": "codex", "icon": "codex"},
    {"label": "Gemini", "command": "gemini", "icon": "gemini"},
]


def _launchers() -> list[dict]:
    raw = os.environ.get("TMUXRC_LAUNCHERS", "").strip()
    if not raw:
        return _DEFAULT_LAUNCHERS
    try:
        if not raw.startswith("["):
            raw = Path(raw).read_text(encoding="utf-8")
        entries = json.loads(raw)
        good = [
            {"label": str(e["label"]), "command": str(e["command"]),
             "icon": str(e.get("icon", ""))}
            for e in entries
            if isinstance(e, dict) and e.get("label") and e.get("command")
        ]
        if good:
            return good
        raise ValueError("no valid entries")
    except Exception:  # noqa: BLE001 - a broken config must not brick the menu
        logger.warning("TMUXRC_LAUNCHERS invalid; using defaults", exc_info=True)
        return _DEFAULT_LAUNCHERS


class ClientErrorBody(BaseModel):
    """A browser-side failure report (see /api/client-error, web/app.js reportError).
    All optional so a partial report still lands; fields are length-capped in the
    endpoint before they reach telemetry."""

    kind: str = "unknown"  # site: mic | ws | poll | onerror | unhandledrejection
    name: str | None = None  # error class (NotAllowedError, TypeError, …)
    endpoint: str | None = None  # URL/path it failed against
    session: str | None = None  # page-load id (joins to live/parse telemetry)
    message: str | None = None  # free-text — to OTel only under TMUXRC_QSDEBUG


# Audit lines ride the standard root config above (INFO). Routine records, not warnings.
_audit_log = logging.getLogger("daemon.server.audit")

# Key CONTENT in the audit trail is on by default (the operator asked for exactly this
# visibility) but can be switched off: keys typed via the phone can include no-echo
# secrets (sudo/ssh passwords) that nothing else in the system captures — pane capture
# never sees unechoed input — and a forwarded journal would persist them. Set
# TMUXRC_AUDIT_KEYS=0 to log actions without key content (local log AND telemetry).
_AUDIT_KEYS = os.environ.get("TMUXRC_AUDIT_KEYS") != "0"


def _trusted_user(request: Request | None) -> str | None:
    """The tunnel owner's email, honored ONLY from a loopback peer (the same trust
    model as _audit): the tunnel-client connects from localhost having validated the
    identity via IAP and stripped spoofed inbound copies. A LAN client's claim is
    unverified, so we return None rather than record it — live telemetry's `actor` is a
    billing-grade attribution key, not a forensic breadcrumb, so an unverifiable claim
    must simply be absent (the anonymous `session` still carries the usage).

    `request` is Optional: live_frame defaults it to None for unit tests, and no actor
    can be attributed without one — return None rather than raise."""
    if request is None:
        return None
    peer = request.client.host if request.client else "?"
    claimed = request.headers.get("x-tunnel-user")
    return claimed if (claimed and peer in ("127.0.0.1", "::1")) else None


def _ua_class(ua: str | None) -> str | None:
    """Coarse platform bucket for a client-error report — the ANSWER to "on what
    platforms does the mic fail" without storing the full (fingerprintable, free-text)
    User-Agent. Derived server-side from the request's own UA, never client-supplied."""
    if not ua:
        return None
    u = ua.lower()
    if "android" in u:
        return "android"
    if "iphone" in u or "ipad" in u or "ipod" in u:
        return "ios"
    if "macintosh" in u or "mac os" in u:
        return "mac"
    if "windows" in u:
        return "windows"
    if "linux" in u:
        return "linux"
    return "other"


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


# Swagger UI moves off /docs to /apidocs so /docs belongs to the Hugo docs site
# (FastAPI's default /docs would otherwise shadow the bare /docs path). ReDoc follows.
app = FastAPI(
    title="tmux-rc", lifespan=lifespan, docs_url="/apidocs", redoc_url="/apiredoc"
)
# Terminal frames are ~13KB raw but ~4.6x compressible (mostly repeated text/escapes).
# The live stream sends one every screen change — gzip drops it to ~2.8KB, turning a
# busy pane's ~100KB/s into ~22KB/s. minimum_size skips tiny replies (no-change frames).
from starlette.middleware.gzip import GZipMiddleware  # noqa: E402

app.add_middleware(GZipMiddleware, minimum_size=512)

# Live Mode (voice): one WebSocket per session — see daemon/live.py and
# docs/design/live-mode.md.
from . import live  # noqa: E402
from .live import router as live_router  # noqa: E402

app.include_router(live_router)


@app.middleware("http")
async def no_cache(request, call_next):
    """Never let the browser cache anything. StaticFiles sends an ETag but no
    Cache-Control, so phones serve stale JS/HTML heuristically and edits never appear.
    Force no-store on every response — fine for a live dev tool on the LAN."""
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    # Live Mode's getUserMedia needs microphone permission granted to THIS origin. An
    # installed PWA / any embedding context can have the mic feature gated off by the
    # default Permissions-Policy even over HTTPS; explicitly allow it for self so the
    # browser prompts (and the PWA keeps the grant) instead of silently rejecting.
    resp.headers["Permissions-Policy"] = "microphone=(self)"
    for h in ("etag", "last-modified"):
        if h in resp.headers:
            del resp.headers[h]
    # Byte size for the live stream, so the payload cost is visible when debugging.
    # DEBUG, not INFO: a busy pane (or several viewers) emits back-to-back responses
    # and this would drown the normal INFO log. endswith, not substring, so it can't
    # accidentally match some future path containing "/live".
    cl = resp.headers.get("content-length")
    if cl and request.url.path.endswith("/live"):
        logger.debug("%s -> %s bytes", request.url.path, cl)
    return resp


@app.get("/api/version")
def get_version():
    """Hash of the web assets, so the client can reload itself when they change
    (see app.js). Cheap to recompute per call — the web dir is tiny. Also reports
    server feature flags the client gates UI on (live_enabled → shows the mic button)."""
    h = hashlib.md5()
    for p in sorted(WEB_DIR.glob("*")):
        if p.is_file():
            h.update(p.name.encode())
            h.update(str(p.stat().st_mtime_ns).encode())
    return {"version": h.hexdigest(), "live_enabled": live.enabled()}


# How long a /api/state long-poll holds before returning unchanged (client re-holds).
# Well under any proxy/tunnel idle timeout, matching the live stream's hold budget.
STATE_HOLD_SECONDS = 25.0


@app.get("/api/state")
async def get_state(v: int | None = None):
    """Deck state for the phone. With `?v=<version>` this LONG-POLLS: it holds until the
    watcher's state_version passes `v` (a pane switch, add/remove, label/activity change,
    or new events on any pane) or ~25s elapses, then returns the fresh state plus the new
    `version`. The client immediately re-holds with that version, so a pane switch shows
    up within the fast-poll cadence instead of a fixed 2s interval. Omitting `v` returns
    immediately (unchanged legacy behavior)."""
    from .llm import last_error, usage_totals

    w = app.state.watcher
    version = w.state_version()
    # Only long-poll once the watcher has produced an initial state (version > 0).
    # A client that sends ?v=0 before the deck has ever ticked (daemon startup, or the
    # legacy first-load behavior) must get the current state now, not hold for ~25s.
    if v is not None and v == version and version > 0:
        version = await w.wait_for_state_change(v, STATE_HOLD_SECONDS)
    # Snapshot version and panes together, AFTER any wait: re-read the version so the echo
    # matches what we're about to serialize, and shallow-copy each pane dict so the worker
    # thread's in-place updates (the fast tmux_active flip) can't mutate objects mid-encode.
    version = w.state_version()
    panes = [dict(s) for s in w.states]
    return {
        "version": version,  # echo so the client re-holds on the next value
        "stale": w.is_stale(),
        # False until the first tick finishes: an empty `panes` then means "still loading
        # the initial parses," not "no panes" — the UI shows a spinner vs. the empty message.
        "booted": w.booted(),
        "llm_error": last_error[
            "msg"
        ],  # transient; UI shows it subtly, not a big banner
        "usage": usage_totals(),  # running tokens/cost/calls/errors for the top-bar readout
        "prefix": tmux.prefix_key(),  # auto-detected tmux prefix, so the phone button matches
        "panes": panes,
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


def _pane_err(e: subprocess.CalledProcessError) -> HTTPException:
    """capture failures, honestly: _run turns tmux timeouts into rc 124 — that's a
    wedged tmux, not a missing pane."""
    return HTTPException(
        *((504, "tmux timed out") if e.returncode == 124 else (404, "pane not found"))
    )


# Live view (docs/design/live-view.md): long-poll — hold until the screen differs
# from what the client displays, then send the WHOLE colored frame (v1 is full
# frames by design: resize/reflow/alt-screen all reduce to "new frame", no delta
# edge cases). `frame` is a content hash, ETag-style: it's how we know the client
# is current, and the no-change answer when the hold expires idle.
LIVE_HOLD_SECONDS = 25  # under the tunnel-client's 60s request bound
LIVE_CHECK_SECONDS = 0.25  # freshness floor — server constant, not a client knob


@app.get("/api/panes/{pane_id}/live")
async def live_frame(
    # request defaults to None only so unit tests can drive the handler directly; FastAPI
    # still injects the real Request. (A `Request | None` annotation breaks FastAPI — it
    # tries to treat it as a Pydantic field — so the bare-Request default is deliberate.)
    pane_id: str,
    request: Request = None,
    frame: str = "",
    session: str = "",
):
    """One long-poll round: the client sends the hash of the frame it's showing and
    we answer with a newer colored frame, or {frame: same} after ~25s of no change
    (the client immediately re-holds). Captures run in a worker thread on a 250ms
    cadence — decoupled from the watcher, never an LLM call. An idle watched pane
    costs one request per hold; a busy one streams responses back-to-back.

    The change hash is over the RAW colored frame — every visible change (including a
    spinner tick or ticking timer) is a new frame, because live mode means live. The
    client repaints only when the rendered HTML actually differs (no-op skip otherwise),
    so full fidelity here does not flicker.

    `session` is the client's per-page-load UUID: the summable spine for live-time /
    usage telemetry (see docs/design/live-telemetry.md). We stamp presence once per
    round after the first successful capture and emit ONE telemetry record per round."""
    started = time.monotonic()
    deadline = started + LIVE_HOLD_SECONDS
    stamped = False
    while True:
        try:
            text = await asyncio.to_thread(tmux.capture_pane, pane_id, keep_colors=True)
        except subprocess.CalledProcessError as e:
            raise _pane_err(e) from None
        # Presence ONCE per round, on the FIRST successful capture: a viewer of a live
        # pane counts (even mid-hold), but a 404/wedged pane never flips has_live_viewer
        # true (that would suppress parse-throttling for a phantom viewer). Stamping every
        # 250ms iteration is needless cross-thread dict churn — one stamp per ~25s round
        # keeps the 60s presence window fresh just as well.
        if not stamped:
            _note_live_poll(pane_id)
            stamped = True
        data = text.encode()  # encode once — reused for the hash and the byte count
        h = hashlib.md5(data).hexdigest()  # full digest: a truncated hash could collide
        # a changed frame onto the client's hash and stall the stream
        changed = h != frame
        if changed or time.monotonic() >= deadline:
            _emit_live_round(
                request,
                pane_id,
                session,
                time.monotonic() - started,
                changed,
                len(data) if changed else None,
            )
            # changed ⇒ send the new colored frame; else unchanged, client re-holds.
            return {"frame": h, "text": text} if changed else {"frame": h}
        await asyncio.sleep(LIVE_CHECK_SECONDS)


def _note_live_poll(pane_id: str) -> None:
    """Stamp live-viewer presence on the watcher, best-effort — the watcher may not be
    wired (e.g. the handler driven directly in a unit test), and presence must never
    break the live stream."""
    try:
        app.state.watcher.note_live_poll(pane_id)
    except Exception:  # noqa: BLE001 - presence must never break the stream
        logger.debug("live presence stamp failed", exc_info=True)


def _emit_live_round(
    request: Request | None,
    pane_id: str,
    session: str,
    hold_s: float,
    changed: bool,
    raw_bytes: int | None,
) -> None:
    """Best-effort telemetry for one completed live round. Off the request's critical
    path (the response is already decided) and fully swallowed, so it can never break
    or slow the live stream."""
    try:
        from . import telemetry

        w = app.state.watcher
        telemetry.emit_live(
            # Pass through as-is: an absent session (empty string) is left
            # un-attributable by emit_live, NOT collapsed under a shared id that would
            # mis-sum unrelated viewers' watch-time.
            session=session or None,
            pane_uid=f"{tmux.server_uid()}:{pane_id}",
            pane_label=w.label_for(pane_id),
            tool=w.tool_for(pane_id),
            hold_s=hold_s,
            changed=changed,
            raw_bytes=raw_bytes,
            actor=_trusted_user(request),
        )
    except Exception:  # noqa: BLE001 - live telemetry must never break the stream
        logger.debug("live emit failed", exc_info=True)


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
    # Input changes the screen — force an immediate re-parse so an answered question /
    # closed menu reflects on the card within a capture, not a poll interval later.
    app.state.watcher.request_reparse(pane_id)
    return {"ok": True}


@app.get("/api/launchers")
def launchers():
    """The dock '+' menu's entries — labels/icons only. Commands never leave the daemon:
    the phone posts a label back and the lookup happens server-side (see new_window)."""
    return {"launchers": [{"label": e["label"], "icon": e["icon"]} for e in _launchers()]}


@app.post("/api/windows")
def new_window(body: NewWindowBody, request: Request):
    """Open a new window in `session` running a CONFIGURED launcher. The label→command
    mapping lives in the daemon so this endpoint can't be handed arbitrary strings —
    anything not in the config is refused (and audited)."""
    entry = next((e for e in _launchers() if e["label"] == body.launcher), None)
    # !r + a cap: both fields are client-supplied, and an audit line is one line. A
    # newline in `session` would otherwise forge a second record in the log.
    detail = f"session={body.session[:80]!r} launcher={body.launcher[:80]!r}"
    if entry is None:
        _audit(request, "new_window", "-", detail, outcome="rejected: unknown launcher")
        raise HTTPException(404, "unknown launcher")
    if not any(p.session == body.session for p in tmux.list_panes()):
        _audit(request, "new_window", "-", detail, outcome="rejected: session not found")
        raise HTTPException(404, "session not found")
    try:
        pane_id = tmux.new_window(body.session, entry["label"], entry["command"])
    except Exception as e:
        _audit(request, "new_window", "-", detail, outcome=f"error: {e}"[:80])
        raise
    _audit(request, "new_window", pane_id, detail)
    return {"ok": True, "pane_id": pane_id}


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


@app.post("/api/client-error")
async def client_error(request: Request):
    """Sink for browser-side failures invisible on mobile (no devtools) — mic denial, ws
    onclose, poll-loop catch, uncaught exceptions. Forwards to OTel next to parse/live
    telemetry so client failures are queryable by platform (issue #57).

    Caps the body so a huge report can't balloon daemon memory (fields are re-capped in
    telemetry). Best-effort: a malformed/oversized report is dropped with the right status,
    never raised past here — reporting an error must not itself become an error."""
    # Accumulate the stream up to the cap and abort the moment it's exceeded — never
    # buffer the whole body first (request.body() would, and a chunked / Content-Length-
    # less client could balloon memory past the cap before any check ran).
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > CLIENT_ERROR_MAX_BYTES:
            raise HTTPException(413, "client-error report too large")
    try:
        body = ClientErrorBody.model_validate_json(raw)
    except Exception:  # noqa: BLE001 - a malformed report is a 400, not a 500
        raise HTTPException(400, "invalid client-error report") from None
    try:
        from . import telemetry

        telemetry.emit_client_error(
            kind=body.kind,
            name=body.name,
            endpoint=body.endpoint,
            # Coarse platform bucket from the request's own UA — never trust a
            # client-supplied class. Same loopback trust model as the audit actor.
            ua_class=_ua_class(request.headers.get("user-agent")),
            session=body.session,
            actor=_trusted_user(request),
            message=body.message,
        )
    except Exception:  # noqa: BLE001 - the report telemetry must never break the request
        logger.debug("client-error emit failed", exc_info=True)
    return {"ok": True}


@app.post("/api/panes/{pane_id}/image")
async def send_image(pane_id: str, file: UploadFile, request: Request):
    """Attach an image to the pane: clipboard + Ctrl-V so the agent embeds it INLINE,
    falling back to typing the staged file's path when no clipboard tool works.

    The clipboard offer is ALWAYS normalized to PNG — paste handlers ask the clipboard
    for image/png, so an offer in the upload's own mime (a phone JPEG) reads as empty
    and the paste silently no-ops. That mime mismatch was the original phone-attach
    bug; PNG-always fixes the happy path, and the typed-path fallback means a broken
    graphical session degrades to a working (if less pretty) paste, never a silent
    200. The upload is staged to disk in both modes."""
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
    # Cap the read: an unbounded read() of a huge upload would balloon daemon memory.
    data = await file.read(IMG_MAX_BYTES + 1)
    if len(data) > IMG_MAX_BYTES:
        _audit(
            request, "paste_image", pane_id, detail=mime, outcome="rejected: too large"
        )
        raise HTTPException(413, f"image too large (max {IMG_MAX_BYTES // 2**20}MB)")
    if not data:
        _audit(request, "paste_image", pane_id, detail=mime, outcome="rejected: empty")
        raise HTTPException(400, "empty upload")
    detail = f"{mime} {len(data)}B"

    # Stage to disk, prune stale stagings (the pane reads the file right after the
    # paste; a day of slack covers "answer later" without growing /tmp forever).
    IMG_DIR.mkdir(parents=True, exist_ok=True)
    # /tmp is shared and sticky: someone could pre-create this path — as a symlink
    # (collecting our chmod + stagings at a target of their choosing) or as their own
    # plain directory (making our chmod EPERM). Stage only in a real dir we own; with
    # ownership verified, the chmod below cannot fail.
    if (
        IMG_DIR.is_symlink()
        or not IMG_DIR.is_dir()
        or IMG_DIR.stat().st_uid != os.getuid()
    ):
        _audit(request, "paste_image", pane_id, outcome="error: staging dir not ours")
        raise HTTPException(
            500, f"{IMG_DIR} is not a directory we own; refusing to stage"
        )
    os.chmod(IMG_DIR, 0o700)  # don't let umask leave stagings listable
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

    # Delivery blocks (Pillow decode, subprocess waits): worker thread, so an upload
    # can't stall the event loop's polling.
    mode = await asyncio.to_thread(_deliver_image, pane_id, data, path)
    _audit(request, "paste_image", pane_id, detail=f"{detail} via {mode}")
    app.state.watcher.request_reparse(pane_id)  # the paste changed the screen
    return {"ok": True, "mode": mode, "path": path, "bytes": len(data)}


def _deliver_image(pane_id: str, data: bytes, path: str) -> str:
    """Get the staged image into the pane; returns the mode for audit/response.
    Clipboard-first: normalize to PNG and Ctrl-V for the inline embed. But a LOCKED
    session means the pane's app cannot read the clipboard (GNOME blocks unfocused
    reads) and the Ctrl-V would silently paste nothing — exactly the remote/phone
    case — so deliver by typed path instead; inline embeds are a desk luxury."""
    tools: list[str] = []
    if not tmux.session_locked():
        try:
            png = data if data[:8] == b"\x89PNG\r\n\x1a\n" else _to_png(data)
            tools = tmux.set_clipboard_image(png)
        except Exception:  # noqa: BLE001 - undecodable: the path route still works
            pass
    if tools:
        tmux.send_keys(pane_id, "C-v", enter=False, literal=False)
        # Claude Code reads + transcodes the pasted image ASYNCHRONOUSLY after C-v,
        # showing its [Image #N] placeholder only once done. Whatever we send next
        # (the following text segment, or submitComposer's final Enter) must not race
        # that ingest, or it lands ahead of the image / submits a half-built line.
        # Runs in a worker thread (to_thread), so this blocks nobody on the loop.
        time.sleep(0.4)
    else:
        # Spaces both sides: the client may have just typed draft text into the
        # pane, and the path must not concatenate onto it (agents trim the space).
        tmux.send_keys(pane_id, f" {path} ", enter=False, literal=True)
    return f"clipboard:{'+'.join(tools)}" if tools else "path"


def _to_png(data: bytes) -> bytes:
    """Transcode image bytes to PNG (Pillow — already a dependency of the LLM stack)."""
    import io

    from PIL import Image

    buf = io.BytesIO()
    with Image.open(io.BytesIO(data)) as im:
        # Dimension guard BEFORE any pixel decode (open only parses the header):
        # a tiny compressed bomb can inflate to gigapixels and pin the daemon.
        # ~40MP comfortably covers any phone photo. Raising routes the caller to
        # the path fallback — the daemon never decodes the bomb.
        if im.width * im.height > 40_000_000:
            raise ValueError(f"suspicious dimensions {im.width}x{im.height}")
        im.save(buf, "PNG")
    return buf.getvalue()


# Docs site (Hugo build) at /docs, before the "/" mount so it wins. The site is built
# with --baseURL /docs/ (see Makefile), so its assets already reference /docs/... —
# mounting the tree here serves them verbatim; StaticFiles strips the /docs prefix on
# lookup. Off by default (no dir = no mount), so dev — which runs Hugo's own hot-reload
# server — isn't shadowed by stale built files. TMUXRC_DOCS_DIR overrides the location.
_docs_dir = os.environ.get("TMUXRC_DOCS_DIR") or str(
    _REPO_ROOT / "docs-site" / "serve"
)
if Path(_docs_dir).is_dir():
    # Bare /docs (no trailing slash) 404s under the real ASGI server — the /docs mount
    # only answers /docs/… and the later "/" catch-all doesn't serve it either. (Note:
    # Starlette's TestClient *does* auto-redirect it, so this route looks removable in a
    # unit test but is load-bearing in production — don't delete it.) Redirect to /docs/.
    @app.get("/docs", include_in_schema=False)
    def _docs_slash():
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/docs/")

    app.mount("/docs", StaticFiles(directory=_docs_dir, html=True), name="docs")

# PWA static files last so /api/* and /docs win. html=True serves index.html at /.
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def main() -> None:
    import uvicorn

    # Reload watches the package source and restarts the process on edits (resetting
    # the watcher's in-memory cache — safe, tmux is the source of truth and state
    # rebuilds within a couple ticks). Defaults ON from a source checkout, OFF when
    # installed as a wheel (no editable source to watch — and a relative reload dir there
    # made uvicorn fall back to watching all of $HOME). TMUXRC_RELOAD forces it either way.
    # Parse it as a real boolean — a bare `!= "0"` would read TMUXRC_RELOAD=false (or an
    # empty value from a `.env` line) as truthy and force a reloader onto immutable
    # site-packages on a wheel install.
    reload = os.environ.get(
        "TMUXRC_RELOAD", "1" if _FROM_CHECKOUT else "0"
    ).strip().lower() in ("1", "true", "yes", "on")
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
        reload_dirs=[str(_PKG_DIR)] if reload else None,
    )


if __name__ == "__main__":
    main()
