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

import array
import base64
import contextlib
import json
import logging
import os
import re
from collections.abc import AsyncIterator
from collections import namedtuple
from dataclasses import dataclass, field
from typing import Any

from .config import json_list

logger = logging.getLogger(__name__)

# backend → the env vars that must be set before the model is OFFERED. Vertex resolves
# credentials at call time (SA key / ADC), so it gates on nothing here. Azure needs the
# resource host as well as the key.
_NEEDS = {
    "vertex": (),
    "gemini-api": ("GEMINI_API_KEY",),
    "openai": ("OPENAI_API_KEY",),
    "azure-openai": ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"),
}
_BACKEND_NAME = {
    "vertex": "Vertex",
    "gemini-api": "AI Studio",
    "openai": "OpenAI",
    "azure-openai": "Azure",
}

# gemini-2.5-flash native-audio published rates, USD per 1M tokens, by Split field. Audio
# and text bill at very different rates (audio out ≈ 24× text in), and CACHED input at a
# different rate again (OpenAI Realtime re-bills the whole context on every response, most
# of it cached at a 97-99% discount — pricing it uncached overstates a long session
# severalfold). So every entry carries a six-way card; an entry that omits a rate gets
# 2.5's — visibly the default, never a silent zero — except the cached rates, which
# default to the entry's OWN uncached rate: no published discount means no discount.
Split = namedtuple(
    "Split", ("text_in", "text_out", "audio_in", "audio_out", "text_cached", "audio_cached")
)
_RATES_25 = Split(0.50, 2.00, 3.00, 12.00, 0.50, 3.00)
_CACHED = {"text_cached": "text_in", "audio_cached": "audio_in"}


@dataclass(frozen=True)
class LiveModel:
    """One picker entry. `flags` holds backend-specific knobs the adapters read
    (proactive_audio for Gemini — 3.1 Flash Live rejects the field outright, so it must be
    per entry, not global; voice for OpenAI). For Azure, `model` is the DEPLOYMENT name."""

    label: str
    model: str
    backend: str = "vertex"
    rates: Split = _RATES_25
    flags: dict = field(default_factory=dict)

    @property
    def needs(self) -> tuple[str, ...]:
        return _NEEDS[self.backend]

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
        return all(os.environ.get(v) for v in self.needs)


def _coerce(e: dict) -> LiveModel:
    backend = e.get("backend", "vertex")
    if backend not in _NEEDS:
        raise ValueError(f"unknown backend {backend!r}")
    given = e.get("rates") or {}
    rates = {k: float(given.get(k, d)) for k, d in _RATES_25._asdict().items()}
    rates.update({c: rates[full] for c, full in _CACHED.items() if c not in given})
    return LiveModel(
        label=str(e["label"]),
        model=str(e["model"]),
        backend=backend,
        rates=Split(**rates),
        flags={
            k: v
            for k, v in e.items()
            if k not in ("label", "model", "backend", "rates")
        },
    )


# Unset TMUXRC_LIVE_MODELS = exactly what shipped before there was a table: 2.5 on Vertex,
# no picker. Newer entries are opt-in via .env (see .env.example for the shapes).
_DEFAULT = [
    LiveModel(
        "Gemini 2.5",
        "gemini-live-2.5-flash-native-audio",
        flags={"proactive_audio": True},
    )
]


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
    "Escape": "Escape",  # cancel / reject the current prompt
    "Enter": "Enter",  # accept / continue with no text
    "Up": "Up",
    "Down": "Down",
    "Left": "Left",
    "Right": "Right",  # menu navigation
    "Tab": "Tab",  # cycle / complete
    "C-c": "C-c",  # interrupt what's running
    "C-d": "C-d",  # EOF / exit a REPL
}

