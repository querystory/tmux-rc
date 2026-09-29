"""Live Mode: talk to every pane at once over a voice-model session (Gemini Live or
OpenAI Realtime — the adapters live in live_providers.py).

One WebSocket (`/api/live-mode`) per session. The browser streams mic PCM up; the
daemon owns the model connection (live_providers.py), feeds it the watcher's
always-current pane state, streams the model's voice + transcripts back, and executes
the session's tools — type_in_pane and press_key — through the same send_keys primitive
every other input path uses.
Design + prompting rationale: docs/design/live-mode.md.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
import time
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import agent_history, live_providers, llm, telemetry, tmux
from .classify import _load_prompt
from .live_providers import KEYS, LiveModel

logger = logging.getLogger(__name__)

router = APIRouter()


def enabled() -> bool:
    """Whether Live Mode (voice) is turned on. OFF by default: the voice UX is still
    being tuned, so it ships dark — the classify/marking improvements it rides in with
    (dim/placeholder markers, window_index) help the phone cards regardless. Flip on with
    TMUXRC_LIVE_MODE=1, then restart the daemon: .env is loaded once at process start
    (python-dotenv), so a StatReload does NOT re-read it — a full restart does. Read from
    os.environ per-call (not cached) so an env change is picked up without a code edit."""
    return os.environ.get("TMUXRC_LIVE_MODE", "").strip().lower() in ("1", "true", "yes", "on")


# Ambient [tmux update] messages: at most one per this many seconds, and only when the
# watcher's state_version moved (the same change signal /api/state long-polls on).
UPDATE_MIN_SECONDS = 2.5
# Screen tail sent per pane in the connect snapshot and in post-type refreshes. Enough
# to answer "what's it doing / asking"; SCREEN_BUDGET_CHARS below is what keeps N of
# these cheap — the per-pane cap alone is not.
SCREEN_TAIL_LINES = 60
SCREEN_TAIL_CHARS = 4000
# Fleet-wide ceiling on screen text in the connect snapshot. The per-pane tail is
# bounded but the fleet is not: a real 24-pane deck put ~36k tokens of screen text in
# the system instruction, and Gemini Live closes the session outright over its setup
# limit (a 1007 naming the token count) — the voice session died the moment audio
# flowed, on every reconnect, forever. Spent on the active pane first, then the panes
# most recently at work; a pane past the budget keeps its digest block (headline,
# summary, pending question) and loses only its screen text. ~24k chars ≈ 7k tokens
# leaves the rest of the window for the conversation itself.
SCREEN_BUDGET_CHARS = 24_000
# After typing, wait this long before sending the acted-on pane's fresh screen — the
# pane's app needs a beat to react before a capture shows anything new.
POST_TYPE_REFRESH_SECONDS = 1.5


class _LiveUsage:
    """A session's token Split (text/audio × in/out, plus cached input) and its cost under
    the MODEL's rate card (live_providers.LiveModel.rates) — a single blended price would
    be badly wrong because audio out is ~24× text in and cached input is ~30× cheaper
    again, and one global card would be wrong the moment a second model is on the menu.
    Providers report CONNECTION-cumulative totals (live_providers.Event.usage), so the
    last event always carries the totals and cost() is always current."""

    def __init__(self, rates: live_providers.Split) -> None:
        self.rates = rates
        self.split = live_providers.Split(*[0] * len(rates))

    def set(self, split: live_providers.Split) -> None:
        """Take the provider's latest CUMULATIVE Split — overwrite, don't sum: the last
        event of a connection carries its totals."""
        self.split = live_providers.Split(*split)

    @property
    def cached(self) -> int:
        return self.split.text_cached + self.split.audio_cached

    @property
    def audio_in(self) -> int:
        return self.split.audio_in + self.split.audio_cached

    @property
    def in_tokens(self) -> int:
        return self.split.text_in + self.split.text_cached + self.audio_in

    @property
    def out_tokens(self) -> int:
        return self.split.text_out + self.split.audio_out

    def cost(self) -> float:
        return sum(n / 1e6 * rate for n, rate in zip(self.split, self.rates, strict=True))


class _Meter:
    """Per-session metering: accumulates usage (via `usage`) and a rolling transcript of
    what was said and typed, emits an OTel record at each voice turn, and a final
    cumulative record + status-bar fold-in at session end. One per _run_session; survives
    reconnects (usage_metadata is cumulative, so a fresh connection continues the count).

    `session` is a per-session UUID — the summable key shared with emit_live's watch-time
    rounds, so a query can join a voice session's cost to its screen-view time."""

    def __init__(self, session: str, actor: str | None, model: LiveModel) -> None:
        self.session = session
        self.actor = actor
        self.model = model
        self.usage = _LiveUsage(model.rates)
        self.turns = 0
        self.started = time.monotonic()
        self._lines: list[str] = []
        # Extra OTel fields a provider adapter wants folded into each record (GPT-Live
        # adds voice_seconds / usage_final / backend_model). Empty for the seam's own
        # providers, which report everything through `usage`.
        self.details: dict = {}

    def note(self, line: str) -> None:
        """Record a transcript fragment (voice in/out, or a typed action). Bounded so a
        long session can't grow this without limit — OTel only ships the tail anyway."""
        self._lines.append(line)
        if len(self._lines) > 200:
            self._lines = self._lines[-200:]

    def _transcript(self) -> str:
        return "\n".join(self._lines)

    def end_turn(self) -> None:
        """A voice turn completed — emit its cumulative usage snapshot."""
        self.turns += 1
        self._emit(final=False)

    def finish(self) -> None:
        """Session ending — emit the final cumulative record and fold cost into the
        status-bar totals. Idempotent-safe to call once in the session's finally."""
        self._emit(final=True)
        llm.record_live_usage(
            in_tokens=self.usage.in_tokens,
            out_tokens=self.usage.out_tokens,
            cost=self.usage.cost(),
        )

    def _emit(self, *, final: bool) -> None:
        telemetry.emit_live_turn(
            session=self.session,
            actor=self.actor,
            model=self.model.model,
            provider=self.model.backend,
            in_tokens=self.usage.in_tokens,
            out_tokens=self.usage.out_tokens,
            cached_tokens=self.usage.cached,
            audio_in_tokens=self.usage.audio_in,
            audio_out_tokens=self.usage.split.audio_out,
            cost=self.usage.cost(),
            turns=self.turns,
            duration_s=time.monotonic() - self.started,
            final=final,
            transcript=self._transcript(),
            **self.details,
        )


