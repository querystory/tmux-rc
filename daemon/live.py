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
import logging
import os
import time
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import live_providers, telemetry, tmux
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
    """A session's token split (text/audio × in/out) and its cost under the MODEL's
    four-way rate card (live_providers.LiveModel.rates) — a single blended price would be
    badly wrong because audio out is ~24× text in, and one global card would be wrong the
    moment a second model is on the menu. Providers report CONNECTION-cumulative totals
    (live_providers.Event.usage), so the last event always carries the totals and cost()
    is always current."""

    def __init__(self, rates: tuple[float, float, float, float]) -> None:
        self.rates = rates
        self.text_in = self.text_out = self.audio_in = self.audio_out = 0

    def set(self, split: tuple[int, int, int, int]) -> None:
        """Take the provider's latest CUMULATIVE (text_in, text_out, audio_in, audio_out)
        — overwrite, don't sum: the last event of a connection carries its totals."""
        self.text_in, self.text_out, self.audio_in, self.audio_out = split

    @property
    def in_tokens(self) -> int:
        return self.text_in + self.audio_in

    @property
    def out_tokens(self) -> int:
        return self.text_out + self.audio_out

    def cost(self) -> float:
        split = (self.text_in, self.text_out, self.audio_in, self.audio_out)
        return sum(n / 1e6 * rate for n, rate in zip(split, self.rates))


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
        from . import llm

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
            in_tokens=self.usage.in_tokens,
            out_tokens=self.usage.out_tokens,
            audio_in_tokens=self.usage.audio_in,
            audio_out_tokens=self.usage.audio_out,
            cost=self.usage.cost(),
            turns=self.turns,
            duration_s=time.monotonic() - self.started,
            final=final,
            transcript=self._transcript(),
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
    if d.get("headline"):
        parts.append(f"now: {d['headline']}")
    if d.get("summary"):
        parts.append(f"recently: {d['summary']}")
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


def _actor(websocket: WebSocket) -> str:
    """WHO is on this session, same trust model as server._audit: the relay-forwarded
    identity header is honored only from loopback peers (the tunnel-client), otherwise
    recorded as a claim."""
    peer = websocket.client.host if websocket.client else "?"
    claimed = websocket.headers.get("x-tunnel-user")
    # Bound the header-derived identity so a peer can't bloat OTel attrs/logs with a huge
    # x-tunnel-user — same 200-char convention as server._audit's actor[:200].
    if claimed and peer in ("127.0.0.1", "::1"):
        return claimed[:200]
    if claimed:
        return f"local:{peer} claiming {claimed[:60]!r}"
    return f"local:{peer}"


