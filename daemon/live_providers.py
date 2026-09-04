"""Live Mode's model table and provider seam: the one place that knows WHICH voice models
exist and HOW each is reached.

live.py owns everything that is NOT the wire — prompt assembly, tool dispatch guardrails,
metering, the browser protocol. It talks to the model through the tiny session protocol
below (send_audio / send_context / send_tool_result / events) so that adding a provider
is a new adapter class here, never an `if provider:` inside the coroutines. The browser
only ever names a LABEL from the table (same rule as launchers: the HTTP/WS surface never
carries a model id, backend, or credential). Rationale: docs/design/live-mode.md
§ Model providers.
"""

from __future__ import annotations

import contextlib
import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from .config import json_list

logger = logging.getLogger(__name__)

# backend → the env var that must hold its credential before the model is OFFERED. Vertex
# resolves credentials at call time (SA key / ADC), so it gates on nothing here.
_KEY_ENV = {"vertex": None, "gemini-api": "GEMINI_API_KEY"}
_BACKEND_NAME = {"vertex": "Vertex", "gemini-api": "AI Studio"}

# gemini-2.5-flash native-audio published rates, USD per 1M tokens, in the order
# (text_in, text_out, audio_in, audio_out). Audio and text bill at very different rates
# (audio out ≈ 24× text in), so every entry carries a four-way card; an entry that omits
# a rate gets 2.5's — visibly the default, never a silent zero.
_RATE_KEYS = ("text_in", "text_out", "audio_in", "audio_out")
_RATES_25 = (0.50, 2.00, 3.00, 12.00)


@dataclass(frozen=True)
class LiveModel:
    """One picker entry. `flags` holds backend-specific knobs the adapters read
    (proactive_audio for Gemini — 3.1 Flash Live rejects the field outright, so it must be
    per entry, not global)."""

    label: str
    model: str
    backend: str = "vertex"
    rates: tuple[float, float, float, float] = _RATES_25
    flags: dict = field(default_factory=dict)

    @property
    def key_env(self) -> str | None:
        return _KEY_ENV[self.backend]

    @property
    def hint(self) -> str:
        """The picker's one-line 'what am I choosing': where it runs and what talking costs
        (audio rates dominate; text is noise by comparison). Rendered here so the client
        stays a dumb list and the backend vocabulary has one home."""
        return f"{_BACKEND_NAME[self.backend]} · ${self.rates[2]:g}/${self.rates[3]:g} per 1M audio"

    def available(self) -> bool:
        """Offered only when its credential is present. A keyless entry stays in the
        table — so the picker appears the moment the key lands, no config edit — but is
        never offered, and `find` refuses it if a stale client names it anyway."""
        return not self.key_env or bool(os.environ.get(self.key_env))


def _coerce(e: dict) -> LiveModel:
    backend = e.get("backend", "vertex")
    if backend not in _KEY_ENV:
        raise ValueError(f"unknown backend {backend!r}")
    rates = e.get("rates") or {}
    return LiveModel(
        label=str(e["label"]), model=str(e["model"]), backend=backend,
        rates=tuple(float(rates.get(k, d)) for k, d in zip(_RATE_KEYS, _RATES_25)),
        flags={k: v for k, v in e.items() if k not in ("label", "model", "backend", "rates")},
    )


# Unset TMUXRC_LIVE_MODELS = exactly what shipped before there was a table: 2.5 on Vertex,
# no picker. Newer entries are opt-in via .env (see .env.example for the shapes).
_DEFAULT = [LiveModel("Gemini 2.5", "gemini-live-2.5-flash-native-audio",
                      flags={"proactive_audio": True})]


def models() -> list[LiveModel]:
    """The configured table. Re-read per call (cheap) so it tracks env like enabled()."""
    return json_list("TMUXRC_LIVE_MODELS", _DEFAULT, _coerce)


def available() -> list[LiveModel]:
    return [m for m in models() if m.available()]


def find(label: str | None) -> LiveModel | None:
    """Resolve a client-supplied label against the OFFERED list. No label = the first
    offered entry (what a user who never touches the picker gets). Unknown, or configured
    but keyless, = None — refused, never defaulted: silently answering with a different
    model would make a side-by-side comparison lie."""
    offered = available()
    if not label:
        return offered[0] if offered else None
    return next((m for m in offered if m.label == label), None)


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


def connect(model: LiveModel, system_prompt: str):
    """Open one connection to `model`: an async context manager yielding a session that
    speaks the protocol. Takes the system prompt per connect so a RECONNECT gets a fresh
    pane snapshot — the connect snapshot is the only place full screens are sent."""
    return _GeminiSession.open(model, system_prompt)


class _GeminiSession:
    """Gemini Live over the google-genai SDK — on Vertex (service account) or the AI Studio
    API (key), which is the only place Gemini 3.x Live models are served."""

    def __init__(self, session) -> None:
        self._s = session

    @staticmethod
    @contextlib.asynccontextmanager
    async def open(model: LiveModel, system_prompt: str):
        from google import genai
        from google.genai import types

        if model.backend == "gemini-api":
            client = genai.Client(api_key=os.environ[model.key_env])
        else:
            # A dedicated Vertex client, deliberately NOT llm._client(): that one pins the
            # classifier's per-request timeout (an anti-wedge guard for one-shot parse
            # calls) which would sever a long-lived bidi stream, and defaults to the
            # 'global' region, which Live models don't serve — they are region-pinned.
            project = os.environ.get("GOOGLE_CLOUD_PROJECT")
            if not project:
                raise RuntimeError("GOOGLE_CLOUD_PROJECT is not set; cannot reach Vertex.")
            client = genai.Client(vertexai=True, project=project,
                                  location=os.environ.get("TMUXRC_LIVE_REGION", "us-central1"))
        cfg = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            tools=[types.Tool(function_declarations=[types.FunctionDeclaration(**t) for t in TOOLS])],
            system_instruction=system_prompt,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            # Let the model choose NOT to answer — required for the noise/silence prompt
            # rules to work instead of the model replying to every sound. Per entry: 3.1
            # Flash Live rejects the setup field, so it is off unless the entry says so.
            proactivity=(types.ProactivityConfig(proactive_audio=True)
                         if model.flags.get("proactive_audio") else None),
        )
        async with client.aio.live.connect(model=model.model, config=cfg) as s:
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