def _screen_tail(watcher, pane_id: str) -> str:
    """Last SCREEN_TAIL_LINES of a pane's current screen, dim-MARKED — the same
    ⟪dim⟫-wrapped text the classify parser sees, so the voice model can tell draft/ghost/
    placeholder runs (composer autocomplete, unsent input) from real output instead of
    treating them as typed content. Use the stored snapshot text directly, NOT
    watcher.snapshot_text() — that one strips the markers for the phone's raw render.
    Empty if none captured yet."""
    hist = watcher.snapshots.get(pane_id) or []
    if not hist:
        return ""
    text = hist[-1]["text"] or ""
    lines = text.rstrip().splitlines()[-SCREEN_TAIL_LINES:]
    # Char-bound on a MARKER boundary — a raw slice could cut a ⟪dim⟫ token or orphan
    # a close from its open, garbling the dim signal the model relies on.
    return tmux.tail_marked("\n".join(lines), SCREEN_TAIL_CHARS)


def _fmt_age(sec: float) -> str:
    """Human idle age at the coarsest useful unit: '40s', '12m', '3h', '2d'."""
    sec = int(sec)
    if sec < 60: return f"{sec}s"
    if sec < 3600: return f"{sec // 60}m"
    if sec < 86400: return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def _pane_block(d: dict, screen: str | None) -> str:
    """One pane's state as prompt text. `d` is a watcher.digest() entry. The heading
    leads with the user-facing identity (window number + title) and gives the internal
    pane id only as `id=%N` — the handle for tool calls, never spoken (see live_prompt)."""
    win = d.get("window_index")
    head = f"window {win}" if win not in (None, "") else "window"
    # Best-first name, matching the phone card: the agent's self-published title, else
    # the window label (which itself falls back to session / session:index). Collapse any
    # newlines and drop embedded quotes so a stray title can't unbalance the quoting or
    # split the heading — the model must be able to parse one clean identity per pane.
    name = d.get("title") or d.get("label")
    if name:
        name = " ".join(str(name).split()).replace('"', "")
        head += f' "{name}"'
    head += f" (id={d['pane_id']}) — {d.get('tool') or 'unknown'}"
    head += f" — {d.get('activity') or 'unknown'}"
    # Idle AGE, not just the state: "idle for 2d" and "idle for 40s" are different routing
    # candidates — the prompt tells the model a long-idle pane is rarely where a new
    # instruction is destined (see live_prompt's targeting ladder).
    idle = d.get("idle_seconds")
    if d.get("activity") == "idle" and isinstance(idle, (int, float)) and idle >= 0:
        head += f" for {_fmt_age(idle)}"
    if d.get("tmux_active"):
        head += " — ACTIVE (the pane the user is looking at; 'here'/'this' means this one)"
    parts = [f"## {head}"]
    if d.get("cwd"):
        # Keep untrusted directory names from introducing fake prompt lines.
        parts.append(f"cwd: {json.dumps(str(d['cwd']), ensure_ascii=True)}")
    if d.get("headline"):
        parts.append(f"now: {d['headline']}")
    if d.get("summary"):
        parts.append(f"recently: {d['summary']}")
    if d.get("prs"):
        refs = ", ".join(f"{p['repo']}#{p['number']}" for p in d["prs"])
        parts.append(f"PRs this pane has worked on: {refs}")
    if d.get("question"):
        parts.append(f"PENDING QUESTION: {d['question']}")
    if screen:
        parts.append(f"screen:\n{screen}")
    return "\n".join(parts)


