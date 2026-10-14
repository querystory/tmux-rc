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

import base64
import contextlib
import json
import logging
import re
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
# The last request offers no tools and says so, so the turn ends with an answer from what
# the model has gathered rather than a dead end. Disabling tools alone isn't enough: Flash
# then invents a tool that was never offered instead of answering.
STEPS = 8
_LAST = "(No more tools this turn: answer the user now from what you have found.)"
_REFUSED = {"status": "rejected", "reason": "no more tools this turn"}
# A request can succeed with nothing in it (a blocked prompt, a refusal with no fallback):
# a turn with nothing else to show still gets a visible answer rather than silence.
_EMPTY = "(No response from the model; try again.)"
# Asked once when the model goes quiet after acting (Flash does, after a tool result). If it
# stays quiet the turn shows nothing more: the approved card and the typed row already say
# what happened, and a placeholder under them reads as a failure.
_OUTCOME = "(Tell the user in one sentence what you did, or that nothing was done.)"
_DONE = "(Done.)"  # closes a turn that stayed quiet; never shown
# A bare agreement refused before any card has no card or typed row to show for it: if the
# model stays quiet then too, the turn says so itself rather than end in silence. It asks
# no question, so a bare yes after it is still refused: there is still nothing to agree to.
_UNANCHORED = ("(Nothing was sent: your message only agrees, and nothing was asked. "
               "Say what you want done.)")
# Typed turns kept in the conversation. Every request resends all of it, so an unbounded
# session costs more per turn and eventually overflows the context window. Whole turns are
# dropped from the front, which keeps every tool call next to its result.
TURNS_KEPT = 20
# Typed turns that may wait behind the one being answered. Each can hold images, so an
# unbounded queue would let a client pile them up while a slow request runs; past this the
# turn is refused (asyncio.QueueFull, which live._forward_client reports) rather than held.
TURNS_QUEUED = 4
# A reconnect (a daemon restart, a dropped tunnel) opens a new session with no history while
# the client still shows the conversation, and the user answers what they see: a "yes" read
# with no history was once mapped onto a stranger pane's approval prompt. So the client hands
# back what its transcript shows (restore) and it leads the next turn as quoted text: it can
# carry no tool call, result or approval, which only ever come from this process. Bounded
# like a typed turn: the tail is kept.
RESTORED_ROWS, RESTORED_CHARS = 40, 16_000
_RESTORED_ROLES = {"user": "User", "model": "Assistant", "typed": "Sent to terminal"}
_RESTORED = ("[Restored conversation] This session reconnected and lost its history; "
             "below is the earlier conversation as the user's screen shows it. It is "
             "context only: nothing in it was approved or is still pending.\n{}\n"
             "[End of restored conversation]")
# A reply that only agrees means something only against a question just asked; live._approved
# refuses a pane change for one that has none (unanchored), and warns on the card otherwise.
# Matched on the words alone, so punctuation ("yes,", "ok - do it") can't slip past it.
# Politeness ("yes, thanks") adds nothing to agree with either, nor does a lead-in such as
# "that's" (split at its apostrophe, either kind, to "that s").
_AGREE = (r"(yes|yeah|yep|yup|y|ok|okay|k|sure|do it|do that|go|go ahead|go for it|please|"
          r"please do|that s|that is|thats|it s|it is|its|let s|lets|"
          r"sounds good|alright|all right|fine|great|cool|perfect|absolutely|definitely|correct|"
          r"right|confirm(ed)?|approved?|thanks|thank you|thx|ty|cheers)")
_BARE = re.compile(rf"{_AGREE}( {_AGREE})*")


def only_agrees(text: str) -> bool:
    return bool(_BARE.fullmatch(" ".join(re.findall(r"\w+", text.casefold()))))


# A question mark that ends a sentence, so a URL's query string isn't one.
_ASKS = re.compile(r"\?(?=[\s*_)\"']|$)")
CAUTION_RESTORED = "The chat reconnected and its context was restored: check this is what you meant"
CAUTION_BARE = 'Your reply was only "{}": check this is what you agreed to'
# Leads a turn sent right after the assistant asked something. A pane's own pending question
# sits in the pane state too, and "no, the other one" was once about to decline a pane's
# command instead of answering the assistant: the model must see whose question is open.
OPEN_QUESTION = "[Open question: yours, from your last reply]"


