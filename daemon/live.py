"""Live Mode: talk to every pane at once over a Gemini Live voice session.

One WebSocket (`/api/live-mode`) per session. The browser streams mic PCM up; the
daemon owns the Gemini Live connection, feeds it the watcher's always-current pane
state, streams the model's voice + transcripts back, and executes the session's tools —
type_in_pane and press_key — through the same send_keys primitive every other input
path uses.
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

from . import telemetry, tmux
from .classify import _load_prompt

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


# The Live-capable model — NOT the flash-lite classifier model (which has no live/bidi
# variant). Region likewise: Live models are region-pinned, not "global".
LIVE_MODEL = os.environ.get("TMUXRC_LIVE_MODEL", "gemini-live-2.5-flash-native-audio")
LIVE_REGION = os.environ.get("TMUXRC_LIVE_REGION", "us-central1")

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

# Native-audio Live pricing (USD per 1M tokens). Audio and text bill at very different
# rates, so we price the two modalities separately rather than with one blended number.
# Overridable in case the rate card moves. Defaults are the gemini-2.5-flash native-audio
# published rates; TEXT covers the system prompt + [tmux update] context we send as text.
_LIVE_TEXT_IN_PER_M = float(os.environ.get("TMUXRC_LIVE_TEXT_IN_PER_M", "0.50"))
_LIVE_TEXT_OUT_PER_M = float(os.environ.get("TMUXRC_LIVE_TEXT_OUT_PER_M", "2.00"))
_LIVE_AUDIO_IN_PER_M = float(os.environ.get("TMUXRC_LIVE_AUDIO_IN_PER_M", "3.00"))
_LIVE_AUDIO_OUT_PER_M = float(os.environ.get("TMUXRC_LIVE_AUDIO_OUT_PER_M", "12.00"))


class _LiveUsage:
    """Accumulates a session's token usage from Gemini Live usage_metadata messages.

    Live reports usage per server message; we keep the latest per-modality split (in vs
    out, text vs audio) and derive cost with the four-way rate card above — a single
    blended price would be badly wrong because audio out is ~24× text in. Cumulative:
    the last message of a session carries the session totals, so cost() is always current.
    Cheap and allocation-free in the hot receive loop (plain int adds)."""

    def __init__(self) -> None:
        self.text_in = self.text_out = self.audio_in = self.audio_out = 0

    def add(self, usage) -> None:
        """Fold one usage_metadata into the running split. Prompt = input, response =
        output; per-modality details break each into text/audio (anything not audio is
        billed as text)."""
        if usage is None:
            return
        prompt = getattr(usage, "prompt_token_count", 0) or 0
        resp = getattr(usage, "response_token_count", 0) or 0
        a_in = _audio_tokens(getattr(usage, "prompt_tokens_details", None))
        a_out = _audio_tokens(getattr(usage, "response_tokens_details", None))
        # These messages carry CUMULATIVE session totals, so overwrite, don't sum.
        self.audio_in, self.audio_out = a_in, a_out
        self.text_in = max(prompt - a_in, 0)
        self.text_out = max(resp - a_out, 0)

    @property
    def in_tokens(self) -> int:
        return self.text_in + self.audio_in

    @property
    def out_tokens(self) -> int:
        return self.text_out + self.audio_out

    def cost(self) -> float:
        return (
            self.text_in / 1e6 * _LIVE_TEXT_IN_PER_M
            + self.text_out / 1e6 * _LIVE_TEXT_OUT_PER_M
            + self.audio_in / 1e6 * _LIVE_AUDIO_IN_PER_M
            + self.audio_out / 1e6 * _LIVE_AUDIO_OUT_PER_M
        )


def _audio_tokens(details) -> int:
    """Sum the AUDIO-modality token counts out of a *_tokens_details list; 0 if absent."""
    from google.genai import types

    total = 0
    for d in details or []:
        if getattr(d, "modality", None) == types.Modality.AUDIO:
            total += getattr(d, "token_count", 0) or 0
    return total


class _Meter:
    """Per-session metering: accumulates usage (via `usage`) and a rolling transcript of
    what was said and typed, emits an OTel record at each voice turn, and a final
    cumulative record + status-bar fold-in at session end. One per _run_session; survives
    reconnects (usage_metadata is cumulative, so a fresh connection continues the count).

    `session` is a per-session UUID — the summable key shared with emit_live's watch-time
    rounds, so a query can join a voice session's cost to its screen-view time."""

    def __init__(self, session: str, actor: str | None) -> None:
        self.session = session
        self.actor = actor
        self.usage = _LiveUsage()
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
            model=LIVE_MODEL,
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


def _live_client():
    """A dedicated Vertex client for Live sessions. Deliberately NOT llm._client():
    that one pins the classifier's per-request timeout (an anti-wedge guard for one-shot
    parse calls) which would sever a long-lived bidi stream, and defaults to the
    'global' region which Live models don't serve."""
    from google import genai

    project = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set; cannot reach Vertex.")
    return genai.Client(vertexai=True, project=project, location=LIVE_REGION)


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