def _pane_context(watcher, screens: str) -> str:
    """All panes' state as prompt text — the digest the phone's cards already use, plus
    each pane's current screen tail per `screens`: "all" (the connect snapshot), "active"
    (only the focused pane — enough for the agent to read what it's acting on, without
    re-streaming every screen on each state change), or "none". No LLM calls; pure reads
    of state the watcher keeps current anyway."""
    if screens not in ("all", "active", "none"):
        raise ValueError(f"bad screens mode: {screens!r}")
    digest = watcher.digest()
    tails: dict[str, str] = {}
    if screens == "active":
        for d in digest:
            if d.get("tmux_active"):
                tails[d["pane_id"]] = _screen_tail(watcher, d["pane_id"])
    elif screens == "all":
        # Allocate SCREEN_BUDGET_CHARS priority-first — the active pane, then panes at
        # work, then the least-idle — but RENDER in digest order, which is tmux's own
        # session/window order: the model's pane map must not reshuffle by activity.
        # The last pane granted may overshoot the budget by at most one tail. A tail
        # itself can run a few chars past SCREEN_TAIL_CHARS — tail_marked() prefixes a
        # marker token (⟪dim⟫/⟪placeholder⟫) when the kept text starts inside a marked
        # run — so the slack is "one tail plus a marker", still bounded and still
        # simpler than truncating mid-screen.
        def prio(d):
            return (not d.get("tmux_active"),
                    d.get("activity") == "idle",
                    d.get("idle_seconds") or 0)
        budget = SCREEN_BUDGET_CHARS
        for d in sorted(digest, key=prio):
            if budget <= 0:
                break
            t = _screen_tail(watcher, d["pane_id"])
            if t:
                tails[d["pane_id"]] = t
                budget -= len(t)
    blocks = [_pane_block(d, tails.get(d["pane_id"])) for d in digest]
    return "\n\n".join(blocks) if blocks else "(no panes)"


def _system_prompt(watcher) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M %Z")
    return (
        f"{_load_prompt('live_prompt.txt')}\nNow: {stamp}"
        f"\n\n# Panes (live state)\n\n{_pane_context(watcher, screens='all')}"
    )


def _audit(meter: _Meter, action: str, pane_id: str = "-", **kw) -> None:
    """A Live audit record, tagged with the voice session it came from. Everything the
    voice typed or asked for is speech: journal content only under TMUXRC_QSDEBUG."""
    telemetry.audit(
        action, pane_id, meter.actor, speech=True, session=meter.session[:64],
        model=meter.model.model, provider=meter.model.backend, **kw,
    )


async def _handle_tool_call(websocket: WebSocket, session, fc, watcher, meter: _Meter) -> None:
    """Run one tool call, audit it, and answer the model tersely. The ONE place every
    Live tool call is recorded: the handler notes what it touched in `rec` (pane, detail,
    content, ids) and the outcome comes from its answer, so no path goes unaudited — a
    call that raises (say, the socket dropping after the keys went in) is audited too,
    with whatever `rec` already shows it did."""
    started, rec = time.monotonic(), {}
    result = {"status": "error", "reason": "aborted"}
    try:
        result = await _dispatch(websocket, session, fc, watcher, rec)
    finally:
        status, reason = result["status"], result.get("reason")
        known = fc.name in {"type_in_pane", "press_key", *_HISTORY_TOOLS}
        _audit(
            meter, f"live_{fc.name if known else 'unknown_tool'}",
            outcome="ok" if status in {"done", "ok", "opened"}
            else f"{status}: {reason}" if reason else status,
            latency_ms=round((time.monotonic() - started) * 1000), **rec,
        )
    await session.send_tool_result(fc, result)


