"""Live Mode's provider seam: the one place that knows how a voice model is reached.

live.py owns everything that is NOT the wire — prompt assembly, tool dispatch guardrails,
metering, the browser protocol. It talks to the model through the tiny session protocol
below (send_audio / send_context / send_tool_result / events) so that adding a provider
is a new adapter class here, never an `if provider:` inside the coroutines. Rationale:
docs/design/live-mode.md § Model providers.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# The Live-capable model — NOT the flash-lite classifier model (which has no live/bidi
# variant). Region likewise: Live models are region-pinned, not "global".
LIVE_MODEL = os.environ.get("TMUXRC_LIVE_MODEL", "gemini-live-2.5-flash-native-audio")
LIVE_REGION = os.environ.get("TMUXRC_LIVE_REGION", "us-central1")

# Named keys press_key can send — a fixed whitelist, mapped to the tmux key names
# send_keys(literal=False) understands. Bounded on purpose: the model can only press
# keys that make sense for terminal UIs (cancel, interrupt, menu nav, submit), never an
# arbitrary key string that could be a chord we didn't vet.
KEYS = {
    "Escape": "Escape",   # cancel / reject the current prompt
    "Enter": "Enter",     # accept / continue with no text
    "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right",  # menu navigation
    "Tab": "Tab",         # cycle / complete
    "C-c": "C-c",         # interrupt what's running
    "C-d": "C-d",         # EOF / exit a REPL
}

_PANE_ID = {"type": "string",
            "description": "Target pane id — the id=%N handle from the window state, e.g. %5"}

# The session's tools as plain JSON Schema — the one format every provider accepts as-is
# (google-genai coerces the lowercase types). Two narrow verbs beat one overloaded one —
# the model can't accidentally fold text and a chord into a single ambiguous call, and
# press_key's whitelist keeps it from inventing arbitrary key sequences.
TOOLS = [
    {
        "name": "type_in_pane",
        "description": (
            "Type text into one tmux pane, exactly as if the user typed it at that "
            "terminal. Use only when the user clearly asks you to type, run, answer, or "
            "tell a pane something."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pane_id": _PANE_ID,
                "text": {"type": "string", "description": "The exact text to type"},
                "press_enter": {"type": "boolean",
                                "description": "Submit with Enter after typing (default true)"},
            },
            "required": ["pane_id", "text"],
        },
    },
    {
        "name": "press_key",
        "description": (
            "Press ONE control key in a pane (no text) — to cancel, interrupt, navigate a "
            "menu, or continue. Escape cancels/rejects the current prompt; C-c interrupts "
            "what's running; Up/Down then Enter picks a menu item; Enter alone "
            "accepts/continues; Tab cycles or completes; C-d sends EOF."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pane_id": _PANE_ID,
                "key": {"type": "string", "enum": list(KEYS),
                        "description": "One of: " + ", ".join(KEYS)},
            },
            "required": ["pane_id", "key"],
        },
    },
]


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: Any  # a dict when well-formed; live._handle_tool_call rejects anything else


@dataclass(frozen=True)
class Event:
    """One provider-neutral thing the model did. `kind` is one of: audio (24 kHz PCM16 in
    `data`), transcript (`role` user|model + `text`), tool_call (`call`), turn_complete,
    interrupted (the user barged in — stop playback), usage (`usage`: text_in, text_out,
    audio_in, audio_out token counts, CUMULATIVE for the connection)."""

    kind: str
    data: bytes | None = None
    role: str | None = None
    text: str | None = None
    call: ToolCall | None = None
    usage: tuple[int, int, int, int] | None = None


def connect(system_prompt: str):
    """Open one model connection: an async context manager yielding a session that speaks
    the protocol. Takes the system prompt per connect so a RECONNECT gets a fresh pane
    snapshot — the connect snapshot is the only place full screens are sent."""
    return _GeminiSession.open(system_prompt)


class _GeminiSession:
    """Gemini Live over the google-genai SDK."""

    def __init__(self, session) -> None:
        self._s = session

    @staticmethod
    @contextlib.asynccontextmanager
    async def open(system_prompt: str):
        from google import genai
        from google.genai import types

        # A dedicated Vertex client, deliberately NOT llm._client(): that one pins the
        # classifier's per-request timeout (an anti-wedge guard for one-shot parse calls)
        # which would sever a long-lived bidi stream, and defaults to the 'global' region
        # which Live models don't serve.
        project = os.environ.get("GOOGLE_CLOUD_PROJECT")
        if not project:
            raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set; cannot reach Vertex.")
        client = genai.Client(vertexai=True, project=project, location=LIVE_REGION)
        cfg = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            tools=[types.Tool(function_declarations=[types.FunctionDeclaration(**t) for t in TOOLS])],
            system_instruction=system_prompt,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            # Let the model choose NOT to answer — required for the noise/silence prompt
            # rules to work instead of the model replying to every sound.
            proactivity=types.ProactivityConfig(proactive_audio=True),
        )
        async with client.aio.live.connect(model=LIVE_MODEL, config=cfg) as s:
            yield _GeminiSession(s)

    async def send_audio(self, pcm16k: bytes) -> None:
        from google.genai import types

        await self._s.send_realtime_input(
            audio=types.Blob(data=pcm16k, mime_type="audio/pcm;rate=16000")
        )

    async def send_context(self, text: str) -> None:
        """turn_complete=False adds the content to the conversation but no model turn
        fires — the model simply has current state the next time the user speaks."""
        from google.genai import types

        await self._s.send_client_content(
            turns=types.Content(role="user", parts=[types.Part(text=text)]),
            turn_complete=False,
        )

    async def send_tool_result(self, call: ToolCall, payload: dict) -> None:
        from google.genai import types

        await self._s.send_tool_response(
            function_responses=[
                # call.id must ride back or the session wedges (inherited lesson).
                types.FunctionResponse(id=call.id, name=call.name, response=payload)
            ]
        )

    async def events(self) -> AsyncIterator[Event]:
        while True:  # receive() ends at each turn boundary; the session outlives it
            async for r in self._s.receive():
                if r.usage_metadata:
                    yield Event("usage", usage=gemini_usage(r.usage_metadata))
                if r.tool_call and r.tool_call.function_calls:
                    for fc in r.tool_call.function_calls:
                        yield Event("tool_call", call=ToolCall(fc.id, fc.name, fc.args))
                if r.data:
                    yield Event("audio", data=r.data)
                sc = r.server_content
                if sc:
                    if sc.input_transcription and sc.input_transcription.text:
                        yield Event("transcript", role="user", text=sc.input_transcription.text)
                    if sc.output_transcription and sc.output_transcription.text:
                        yield Event("transcript", role="model", text=sc.output_transcription.text)
                    if sc.interrupted:
                        yield Event("interrupted")
                    if sc.turn_complete:
                        yield Event("turn_complete")


def gemini_usage(u) -> tuple[int, int, int, int]:
    """Gemini's usage_metadata → (text_in, text_out, audio_in, audio_out). Prompt = input,
    response = output; the per-modality details split out audio and anything that isn't
    audio bills as text. These are CUMULATIVE session totals, which is what Event.usage
    promises."""
    from google.genai import types

    def audio(details) -> int:
        return sum((getattr(d, "token_count", 0) or 0) for d in details or []
                   if getattr(d, "modality", None) == types.Modality.AUDIO)

    prompt = getattr(u, "prompt_token_count", 0) or 0
    resp = getattr(u, "response_token_count", 0) or 0
    a_in = audio(getattr(u, "prompt_tokens_details", None))
    a_out = audio(getattr(u, "response_tokens_details", None))
    return (max(prompt - a_in, 0), max(resp - a_out, 0), a_in, a_out)
