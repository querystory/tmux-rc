"""Live Mode: talk to every pane at once over a Gemini Live voice session.

One WebSocket (`/api/live-mode`) per session. The browser streams mic PCM up; the
daemon owns the Gemini Live connection, feeds it the watcher's always-current pane
state, streams the model's voice + transcripts back, and executes the session's single
tool — type_in_pane — through the same send_keys primitive every other input path uses.
Design + prompting rationale: docs/design/live-mode.md.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import time

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from . import telemetry, tmux
from .classify import _load_prompt

logger = logging.getLogger(__name__)

router = APIRouter()

# The Live-capable model — NOT the flash-lite classifier model (which has no live/bidi
# variant). Region likewise: Live models are region-pinned, not "global".
LIVE_MODEL = os.environ.get("TMUXRC_LIVE_MODEL", "gemini-live-2.5-flash-native-audio")
LIVE_REGION = os.environ.get("TMUXRC_LIVE_REGION", "us-central1")

# Ambient [tmux update] messages: at most one per this many seconds, and only when the
# watcher's state_version moved (the same change signal /api/state long-polls on).
UPDATE_MIN_SECONDS = 2.5
# Screen tail sent per pane in the connect snapshot and in post-type refreshes. Enough
# to answer "what's it doing / asking", small enough that N panes stay cheap.
SCREEN_TAIL_LINES = 60
SCREEN_TAIL_CHARS = 4000
# After typing, wait this long before sending the acted-on pane's fresh screen — the
# pane's app needs a beat to react before a capture shows anything new.
POST_TYPE_REFRESH_SECONDS = 1.5



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
    """Last SCREEN_TAIL_LINES of a pane's current screen, via the newest snapshot (the
    watcher strips the LLM dim-markers at that boundary). Empty if none captured yet."""
    hist = watcher.snapshots.get(pane_id) or []
    if not hist:
        return ""
    text = watcher.snapshot_text(pane_id, hist[-1]["id"]) or ""
    lines = text.rstrip().splitlines()[-SCREEN_TAIL_LINES:]
    return "\n".join(lines)[-SCREEN_TAIL_CHARS:]


def _pane_block(d: dict, screen: str | None) -> str:
    """One pane's state as prompt text. `d` is a watcher.digest() entry."""
    head = f"pane {d['pane_id']} — {d.get('tool') or 'unknown'}"
    if d.get("label"):
        head += f" ({d['label']})"
    head += f" — {d.get('activity') or 'unknown'}"
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


def _pane_context(watcher, with_screens: bool) -> str:
    """All panes' state as prompt text — the digest the phone's cards already use, plus
    (optionally) each pane's current screen tail. No LLM calls; pure reads of state the
    watcher keeps current anyway."""
    blocks = [
        _pane_block(d, _screen_tail(watcher, d["pane_id"]) if with_screens else None)
        for d in watcher.digest()
    ]
    return "\n\n".join(blocks) if blocks else "(no panes)"


def _system_prompt(watcher) -> str:
    stamp = time.strftime("%Y-%m-%d %H:%M %Z")
    return (
        f"{_load_prompt('live_prompt.txt')}\nNow: {stamp}"
        f"\n\n# Panes (live state)\n\n{_pane_context(watcher, with_screens=True)}"
    )


def _actor(websocket: WebSocket) -> str:
    """WHO is on this session, same trust model as server._audit: the relay-forwarded
    identity header is honored only from loopback peers (the tunnel-client), otherwise
    recorded as a claim."""
    peer = websocket.client.host if websocket.client else "?"
    claimed = websocket.headers.get("x-tunnel-user")
    if claimed and peer in ("127.0.0.1", "::1"):
        return claimed
    if claimed:
        return f"local:{peer} claiming {claimed[:60]!r}"
    return f"local:{peer}"


def _type_tool():
    """The session's single tool. Kept to exactly one action on purpose (v0.1) — see
    the design doc's 'deliberately absent' list."""
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
                                description="Target pane id from the pane state, e.g. %5",
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
                )
            ]
        )
    ]