async def _dispatch(websocket: WebSocket, session, fc, watcher, rec: dict) -> dict:
    """Route a tool call (type_in_pane / press_key, or a history tool) and return its
    answer. The result NEVER rides back through the tool response (echo loops — see
    design doc); the model sees the outcome via the post-action ambient refresh instead."""
    args = fc.args if isinstance(fc.args, dict) else {}
    if fc.name in _HISTORY_TOOLS:
        if not agent_history.offered():
            return {"status": "rejected", "reason": "session history not available"}
        return await _HISTORY_TOOLS[fc.name](websocket, args, watcher, rec)

    # Keep the RAW value as well as the coerced one: str() turns a dict or an int into a
    # perfectly plausible-looking string, and the guards below have to reject a wrong TYPE
    # rather than silently accept its repr. A model parroting our own tool response back
    # as a new call is exactly how a dict arrives here.
    raw_pane_id = args.get("pane_id")
    pane_id = str(raw_pane_id or "").strip()
    labels = {d["pane_id"]: d.get("label") or d["pane_id"] for d in watcher.digest()}

    # Parse per-tool into (send_args for tmux.send_keys, a human "what" for the audit/feed,
    # whether it counts as submitted). malformed stays None ⇒ reject below.
    send_args = what = None
    submitted = False
    if (
        fc.name == "type_in_pane"
        and isinstance(fc.args, dict)
        and isinstance(raw_pane_id, str)
        and isinstance(args.get("text"), str)
        and not (set(args) - {"pane_id", "text", "press_enter"})
    ):
        text = args["text"]
        raw_enter = args.get("press_enter", True)
        # Never coerce press_enter: bool("false") is True and would submit an unsent
        # command. A non-bool value is malformed.
        if text.strip() and isinstance(raw_enter, bool):
            send_args = (pane_id, text, raw_enter, True)  # literal text
            what, submitted = text, raw_enter
    elif (
        fc.name == "press_key"
        and isinstance(fc.args, dict)
        and isinstance(raw_pane_id, str)
        and isinstance(args.get("key"), str)
        and not (set(args) - {"pane_id", "key"})
    ):
        key = KEYS.get(args["key"])
        if key:
            send_args = (pane_id, key, False, False)  # named key, not literal, no auto-Enter
            what, submitted = f"[{key}]", key == "Enter"

    # The payload may hold secrets: it is speech, so it reaches the journal only under
    # QSDEBUG. The pane id is recorded only once it names a real pane.
    rec["keys"] = what or str(args)
    if pane_id not in labels:
        return {"status": "rejected", "reason": "unknown pane"}
    rec["pane_id"] = pane_id
    if send_args is None:
        return {"status": "rejected", "reason": "malformed call"}

    label = labels[pane_id]
    invalidate = getattr(watcher, "invalidate_input_actions", None)
    try:
        if invalidate is not None:
            await asyncio.to_thread(tmux.before_send, pane_id, lambda: invalidate(pane_id))
        await asyncio.to_thread(tmux.send_keys, *send_args)
    except Exception as e:  # report, don't kill the session
        # The error's text can quote the typed text (send-keys argv): speech, so only
        # the class leaves here unless QSDEBUG.
        rec["detail"] = type(e).__name__
        logger.warning("[live] %s failed for %s: %s", fc.name, pane_id, rec["detail"],
                       exc_info=telemetry.QSDEBUG)
        return {"status": "error", "reason": "pane did not accept input"}

    rec["detail"] = f"into {label}" + (" +enter" if submitted else "")
    watcher.request_reparse(pane_id)
    # Every action the voice takes is visibly logged in the overlay.
    await websocket.send_json(
        {"type": "typed", "pane_id": pane_id, "label": label,
         "text": what, "submitted": submitted}
    )

    # Let the pane react, then show the model what its keystrokes did — as ambient
    # state, not as a tool result.
    async def refresh() -> None:
        await asyncio.sleep(POST_TYPE_REFRESH_SECONDS)
        tail = await asyncio.to_thread(_screen_tail, watcher, pane_id)
        if tail:
            await _send_ambient(
                session, f"[tmux update] {label} ({pane_id}) after your input:\n{tail}"
            )

    _background(asyncio.create_task(refresh()))
    return {"status": "done", "pane": label}


async def _find_sessions(_websocket, args: dict, watcher, rec: dict) -> dict:
    """Past sessions for a topic, trimmed to what choosing needs. No message text: the
    model routes on titles, recency and liveness, and nothing from an old session is
    handed to it as if it were current."""
    query = args.get("query")
    rec["keys"] = str(query)  # the user's words: speech, like a transcript
    if set(args) - {"query"} or not isinstance(query, str) or not query.strip():
        return {"status": "rejected", "reason": "malformed call"}
    projects = await asyncio.to_thread(agent_history.resolve, query.strip())
    if projects is None:
        return {"status": "error", "reason": "session history unavailable"}
    labels = {d["pane_id"]: d.get("label") or d["pane_id"] for d in watcher.digest()}
    results = []
    for p in projects:
        sessions = []
        for s in p["sessions"]:
            pane = s.get("running") and await asyncio.to_thread(_pane_of, s["running"])
            if pane and pane not in labels:
                watcher.request_reparse(pane)  # publish it before the model types there
            sessions.append({
                "session_id": s["session_id"],
                "title": s.get("title") or "(untitled)",
                "last_active": (s.get("last_active") or "")[:10],
                # Named the way windows are everywhere else, once the watcher has it.
                **({"running_in": labels.get(pane, pane), "pane_id": pane} if pane else {}),
                **({"running_elsewhere": True} if s.get("running") and not pane else {}),
                **({"running_unknown": True} if s.get("running_unknown") else {}),
            })
        # The path, shortened under home, so two repos with one name stay distinct.
        results.append({"repo": _home_relative(p["repo"]), "sessions": sessions})
    ids = [s["session_id"] for r in results for s in r["sessions"]]
    rec.update(results=len(ids), top=",".join(ids[:3]))
    return {"status": "ok", "results": results}


