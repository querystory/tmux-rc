"""Replay scripted conversations through Live Mode's real routing, and record what it did.

A conversation runs through `live.live_mode`, the daemon's own socket handler, end to end:
the meter, the reconnect loop, the receiver, the tool choke point (`_handle_tool_call`) and
`tmux.send_keys`. Three things are swapped, and nothing else:

- the phone: a `Phone` that sends each scripted step once the daemon has answered the last;
- the fleet: a `Fleet` of invented panes in place of the watcher, which also answers the
  few tmux calls a send makes, so no replay can reach a real tmux server;
- the model: `live_providers.connect`, the provider seam, returns whatever `replay` is
  given: a `Script` here (scripted tool calls, for make test), or a real model (__main__).

Rationale: research/eval/README.md, "Live Mode replay".
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi import WebSocketDisconnect

from openbus import agent_history, live, live_providers, llm, scratch, telemetry, tmux
from openbus.live_providers import Event, LiveModel, ToolCall

HERE = Path(__file__).parent
RECONNECT = "<the phone's socket drops; the chat reconnects>"  # a Phone step
DROP = "<the provider connection drops>"  # a Script step


class Fleet:
    """The slice of openbus.watcher.Watcher Live Mode reads, over invented panes. Each pane
    is a digest() entry plus an optional `screen` (its terminal text) and `pid` (its
    process: change it to recycle the pane id under a card)."""

    def __init__(self, panes: list[dict]) -> None:
        self.panes = {p["pane_id"]: dict(p) for p in panes}
        self.reparsed: list[str] = []

    @classmethod
    def load(cls) -> Fleet:
        return cls(json.loads((HERE / "fleet.json").read_text())["panes"])

    @property
    def snapshots(self) -> dict:
        return {k: [{"id": "s", "text": p["screen"], "ts": 0}]
                for k, p in self.panes.items() if p.get("screen")}

    def digest(self) -> list[dict]:
        return [{k: v for k, v in p.items() if k not in {"screen", "pid"}}
                for p in self.panes.values()]

    def state_version(self) -> int:
        return 1

    async def wait_for_state_change(self, since: int, timeout: float) -> int:  # noqa: ASYNC109
        await asyncio.sleep(timeout)  # nothing changes unless a script step changes it
        return since

    def request_reparse(self, pane_id: str) -> None:
        self.reparsed.append(pane_id)

    def pid(self, pane_id: str) -> str | None:
        pane = self.panes.get(pane_id)
        return pane.get("pid", "1000") if pane else None


@dataclass
class Trace:
    """What one replay did. Frames are everything the phone was sent, each tagged with the
    user turn it followed (`_turn`, 0 before the first); `keys` are the send-keys argv tails
    that reached the fake tmux; `results` the tool answers the model got."""

    frames: list[dict] = field(default_factory=list)
    keys: list[tuple] = field(default_factory=list)
    results: list[tuple] = field(default_factory=list)
    audits: list[dict] = field(default_factory=list)
    turn: int = 0

    def of(self, kind: str, turn: int | None = None) -> list[dict]:
        return [f for f in self.frames
                if f["type"] == kind and turn in (None, f["_turn"])]

    def sends(self, turn: int | None = None) -> list[tuple[str, str, str]]:
        """(pane id, label, what was typed) for each delivery the phone was shown."""
        return [(f["pane_id"], f["label"], f["text"]) for f in self.of("typed", turn)]

    def reply(self, turn: int) -> str:
        return "".join(f["text"] for f in self.of("transcript", turn) if f["role"] == "model")


_ids = itertools.count(1)


def call(name: str, **args) -> ToolCall:
    """One scripted tool call, as a provider would hand it over."""
    return ToolCall(f"call-{next(_ids)}", name, args)


@dataclass
class Script:
    """A scripted model: each user turn it hears is answered by the next entry of `turns`,
    a list of tool calls (call()), spoken lines (str) and DROP, then turn_complete. A turn
    with no entry left gets nothing but turn_complete. `prompts` holds the system prompt of
    each connection, `heard` the user turns, `context` the [tmux update]s."""

    turns: list[list] = field(default_factory=list)
    prompts: list[str] = field(default_factory=list)
    heard: list[str] = field(default_factory=list)
    context: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self._next = iter(self.turns)

    def connect(self, _model: LiveModel, system_prompt: str):
        self.prompts.append(system_prompt)
        return contextlib.nullcontext(_Session(self))  # async-usable since 3.10


class _Session:
    """The provider-seam session (live_providers: send_* and events) a Script plays."""

    images = False

    def __init__(self, script: Script) -> None:
        self.script = script
        self.inbox: asyncio.Queue[str] = asyncio.Queue()

    async def send_audio(self, _pcm: bytes) -> None:
        pass

    async def send_text(self, text: str, _images=()) -> None:
        self.script.heard.append(text)
        self.inbox.put_nowait(text)

    async def send_context(self, text: str) -> None:
        self.script.context.append(text)

    async def send_tool_result(self, fc, payload: dict) -> None:
        pass  # recorded for every model at the choke point (replay)

    async def events(self):
        while True:
            await self.inbox.get()
            for step in next(self.script._next, []):  # its own iterator
                if step is DROP:
                    raise ConnectionError("scripted provider drop")
                yield (Event("transcript", role="model", text=step) if isinstance(step, str)
                       else Event("tool_call", call=step))
            yield Event("turn_complete")


def approve(ok: bool = True) -> dict:
    """The phone's Send (or Cancel) on the newest card it was shown."""
    return {"action": "approve", "ok": ok}


