"""FastAPI app: serves the PWA and the state/answer API.

Endpoints:
  GET  /api/state                 -> list of raw parser-JSON dicts (list-shaped for M2)
  GET  /api/panes/{id}/snapshots  -> recent snapshot ids + timestamps
  GET  /api/panes/{id}/snapshots/{snap} -> raw captured text of one snapshot
  POST /api/panes/{id}/send       -> inject keys / answer a prompt
  GET  /                          -> redirect to the PWA at /m/ (static)
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import os
import re
import shlex
import shutil
import socket
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections.abc import Callable
from contextlib import asynccontextmanager, suppress
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
_PKG_DIR = Path(__file__).resolve().parent  # .../openbus (checkout or site-packages)
_REPO_ROOT = _PKG_DIR.parent

# Prefer the repo-root .env next to the package (the dev/run-from-checkout case); if that
# doesn't exist (e.g. installed as a wheel and launched elsewhere), fall back to the
# usual upward search from cwd. Either way, real env vars still win (override=False).
_repo_env = _REPO_ROOT / ".env"
load_dotenv(_repo_env if _repo_env.exists() else find_dotenv(usecwd=True))
# Live Mode provider keys (OpenAI, Azure, AI Studio) live in ONE mode-600 file outside
# every checkout, so a worktree's .env or a commit can never carry them. Same
# override=False rule; silently a no-op when the file is absent.
load_dotenv(Path.home() / ".config" / "tmux-rc" / "openai.env")

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
from fastapi.responses import (  # noqa: E402
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles  # noqa: E402
from PIL import Image  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

from . import agent_history, expunge, telemetry, tmux  # noqa: E402
from .config import json_list  # noqa: E402
from .history import History, default_path  # noqa: E402
from .llm import last_error, usage_totals  # noqa: E402
from .plan_usage import PlanUsage  # noqa: E402
from .push import PushManager, claim_question, contract, pane_input  # noqa: E402
from .watcher import Watcher  # noqa: E402

# One standard, human-readable log format for ALL loggers (uvicorn included — main()
# passes log_config=None so its loggers propagate here instead of using its own):
# timestamp, level, logger name. Previously nothing configured logging, so module
# loggers fell through to Python's bare lastResort handler — unprefixed lines, and
# anything below WARNING silently invisible (which pushed routine lines to WARNING just
# to be seen). Import-time, not main(): under --reload the worker process re-imports
# this module but never calls main(). basicConfig is a no-op if root is already set up.
#
# Under systemd, stderr goes to journald, which files every line at the unit's default
# priority (info) — so `journalctl -p warning` hid real WARNING/ERROR lines. Prefixing
# each line with its sd-daemon "<N>" syslog priority lets journald (SyslogLevelPrefix=yes,
# the default) file it correctly, with no systemd-python dependency. Every line of a
# multi-line record (tracebacks) is prefixed, or the traceback would sink back to info.
# Gated on JOURNAL_STREAM matching *our* stderr, not merely being set: it is inherited,
# e.g. by shells under a systemd-launched tmux, where `make dev` must stay unprefixed.
_SYSLOG_PRIORITY = {
    logging.DEBUG: 7, logging.INFO: 6, logging.WARNING: 4, logging.ERROR: 3, logging.CRITICAL: 2,
}


class JournalFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        prefix = f"<{_SYSLOG_PRIORITY.get(record.levelno, 6)}>"
        return "\n".join(prefix + line for line in super().format(record).split("\n"))


def stderr_is_journal() -> bool:
    try:
        st = os.fstat(2)
    except OSError:
        return False
    return os.environ.get("JOURNAL_STREAM") == f"{st.st_dev}:{st.st_ino}"


_log_handler = logging.StreamHandler()
_log_handler.setFormatter(
    (JournalFormatter if stderr_is_journal() else logging.Formatter)(
        "%(asctime)s %(levelname)-7s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S",
    ),
)
_log_level = os.environ.get("TMUXRC_LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=_log_level, handlers=[_log_handler])
# Chatty third-party libraries log a line per LLM call at INFO (httpx: every Vertex
# POST; google_genai: an "AFC is enabled" banner). That's ~2 lines per parse of pure
# noise drowning our own signal — pin them to WARNING. EXCEPT under DEBUG: those same
# loggers are the ones you need when debugging the Vertex/HTTP path, so an explicit
# TMUXRC_LOG_LEVEL=DEBUG unmutes everything.
if _log_level != "DEBUG":
    for _noisy in ("httpx", "httpcore", "google_genai"):
        logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

# Key on the actual asset, not a marker file: the repo-root web/ exists only in a source
# checkout, since the wheel carries the UI bundled at openbus/web/ instead. Its presence is
# therefore an unambiguous "running from a checkout" signal — a stray pyproject.toml beside
# the package in a shared venv (or a `pip install --target` into such a dir) can't fake it.
# Prefer the checkout copy so edits are served live (and /api/version's hash changes, so the
# client self-reloads); fall back to the bundled copy when installed. Same asset-existence
# predicate as the .env lookup above. Caveat: a *non-editable* `pip install .` from a
# checkout has no openbus/-adjacent web/ and serves the bundled install-time snapshot, so
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
    question: str | None = None  # a menu answer's question fingerprint (its `fp`)


class PushKeysBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    p256dh: str = Field(min_length=1, max_length=128)
    auth: str = Field(min_length=1, max_length=64)


class PushSubscriptionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: str = Field(min_length=1, max_length=4096)
    keys: PushKeysBody
    expiration_time: float | None = Field(default=None, alias="expirationTime")


class PushUnsubscribeBody(BaseModel):
    endpoint: str = Field(min_length=1, max_length=4096)


class PushPresenceBody(BaseModel):
    client: str = Field(min_length=1, max_length=100)
    visible: bool


class PushAnswerBody(BaseModel):
    nonce: str = Field(min_length=16, max_length=200)
    option_index: int = Field(ge=0)


class ClickBody(BaseModel):
    frame: str = Field(pattern=r"^[0-9a-f]{32}$")
    from_bottom: int = Field(ge=0)  # lines above the live frame's last line (see tmux.click)
    col: int = Field(ge=1)  # 1-based


class WheelBody(BaseModel):
    lines: int = Field(ge=-30, le=30)  # wheel notches, positive = up (see tmux.wheel)


class ExpungeBody(BaseModel):
    session_id: str  # the session the confirmation named; refused if the pane's has changed
    birth: str  # the pane incarnation the menu was drawn for (its `birth` in /api/state)


class NewWindowBody(BaseModel):
    session: str
    launcher: str  # label of a configured launcher — never a raw command


class NewSessionBody(BaseModel):
    name: str  # checked in new_session, not here, so a refusal is audited
    cwd: str = "~"
    launcher: str | None = None  # a configured label as above; None = a plain shell


# Agent launchers offered by the dock's "+" menu. Configurable so a fleet can offer
# model/provider variants ("Claude (Fable)" → `claude --model fable`); the phone sends
# back only the LABEL and the daemon looks the command up here, so the HTTP surface
# can't be asked to run arbitrary strings. `icon` names one of the web app's built-in
# tool logos (claude/codex/gemini/opencode/omp/shell) or any image URL it serves.
# TMUXRC_LAUNCHERS: inline JSON list, or a path to a JSON file containing one.
_DEFAULT_LAUNCHERS = [
    {"label": "Claude", "command": "claude", "icon": "claude"},
    {"label": "Codex", "command": "codex", "icon": "codex"},
    {"label": "OpenCode", "command": "opencode", "icon": "opencode"},
    {"label": "omp", "command": "omp", "icon": "omp"},
    {"label": "Gemini", "command": "gemini", "icon": "gemini"},
    {"label": "Shell", "command": "bash", "icon": "shell"},
]


def _unavailable(command: str, path: str | None = None, *,
                 daemon_path: bool = True) -> str | None:
    r"""Why nothing here can run `command`, or None if something can — or can't tell.

    A launcher is looked up twice, because there are two PATHs and neither is reliably
    the one that matters. tmux runs the command from the SERVER's environment (`path`,
    from tmux.server_path), and the daemon does not start that server — so a session the
    user opened from a login shell carries nvm and ~/bin, while this unit's own PATH is
    deliberately minimal. Resolving against either one is enough to stay quiet: the point
    is to catch a command that exists NOWHERE (the configured `gemini` that was never
    installed), and a name found in either list is not that.

    Only argv[0] is checked, and a command is a shell string, so leading `VAR=x`
    assignments are skipped the way sh would; `~` is expanded because the documented
    escape hatch (a home-relative path in TMUXRC_LAUNCHERS) is otherwise unresolvable
    here, though the message quotes the token as configured. Everything this can't
    confidently decide is left to the shell rather than guessed at — a false
    "unavailable" would block a working launcher, which is worse than the fuzzy failure
    this exists to explain. So it declines whenever the answer would be a guess:

    - the line isn't a plain argv — a quote, a pipeline, a substitution, a newline, a
      backslash, a bare assignment. `cd /tmp\nclaude` runs a working launcher, and
      answering about its first word would report the BUILTIN `cd` as missing;
    - an assignment to PATH, which changes the very search we would be doing;
    - an option after `exec`/`command`, which belongs to the builtin, not to us;
    - a RELATIVE path, which tmux resolves against the session's directory
      (`new_window -c #{session_path}`) and `shutil.which` would resolve against the
      daemon's own cwd — two different files, so the answer would be meaningless.

    Even two lists is an approximation: the window's shell runs its rc files and can
    prepend more, so a name found in NEITHER can still turn out to exist. That asymmetry
    is deliberate — it costs a window that opens and dies, which is the failure this
    endpoint explains, rather than a refusal to open one that would have worked.

    `daemon_path=False` drops the daemon's own PATH from the lookup, for the one caller
    that knows `path` is the whole answer: new_session with no server yet, whose server
    will inherit only that PATH.

    Returning the reason rather than the word keeps one wording for both callers: the
    phone says the same thing whether it asked before the tap or after it."""
    # Neither of these survives tokenizing: shlex eats a newline as whitespace, and with
    # posix=False it keeps a backslash as a literal character rather than as the escape it
    # is — so `FOO=bar\ baz claude` splits into three words and argv[0] comes out as
    # "baz", refusing a launcher that works. A newline separates commands and a backslash
    # escapes; both are shell syntax, so both end the judging before it starts.
    if re.search(r"[\x00-\x1f\\]", command):
        return None
    try:
        # posix=False KEEPS the quotes on a quoted word, so the "plain argv" gate below
        # can see them and decline. Stripping them first would hide the one case where
        # expanding `~` is wrong: sh does not expand it inside quotes, so `'~/bin/codex'`
        # is a literal path the shell will fail to find while expanduser reports success.
        words = shlex.split(command, posix=False)
    except ValueError:  # unbalanced quotes — the shell's problem to report, not ours
        return None
    # Strip the prefix words sh strips before it has a command to look up: assignments,
    # then a wrapper builtin — `exec claude` is a real launcher config (it replaces the
    # shell with the agent, so the pane dies with it instead of dropping to a prompt) and
    # `command` is its neighbour; both are BUILTINS, so resolving one as if it were the
    # command would report a working launcher as missing. In that order and no other:
    # assignments are a prefix to `exec` itself, and a word after it is already exec's
    # ARGUMENT, so `exec FOO=1 sh` really does make sh look for a file named "FOO=1" —
    # and this then says so, which is the honest answer rather than a lenient one.
    # (Other builtins as argv[0] don't describe a launcher, and the newline gate above
    # already covers the way one realistically appears: `cd /tmp` on its own line.)
    while words and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", words[0]):
        if words.pop(0).startswith("PATH="):
            return None
    if words and words[0] in ("exec", "command"):
        words.pop(0)
        if words and words[0].startswith("-"):
            return None  # `exec -a name cmd`, `command -p cmd`: their options, not argv[0]
    # argv[0] must be a plain word — it is the thing being resolved, so anything that
    # isn't literally a name or a path (a quoted string, a substitution) is unanswerable.
    # The REST of the line only has to be free of shell syntax, which is a much weaker
    # requirement: an argument is not resolved, it merely has to not turn the line into
    # something other than a plain argv. Holding arguments to argv[0]'s spelling meant a
    # colon was enough to abandon the check — `codex --endpoint https://api.example.com`
    # got no preflight at all, which is precisely a config whose argv[0] lives off PATH.
    if not words or not re.fullmatch(r"[\w.@/=+~-]+", words[0]):
        return None
    if any(re.search(r"""[|&;<>()$`\\"'*?\[\]{}]""", w) for w in words[1:]):
        return None  # an operator or an expansion later on: a shell line, not a plain argv
    word = os.path.expanduser(words[0])
    if "/" in word and not os.path.isabs(word):
        return None
    # Either list will do — see the two-PATH note above. A word with a slash is checked as
    # a file by both calls, so passing `path` is harmless there.
    if (daemon_path and shutil.which(word)) or (path is not None and shutil.which(word, path=path)):
        return None
    if os.path.isabs(word):
        # A path answers for itself; neither PATH was ever going to be consulted.
        return f"{words[0]} does not exist, or is not executable."
    if path is None:
        # tmux could not say what its PATH is (no server yet, a wedged one, an old
        # version). That is half an answer, and half an answer here is the daemon's PATH
        # alone — the very thing that was wrong before. Not knowing is not evidence.
        return None
    # Name WHICH PATH to widen. "Put it on the PATH" is actively misleading here: adding
    # the directory to the daemon's unit satisfies the first lookup and silences this
    # message without making the command runnable, because the window inherits the tmux
    # SERVER's environment. The absolute path is the advice that cannot be misapplied.
    if not daemon_path:  # no server yet: `path` is what the login shell will give it
        return (f"{words[0]} is not on your login shell's PATH, which is the PATH a new "
                "tmux server started from here gets. Add it in your shell profile, or "
                "give TMUXRC_LAUNCHERS an absolute path.")
    return (f"{words[0]} is on neither the daemon's PATH nor the tmux server's. An "
            "absolute path in TMUXRC_LAUNCHERS always works; otherwise put it on the PATH "
            "of the shell you start tmux FROM — the window inherits the server's "
            "environment, so widening the daemon's alone would only hide this message.")


def _launcher(e: dict) -> dict:
    if not (e.get("label") and e.get("command")):
        raise ValueError("launcher needs label and command")
    return {"label": str(e["label"]), "command": str(e["command"]), "icon": str(e.get("icon", ""))}


def _launchers() -> list[dict]:
    # All-or-nothing, via the shared helper: one malformed entry falls the WHOLE list back
    # to the defaults rather than being skipped. That is a change from the hand-rolled
    # filter this replaced, and a deliberate one — a silently missing launcher is a menu
    # that looks correct and quietly is not, which nobody investigates, whereas a menu that
    # has visibly reverted sends you to the config and the log line waiting there. The Live
    # model table needs the same rule for a stronger reason (a mis-parsed entry must never
    # be offered at a made-up price), and one rule for both is one thing to know.
    return json_list("TMUXRC_LAUNCHERS", _DEFAULT_LAUNCHERS, _launcher)


class ClientErrorBody(BaseModel):
    """A browser-side failure report (see /api/client-error, web/m/app.js reportError).
    All optional so a partial report still lands; fields are length-capped in the
    endpoint before they reach telemetry."""

    kind: str = "unknown"  # site: mic | ws | poll | onerror | unhandledrejection
    name: str | None = None  # error class (NotAllowedError, TypeError, …)
    endpoint: str | None = None  # URL/path it failed against
    session: str | None = None  # page-load id (joins to live/parse telemetry)
    message: str | None = None  # free-text — to OTel only under TMUXRC_QSDEBUG


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
    to my terminals?". See telemetry.actor for the trust model, telemetry.audit for the
    record."""
    telemetry.audit(action, pane_id, telemetry.actor(request, via=True), detail, keys, outcome)


@asynccontextmanager
async def lifespan(app: FastAPI):
    target = os.environ.get("TMUXRC_TARGET")
    use_llm = os.environ.get("TMUXRC_NO_LLM") != "1"
    try:
        app.state.history = History(default_path())
    except (OSError, sqlite3.Error, ValueError):
        logger.warning("Pane history unavailable; recording disabled", exc_info=True)
        app.state.history = None
    app.state.watcher = Watcher(target=target, use_llm=use_llm, history=app.state.history)
    app.state.watcher.start()
    app.state.push = PushManager(app.state.watcher)
    app.state.push.start()
    app.state.usage = PlanUsage(app.state.history)
    usage_task = asyncio.create_task(app.state.usage.run(app.state.watcher))
    try:
        yield
    finally:
        usage_task.cancel()
        with suppress(asyncio.CancelledError):
            await usage_task
        await app.state.push.stop()
        await app.state.watcher.stop()


# Swagger UI moves off /docs to /apidocs so /docs belongs to the Hugo docs site
# (FastAPI's default /docs would otherwise shadow the bare /docs path). ReDoc follows.
# auto_configure off: FastAPI would otherwise attach its own OTLP exporters, from the
# OTEL_* env the session shares with Claude Code, and ship every request span, metric and
# log to that receiver. Our export is telemetry.py's scoped records, on its own provider.
app = FastAPI(
    title="tmux-rc",
    lifespan=lifespan,
    docs_url="/apidocs",
    redoc_url="/apiredoc",
    telemetry={"auto_configure": False},
)
# Terminal frames are ~13KB raw but ~4.6x compressible (mostly repeated text/escapes).
# The live stream sends one every screen change — gzip drops it to ~2.8KB, turning a
# busy pane's ~100KB/s into ~22KB/s. minimum_size skips tiny replies (no-change frames).
from starlette.middleware.gzip import GZipMiddleware  # noqa: E402

app.add_middleware(GZipMiddleware, minimum_size=512)

# Live Mode (voice): one WebSocket per session — see openbus/live.py and
# docs/design/live-mode.md.
from . import live, live_providers  # noqa: E402
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
    # Scratch previews are arbitrary HTML on the daemon's own origin, where a script could
    # call /api/* (type into terminals) with the viewer's session. A CSP sandbox without
    # allow-same-origin gives them an opaque origin instead: they still run, but the
    # daemon's API is cross-origin to them, exactly as from any other site. That only stops
    # reading responses, so also refuse the blind writes (form posts, no-cors fetches) that
    # would still carry the front door's cookie.
    if request.url.path.startswith("/scratch/"):
        resp.headers["Content-Security-Policy"] = (
            "sandbox allow-scripts allow-popups; form-action 'none'; connect-src 'none'")
    # StaticFiles redirects a directory without its slash to an absolute URL built from
    # the request's own scheme — http:// behind the TLS-terminating tunnel, which the
    # phone can't reach (#174). Make same-host redirects path-only.
    base = str(request.base_url)
    if resp.headers.get("location", "").startswith(base):
        resp.headers["location"] = "/" + resp.headers["location"][len(base):]
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
    (see web/m/app.js). Cheap to recompute per call — the web dir is tiny. Also reports
    server feature flags the client gates UI on (live_enabled → shows the mic button;
    live_models → the labels the model picker offers, shown only when there are ≥2;
    docs → whether /docs is mounted, so the Docs link never points at a 404).
    live_enabled is false when the table is empty even with the flag on. An all-keyless
    table still shows the button: its picker is every row greyed with the key it needs,
    which is the one place the user can learn why nothing runs."""
    h = hashlib.md5()
    for p in sorted(WEB_DIR.rglob("*")):
        if p.is_file():
            h.update(p.relative_to(WEB_DIR).as_posix().encode())
            h.update(str(p.stat().st_mtime_ns).encode())
    # The menu is live.offered() and nothing else — the same list the socket gates on, so
    # the picker can never show a row the socket would refuse. The label is the only thing
    # the browser ever sends back; hints are rendered by the entry (see LiveModel.hint).
    # Keyless table entries follow, greyed with the reason, as launchers do: the user
    # configured them, so the picker says why they can't run instead of hiding them.
    offered = [{"label": m.label, "hint": m.hint, "text": m.text}
               for m in live.offered()]
    keyless = [{"label": m.label, "hint": m.unavailable, "text": m.text, "unavailable": True}
               for m in live_providers.models() if not m.available()]
    return {"version": h.hexdigest(), "live_enabled": live.enabled() and bool(offered or keyless),
            "live_models": offered + keyless, "docs": DOCS_MOUNTED}


# How long a /api/state long-poll holds before returning unchanged (client re-holds).
# Well under any proxy/tunnel idle timeout, matching the live stream's hold budget.
STATE_HOLD_SECONDS = 25.0


def _history() -> History:
    history = getattr(app.state, "history", None)
    if history is None:
        raise HTTPException(status_code=503, detail="Pane history is unavailable")
    return history


@app.get("/api/history")
def get_history(window: str = "24h", lead: str | None = None):
    history = _history()
    try:
        return history.query(window, lead=lead)
    except ValueError:
        raise HTTPException(400, "window and lead are all, or a count of hours or days "
                                 "(24h, 7d) up to 90 days") from None


class GoalBody(BaseModel):
    goal: int | None = Field(..., ge=1, le=999)  # required; an explicit null clears it


@app.put("/api/history/goal")
def put_goal(body: GoalBody, request: Request):
    _history().set_goal(body.goal)
    _audit(request, "set_goal", "-", f"goal={body.goal}")
    return {"goal": body.goal}


@app.get("/api/usage")
def get_usage():
    """Each Claude and Codex account's plan windows, trend and projection."""
    usage = getattr(app.state, "usage", None)
    return {"accounts": usage.report() if usage else []}


@app.get("/api/state")
async def get_state(v: int | None = None, client: str = "", visible: bool = False):
    """Deck state for the phone. With `?v=<version>` this LONG-POLLS: it holds until the
    watcher's state_version passes `v` (a pane switch, add/remove, label/activity change,
    or new events on any pane) or ~25s elapses, then returns the fresh state plus the new
    `version`. The client immediately re-holds with that version, so a pane switch shows
    up within the fast-poll cadence instead of a fixed 2s interval. Omitting `v` returns
    immediately (unchanged legacy behavior)."""
    w = app.state.watcher
    push = getattr(app.state, "push", None)
    if push is not None and client:
        push.note_presence(client, visible=visible)
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
    # Each question carries what a push nonce binds: its contract digest and the input
    # generation its parse captured, plus that parse's frame. A menu answer names all
    # three, and /send refuses it once the pane holds a different ask, has taken input
    # since (a double tap, a second client, a refetch before the reparse lands), or shows
    # a different screen (advanced from the keyboard, its parse not yet published).
    for s in panes:
        if isinstance(s.get("question"), dict):
            fp = ":".join((contract(s, s.get("birth"))[0],
                           str(s.get("input_generation", 0)), s.get("frame", "")))
            s["question"] = {**s["question"], "fp": fp}
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
        "tmux_running": w.tmux_running,  # False = no server at all (e.g. after a reboot)
        "panes": panes,
    }


@app.get("/api/push/config")
def push_config():
    """Return the stable VAPID public key needed by PushManager.subscribe()."""
    _, public = app.state.push.store.keys()
    return {"public_key": public}


@app.post("/api/push/subscribe")
def push_subscribe(body: PushSubscriptionBody, request: Request):
    try:
        app.state.push.subscribe(body.model_dump())
    except ValueError as exc:
        _audit(request, "push_subscribe", "push", outcome=f"rejected: {exc}")
        raise HTTPException(400, str(exc)) from exc
    _audit(request, "push_subscribe", "push")
    return {"ok": True}


@app.post("/api/push/unsubscribe")
def push_unsubscribe(body: PushUnsubscribeBody, request: Request):
    removed = app.state.push.store.remove(body.endpoint)
    _audit(request, "push_unsubscribe", "push", detail=f"removed={removed}")
    return {"ok": True, "removed": removed}


@app.post("/api/push/revoke-all")
def push_revoke_all(request: Request):
    count = app.state.push.store.revoke_all()
    _audit(request, "push_revoke_all", "push", detail=f"removed={count}")
    return {"ok": True, "removed": count}


@app.post("/api/push/presence")
def push_presence(body: PushPresenceBody):
    app.state.push.note_presence(body.client, visible=body.visible)
    return {"ok": True}


@app.post("/api/push/answer")
def push_answer(body: PushAnswerBody, request: Request):
    try:
        pane_id, keys = app.state.push.answer(body.nonce, body.option_index)
    except (ValueError, tmux.PaneChangedError) as exc:
        _audit(request, "push_answer", "push", detail="actor=push-action",
               outcome=f"rejected: {exc}")
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        _audit(request, "push_answer", "push", detail="actor=push-action",
               outcome=f"error: {exc}"[:80])
        raise
    _audit(request, "push_answer", pane_id, detail="actor=push-action", keys=keys)
    return {"ok": True}


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
    start the feed from zero. In memory; its tail survives a restart only for an
    unchanged screen (docs/design/activity-clock-persistence.md).
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
    except Exception:  # presence must never break the stream
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
            actor=telemetry.tunnel_user(request) if request else None,
        )
    except Exception:  # live telemetry must never break the stream
        logger.debug("live emit failed", exc_info=True)


def _pane_input(pane_id: str, *, invalidate: bool = True, watcher=None):
    """push.pane_input on the app's watcher unless one is given."""
    return pane_input(watcher or getattr(app.state, "watcher", None), pane_id,
                      invalidate=invalidate)


def _input_attempt(pane_id: str, deliver: Callable, *args, watcher=None):
    """deliver(*args) inside _pane_input, synchronously: run it in a worker thread whole.
    Cancelling an await does not stop the thread behind it, so a context held around
    `await asyncio.to_thread(...)` would reparse while the delivery is still typing."""
    with _pane_input(pane_id, watcher=watcher):
        return deliver(*args)


@app.post("/api/panes/{pane_id}/send")
def send(pane_id: str, body: SendBody, request: Request):
    detail = f"enter={body.enter} literal={body.literal}"
    # Resolve to the pane's own id and send to THAT. "%3", "work:0.0" and a label can all
    # name one pane, and send_keys locks per pane id — two spellings would take two locks
    # and interleave into the same draft. The lookup is the validation we already do here,
    # so canonicalizing costs nothing; doing it inside send_keys would put a list-panes
    # subprocess on every keystroke. Audits keep the caller's spelling, which is what the
    # client actually asked for.
    pane = tmux.find_pane(pane_id)
    if pane is None:
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
    # A menu digit means nothing on its own: the "1" that said Yes to one ask says Yes to
    # whatever replaced it. So a menu answer names its question, claimed under the send
    # lock exactly as a push action is.
    w, fp = app.state.watcher, body.question
    birth = w.pane_birth(pane.id) if fp else None  # the incarnation the question names

    def claim() -> None:
        digest, generation, frame = fp.split(":")  # a malformed token: ValueError, 409
        claim_question(w, pane.id, digest, int(generation), frame)

    try:
        with _pane_input(pane.id, invalidate=not fp):  # a menu answer's claim bumps it
            tmux.send_keys(pane.id, body.keys, enter=body.enter, literal=body.literal,
                           expected_pid=birth, guard=claim if fp else None)
    except Exception as e:
        # Keys refused at a password prompt are probably the password: never recorded.
        keys = None if isinstance(e, tmux.PasswordPromptError) else body.keys
        _audit(request, "send_keys", pane_id, detail, keys, outcome=f"error: {e}"[:80])
        if isinstance(e, (tmux.PaneChangedError, ValueError)):
            raise HTTPException(409, str(e)) from e
        raise
    _audit(request, "send_keys", pane_id, detail, body.keys)
    return {"ok": True}


@app.post("/api/panes/{pane_id}/click")
def click(pane_id: str, body: ClickBody, request: Request):
    """A tap on the live terminal, forwarded as a mouse click when the pane's app takes
    them (tmux.click). `sent: false` is a normal answer — the tap landed on a shell, or
    on history — so the client just lets it be a tap."""
    def deliver(pane: tmux.Pane) -> bool:
        with _pane_input(pane.id):
            return tmux.click(pane.id, body.from_bottom, body.col, expected_pid=pane.pid,
                              expected_frame=body.frame)
    return {"sent": _mouse("click", pane_id, f"from_bottom={body.from_bottom} col={body.col}",
                           request, deliver)}


@app.post("/api/panes/{pane_id}/wheel")
def wheel(pane_id: str, body: WheelBody, request: Request):
    """Scroll-wheel notches for the pane's own app, from the live view's overscroll past
    its top (tmux.wheel). `sent: false` means the app keeps no history of its own — the
    view already shows everything tmux has — and the client stops asking. No reparse: a
    scroll changes what is on screen, not what the agent is doing."""
    return {"sent": _mouse("wheel", pane_id, f"lines={body.lines}", request,
                           lambda pane: tmux.wheel(pane.id, body.lines, expected_pid=pane.pid))}


def _mouse(action: str, pane_id: str, detail: str, request: Request,
           deliver: Callable[[tmux.Pane], bool]) -> bool:
    """Deliver a synthesized mouse report to the canonical pane (the per-pane lock is
    keyed on it, as in send()) and audit every outcome, refusals included."""
    try:
        pane = tmux.find_pane(pane_id)
        if pane is None:
            _audit(request, action, pane_id, detail, outcome="rejected: pane not found")
            raise HTTPException(404, "pane not found")
        sent = deliver(pane)
    except subprocess.CalledProcessError as e:
        _audit(request, action, pane_id, detail, outcome=f"error: tmux rc {e.returncode}")
        raise _pane_err(e) from e
    except tmux.PaneChangedError as e:
        _audit(request, action, pane_id, detail, outcome="rejected: pane changed")
        raise HTTPException(409, str(e)) from e
    _audit(request, action, pane_id, detail, outcome="ok" if sent else "not sent")
    return sent


@app.get("/api/launchers")
def launchers():
    """The dock '+' menu's entries — labels/icons only. Commands never leave the daemon:
    the phone posts a label back and the lookup happens server-side (see new_window)."""
    # `unavailable` (absent when the command resolves) lets the dialog say WHY a
    # configured launcher can't run instead of offering a button that opens a window
    # which dies in milliseconds. Never hide the entry: the user configured it, so the
    # reason has to be visible.
    out = []
    path = tmux.server_path()  # once: the same answer for every entry
    for e in _launchers():
        item = {"label": e["label"], "icon": e["icon"]}
        why = _unavailable(e["command"], path)
        if why:
            item["unavailable"] = why
        out.append(item)
    return {"launchers": out}


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
    # Check the command EXISTS before opening a window for it. Without this the window
    # is created, the shell exits instantly ("command not found"), the pane is gone
    # before the phone can select it, and the only thing the user sees is a bogus
    # "could not focus this pane" — a third-order symptom of a PATH problem.
    why = _unavailable(entry["command"], tmux.server_path())
    if why:
        _audit(request, "new_window", "-", detail, outcome="rejected: command not found")
        raise HTTPException(400, why)
    try:
        pane_id = tmux.new_window(body.session, entry["label"], entry["command"])
    except Exception as e:
        _audit(request, "new_window", "-", detail, outcome=f"error: {e}"[:80])
        raise
    _audit(request, "new_window", pane_id, detail)
    return _opened(pane_id)


def _opened(pane_id: str) -> dict:
    """The reply for a request that just created a pane. Wakes the watcher NOW instead of
    letting the new pane wait up to a poll interval to be discovered. The tick that runs
    publishes the pane's identity before it classifies it (see watcher._tick), so the card
    the phone just navigated to appears at once as a known-but-unclassified pane rather
    than as a missing pane id. Best-effort: the pane already exists, so a watcher that
    isn't up must not turn a success into a 500 — the next ordinary tick finds it anyway."""
    watcher = getattr(app.state, "watcher", None)
    if watcher is not None:
        watcher.request_reparse(pane_id)
    return {"ok": True, "pane_id": pane_id}


@app.get("/api/sessions/dirs")
def session_dirs():
    """Directory suggestions for the New session dialog: where panes are open now, then
    where past agent sessions ran (agent_history.recent_dirs). Suggestions only — the
    dialog takes any path and new_session validates it — so nothing here browses the
    filesystem. Home-relative paths come back as ~/…, which new_session expands."""
    home = os.path.expanduser("~")
    seen = [s.get("cwd") for s in app.state.watcher.states] + agent_history.recent_dirs()
    tilde = ("~" + d[len(home):] if d == home or d.startswith(home + "/") else d
             for d in seen if d)
    return {"dirs": list(dict.fromkeys(tilde))[:30]}


@app.post("/api/sessions")
def new_session(body: NewSessionBody, request: Request):
    """Create a tmux session in `cwd`, optionally running a configured launcher — and
    start the tmux server if none is running, which is the case this exists for: after a
    reboot nothing else on the phone can bring tmux back. Same label-only rule and
    preflight as new_window. `cwd` is not confined to $HOME: a client that can type into
    any shell here can already `cd` anywhere, so a fence would only cost real use."""
    detail = (f"session={body.name[:80]!r} cwd={body.cwd[:120]!r} "
              f"launcher={(body.launcher or '')[:80]!r}")

    def refuse(status: int, why: str):
        _audit(request, "new_session", "-", detail, outcome=f"rejected: {why}"[:80])
        raise HTTPException(status, why)

    # Single-pane mode publishes only its target, so the new pane could never appear
    # (the history tools are withheld for the same reason: agent_history.offered).
    if os.environ.get("TMUXRC_TARGET"):
        refuse(409, "single-pane mode (TMUXRC_TARGET) cannot show a new session")
    # tmux silently rewrites ':' and '.' in a session name (they are target syntax), so
    # the session would not be called what was asked for; refuse rather than surprise.
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", body.name):
        refuse(422, "session names are 1-64 letters, digits, - and _")
    entry = None
    if body.launcher is not None:
        entry = next((e for e in _launchers() if e["label"] == body.launcher), None)
        if entry is None:
            refuse(404, "unknown launcher")
    cwd = os.path.expanduser(body.cwd)
    if not (os.path.isabs(cwd) and os.path.isdir(cwd)):
        # 422, not 400: the phone reads a 400 as the launcher preflight's verdict and
        # greys that launcher out, which a mistyped directory must not do.
        refuse(422, f"{body.cwd} is not a directory")
    # With no server yet, the launcher will run under exactly the PATH new_session gives
    # the server it starts — a known answer, and the only one: the daemon's own PATH
    # (its virtualenv included) is not inherited, so it must not vouch for the command.
    # One login-shell probe per request, shared by the preflight and the server start.
    env, running = tmux.server_env(), tmux.server_running()
    path = tmux.server_path() if running else env.get("PATH", "")
    why = entry and _unavailable(entry["command"], path, daemon_path=running)
    if why:
        refuse(400, why)
    try:
        pane_id = tmux.new_session(body.name, cwd, entry and entry["command"],
                                   entry["label"] if entry else "", env=env)
    except subprocess.CalledProcessError as e:
        if "duplicate session" in (e.stderr or ""):
            refuse(409, f"a session named {body.name} already exists")
        _audit(request, "new_session", "-", detail, outcome=f"error: tmux rc {e.returncode}")
        raise
    _audit(request, "new_session", pane_id, detail)
    return _opened(pane_id)


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


def _kill_window(request: Request, pane_id: str, action: str, detail: str = "",
                 pid: str | None = None) -> None:
    """Kill the pane's window; with `pid`, only while that process still owns the pane
    (tmux.kill_window), refusing if it no longer does."""
    pane = tmux.find_pane(pane_id)
    if pane is None:
        _audit(request, action, pane_id, detail, outcome="rejected: pane not found")
        raise HTTPException(404, "pane not found")
    try:
        killed = tmux.kill_window(pane.id, pid)
    except Exception as e:
        _audit(request, action, pane_id, detail, outcome=f"error: {type(e).__name__}")
        raise
    if not killed:
        _audit(request, action, pane_id, detail, outcome="rejected: the pane changed")
        raise HTTPException(409, "the pane changed")


@app.post("/api/panes/{pane_id}/close")
def close_window(pane_id: str, request: Request):
    """Close the WINDOW that contains this pane — the phone's "I'm done with this" control.
    Destructive: any process in the window is killed. The watcher evicts the pane on its next
    tick (emitting pane_removed), so the card disappears on the client's next poll with no
    special cleanup — the same path as a window closed on the host."""
    _kill_window(request, pane_id, "kill_window")
    _audit(request, "kill_window", pane_id)
    return {"ok": True}


def _pane_session(pane_id: str, birth: str, expected: str | None = None):
    """The pane (resolved to its canonical %N), its one agent session and that session's
    targets (openbus/expunge.py), or the refusal. `birth` is the incarnation the client's
    menu describes: tmux reuses %N, so a stale menu must not reach a newer pane. A path
    that resolves outside its root refuses here, before anything is killed."""
    pane = tmux.find_pane(pane_id)
    try:  # read now, not from the watcher: a cached screen may predate a recycled pane id
        screen = tmux.capture_pane(pane.id) if pane and pane.pid else ""
    except (OSError, subprocess.CalledProcessError):
        pane = None
    if not (pane and pane.pid):
        raise HTTPException(404, "pane not found")
    if pane.pid != birth:
        raise HTTPException(409, "this is a different window now")
    try:  # a screen from a newer pane under this id fails the pid guard on the kill
        s = expunge.identify(pane.pid, screen, expected)
        return pane, s, expunge.targets(s)
    except expunge.Refused as e:
        raise HTTPException(409, str(e)) from e


@app.get("/api/panes/{pane_id}/expunge")
def expunge_preview(pane_id: str, birth: str):
    """What Expunge would delete, for the confirmation to name."""
    _, s, (own, shared) = _pane_session(pane_id, birth)
    return {"harness": s.harness, "session_id": s.session_id,
            "files": [p.name for p in own], "shared": [p.name for p in shared]}


@app.post("/api/panes/{pane_id}/expunge")
def expunge_session(pane_id: str, body: ExpungeBody, request: Request):
    """Kill the window, then delete its agent session's local files. The window goes first
    so the agent can't write them again, and files go only once it has exited. The audit
    line names the pane and session, never what was deleted."""
    detail = f"session={body.session_id[:64]}"
    try:
        pane, s, _ = _pane_session(pane_id, body.birth, body.session_id)
        uid = app.state.watcher.checkpoint_key(pane.id, pane.pid)
    except HTTPException as e:
        _audit(request, "expunge", pane_id, detail, outcome=f"rejected: {e.detail}"[:80])
        raise
    except (OSError, subprocess.CalledProcessError) as e:
        _audit(request, "expunge", pane_id, detail, outcome="rejected: no tmux server id")
        raise HTTPException(409, "tmux can't name its server, so tmux-rc's own card for the "
                                 "pane couldn't be found: nothing was killed") from e
    # The kill's guard is the pane's process, usually a shell; the agent it ran is checked
    # here too, so one that was replaced since the preview never takes its successor down.
    if not expunge.alive(s):
        _audit(request, "expunge", pane_id, detail, outcome="rejected: agent exited")
        raise HTTPException(409, "the agent exited before the window was killed: nothing was "
                                 "touched")
    if app.state.watcher.history is None:  # its own card for the pane could not be deleted
        _audit(request, "expunge", pane_id, detail, outcome="rejected: no history database")
        raise HTTPException(409, "tmux-rc's history database is unavailable: nothing was "
                                 "touched")
    _kill_window(request, pane.id, "expunge", detail, pane.pid)
    if not expunge.wait_gone(s):  # the window is gone, so this is a failure, not a refusal
        _audit(request, "expunge", pane_id, detail, outcome="error: agent still running")
        raise HTTPException(500, "the window closed, but the agent is still running: "
                                 "nothing was deleted")
    try:
        result = expunge.expunge(s)
    except (OSError, expunge.Refused) as e:
        # Only the error's type: its message can carry a path (a project's name).
        _audit(request, "expunge", pane_id, detail, outcome=f"error: {type(e).__name__}")
        raise HTTPException(500, f"expunge failed partway: {e}") from e
    if not app.state.watcher.forget_checkpoint(uid):
        _audit(request, "expunge", pane_id, detail, outcome="error: checkpoint kept")
        raise HTTPException(500, "the session's files are deleted, but tmux-rc could not "
                                 "delete its own stored card for the pane yet")
    _audit(request, "expunge", pane_id, detail)
    return {"ok": True, **result}


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
        telemetry.emit_client_error(
            kind=body.kind,
            name=body.name,
            endpoint=body.endpoint,
            # Coarse platform bucket from the request's own UA — never trust a
            # client-supplied class. Same loopback trust model as the audit actor.
            ua_class=_ua_class(request.headers.get("user-agent")),
            session=body.session,
            actor=telemetry.tunnel_user(request),
            message=body.message,
        )
    except Exception:  # the report telemetry must never break the request
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
    # Canonical pane id for the same reason /send resolves one: _deliver_image types into
    # the pane, and send_keys locks per pane id.
    pane = tmux.find_pane(pane_id)
    if pane is None:
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

    try:
        path, mode = await attach_image(pane.id, pane.pid, data, mime)
    except Exception as error:
        _audit(request, "paste_image", pane_id, outcome=f"error: {error}"[:80])
        if isinstance(error, tmux.PaneChangedError):
            raise HTTPException(409, str(error)) from error
        raise
    _audit(request, "paste_image", pane_id, detail=f"{detail} via {mode}")
    return {"ok": True, "mode": mode, "path": path, "bytes": len(data)}


async def attach_image(
    pane_id: str, expected_pid: str, data: bytes, mime: str, caption: str | None = None
) -> tuple[str, str]:
    """Stage an already-validated image and deliver it to the pane, bound to the process
    the caller saw there (PaneChangedError if it changed). Returns (path, mode). The one
    delivery path for the pane composer's endpoint and Live Chat's send_image_to_pane.
    With a caption (even ""), the image, the caption and the submitting Enter go in as one
    composer draft under a single pane lock, so no other sender can land between them."""
    path = _stage_image(data, mime)
    # Delivery blocks (Pillow/subprocess waits), so run it outside the event loop.
    if caption is None:
        return path, await asyncio.to_thread(
            _input_attempt, pane_id, _deliver_image, pane_id, data, path, expected_pid)
    segments = [(data, path), *([caption] if caption else [])]
    return path, await asyncio.to_thread(
        _input_attempt, pane_id, _deliver_composer, pane_id, expected_pid, segments)


def _stage_image(data: bytes, mime: str) -> str:
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

    return path


@app.post("/api/panes/{pane_id}/compose")
async def compose(pane_id: str, request: Request):
    """Audit every attempt, including upload validation and staging failures."""
    try:
        return await _compose(pane_id, request)
    except Exception as error:
        _audit(request, "compose", pane_id, outcome=f"error: {error}"[:80])
        if isinstance(error, tmux.PaneChangedError):
            raise HTTPException(409, str(error)) from error
        raise


async def _compose(pane_id: str, request: Request):
    """Receive the entire ordered draft before locking or typing into its pane."""
    pane = tmux.find_pane(pane_id)
    if pane is None:
        raise HTTPException(404, "pane not found")
    segments, secret = [], None
    # Multipart parsing finishes before delivery. Limits also bound the time a single
    # draft can occupy the pane lock; no client round trips happen inside that lock.
    async with request.form(max_files=16, max_fields=128, max_part_size=IMG_MAX_BYTES) as form:
        text_bytes = 0
        for kind, value in form.multi_items():
            if kind == "text" and isinstance(value, str):
                text_bytes += len(value.encode())
                if text_bytes > 256 * 1024:
                    raise HTTPException(413, "composer text too large")
                segments.append(value)
            elif kind == "image" and not isinstance(value, str):
                mime = value.content_type or "image/png"
                if mime not in _EXT:
                    raise HTTPException(415, "unsupported image type")
                data = await value.read(IMG_MAX_BYTES + 1)
                if not data or len(data) > IMG_MAX_BYTES:
                    raise HTTPException(413, "image empty or too large")
                segments.append((data, _stage_image(data, mime)))
            elif kind == "secret" and isinstance(value, str) and secret is None:
                if len(value.encode()) > 4095:  # the tty's line, less its newline
                    raise HTTPException(413, "password too long")
                secret = value
            else:
                raise HTTPException(400, "invalid composer segment")
    # A password answers a no-echo prompt alone, and never reaches an audit or log
    # line: the record says only that one was sent. See tmux.send_secret. The
    # converse, refusing plain text at that prompt, is send_keys' own guard.
    if bool(segments) == bool(secret):
        raise HTTPException(400, "send a draft or a secret")
    if not (secret or "").isprintable():  # a newline or ^D would end the read early,
        raise HTTPException(400, "a password is printable text")  # the rest run as input
    if secret:
        await asyncio.to_thread(_input_attempt, pane.id, tmux.send_secret, pane, secret)
    else:
        await asyncio.to_thread(
            _input_attempt, pane.id, _deliver_composer, pane.id, pane.pid, segments)
    _audit(request, "compose", pane_id,
           detail="secret" if secret else f"{len(segments)} segments")
    return {"ok": True}


def _deliver_composer(pane_id: str, expected_pid: str, segments: list) -> str | None:
    """Type a whole draft under one lock; returns the last image's delivery mode, if any."""
    mode = None
    with tmux.send_transaction(pane_id) as identity:
        # Include time spent uploading and waiting for the lock in the identity guard.
        if identity != expected_pid:
            raise tmux.PaneChangedError("Pane changed while uploading; draft was not sent.")
        for segment in segments:
            tmux.check_pane(pane_id, identity)
            if isinstance(segment, str):
                tmux.send_keys(pane_id, segment, enter=False, expected_pid=identity)
            else:
                mode = _deliver_image(pane_id, *segment, identity)
        tmux.check_pane(pane_id, identity)
        tmux.send_keys(pane_id, "", enter=True, expected_pid=identity)
    return mode


# Clipboard ownership is global even when two drafts target different panes.
_image_delivery_lock = threading.Lock()


def _deliver_image(pane_id: str, data: bytes, path: str, expected_pid: str) -> str:
    with tmux.send_transaction(pane_id) as identity, _image_delivery_lock:
        if identity != expected_pid:
            raise tmux.PaneChangedError("Pane changed while uploading; image was not sent.")
        tmux.check_pane(pane_id, expected_pid)
        return _deliver_image_locked(pane_id, data, path, identity)


def _deliver_image_locked(pane_id: str, data: bytes, path: str, identity: str) -> str:
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
    tmux.check_pane(pane_id, identity)
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


def _mount_static(prefix: str, directory: str) -> None:
    """Serve a directory read-only at prefix/, before the "/" mount so it wins. StaticFiles
    confines lookups to the directory (no ../ or symlink escape) and lists nothing.
    Bare prefix (no trailing slash) 404s under the real ASGI server — the mount only
    answers prefix/… and the later "/" catch-all doesn't serve it either. (Note:
    Starlette's TestClient *does* auto-redirect it, so this route looks removable in a
    unit test but is load-bearing in production — don't delete it.) Redirect to prefix/,
    keeping the query."""

    def slash(request: Request) -> RedirectResponse:
        query = request.url.query
        return RedirectResponse(prefix + "/" + (f"?{query}" if query else ""))

    app.add_api_route(prefix, slash, include_in_schema=False)
    app.mount(prefix, StaticFiles(directory=directory, html=True), name=prefix.strip("/"))


# Docs site (Hugo build) at /docs. The site is built with --baseURL /docs/ (see
# Makefile), so its assets already reference /docs/... — mounting the tree here serves
# them verbatim; StaticFiles strips the /docs prefix on lookup. Off by default (no dir =
# no mount), so dev — which runs Hugo's own hot-reload server — isn't shadowed by stale
# built files. TMUXRC_DOCS_DIR overrides the location.
# /api/version reports DOCS_MOUNTED so the client hides its Docs link instead of linking
# to a 404 when the site was never built. index.html, not just the dir: an empty or
# half-written build dir would mount yet still 404 at /docs/.
_docs_dir = os.environ.get("TMUXRC_DOCS_DIR") or str(
    _REPO_ROOT / "docs-site" / "serve"
)
DOCS_MOUNTED = (Path(_docs_dir) / "index.html").is_file()
if DOCS_MOUNTED:
    _mount_static("/docs", _docs_dir)

# Throwaway previews (mocks, a built site under review) at /scratch/, so showing one on
# the phone needs no second tunnel: it rides the same authenticated front door as /docs.
# Only an explicitly configured dir is served — there is no default — because whatever
# lands in it is published to everyone the tunnel admits. Mocks worth keeping belong in
# docs-site/static/mocks/ instead, which ships with the docs at /docs/mocks/.
_scratch_dir = os.environ.get("TMUXRC_SCRATCH_DIR")
if _scratch_dir and Path(_scratch_dir).is_dir():
    _mount_static("/scratch", _scratch_dir)

# Bare /m needs its own route; /m/ does not. The "/" mount below (html=True) serves
# web/m/index.html for /m/, but answers bare /m with a 307 built from the request's own
# scheme/host — behind the TLS-terminating phone tunnel that is http://…/m/, an origin
# the phone can't reach (#174). GET and HEAD both, since phones/PWAs probe with HEAD.
# The 404 is inline HTML rather than HTTPException(404): the JSON {"detail":"Not Found"}
# body made the phone browser download an "m.json" file (#174), whereas text/html is
# rendered. The check is per-request, so a daemon started before the assets were
# deployed recovers without a restart. (Starlette's TestClient follows that 307 by
# default, so a naive test would pass without this route; test_mobile_entrypoint sets
# follow_redirects=False on purpose to pin it.) response_model=None: FastAPI can't build
# a response model from a union of Response classes and refuses to import otherwise.
@app.api_route("/m", methods=["GET", "HEAD"], include_in_schema=False, response_model=None)
def mobile_ui() -> FileResponse | HTMLResponse:
    entrypoint = WEB_DIR / "m" / "index.html"
    if not entrypoint.is_file():
        return HTMLResponse(
            "<!doctype html><title>Not found</title>"
            "<h1>Mobile UI assets are not installed</h1>",
            status_code=404,
        )
    return FileResponse(entrypoint)


# "/" was the retired desktop UI's address, so bookmarks and Home Screen apps installed from
# it still land here. Redirect rather than move the app: /m/ keeps its manifest, scope and
# service-worker registration, so a phone app installed from /m is untouched. The Location is
# relative for the same TLS-terminating-tunnel reason as /m above, and the browser carries
# the fragment across the redirect itself; m/app.js rewrites the old desktop hash routes.
@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def root_redirect(request: Request) -> RedirectResponse:
    query = request.url.query
    return RedirectResponse("/m/" + (f"?{query}" if query else ""))


# Static files last so /api/* and /docs win. html=True serves index.html at /m/.
if WEB_DIR.is_dir():
    app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")


def main() -> None:
    import uvicorn  # noqa: PLC0415 - entrypoint-only; keeps `import openbus.server` cheap

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
        "openbus.server:app" if reload else app,
        proxy_headers=False,
        log_config=None,
        host=os.environ.get("TMUXRC_HOST", "127.0.0.1"),
        port=int(os.environ.get("TMUXRC_PORT", "18030")),
        reload=reload,
        reload_dirs=[str(_PKG_DIR)] if reload else None,
    )


if __name__ == "__main__":
    main()