# A resumed Claude process takes a moment to register itself as running, so a repeat
# call before then would see nothing running and start a second copy on the same
# transcript. Resumes are serialized, and each launch holds its session for as long as
# the pane it opened lives. The pane's pid, not its id, is the identity: tmux reuses ids.
_resume_lock = asyncio.Lock()
_resumed: dict[str, tuple[str, str, dict]] = {}  # session id -> (pane id, pid, audit rec)


def _ancestors(pid: int):
    """pid, its parent, and so on up, read from /proc; stops at init or a gone process."""
    while pid > 1:
        yield pid
        try:
            with open(f"/proc/{pid}/stat") as f:
                pid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return


def _pane_of(running: dict) -> str | None:
    """A registry entry's pane in THIS tmux server, or None. Its %N comes from whatever
    server Claude ran under, so it counts only if that pane's process here is an
    ancestor of the registered pid."""
    pane, pid = running.get("tmux_pane"), running.get("pid")
    root = pane and isinstance(pid, int) and tmux.pane_pid(pane)
    return pane if root and int(root) in _ancestors(pid) else None


def _home_relative(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home + "/") else path


async def _resume_session(websocket, args: dict, watcher, rec: dict) -> dict:
    """Open a past session in a new window. Everything that reaches tmux comes from the
    index, not the model: the model only names a session id."""
    sid = args.get("session_id")
    rec["session_id"] = str(sid)[:80]
    if set(args) - {"session_id"} or not isinstance(sid, str) or not sid.strip():
        return {"status": "rejected", "reason": "malformed call"}
    async with _resume_lock:
        return await _resume_locked(websocket, sid.strip(), watcher, rec)


async def _resume_locked(websocket, sid: str, watcher, rec: dict) -> dict:
    if sid in _resumed:
        pane, pid, launched = _resumed[sid]
        # Held while that same process lives in the pane (tmux reuses pane ids).
        if await asyncio.to_thread(tmux.pane_pid, pane) == pid:
            rec.update(launched, detail="resumed moments ago")
            return {"status": "already_running", "pane_id": pane}
        _resumed.pop(sid)  # that pane is gone; fall through to the registry
    entry = await asyncio.to_thread(agent_history.get, sid)
    if entry is None:
        return {"status": "rejected", "reason": "unknown session"}
    argv, cwd = entry.get("resume_argv") or [], entry.get("cwd") or ""
    rec.update(tool=argv[0] if argv else "-", cwd=_home_relative(cwd))

    # Never start a second process on a live session's transcript — including when
    # agent-history couldn't tell whether one is running.
    if entry.get("running_unknown"):
        return {"status": "error", "reason": "can't tell whether it's already running"}
    running = entry.get("running")
    if running:
        # The registry, not the watcher's last tick, is what says which pane it's in.
        pane = await asyncio.to_thread(_pane_of, running)
        if not pane:
            return {"status": "rejected", "reason": "already running outside this tmux"}
        rec["pane_id"] = pane
        labels = {d["pane_id"]: d.get("label") or d["pane_id"] for d in watcher.digest()}
        if pane not in labels:
            watcher.request_reparse(pane)  # publish it before the model types there
        return {"status": "already_running", "pane_id": pane, "pane": labels.get(pane, pane)}

    if not argv or argv[0] not in agent_history.RESUMABLE:
        return {"status": "rejected", "reason": "session can't be resumed"}
    if not os.path.isdir(cwd):
        return {"status": "rejected", "reason": "session directory is gone"}

    title = entry.get("title") or entry["session_id"][:8]
    name = title[:24]
    try:  # tmux can fail here too; report, don't kill the session
        target = _session_for(await asyncio.to_thread(tmux.list_panes), cwd)
        if target is None:
            return {"status": "error", "reason": "no tmux session to open a window in"}
        pane_id = await asyncio.to_thread(tmux.new_window, target, name, argv, cwd)
    except Exception as e:
        logger.warning("[live] resume_session failed for %s", entry["session_id"], exc_info=True)
        rec["detail"] = str(e)[:120]
        return {"status": "error", "reason": "could not open a window"}
    rec.update(pane_id=pane_id, window=name, tmux_session=target)
    pid = await _launched_pid(pane_id)
    if pid is None:  # the window closed at once: the command failed to start
        return {"status": "error", "reason": "the resumed session exited immediately"}
    _resumed[sid] = (pane_id, pid, dict(rec))
    watcher.request_reparse(pane_id)  # wakes a full tick, so the new pane is addressable
    await websocket.send_json(
        {"type": "typed", "pane_id": pane_id, "label": name,
         "text": f"[resumed {title}]", "submitted": True}
    )
    return {"status": "opened", "pane_id": pane_id, "window": name}