# Named keys press_key can send — a fixed whitelist, mapped to the tmux key names
# send_keys(literal=False) understands. Bounded on purpose: the model can only press
# keys that make sense for terminal UIs (cancel, interrupt, menu nav, submit), never an
# arbitrary key string that could be a chord we didn't vet.
_KEYS = {
    "Escape": "Escape",   # cancel / reject the current prompt
    "Enter": "Enter",     # accept / continue with no text
    "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right",  # menu navigation
    "Tab": "Tab",         # cycle / complete
    "C-c": "C-c",         # interrupt what's running
    "C-d": "C-d",         # EOF / exit a REPL
}


def _tools():
    """The session's tools: type_in_pane (types a string) and press_key (sends one named
    control key). Two narrow verbs beat one overloaded one — the model can't accidentally
    fold text and a chord into a single ambiguous call, and press_key's whitelist keeps it
    from inventing arbitrary key sequences."""
    from google.genai import types

    return [
        types.Tool(
            function_declarations=[
                types.FunctionDeclaration(
                    name="type_in_pane",
                    description=(
                        "Type text into one tmux pane, exactly as if the user typed "
                        "it at that terminal. Use only when the user clearly asks "
                        "you to type, run, answer, or tell a pane something."
                    ),
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "pane_id": types.Schema(
                                type=types.Type.STRING,
                                description="Target pane id — the id=%N handle from the window state, e.g. %5",
                            ),
                            "text": types.Schema(
                                type=types.Type.STRING,
                                description="The exact text to type",
                            ),
                            "press_enter": types.Schema(
                                type=types.Type.BOOLEAN,
                                description="Submit with Enter after typing (default true)",
                            ),
                        },
                        required=["pane_id", "text"],
                    ),
                ),
                types.FunctionDeclaration(
                    name="press_key",
                    description=(
                        "Press ONE control key in a pane (no text) — to cancel, "
                        "interrupt, navigate a menu, or continue. Escape cancels/rejects "
                        "the current prompt; C-c interrupts what's running; Up/Down then "
                        "Enter picks a menu item; Enter alone accepts/continues; Tab "
                        "cycles or completes; C-d sends EOF."
                    ),
                    parameters=types.Schema(
                        type=types.Type.OBJECT,
                        properties={
                            "pane_id": types.Schema(
                                type=types.Type.STRING,
                                description="Target pane id — the id=%N handle from the window state, e.g. %5",
                            ),
                            "key": types.Schema(
                                type=types.Type.STRING,
                                enum=list(_KEYS),
                                description="One of: " + ", ".join(_KEYS),
                            ),
                        },
                        required=["pane_id", "key"],
                    ),
                ),
            ]
        )
    ]


async def _handle_tool_call(websocket: WebSocket, session, fc, watcher, actor: str) -> None:
    """Route a tool call (type_in_pane / press_key) to the pane and answer Gemini tersely.
    The result NEVER rides back through the FunctionResponse (echo loops — see design doc);
    the model sees the outcome via the post-action ambient refresh instead."""
    from google.genai import types

    async def respond(payload: dict) -> None:
        await session.send_tool_response(
            function_responses=[
                # fc.id must ride back or the session wedges (inherited lesson).
                types.FunctionResponse(id=fc.id, name=fc.name, response=payload)
            ]
        )

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
        key = _KEYS.get(str(args.get("key", "")))
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
    """Inject context WITHOUT prompting a response: turn_complete=False adds the content
    to the conversation but no model turn fires — the model simply has current state the
    next time the user speaks. This is the whole 'state is just always up to date'
    mechanism; the prompt additionally fences [tmux update] messages off from replies."""
    from google.genai import types

    try:
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]),
            turn_complete=False,
        )
    except Exception:
        # A dropped session is the reconnect loop's job, but a PERSISTENT non-transport
        # failure would otherwise freeze the model's pane view with no signal.
        logger.warning("[live] ambient context update failed", exc_info=True)


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
    """Client → Gemini: base64 16kHz PCM frames until the client says stop."""
    from google.genai import types

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
            await session.send_realtime_input(
                audio=types.Blob(data=audio, mime_type="audio/pcm;rate=16000")
            )
        elif action == "stop":
            return
        else:
            logger.debug("[live] unknown client action: %s", action)