_PANE_ID = {
    "type": "string",
    "description": "Target pane id — the id=%N handle from the window state, e.g. %5",
}

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
                "press_enter": {
                    "type": "boolean",
                    "description": "Submit with Enter after typing (default true)",
                },
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
                "key": {
                    "type": "string",
                    "enum": list(KEYS),
                    "description": "One of: " + ", ".join(KEYS),
                },
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
    interrupted (the user barged in — stop playback), usage (`usage`: a Split of token
    counts, CUMULATIVE for the connection; the *_in fields EXCLUDE the cached ones)."""

    kind: str
    data: bytes | None = None
    role: str | None = None
    text: str | None = None
    call: ToolCall | None = None
    usage: Split | None = None


class Unreachable(RuntimeError):
    """The model cannot be reached for a reason a retry will never fix — a deployment or
    model id that does not exist, a rejected key. live.py neither reconnects on it nor
    hides the message: the user fixes config, and needs to see which."""


def connect(model: LiveModel, system_prompt: str):
    """Open one connection to `model`: an async context manager yielding a session that
    speaks the protocol. Takes the system prompt per connect so a RECONNECT gets a fresh
    pane snapshot — the connect snapshot is the only place full screens are sent."""
    opener = (
        _OpenAISession.open
        if model.backend in ("openai", "azure-openai")
        else _GeminiSession.open
    )
    return opener(model, system_prompt)


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
            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        else:
            # A dedicated Vertex client, deliberately NOT llm._client(): that one pins the
            # classifier's per-request timeout (an anti-wedge guard for one-shot parse
            # calls) which would sever a long-lived bidi stream, and defaults to the
            # 'global' region, which Live models don't serve — they are region-pinned.
            project = os.environ.get("GOOGLE_CLOUD_PROJECT")
            if not project:
                raise RuntimeError(
                    "GOOGLE_CLOUD_PROJECT is not set; cannot reach Vertex."
                )
            client = genai.Client(
                vertexai=True,
                project=project,
                location=os.environ.get("TMUXRC_LIVE_REGION", "us-central1"),
            )
        cfg = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            tools=[
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(**t) for t in TOOLS
                    ]
                )
            ],
            system_instruction=system_prompt,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            # Let the model choose NOT to answer — required for the noise/silence prompt
            # rules to work instead of the model replying to every sound. Per entry: 3.1
            # Flash Live rejects the setup field, so it is off unless the entry says so.
            proactivity=(
                types.ProactivityConfig(proactive_audio=True)
                if model.flags.get("proactive_audio")
                else None
            ),
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
                        yield Event(
                            "transcript", role="user", text=sc.input_transcription.text
                        )
                    if sc.output_transcription and sc.output_transcription.text:
                        yield Event(
                            "transcript",
                            role="model",
                            text=sc.output_transcription.text,
                        )
                    if sc.interrupted:
                        yield Event("interrupted")
                    if sc.turn_complete:
                        yield Event("turn_complete")


def gemini_usage(u) -> Split:
    """Gemini's usage_metadata → Split. Prompt = input, response = output; the
    per-modality details split out audio and anything that isn't audio bills as text.
    prompt_token_count INCLUDES the cached tokens, so those come out of the uncached
    buckets. These are CUMULATIVE session totals, which is what Event.usage promises."""
    from google.genai import types

    def audio(details) -> int:
        return sum(
            (getattr(d, "token_count", 0) or 0)
            for d in details or []
            if getattr(d, "modality", None) == types.Modality.AUDIO
        )

    prompt = getattr(u, "prompt_token_count", 0) or 0
    resp = getattr(u, "response_token_count", 0) or 0
    a_in = audio(getattr(u, "prompt_tokens_details", None))
    a_out = audio(getattr(u, "response_tokens_details", None))
    a_cached = audio(getattr(u, "cache_tokens_details", None))
    t_cached = (getattr(u, "cached_content_token_count", 0) or 0) - a_cached
    return Split(
        max(prompt - a_in - t_cached, 0), max(resp - a_out, 0), a_in - a_cached, a_out,
        t_cached, a_cached,
    )


def openai_endpoint(model: LiveModel) -> tuple[str, dict[str, str]]:
    """WebSocket URL + auth header. Azure speaks the same v1 Realtime protocol at the
    resource host with an api-key header, `model` being the deployment name; the host may
    be given as *.openai.azure.com or *.cognitiveservices.azure.com, with or without a
    scheme or trailing slash."""
    if model.backend == "azure-openai":
        host = re.sub(r"^https?://", "", os.environ["AZURE_OPENAI_ENDPOINT"]).split(
            "/"
        )[0]
        return (
            f"wss://{host}/openai/v1/realtime?model={model.model}",
            {"api-key": os.environ["AZURE_OPENAI_API_KEY"]},
        )
    return (
        f"wss://api.openai.com/v1/realtime?model={model.model}",
        {"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
    )


def resample_16k_to_24k(pcm: bytes) -> bytes:
    """Linear interpolation, 2 samples in → 3 out, int16 mono little-endian. Stateless per
    frame: the at-most-one-sample seam between 4096-sample frames is inaudible on speech
    and not worth carrying state for. No numpy — ~6k output samples every 256 ms is a
    couple of milliseconds of plain Python."""
    x = array.array("h")
    x.frombytes(pcm[: len(pcm) & ~1])
    n = len(x)
    if n < 2:
        return pcm
    y = array.array("h", bytes(2 * (n * 3 // 2)))
    for j in range(len(y)):
        pos = j * 2 / 3
        i = int(pos)
        a = x[i]
        b = x[i + 1] if i + 1 < n else a
        y[j] = int(a + (b - a) * (pos - i))
    return y.tobytes()


class _OpenAISession:
    """OpenAI Realtime (GA event names) over its raw WebSocket — no SDK: the protocol is a
    dozen JSON event types, and an SDK would be a dependency wrapping a websocket we
    already hold. Realtime is 24 kHz PCM16 both ways; the browser captures 16 kHz, so
    input is resampled here and web/ never learns which provider is on the wire."""

    def __init__(self, ws) -> None:
        self._ws = ws
        self._usage = [0] * len(Split._fields)  # per-response usage summed to totals
        self._active = False  # a response is streaming (response.created … done)
        self._pending = False  # a tool result is waiting for the turn to end

    @staticmethod
    @contextlib.asynccontextmanager
    async def open(model: LiveModel, system_prompt: str):
        import websockets

        url, headers = openai_endpoint(model)
        try:
            async with websockets.connect(
                url, additional_headers=headers, max_size=None
            ) as ws:
                s = _OpenAISession(ws)
                await s._send(
                    {
                        "type": "session.update",
                        "session": {
                            "type": "realtime",
                            "instructions": system_prompt,
                            "tools": [{"type": "function", **t} for t in TOOLS],
                            "tool_choice": "auto",
                            "output_modalities": ["audio"],
                            "audio": {
                                "input": {
                                    "format": {"type": "audio/pcm", "rate": 24000},
                                    "transcription": {
                                        "model": "gpt-4o-mini-transcribe"
                                    },
                                    # The model decides when a turn ended and speaks unprompted —
                                    # the analogue of Gemini's server-side VAD; semantic so a
                                    # thinking pause mid-command isn't cut off.
                                    "turn_detection": {
                                        "type": "semantic_vad",
                                        "interrupt_response": True,
                                    },
                                },
                                "output": {
                                    "format": {"type": "audio/pcm", "rate": 24000},
                                    "voice": model.flags.get("voice", "marin"),
                                },
                            },
                        },
                    }
                )
                yield s
        except websockets.exceptions.InvalidStatus as e:
            # The handshake is where a wrong deployment/model name or a bad key fails:
            # Azure answers 400/404 for a deployment that does not exist. Name the thing to
            # fix — retrying (live.py's reconnect loop) can't.
            host = url.split("/")[2]
            what = (
                f"deployment {model.model!r} not found on {host}"
                if e.response.status_code in (400, 404)
                else f"{host} refused the connection (HTTP {e.response.status_code})"
            )
            raise Unreachable(what) from e

    async def _send(self, ev: dict) -> None:
        await self._ws.send(json.dumps(ev))

    async def send_audio(self, pcm16k: bytes) -> None:
        await self._send(
            {
                "type": "input_audio_buffer.append",
                "audio": base64.b64encode(resample_16k_to_24k(pcm16k)).decode(),
            }
        )

    async def send_context(self, text: str) -> None:
        """A system-role item and NO response.create — the analogue of turn_complete=False:
        it lands in the conversation and nothing fires until the user next speaks. System
        rather than user role so the model can't mistake a state refresh for speech."""
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "message",
                    "role": "system",
                    "content": [{"type": "input_text", "text": text}],
                },
            }
        )

    async def send_tool_result(self, call: ToolCall, payload: dict) -> None:
        await self._send(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call.id,
                    "output": json.dumps(payload),
                },
            }
        )
        # Unlike Gemini, Realtime does not continue the turn on its own after a tool
        # result: ask for the follow-up so the model confirms what it did. If the response
        # that made the call is still streaming, asking now is an error
        # (conversation_already_has_active_response) — defer to its response.done.
        if self._active:
            self._pending = True
        else:
            await self._send({"type": "response.create"})

    async def events(self) -> AsyncIterator[Event]:
        async for raw in self._ws:
            ev = json.loads(raw)
            t = ev.get("type", "")
            if t == "response.output_audio.delta":
                yield Event("audio", data=base64.b64decode(ev["delta"]))
            elif t == "response.output_audio_transcript.delta":
                yield Event("transcript", role="model", text=ev["delta"])
            elif t == "conversation.item.input_audio_transcription.completed":
                yield Event("transcript", role="user", text=ev.get("transcript") or "")
            elif t == "response.function_call_arguments.done":
                yield Event(
                    "tool_call",
                    call=ToolCall(
                        ev["call_id"], ev["name"], _args(ev.get("arguments"))
                    ),
                )
            elif t == "input_audio_buffer.speech_started":
                yield Event("interrupted")
            elif t == "response.created":
                self._active = True
            elif t == "response.done":
                self._active = False
                self._add_usage((ev.get("response") or {}).get("usage") or {})
                yield Event("usage", usage=Split(*self._usage))
                yield Event("turn_complete")
                if self._pending:
                    self._pending = False
                    await self._send({"type": "response.create"})
            elif t == "error":
                # Session-fatal errors also close the socket, which ends this loop and
                # hands the reconnect to live.py; the rest are per-event and just logged.
                logger.warning("[live] realtime error: %s", ev.get("error"))

    def _add_usage(self, u: dict) -> None:
        """Per-response usage → connection totals. Each response re-bills the whole context
        as input (minus what the server cached), which is the real cost shape of this API
        and why it is summed rather than overwritten. The cached counts are a SUBSET of
        the text/audio input counts, so they move out of those buckets into their own
        (billed at the card's cached rates). The API returns token counts only, never a
        price, so counts × card is the only cost there is."""
        i, o = u.get("input_token_details") or {}, u.get("output_token_details") or {}
        c = i.get("cached_tokens_details") or {}
        ct, ca = c.get("text_tokens") or 0, c.get("audio_tokens") or 0
        split = (
            (i.get("text_tokens") or 0) - ct,
            o.get("text_tokens"),
            (i.get("audio_tokens") or 0) - ca,
            o.get("audio_tokens"),
            ct,
            ca,
        )
        for k, v in enumerate(split):
            self._usage[k] += v or 0


def _args(raw) -> Any:
    """Realtime ships tool arguments as a JSON string; anything unparseable is handed on
    as-is so live._handle_tool_call rejects it as malformed rather than us guessing."""
    try:
        return json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return raw