_LAUNCH_PID_RETRY_S = 0.1


async def _launched_pid(pane_id: str) -> str | None:
    """The pid of a just-opened pane. tmux sets it as it forks, so this is normally
    immediate; a few short retries cover a slow tmux. None means the pane is gone."""
    for _ in range(5):
        pid = await asyncio.to_thread(tmux.pane_pid, pane_id)
        if pid is not None:
            return pid
        await asyncio.sleep(_LAUNCH_PID_RETRY_S)
    return None


def _session_for(panes, cwd: str) -> str | None:
    """The tmux session to open a resumed agent in: one already working in or under its
    directory, else the session of the active window, else any."""
    if not panes:
        return None
    for p in panes:
        if p.cwd and (p.cwd == cwd or p.cwd.startswith(cwd.rstrip("/") + "/")):
            return p.session
    active = next((p for p in panes if p.window_active == "1" and p.pane_active == "1"), panes[0])
    return active.session


_HISTORY_TOOLS = {"find_sessions": _find_sessions, "resume_session": _resume_session}


# Keep strong refs to fire-and-forget tasks so they aren't GC'd mid-flight.
_tasks: set[asyncio.Task] = set()


def _background(task: asyncio.Task) -> None:
    def _done(t: asyncio.Task) -> None:
        _tasks.discard(t)
        if not t.cancelled() and t.exception():  # retrieve, or asyncio warns at GC
            logger.warning("[live] background task failed: %r", t.exception())

    _tasks.add(task)
    task.add_done_callback(_done)


async def _send_ambient(session, text: str) -> None:
    """Inject context WITHOUT prompting a response — the model simply has current state
    the next time the user speaks. This is the whole 'state is just always up to date'
    mechanism; the prompt additionally fences [tmux update] messages off from replies."""
    try:
        await session.send_context(text)
    except Exception as e:
        # A closed socket here is the normal stop/reconnect race (the updater lost to the
        # client's stop, or the session dropped — the reconnect loop's job); log it quietly.
        # A PERSISTENT non-transport failure is different: it would freeze the model's pane
        # view with no signal, so that keeps the warning + traceback.
        closed = type(e).__name__.startswith("ConnectionClosed")
        logger.log(logging.DEBUG if closed else logging.WARNING,
                   "[live] ambient context update skipped: %s", e, exc_info=not closed)


async def _context_updater(session, watcher) -> None:
    """Push digest-level [tmux update]s whenever the deck actually changes — driven by
    the same state_version the /api/state long-poll uses, throttled so a busy session
    drips small updates instead of streaming screens."""
    version = watcher.state_version()
    while True:
        # wait_for_state_change returns the current version even on timeout — only a
        # real advance earns an update (no 30s heartbeat; see docs/design/parse-cadence.md).
        new = await watcher.wait_for_state_change(version, timeout=30.0)
        if new == version:
            continue
        await asyncio.sleep(UPDATE_MIN_SECONDS)  # coalesce a burst into one update
        version = watcher.state_version()  # whatever landed during the throttle window
        await _send_ambient(
            session,
            f"[tmux update] current pane state:\n\n{_pane_context(watcher, screens='active')}",
        )


async def _forward_audio(websocket: WebSocket, session) -> None:
    """Client → model: base64 16kHz PCM frames until the client says stop."""
    while True:
        data = await websocket.receive_json()
        action = data.get("action")
        if action == "audio":
            raw = data.get("data")
            if not raw:
                continue
            try:
                audio = base64.b64decode(raw)
            except Exception:  # noqa: BLE001 - skip one bad frame, keep streaming
                continue
            await session.send_audio(audio)
        elif action == "stop":
            return
        else:
            logger.debug("[live] unknown client action: %s", action)


async def _receiver(websocket: WebSocket, session, watcher, meter: _Meter) -> None:
    """Model → client: voice audio, both transcripts, tool calls, barge-in. Also meters
    the session — takes each usage event into `meter` and emits a per-turn OTel record
    at every turn boundary."""
    async for ev in session.events():
        if ev.kind == "usage":
            meter.usage.set(ev.usage)
        elif ev.kind == "tool_call":
            meter.note(f"[typed] {ev.call.args}")
            await _handle_tool_call(websocket, session, ev.call, watcher, meter)
        elif ev.kind == "audio":
            await websocket.send_json({"type": "audio", "data": base64.b64encode(ev.data).decode()})
        elif ev.kind == "transcript":
            if telemetry.QSDEBUG:  # content reaches the journal under the same flag as OTel
                logger.info("[live] %s: %s", ev.role, ev.text)
            meter.note(f"{ev.role}: {ev.text}")
            await websocket.send_json({"type": "transcript", "role": ev.role, "text": ev.text})
        elif ev.kind == "turn_complete":
            meter.end_turn()
            await websocket.send_json({"type": "turn_complete"})
        elif ev.kind == "interrupted":
            await websocket.send_json({"type": "interrupted"})