async def _handle_tool_call(websocket: WebSocket, session, fc, watcher, actor: str) -> None:
    """Route a tool call (type_in_pane / press_key) to the pane and answer the model tersely.
    The result NEVER rides back through the tool response (echo loops — see design doc);
    the model sees the outcome via the post-action ambient refresh instead."""

    async def respond(payload: dict) -> None:
        await session.send_tool_result(fc, payload)

    args = fc.args if isinstance(fc.args, dict) else {}
    pane_id = str(args.get("pane_id", "")).strip()
    labels = {d["pane_id"]: d.get("label") or d["pane_id"] for d in watcher.digest()}

    # Parse per-tool into (send_args for tmux.send_keys, a human "what" for the audit/feed,
    # whether it counts as submitted). malformed stays None ⇒ reject below.
    send_args = what = None
    submitted = False
    if fc.name == "type_in_pane" and isinstance(fc.args, dict) and not (set(args) - {"pane_id", "text", "press_enter"}):
        text = str(args.get("text", ""))
        raw_enter = args.get("press_enter", True)
        # Never coerce press_enter: bool("false") is True and would submit an unsent
        # command. A non-bool value is malformed.
        if text.strip() and isinstance(raw_enter, bool):
            send_args = (pane_id, text, raw_enter, True)  # literal text
            what, submitted = text, raw_enter
    elif fc.name == "press_key" and isinstance(fc.args, dict) and not (set(args) - {"pane_id", "key"}):
        key = KEYS.get(str(args.get("key", "")))
        if key:
            send_args = (pane_id, key, False, False)  # named key, not literal, no auto-Enter
            what, submitted = f"[{key}]", key == "Enter"

    if send_args is None or pane_id not in labels:
        reason = "unknown pane" if pane_id not in labels else "malformed call"
        # Log the verb + target + reason only — the payload may hold secrets and logs
        # aren't QSDEBUG-gated. (The keys= on emit_action IS gated, so it keeps them.)
        logger.info("[live] rejecting tool call %s -> %s: %s", fc.name, pane_id or "?", reason)
        telemetry.emit_action(
            action="live_type", pane_uid=f"{tmux.server_uid()}:{pane_id or '?'}", actor=actor,
            detail=reason, keys=str(args), outcome=f"rejected: {reason}",
        )
        await respond({"status": "rejected", "reason": reason})
        return

    label = labels[pane_id]
    try:
        await asyncio.to_thread(tmux.send_keys, *send_args)
    except Exception as e:  # noqa: BLE001 - report, don't kill the session
        logger.warning("[live] %s failed for %s", fc.name, pane_id, exc_info=True)
        telemetry.emit_action(
            action="live_type", pane_uid=f"{tmux.server_uid()}:{pane_id}", actor=actor,
            detail=str(e)[:120], keys=what, outcome="error",
        )
        await respond({"status": "error", "reason": "pane did not accept input"})
        return

    telemetry.emit_action(
        action="live_type", pane_uid=f"{tmux.server_uid()}:{pane_id}", actor=actor,
        detail=f"into {label}" + (" +enter" if submitted else ""), keys=what,
    )
    watcher.request_reparse(pane_id)  # the keystrokes changed the screen
    # Every action the voice takes is visibly logged in the overlay.
    await websocket.send_json(
        {"type": "typed", "pane_id": pane_id, "label": label,
         "text": what, "submitted": submitted}
    )
    await respond({"status": "done", "pane": label})

    # Let the pane react, then show the model what its keystrokes did — as ambient
    # state, not as a tool result.
    async def refresh() -> None:
        await asyncio.sleep(POST_TYPE_REFRESH_SECONDS)
        tail = await asyncio.to_thread(_screen_tail, watcher, pane_id)
        if tail:
            await _send_ambient(session, f"[tmux update] {label} ({pane_id}) after your input:\n{tail}")

    task = asyncio.create_task(refresh())
    _background(task)


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
            session, f"[tmux update] current pane state:\n\n{_pane_context(watcher, screens='active')}"
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


async def _receiver(websocket: WebSocket, session, watcher, actor: str, meter: _Meter) -> None:
    """Model → client: voice audio, both transcripts, tool calls, barge-in. Also meters
    the session — takes each usage event into `meter` and emits a per-turn OTel record
    at every turn boundary."""
    async for ev in session.events():
        if ev.kind == "usage":
            meter.usage.set(ev.usage)
        elif ev.kind == "tool_call":
            fc = ev.call
            # Log the verb + target only, never the payload — typed text can carry
            # secrets, and logs aren't gated by TMUXRC_QSDEBUG the way telemetry/meter
            # content is.
            pane = fc.args.get("pane_id") if isinstance(fc.args, dict) else None
            logger.info("[live] tool call: %s -> %s", fc.name, pane)
            meter.note(f"[typed] {fc.args}")
            await _handle_tool_call(websocket, session, fc, watcher, actor)
        elif ev.kind == "audio":
            await websocket.send_json({"type": "audio", "data": base64.b64encode(ev.data).decode()})
        elif ev.kind == "transcript":
            meter.note(f"{ev.role}: {ev.text}")
            await websocket.send_json({"type": "transcript", "role": ev.role, "text": ev.text})
        elif ev.kind == "turn_complete":
            meter.end_turn()
            await websocket.send_json({"type": "turn_complete"})
        elif ev.kind == "interrupted":
            await websocket.send_json({"type": "interrupted"})


