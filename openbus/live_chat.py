"""Live Mode's text sessions on ordinary chat models (Gemini Flash on Vertex, Claude).

A realtime voice model is the wrong tool for typing: the ones that can answer in text at
all are the small realtime models, and a typed request deserves a real chat model. This
adapter speaks the same session protocol as the voice ones (live_providers.connect), so
live.py's receiver, tool choke point (_handle_tool_call: audit, consent cards), prompt and
browser protocol are unchanged — only the wire differs. The wire is a plain request/response
tool loop: send the conversation, get text and/or tool calls, hand each call to live.py as
an event, append the answers, repeat until a reply comes back with no calls.
Rationale: docs/design/live-mode.md § Text mode.
"""

from __future__ import annotations

import contextlib
import json
import logging
from asyncio import Queue
from collections import deque

from . import llm
from .live_providers import Event, LiveModel, Split, ToolCall, Unreachable, tools

logger = logging.getLogger(__name__)

# Pane updates kept for the next typed turn. They supersede each other (a digest, or one
# pane's screen after an action), so a long quiet spell must not pile up dozens of them.
CONTEXT_KEPT = 4
# Statuses that mean the entry itself is wrong (bad key, no access, no such model): the
# user fixes config, so they end the session with the reason instead of a retry loop.
_FATAL = {401, 403, 404}
_FAILED = "(The model call failed; try again.)"
# Model requests per typed turn. A turn normally takes two or three (look up, act, say);
# a model that keeps calling tools instead of answering is stopped, not paid for forever.
STEPS = 8
_STOPPED = f"(Stopped after {STEPS} steps without a reply.)"
# A request can succeed with nothing in it (a blocked prompt, a refusal with no fallback):
# a turn with nothing else to show still gets a visible answer rather than silence.
_EMPTY = "(No response from the model; try again.)"
# Asked once when the model goes quiet after acting (Flash does, after a tool result). If it
# stays quiet the turn shows nothing more: the approved card and the typed row already say
# what happened, and a placeholder under them reads as a failure.
_OUTCOME = "(Tell the user the outcome in one sentence.)"
_DONE = "(Done.)"  # closes a turn that stayed quiet; never shown
# Typed turns kept in the conversation. Every request resends all of it, so an unbounded
# session costs more per turn and eventually overflows the context window. Whole turns are
# dropped from the front, which keeps every tool call next to its result.
TURNS_KEPT = 20


class _Chat:
    """The loop, independent of the vendor. A subclass keeps the conversation in its own
    wire format and supplies _user / _model / _complete / _results."""

    def __init__(self, model: LiveModel, system: str) -> None:
        self.model, self.system, self.history = model, system, []
        self._inbox: Queue[str] = Queue()
        self._context: deque[str] = deque(maxlen=CONTEXT_KEPT)
        self._answers: list[tuple[ToolCall, dict]] = []
        self._usage = [0] * len(Split._fields)
        self._starts: list[int] = []  # where each kept turn begins in history

    async def send_audio(self, _pcm: bytes) -> None:
        """A text session has no mic; a stray frame is dropped."""

    async def send_text(self, text: str) -> None:
        self._inbox.put_nowait(text)

    async def send_context(self, text: str) -> None:
        """No reply-less channel exists on a chat API, so updates wait for the next turn."""
        self._context.append(text)

    async def send_tool_result(self, call: ToolCall, payload: dict) -> None:
        self._answers.append((call, payload))

    async def events(self):
        """A turn the model did not finish (failed, stopped, empty) is closed in history with
        the note the user saw, so a later request cannot resume its request or tool chain."""
        while True:
            text = await self._inbox.get()
            self._starts.append(len(self.history))
            if len(self._starts) > TURNS_KEPT:
                cut = self._starts[-TURNS_KEPT]
                del self.history[:cut]
                self._starts = [start - cut for start in self._starts[-TURNS_KEPT:]]
            self._user("\n\n".join([*self._context, text]))
            self._context.clear()
            sep = ""  # both clients join a turn's model transcripts verbatim
            acted = asked = False
            for _ in range(STEPS):
                try:
                    reply, calls, usage = await self._complete()
                except Exception as e:
                    code = getattr(e, "status_code", None) or getattr(e, "code", None)
                    if code in _FATAL:
                        raise Unreachable(
                            f"{self.model.label}: {self.model.model} refused (HTTP {code})"
                        ) from e
                    # Anything else (rate limit, 5xx, timeout) costs this turn, not the
                    # conversation: a reconnect would start over with no history.
                    logger.warning("[live] %s call failed: %r", self.model.model, e)
                    yield Event("transcript", role="model", text=sep + _FAILED)
                    self._model(_FAILED)
                    break
                self._usage = [a + b for a, b in zip(self._usage, usage, strict=True)]
                yield Event("usage", usage=Split(*self._usage))
                if not (reply or calls):
                    if acted and not asked:
                        asked = True
                        self._user(_OUTCOME)
                        continue
                    self._model(_DONE if acted else _EMPTY)  # closed, like a failed turn
                    reply = "" if acted else _EMPTY
                if reply:
                    yield Event("transcript", role="model", text=sep + reply)
                    sep = " "
                if not calls:
                    break
                acted = True
                # live._receiver runs each call to completion (consent included) and
                # answers it through send_tool_result before asking for the next event,
                # so every answer is in by the time this generator resumes.
                for call in calls:
                    yield Event("tool_call", call=call)
                self._results(self._answers)
                self._answers = []
            else:
                self._model(_STOPPED)
                yield Event("transcript", role="model", text=sep + _STOPPED)
            yield Event("turn_complete")