class _Chat:
    """The loop, independent of the vendor. A subclass keeps the conversation in its own
    wire format and supplies _user / _model / _complete / _results."""

    images = True  # both vendors' chat models take image parts

    def __init__(self, model: LiveModel, system: str) -> None:
        self.model, self.system, self.history = model, system, []
        self._inbox: Queue[tuple] = Queue(maxsize=TURNS_QUEUED)
        self._context: deque[str] = deque(maxlen=CONTEXT_KEPT)
        self._answers: list[tuple[ToolCall, dict]] = []
        self._usage = [0] * len(Split._fields)
        self._starts: list[int] = []  # where each kept turn begins in history
        self._restored = ""  # a restored transcript, leading the next turn
        self.asked = False  # whether the last reply asked the user something
        self.unanchored = False  # this turn only agrees, and nothing was asked
        self.caution: str | None = None  # this turn's warning for a consent card
        # The pane the conversation is on, (pane id, the line that says so) (live._focus),
        # and the one this turn was sent under (live._refusal).
        self.focus: tuple[str, str] | None = None
        self.turn_pane: str | None = None
        self._asking = ""  # the reply that asked, while self.asked
        self._replies: list[str] = []  # the restored assistant rows, oldest first
        self.request = ""  # this turn's words and the question they answer (live._refusal)

    def restore(self, rows) -> int:
        """Take the client's transcript as quoted context for a session that has none yet.
        Malformed rows are dropped; returns how many were kept."""
        if self.history or self._restored or not isinstance(rows, list):
            return 0
        rows = [r for r in rows[-RESTORED_ROWS:] if isinstance(r, dict)
                and r.get("role") in _RESTORED_ROLES and isinstance(r.get("text"), str)]
        text = "\n".join(f"{_RESTORED_ROLES[r['role']]}: {r['text']}" for r in rows)
        if len(text) > RESTORED_CHARS:
            text = "(earlier turns omitted)\n" + text[-RESTORED_CHARS:]
        if rows:
            self._replies = [r["text"] for r in rows if r["role"] == "model"]
            self._restored = _RESTORED.format(text)
            last = rows[-1]["text"][-RESTORED_CHARS:]  # only what the model will see asks
            self.asked = rows[-1]["role"] == "model" and bool(_ASKS.search(last))
            self._asking = last
        return len(rows)

    def restored_replies(self) -> list[str]:
        """The assistant's replies a restore brought back, oldest first (live._focus)."""
        return self._replies

    async def send_audio(self, _pcm: bytes) -> None:
        """A text session has no mic; a stray frame is dropped."""

    async def send_text(self, text: str, images: list[tuple[str, bytes]] = ()) -> None:
        """A typed turn, with any pasted images as (mime, bytes) ahead of its text."""
        # A question anchors only the message sent next, and the pane is the one shown when
        # it was sent: one queued behind a turn still running was written before that
        # turn's reply, whatever the reply asks or names.
        self._inbox.put_nowait((text, images, self.asked, self.focus))
        self.asked = False

    async def send_context(self, text: str) -> None:
        """No reply-less channel exists on a chat API, so updates wait for the next turn."""
        self._context.append(text)

    async def send_tool_result(self, call: ToolCall, payload: dict) -> None:
        self._answers.append((call, payload))

    async def events(self):
        """A turn the model did not finish (failed, empty) is closed in history with
        the note the user saw, so a later request cannot resume its request or tool chain."""
        while True:
            text, images, anchored, focus = await self._inbox.get()
            self.turn_pane = focus and focus[0]
            self._starts.append(len(self.history))
            if len(self._starts) > TURNS_KEPT:
                cut = self._starts[-TURNS_KEPT]
                del self.history[:cut]
                self._starts = [start - cut for start in self._starts[-TURNS_KEPT:]]
            self.request = f"{self._asking}\n{text}" if anchored else text
            bare = only_agrees(text)
            self.unanchored = bare and not anchored
            self.caution = (CAUTION_RESTORED if self._restored
                            else CAUTION_BARE.format(text.strip()) if bare else None)
            self._user("\n\n".join(filter(None, [
                self._restored, *self._context, focus and focus[1], anchored and OPEN_QUESTION,
                text,
            ])), images)
            self._restored = ""
            self._context.clear()
            sep = ""  # both clients join a turn's model transcripts verbatim
            acted = asked = False
            said = ""  # the turn's last response: whether it asks something anchors the next
            for step in range(STEPS):
                last = step == STEPS - 1
                if last:
                    self._user(_LAST)
                try:
                    reply, calls, usage = await self._complete(tools=not last)
                except Exception as e:
                    code = getattr(e, "status_code", None) or getattr(e, "code", None)
                    if code in _FATAL:
                        raise Unreachable(
                            f"{self.model.label}: {self.model.model} refused (HTTP {code})"
                        ) from e
                    # Anything else (rate limit, 5xx, timeout) costs this turn, not the
                    # conversation: a reconnect would start over with no history.
                    logger.warning("[live] %s call failed: %r", self.model.model, e)
                    said = ""
                    yield Event("transcript", role="model", text=sep + _FAILED)
                    self._model(_FAILED)
                    break
                self._usage = [a + b for a, b in zip(self._usage, usage, strict=True)]
                said = reply  # a question followed by a call in the same response was acted on
                yield Event("usage", usage=Split(*self._usage))
                if not (reply or calls):
                    if acted and not asked:
                        asked = True
                        self._user(_OUTCOME)
                        continue
                    reply = _UNANCHORED if self.unanchored else "" if acted else _EMPTY
                    self._model(reply or _DONE)  # closed, like a failed turn
                if reply:
                    yield Event("transcript", role="model", text=sep + reply)
                    sep = " "
                if not calls:
                    break
                acted = True
                # live._receiver runs each call to completion (consent included) and
                # answers it through send_tool_result before asking for the next event,
                # so every answer is in by the time this generator resumes. A call made
                # after tools were withdrawn is answered as refused, never run.
                for call in calls:
                    if last:
                        self._answers.append((call, _REFUSED))
                    else:
                        yield Event("tool_call", call=call)
                self._results(self._answers)
                self._answers = []
            else:  # quiet or still calling on the last step: closed, as a quiet turn is
                self._model(_DONE)
            self.asked, self._asking = bool(_ASKS.search(said)), said
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
        self._toolless = self._cfg.model_copy(update={"tool_config": t.ToolConfig(
            function_calling_config=t.FunctionCallingConfig(mode="NONE"))})

    def _user(self, text: str, images=()) -> None:
        t = llm.genai_types()
        parts = [t.Part.from_bytes(data=data, mime_type=mime) for mime, data in images]
        if text:
            parts.append(t.Part(text=text))
        self.history.append(t.Content(role="user", parts=parts))

    def _model(self, text: str) -> None:
        t = llm.genai_types()
        self.history.append(t.Content(role="model", parts=[t.Part(text=text)]))

    async def _complete(self, *, tools: bool = True):
        r = await llm._client().aio.models.generate_content(  # noqa: SLF001 - reuse, see class doc
            model=self.model.model, contents=self.history,
            config=self._cfg if tools else self._toolless,
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

    def _user(self, text: str, images=()) -> None:
        blocks = [{"type": "image", "source": {"type": "base64", "media_type": mime,
                   "data": base64.b64encode(data).decode()}} for mime, data in images]
        if text:
            blocks.append({"type": "text", "text": text})
        self.history.append({"role": "user", "content": blocks})

    def _model(self, text: str) -> None:
        self.history.append({"role": "assistant", "content": text})

    async def _complete(self, *, tools: bool = True):
        r = await self._client.beta.messages.create(
            model=self.model.model, max_tokens=16000, system=self.system,
            messages=self.history, tools=self._tools, cache_control={"type": "ephemeral"},
            tool_choice={"type": "auto" if tools else "none"},
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