async def _run_session(websocket: WebSocket, watcher, actor: str, meter: _Meter) -> None:
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
                logger.info("[live] session up (model=%s via %s, actor=%s)", model.model, model.backend, actor)
                await websocket.send_json({"type": "status", "status": "listening"})
                side = [
                    asyncio.create_task(
                        _receiver(websocket, session, watcher, actor, meter), name="live-receiver"
                    ),
                    asyncio.create_task(
                        _context_updater(session, watcher), name="live-context-updater"
                    ),
                ]
                try:
                    await _forward_audio(websocket, session)
                    return  # client sent stop — clean exit
                finally:
                    for t in side:
                        t.cancel()
                    # Bounded drain: a side task stuck in an un-cancellable unwind (e.g. genai's
                    # generator finally awaiting a close handshake on a half-open socket) must not
                    # delay the reconnect / websocket-close this finally gates by an OS TCP timeout.
                    # wait() never raises for task outcomes, so the CancelledError from the cancel
                    # above is absorbed here rather than escaping as it did under suppress(Exception).
                    done, pending = await asyncio.wait(side, timeout=2)
                    for t in pending:
                        logger.warning("[live] side task %s did not unwind within 2s; abandoning it", t.get_name())
                    for t in done:
                        if not t.cancelled() and (exc := t.exception()) is not None:
                            logger.warning("[live] side task %s ended in error: %r", t.get_name(), exc)
        except (WebSocketDisconnect, live_providers.Unreachable):
            raise  # client gone, or a misconfiguration no retry can fix
        except Exception:
            if attempt >= max_reconnects:
                raise
            backoff = min(2**attempt, 15)
            logger.warning("[live] session dropped; reconnecting in %ds", backoff, exc_info=True)
            await websocket.send_json({"type": "status", "status": "reconnecting"})
            await asyncio.sleep(backoff)


@router.websocket("/api/live-mode")
async def live_mode(websocket: WebSocket) -> None:
    if not enabled():
        # Feature-flagged off — refuse before any model connection or mic streaming.
        # 1008 = policy violation; the client hides the button too, so this only fires
        # for a stale tab or a direct probe. Reason points at the fix (reload the page —
        # a current client reads live_enabled from /api/version and hides the button).
        await websocket.close(code=1008, reason="Live Mode is disabled — reload the page")
        return
    # Label-only, like launchers: the client names an entry from the server's table and
    # never a model id or backend. An unoffered label (unknown, or its key is absent) is
    # refused rather than defaulted — the picker must never lie about who answered.
    model = live_providers.find(websocket.query_params.get("model"))
    if model is None:
        # Nothing offered at all (every entry key-gated, no key set) is the operator's
        # config problem, not a stale tab's — a reload can't fix it, so don't say so.
        why = "reload the page" if live_providers.available() else "no configured model has its key set"
        await websocket.close(code=1008, reason=f"Live model not available — {why}")
        return
    await websocket.accept()
    watcher = websocket.app.state.watcher
    actor = _actor(websocket)
    # Per-session UUID — the summable key that ties this voice session's cost (emit_live_turn)
    # to its screen watch-time (emit_live). Accept the client's if it passes one, else mint one.
    session_id = websocket.query_params.get("session") or uuid.uuid4().hex
    meter = _Meter(session_id, actor, model)
    logger.info("[live] session start (actor=%s, session=%s, model=%s)", actor, session_id, model.label)
    telemetry.emit_action(action="live_session", pane_uid="-", actor=actor, detail="start", keys=None)
    outcome = "ok"
    try:
        await _run_session(websocket, watcher, actor, meter)
    except WebSocketDisconnect:
        pass  # phone lock / tab close — the normal way sessions end
    except Exception as e:
        outcome = "error"
        # A model that can't be reached (bad deployment name, rejected key) says exactly
        # what to fix — the user fixes config, not the retry count. Anything else stays a
        # generic line so internal detail never reaches the browser.
        fatal = isinstance(e, live_providers.Unreachable)
        (logger.error if fatal else logger.exception)("[live] session failed%s", f": {e}" if fatal else "")
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "error", "message": str(e) if fatal else "live session failed"})
    finally:
        meter.finish()  # final cumulative OTel record + fold cost into the status bar
        telemetry.emit_action(
            action="live_session", pane_uid="-", actor=actor,
            detail=f"end ({meter.turns} turns, ${meter.usage.cost():.4f})", keys=None, outcome=outcome,
        )
        with contextlib.suppress(Exception):
            await websocket.close()