class _Gemini(_Chat):
    """A Gemini text model through the classifier's Vertex client (llm._client) — the same
    project, credentials and region, with a longer timeout than a parse call needs."""

    def __init__(self, model: LiveModel, system: str) -> None:
        super().__init__(model, system)
        t = llm.genai_types()
        self._cfg = t.GenerateContentConfig(
            system_instruction=system,
            tools=[t.Tool(function_declarations=[t.FunctionDeclaration(**d) for d in tools()])],
            automatic_function_calling=t.AutomaticFunctionCallingConfig(disable=True),
            http_options=t.HttpOptions(timeout=60_000),
        )

    def _user(self, text: str) -> None:
        t = llm.genai_types()
        self.history.append(t.Content(role="user", parts=[t.Part(text=text)]))

    def _model(self, text: str) -> None:
        t = llm.genai_types()
        self.history.append(t.Content(role="model", parts=[t.Part(text=text)]))

    async def _complete(self):
        r = await llm._client().aio.models.generate_content(  # noqa: SLF001 - reuse, see class doc
            model=self.model.model, contents=self.history, config=self._cfg
        )
        content = r.candidates[0].content if r.candidates else None
        parts = (content and content.parts) or []
        if parts:
            self.history.append(content)  # whole, so thought signatures ride back
        reply = "".join(p.text for p in parts if p.text and not p.thought)
        calls = [ToolCall(p.function_call.id, p.function_call.name, p.function_call.args)
                 for p in parts if p.function_call]
        u = r.usage_metadata
        cached = (u and u.cached_content_token_count) or 0
        return reply, calls, Split(
            ((u and u.prompt_token_count) or 0) - cached,
            ((u and u.candidates_token_count) or 0) + ((u and u.thoughts_token_count) or 0),
            0, 0, cached, 0,
        )

    def _results(self, answers) -> None:
        t = llm.genai_types()
        self.history.append(t.Content(role="user", parts=[
            t.Part(function_response=t.FunctionResponse(id=c.id, name=c.name, response=p))
            for c, p in answers
        ]))


class _Claude(_Chat):
    """Claude over the Anthropic API. Effort low: these are short conversational turns,
    and the model's own default is tuned for long agentic work. Refusals fall back
    server-side rather than ending the turn with nothing."""

    def __init__(self, model: LiveModel, system: str, client) -> None:
        super().__init__(model, system)
        self._client = client
        self._tools = [{"name": d["name"], "description": d["description"],
                        "input_schema": d["parameters"]} for d in tools()]

    def _user(self, text: str) -> None:
        self.history.append({"role": "user", "content": text})

    def _model(self, text: str) -> None:
        self.history.append({"role": "assistant", "content": text})

    async def _complete(self):
        r = await self._client.beta.messages.create(
            model=self.model.model, max_tokens=16000, system=self.system,
            messages=self.history, tools=self._tools, cache_control={"type": "ephemeral"},
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"], fallbacks="default",
        )
        if r.content:
            self.history.append({"role": "assistant", "content": r.content})
        reply = "".join(b.text for b in r.content if b.type == "text")
        calls = [ToolCall(b.id, b.name, b.input) for b in r.content if b.type == "tool_use"]
        u = r.usage
        # Cache writes bill above list and reads far below it; writes are folded into
        # uncached input (a small undercount), reads get the card's cached rate.
        return reply, calls, Split(
            u.input_tokens + (u.cache_creation_input_tokens or 0), u.output_tokens,
            0, 0, u.cache_read_input_tokens or 0, 0,
        )

    def _results(self, answers) -> None:
        self.history.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": c.id, "content": json.dumps(p)}
            for c, p in answers
        ]})


@contextlib.asynccontextmanager
async def open_session(model: LiveModel, system_prompt: str):
    if model.backend == "anthropic":
        import anthropic  # noqa: PLC0415 - only a Claude entry pays for the import

        async with anthropic.AsyncAnthropic() as client:
            yield _Claude(model, system_prompt, client)
    else:
        yield _Gemini(model, system_prompt)