class Phone:
    """The browser end of /api/live-mode, playing `steps`: a str is an utterance (the
    composer's typed turn, which a voice session takes too), a dict a raw client frame
    (approve() fills in the newest card's id), a callable is run on the fleet (a pane
    recycled under a card), and RECONNECT drops this socket. Each step waits until the
    daemon waits on the phone: no turn still running, or a card up. `steps` is shared
    with the socket a RECONNECT opens next."""

    def __init__(self, steps: list, fleet: Fleet, trace: Trace, query: dict) -> None:
        self.steps, self.fleet, self.trace, self.query_params = steps, fleet, trace, query
        self.app = SimpleNamespace(state=SimpleNamespace(watcher=fleet, push=None))
        self.client, self.headers = None, {}
        self.changed = asyncio.Event()
        self.up = self.dropped = False
        self.owed, self.card = 0, None  # turns not yet complete; the card up on this socket

    async def accept(self) -> None:
        pass

    async def close(self, code: int = 1000, reason: str = "") -> None:
        pass

    async def send_json(self, frame: dict) -> None:
        # Tagged with the turn being answered: a turn that superseded a card is queued
        # behind the one still finishing, which owns whatever arrives until it completes.
        late = 0 if frame.get("role") == "user" else max(self.owed - 1, 0)
        self.trace.frames.append({**frame, "_turn": self.trace.turn - late})
        if frame.get("status") == "listening":  # a reconnect drops the turn it was in
            self.up, self.owed = True, 0
        elif frame["type"] == "turn_complete":
            self.owed = max(0, self.owed - 1)
        elif frame["type"] == "propose":
            self.card = frame["id"]
        self.changed.set()

    async def receive_json(self) -> dict:
        while True:
            while not (self.up and (self.owed == 0 or self.card)):
                self.changed.clear()
                await self.changed.wait()
            # Taken only now: a wait cancelled (a reconnect's backoff) leaves it for the next.
            step = self.steps.pop(0) if self.steps else {"action": "stop"}
            if callable(step):
                step(self.fleet)
                continue
            if step == RECONNECT:
                self.dropped = True
                raise WebSocketDisconnect(1006)
            if isinstance(step, str):
                self.trace.turn += 1
                self.owed, self.card = self.owed + 1, None  # a new turn supersedes a card
                return {"action": "text", "text": step}
            if step["action"] == "approve":
                step = {"id": self.trace.of("propose")[-1]["id"], **step}
                if step["id"] == self.card:  # its turn goes on, to turn_complete
                    self.card = None
            return step