async def _receiver(websocket: WebSocket, session, watcher, actor: str, meter: _Meter) -> None:
    """Gemini → client: voice audio, both transcripts, and tool calls. Also meters the
    session — folds each message's usage_metadata into `meter` and emits a per-turn OTel
    record at every turn boundary."""
    while True:
        async for response in session.receive():
            if response.usage_metadata:
                meter.usage.add(response.usage_metadata)
            if response.tool_call and response.tool_call.function_calls:
                for fc in response.tool_call.function_calls:
                    # Log the verb + target only, never the payload — typed text can
                    # carry secrets, and logs aren't gated by TMUXRC_QSDEBUG the way
                    # telemetry/meter content is.
                    pane = (fc.args or {}).get("pane_id") if isinstance(fc.args, dict) else None
                    logger.info("[live] tool call: %s -> %s", fc.name, pane)
                    meter.note(f"[typed] {fc.args}")
                    await _handle_tool_call(websocket, session, fc, watcher, actor)
            if response.data:
                await websocket.send_json(
                    {"type": "audio", "data": base64.b64encode(response.data).decode()}
                )
            sc = response.server_content
            if sc:
                if sc.input_transcription and sc.input_transcription.text:
                    if telemetry._QSDEBUG:  # content reaches the journal under the same flag as OTel
                        logger.info("[live] user: %s", sc.input_transcription.text)
                    meter.note("user: " + sc.input_transcription.text)
                    await websocket.send_json(
                        {"type": "transcript", "role": "user",
                         "text": sc.input_transcription.text}
                    )
                if sc.output_transcription and sc.output_transcription.text:
                    if telemetry._QSDEBUG:
                        logger.info("[live] model: %s", sc.output_transcription.text)
                    meter.note("model: " + sc.output_transcription.text)
                    await websocket.send_json(
                        {"type": "transcript", "role": "model",
                         "text": sc.output_transcription.text}
                    )
                if sc.turn_complete:
                    meter.end_turn()
                    await websocket.send_json({"type": "turn_complete"})


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


async def _run_session(websocket: WebSocket, watcher, actor: str, meter: _Meter) -> None:
    """Connect to Gemini Live and run the session; reconnect with backoff on drops."""
    from google.genai import types

    client = _live_client()

    def _config():
        # Rebuilt per connect attempt so a RECONNECT gets a fresh pane snapshot in its
        # system prompt — the connect snapshot is the only place full screens are sent
        # (ambient [tmux update]s omit them), so reusing a stale one would leave a
        # reconnected session answering/acting on minutes-old screen state.
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            tools=_tools(),
            system_instruction=_system_prompt(watcher),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            # Let the model choose NOT to answer — required for the noise/silence prompt
            # rules to work instead of the model replying to every sound.
            proactivity=types.ProactivityConfig(proactive_audio=True),
        )

    max_reconnects = 5
    for attempt in range(max_reconnects + 1):
        await websocket.send_json({"type": "status", "status": "connecting"})
        try:
            async with client.aio.live.connect(model=LIVE_MODEL, config=_config()) as session:
                logger.info("[live] session up (model=%s, actor=%s)", LIVE_MODEL, actor)
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
        except WebSocketDisconnect:
            raise  # client gone — nothing to reconnect for
        except Exception:
            if attempt >= max_reconnects:
                raise
            backoff = min(2**attempt, 15)
            logger.warning("[live] session dropped; reconnecting in %ds", backoff, exc_info=True)
            await websocket.send_json({"type": "status", "status": "reconnecting"})
            if not await _hold(websocket, backoff):
                return  # client sent stop during the backoff


@router.websocket("/api/live-mode")
async def live_mode(websocket: WebSocket) -> None:
    if not enabled():
        # Feature-flagged off — refuse before any Gemini connection or mic streaming.
        # 1008 = policy violation; the client hides the button too, so this only fires
        # for a stale tab or a direct probe. Reason points at the fix (reload the page —
        # a current client reads live_enabled from /api/version and hides the button).
        await websocket.close(code=1008, reason="Live Mode is disabled — reload the page")
        return
    await websocket.accept()
    watcher = websocket.app.state.watcher
    actor = _actor(websocket)
    # Per-session UUID — the summable key that ties this voice session's cost (emit_live_turn)
    # to its screen watch-time (emit_live). Accept the client's if it passes one, else mint one.
    session_id = websocket.query_params.get("session") or uuid.uuid4().hex
    meter = _Meter(session_id, actor)
    logger.info("[live] session start (actor=%s, session=%s)", actor, session_id)
    telemetry.emit_action(action="live_session", pane_uid="-", actor=actor, detail="start", keys=None)
    outcome, reason = "ok", "stop"
    try:
        await _run_session(websocket, watcher, actor, meter)
    except WebSocketDisconnect:
        reason = "client gone"  # phone lock / tab close / tunnel drop — the normal ends
    except Exception:
        outcome = reason = "error"
        logger.exception("[live] session failed")
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "error", "message": "live session failed"})
    finally:
        logger.info(
            "[live] session end: %s (%d turns, $%.4f, session=%s)",
            reason, meter.turns, meter.usage.cost(), session_id,
        )
        meter.finish()  # final cumulative OTel record + fold cost into the status bar
        telemetry.emit_action(
            action="live_session", pane_uid="-", actor=actor,
            detail=f"end ({meter.turns} turns, ${meter.usage.cost():.4f})", keys=None, outcome=outcome,
        )
        with contextlib.suppress(Exception):
            await websocket.close()