async def _hold(websocket: WebSocket, seconds: float) -> bool:
    """Wait out a reconnect backoff while still reading the browser socket. Without this
    the loop reconnected to Vertex for a phone that was already gone — the tunnel drops
    both legs at once, and the browser's disconnect is only observed by a receive. A
    WebSocketDisconnect propagates (client gone); a stop returns False; a timeout, True."""
    try:
        async with asyncio.timeout(seconds):
            while (await websocket.receive_json()).get("action") != "stop":
                pass  # a stray frame (audio already in flight) is no reason to reconnect early
    except TimeoutError:
        return True
    return False


async def _run_session(websocket: WebSocket, watcher, meter: _Meter) -> None:
    """Connect to meter.model and run the session; reconnect with backoff on drops."""
    model = meter.model
    max_reconnects = 5
    for attempt in range(max_reconnects + 1):
        await websocket.send_json({"type": "status", "status": "connecting"})
        try:
            # The system prompt is rebuilt per connect attempt so a RECONNECT gets a fresh
            # pane snapshot — the connect snapshot is the only place full screens are sent
            # (ambient [tmux update]s omit them), so reusing a stale one would leave a
            # reconnected session answering/acting on minutes-old screen state.
            async with live_providers.connect(model, _system_prompt(watcher)) as session:
                logger.info(
                    "[live] session up (model=%s via %s, actor=%s)",
                    model.model, model.backend, meter.actor,
                )
                await websocket.send_json({"type": "status", "status": "listening"})
                side = [
                    asyncio.create_task(
                        _receiver(websocket, session, watcher, meter), name="live-receiver"
                    ),
                    asyncio.create_task(
                        _context_updater(session, watcher), name="live-context-updater"
                    ),
                ]
                # The mic pump is RACED against the side tasks, not awaited alone. A
                # provider that drops surfaces here as the receiver ending — cleanly, when
                # its event stream just stops, or with an exception — and awaiting only the
                # pump meant nobody looked until the browser happened to send another
                # frame. A muted or backgrounded phone sends none, so the session sat in
                # "listening" against a dead socket with the reconnect loop one frame away
                # and never entered. (The GPT-Live adapter already waits on all of its
                # tasks together; this is the seam saying the same thing.)
                pump = asyncio.create_task(
                    _forward_audio(websocket, session), name="live-audio"
                )
                side.append(pump)  # so the drain below tears this one down too
                try:
                    done, _ = await asyncio.wait(side, return_when=asyncio.FIRST_COMPLETED)
                    if pump in done:
                        pump.result()  # a WebSocketDisconnect here is the browser going away
                        return  # client sent stop — clean exit
                    for t in done:
                        t.result()  # a real failure, re-raised into the reconnect below
                    # Nothing raised, so a side task simply ENDED: the provider closed its
                    # stream. A quiet end is still an end — reconnect rather than sit.
                    raise ConnectionError("live provider stream ended")
                finally:
                    for t in side:
                        t.cancel()
                    # Bounded drain: a side task stuck in an un-cancellable unwind (e.g. genai's
                    # generator finally awaiting a close handshake on a half-open socket) must not
                    # delay the reconnect / websocket-close this finally gates by an OS TCP timeout.
                    # wait() never raises for task outcomes, so the CancelledError from the cancel
                    # above is absorbed here rather than escaping as it did under
                    # suppress(Exception).
                    done, pending = await asyncio.wait(side, timeout=2)
                    for t in pending:
                        logger.warning(
                            "[live] side task %s did not unwind within 2s; abandoning it",
                            t.get_name(),
                        )
                    for t in done:
                        if not t.cancelled() and (exc := t.exception()) is not None:
                            logger.warning(
                                "[live] side task %s ended in error: %r", t.get_name(), exc
                            )
        except (WebSocketDisconnect, live_providers.Unreachable):
            raise  # client gone, or a misconfiguration no retry can fix
        except Exception:
            if attempt >= max_reconnects:
                raise
            backoff = min(2**attempt, 15)
            logger.warning("[live] session dropped; reconnecting in %ds", backoff, exc_info=True)
            await websocket.send_json({"type": "status", "status": "reconnecting"})
            if not await _hold(websocket, backoff):
                return  # client sent stop during the backoff