async def _handle_tool_call(websocket: WebSocket, session, fc, watcher, actor: str) -> None:
    """Execute type_in_pane and answer Gemini tersely. The result NEVER rides back
    through the FunctionResponse (echo loops — see design doc); the model sees the
    outcome via the post-type ambient refresh instead."""
    from google.genai import types

    async def respond(payload: dict) -> None:
        await session.send_tool_response(
            function_responses=[
                # fc.id must ride back or the session wedges (inherited lesson).
                types.FunctionResponse(id=fc.id, name=fc.name, response=payload)
            ]
        )

    args = fc.args or {}
    pane_id = str(args.get("pane_id", "")).strip()
    text = str(args.get("text", ""))
    press_enter = bool(args.get("press_enter", True))

    # Reject malformed/echoed calls (the model occasionally parrots a FunctionResponse
    # back as a new tool call) instead of typing garbage into a real terminal.
    known = {"pane_id", "text", "press_enter"}
    labels = {d["pane_id"]: d.get("label") or d["pane_id"] for d in watcher.digest()}
    if fc.name != "type_in_pane" or set(args) - known or not text.strip() or pane_id not in labels:
        reason = "unknown pane" if pane_id not in labels else "malformed call"
        logger.info("[live] rejecting tool call %s(%s): %s", fc.name, args, reason)
        telemetry.emit_action(
            action="live_type", pane_uid=f"{tmux.server_uid()}:{pane_id or '?'}", actor=actor,
            detail=reason, keys=text, outcome=f"rejected: {reason}",
        )
        await respond({"status": "rejected", "reason": reason})
        return

    label = labels[pane_id]
    try:
        await asyncio.to_thread(tmux.send_keys, pane_id, text, press_enter, True)
    except Exception as e:  # noqa: BLE001 - report, don't kill the session
        logger.warning("[live] type_in_pane failed for %s", pane_id, exc_info=True)
        telemetry.emit_action(
            action="live_type", pane_uid=f"{tmux.server_uid()}:{pane_id}", actor=actor,
            detail=str(e)[:120], keys=text, outcome="error",
        )
        await respond({"status": "error", "reason": "pane did not accept input"})
        return

    telemetry.emit_action(
        action="live_type", pane_uid=f"{tmux.server_uid()}:{pane_id}", actor=actor,
        detail=f"into {label}" + (" +enter" if press_enter else ""), keys=text,
    )
    watcher.request_reparse(pane_id)  # the keystrokes changed the screen
    # Every action the voice takes is visibly logged in the overlay.
    await websocket.send_json(
        {"type": "typed", "pane_id": pane_id, "label": label,
         "text": text, "submitted": press_enter}
    )
    await respond({"status": "typed", "pane": label})

    # Let the pane react, then show the model what its keystrokes did — as ambient
    # state, not as a tool result.
    async def refresh() -> None:
        await asyncio.sleep(POST_TYPE_REFRESH_SECONDS)
        tail = await asyncio.to_thread(_screen_tail, watcher, pane_id)
        if tail:
            await _send_ambient(session, f"[tmux update] {label} ({pane_id}) after your typing:\n{tail}")

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

    with contextlib.suppress(Exception):  # a dropped session is the reconnect loop's job
        await session.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]),
            turn_complete=False,
        )


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
            session, f"[tmux update] current pane state:\n\n{_pane_context(watcher, with_screens=False)}"
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


async def _receiver(websocket: WebSocket, session, watcher, actor: str) -> None:
    """Gemini → client: voice audio, both transcripts, and tool calls."""
    try:
        while True:
            async for response in session.receive():
                if response.tool_call and response.tool_call.function_calls:
                    for fc in response.tool_call.function_calls:
                        logger.info("[live] tool call: %s(%s)", fc.name, fc.args)
                        await _handle_tool_call(websocket, session, fc, watcher, actor)
                if response.data:
                    await websocket.send_json(
                        {"type": "audio", "data": base64.b64encode(response.data).decode()}
                    )
                sc = response.server_content
                if sc:
                    if sc.input_transcription and sc.input_transcription.text:
                        await websocket.send_json(
                            {"type": "transcript", "role": "user",
                             "text": sc.input_transcription.text}
                        )
                    if sc.output_transcription and sc.output_transcription.text:
                        await websocket.send_json(
                            {"type": "transcript", "role": "model",
                             "text": sc.output_transcription.text}
                        )
                    if sc.turn_complete:
                        await websocket.send_json({"type": "turn_complete"})
    except asyncio.CancelledError:
        pass


async def _run_session(websocket: WebSocket, watcher, actor: str) -> None:
    """Connect to Gemini Live and run the session; reconnect with backoff on drops."""
    from google.genai import types

    client = _live_client()
    config = types.LiveConnectConfig(
        response_modalities=[types.Modality.AUDIO],
        tools=_type_tool(),
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
            async with client.aio.live.connect(model=LIVE_MODEL, config=config) as session:
                logger.info("[live] session up (model=%s, actor=%s)", LIVE_MODEL, actor)
                await websocket.send_json({"type": "status", "status": "listening"})
                side = [
                    asyncio.create_task(_receiver(websocket, session, watcher, actor)),
                    asyncio.create_task(_context_updater(session, watcher)),
                ]
                try:
                    await _forward_audio(websocket, session)
                    return  # client sent stop — clean exit
                finally:
                    for t in side:
                        t.cancel()
                    for t in side:
                        with contextlib.suppress(Exception):
                            await t
        except WebSocketDisconnect:
            raise  # client gone — nothing to reconnect for
        except Exception:
            if attempt >= max_reconnects:
                raise
            backoff = min(2**attempt, 15)
            logger.warning("[live] session dropped; reconnecting in %ds", backoff, exc_info=True)
            await websocket.send_json({"type": "status", "status": "reconnecting"})
            await asyncio.sleep(backoff)


@router.websocket("/api/live-mode")
async def live_mode(websocket: WebSocket) -> None:
    await websocket.accept()
    watcher = websocket.app.state.watcher
    actor = _actor(websocket)
    logger.info("[live] session start (actor=%s)", actor)
    telemetry.emit_action(action="live_session", pane_uid="-", actor=actor, detail="start", keys=None)
    outcome = "ok"
    try:
        await _run_session(websocket, watcher, actor)
    except WebSocketDisconnect:
        pass  # phone lock / tab close — the normal way sessions end
    except Exception:
        outcome = "error"
        logger.exception("[live] session failed")
        with contextlib.suppress(Exception):
            await websocket.send_json({"type": "error", "message": "live session failed"})
    finally:
        telemetry.emit_action(
            action="live_session", pane_uid="-", actor=actor, detail="end", keys=None, outcome=outcome
        )
        with contextlib.suppress(Exception):
            await websocket.close()