def _tmux(fleet: Fleet, trace: Trace):
    """tmux._run over the fleet: a send-keys is recorded, a pid read from the fleet, and
    anything else refused, as a server with no panes would."""
    def run(args, *_a, **_kw) -> str:
        if args[0] == "send-keys" and args[2] in fleet.panes:
            trace.keys.append(tuple(args[2:]))
            return ""
        if args[0] == "list-panes":
            return ""
        raise subprocess.CalledProcessError(1, ["tmux", *args], stderr="replay: no tmux")
    return run


async def replay(fleet: Fleet, steps: list, connect, *, text: bool = False,
                 model: LiveModel | None = None, seconds: float = 30) -> Trace:
    """Play `steps` (see Phone) through live_mode against `fleet`, with `connect` as the
    provider seam (Script.connect, or a real model's). Each RECONNECT opens a new socket
    for the same chat, as a phone back from a lock does."""
    trace, steps, chat = Trace(), list(steps), uuid.uuid4().hex
    model = model or LiveModel("replay", "replay", "vertex-chat" if text else "vertex")
    hold = live._hold  # the backoff's length is not under test

    @contextlib.asynccontextmanager
    async def seam(entry: LiveModel, prompt: str):
        """`connect`, with every tool answer the model gets recorded on its way back."""
        async with connect(entry, prompt) as session:
            send = session.send_tool_result

            async def answered(fc, payload: dict) -> None:
                trace.results.append((fc, payload))
                await send(fc, payload)
            session.send_tool_result = answered
            yield session

    with contextlib.ExitStack() as stack:
        for target, name, value in (
            (live, "enabled", lambda: True),
            (live, "pick", lambda *_a, **_k: model),
            (live, "_hold", lambda ws, _s, m: hold(ws, 0.01, m)),
            (live_providers, "connect", seam),
            (tmux, "_run", _tmux(fleet, trace)),
            (tmux, "pane_pid", fleet.pid),
            (tmux, "at_password_prompt", lambda *_a, **_k: False),
            (telemetry, "audit", lambda action, pane, *_a, **kw: trace.audits.append(
                {"action": action, "pane": pane, **kw})),
            (telemetry, "emit_live_turn", lambda **_k: None),
            (llm, "record_live_usage", lambda **_k: None),
            (agent_history, "offered", lambda: False),
            (scratch, "configured", lambda: None),
        ):
            stack.enter_context(mock.patch.object(target, name, value))
        try:
            async with asyncio.timeout(seconds):
                query = {"mode": "text" if text else "voice", "session": chat}
                while True:
                    phone = Phone(steps, fleet, trace, query)
                    await live.live_mode(phone)
                    if not phone.dropped:
                        break
        finally:
            # A cancelled await leaves its send-keys thread running: let every one finish
            # before the patches come off, or its Enter could reach the real tmux server.
            await asyncio.to_thread(_drain, fleet)
    return trace


def _drain(fleet: Fleet) -> None:
    """Wait out any delivery in flight: send_keys holds its pane's lock until it is done."""
    for pane in fleet.panes:
        with tmux._pane_lock(pane):
            pass


def cases() -> list[dict]:
    return json.loads((HERE / "cases.json").read_text())["cases"]


def score(turn: dict, trace: Trace, n: int) -> dict[str, bool | None]:
    """Turn `n` (1-based) of a case against what the replay did: every send went to a pane
    in `to`, the number of sends is within `sends`, and the reply holds each `answer`
    string. None where the turn sets no expectation."""
    panes = [pane for pane, _, _ in trace.sends(n)]
    low, high = (turn["sends"],) * 2 if isinstance(turn["sends"], int) else turn["sends"]
    reply = trace.reply(n).casefold()
    return {
        "target": all(p in turn["to"] for p in panes) if "to" in turn else None,
        "sends": low <= len(panes) <= high,
        "answered": all(a.casefold() in reply for a in turn["answer"]) if "answer" in turn
        else None,
    }