def offered() -> list[live_providers.LiveModel]:
    """Everything a client may pick, in picker order: the configured table, then GPT-Live
    when its key is set. ONE list, behind both /api/version's menu and this module's socket
    gate, because the two saying it separately is exactly how they come to disagree — and a
    gate that refuses what the menu just offered is indistinguishable from a broken pick.

    GPT-Live is appended rather than configured: the seam opens a CONNECTION and hands it
    to the shared coroutines, while the adapter owns a whole SESSION, so it cannot be a
    table entry. The TABLE wins a label collision — an operator who names an entry
    "GPT-Live 1" gets the entry they configured, not a second row shadowing it — and it
    wins even while that entry is KEYLESS and therefore off the menu. A label is owned by
    whoever configured it, not by whoever currently has credentials: otherwise a remembered
    pick would silently change which model answers as keys come and go, which is the one
    thing label-only selection exists to prevent."""
    from . import gpt_live  # noqa: PLC0415 - the adapter imports this module's handlers

    # Read the table ONCE and filter here rather than calling available(): the reservation
    # above has to see the unoffered half too, and two reads of the same env for one answer
    # is two chances for the halves to disagree.
    table = live_providers.models()
    menu = [m for m in table if m.available()]
    if os.environ.get("OPENAI_API_KEY") and all(m.label != gpt_live.LABEL for m in table):
        menu.append(gpt_live.ENTRY)
    return menu


def pick(label: str | None) -> live_providers.LiveModel | None:
    """Resolve a client-supplied label against `offered()`. Label-only, like launchers: the
    client names an entry and never a model id, backend or credential. No label at all is
    the first entry — what a user who never opens the picker gets. Unknown, or configured
    but keyless, is None: refused, never defaulted, because silently answering with a
    different model would make a side-by-side comparison lie."""
    menu = offered()
    if not label:
        return menu[0] if menu else None
    return next((m for m in menu if m.label == label), None)


@router.websocket("/api/live-mode")
async def live_mode(websocket: WebSocket) -> None:
    if not enabled():
        # Feature-flagged off — refuse before any model connection or mic streaming.
        # 1008 = policy violation; the client hides the button too, so this only fires
        # for a stale tab or a direct probe. Reason points at the fix (reload the page —
        # a current client reads live_enabled from /api/version and hides the button).
        await websocket.close(code=1008, reason="Live Mode is disabled — reload the page")
        return
    from . import gpt_live  # noqa: PLC0415 - adapter imports this module's shared handlers

    model = pick(websocket.query_params.get("model"))
    if model is None:
        # Nothing offered at all (every entry key-gated, no key set) is the operator's
        # config problem, not a stale tab's — a reload can't fix it, so don't say so.
        why = "reload the page" if offered() else "no configured model has its key set"
        await websocket.close(code=1008, reason=f"Live model not available — {why}")
        return
    # Which runner, decided by identity rather than by re-reading the label: `offered()`
    # already settled who owns this pick, including a table entry that claimed the
    # adapter's label.
    use_gpt = model is gpt_live.ENTRY
    await websocket.accept()
    watcher = websocket.app.state.watcher
    # Per-session UUID — the summable key that ties this voice session's cost (emit_live_turn)
    # to its screen watch-time (emit_live). Accept the client's if it passes one, else mint one.
    session_id = websocket.query_params.get("session") or uuid.uuid4().hex
    meter = _Meter(session_id, telemetry.actor(websocket), model)
    _audit(meter, "live_session", detail="start")
    outcome, reason = "ok", "stop"
    try:
        if use_gpt:
            await gpt_live.run_session(websocket, watcher, meter)
        else:
            await _run_session(websocket, watcher, meter)
    except WebSocketDisconnect:
        reason = "client gone"  # phone lock / tab close / tunnel drop — the normal ends
    except Exception as e:  # noqa: BLE001 - the session's last stop: report it, never crash the WS
        outcome = reason = "error"
        # A model that can't be reached (bad deployment name, rejected key) says exactly
        # what to fix — the user fixes config, not the retry count. Anything else stays a
        # generic line so internal detail never reaches the browser. The adapter's
        # ProviderError is the same promise from the other side of the seam — it has
        # already sanitized the provider's diagnostics — so it rides this one path rather
        # than an except clause of its own, which would have skipped the log line and sent
        # on a socket that may already be gone.
        fatal = isinstance(e, (live_providers.Unreachable, gpt_live.ProviderError))
        (logger.error if fatal else logger.exception)(
            "[live] session failed%s", f": {e}" if fatal else ""
        )
        with contextlib.suppress(Exception):
            await websocket.send_json(
                {"type": "error", "message": str(e) if fatal else "live session failed"}
            )
    finally:
        meter.finish()  # final cumulative OTel record + fold cost into the status bar
        _audit(
            meter, "live_session", detail=f"end: {reason}", outcome=outcome, turns=meter.turns,
            cost_usd=round(meter.usage.cost(), 4),
            duration_s=round(time.monotonic() - meter.started, 1),
        )
        with contextlib.suppress(Exception):
            await websocket.close()
