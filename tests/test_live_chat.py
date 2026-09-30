"""Text sessions on chat models (live_chat.py): the request/response tool loop behind the
same session protocol as voice, so every call still goes through live._handle_tool_call.

Under test: a plain reply; a tool call answered and fed back until a reply with no calls;
a pane-changing call waiting on the user's Send or Cancel; one audit per call; pane updates
riding in front of the next turn; failures that cost a turn versus ones that end the
session; and each vendor adapter's wire shapes, against fakes."""

import asyncio
from types import SimpleNamespace as Ns

import pytest

import openbus.live as L
import openbus.live_chat as C
import openbus.live_providers as P
from tests.test_live_mode import _Watcher

_FLASH = P._CHAT_DEFAULT[0]


class _Fake(C._Chat):
    """A chat model that answers from a script: each item is (reply, [(name, args)]) or an
    exception to raise. The conversation is kept as plain tuples to assert on."""

    def __init__(self, script):
        super().__init__(_FLASH, "system")
        self.script = list(script)

    def _user(self, text):
        self.history.append(("user", text))

    def _model(self, text):
        self.history.append(("model", text, []))

    async def _complete(self):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        reply, calls = item
        self.history.append(("model", reply, [n for n, _ in calls]))
        n = len(self.history)
        return reply, [P.ToolCall(f"c{n}{i}", nm, a) for i, (nm, a) in enumerate(calls)], \
            P.Split(10, 2, 0, 0, 5, 0)

    def _results(self, answers):
        self.history.append(("results", [(c.name, p["status"]) for c, p in answers]))


class _Browser:
    """The socket to the client: records frames, taps Send/Cancel on a proposal, and says
    when the turn is over."""

    def __init__(self, meter, ok=None):
        self.sent, self.meter, self.ok, self.done = [], meter, ok, asyncio.Event()

    async def send_json(self, obj):
        self.sent.append(obj)
        if obj["type"] == "propose":
            self.meter.approvals[obj["id"]].set_result(self.ok)
        if obj["type"] == "turn_complete":
            self.done.set()


def _turn(session, text, monkeypatch, *, ok=None):
    """One typed turn through the real receiver; returns (frames, audits, typed keys)."""
    audits, typed = [], []
    monkeypatch.setattr(L.telemetry, "audit", lambda action, *a, **k: audits.append(action))
    monkeypatch.setattr(L.tmux, "send_keys", lambda *a, **k: typed.append(a))
    monkeypatch.setattr(L.tmux, "pane_pid", lambda pane: "42")
    meter = L._Meter("s", "a", _FLASH, text=True)
    ws = _Browser(meter, ok)

    async def go():
        rx = asyncio.create_task(L._receiver(ws, session, _Watcher(), meter))
        await session.send_text(text)
        await asyncio.wait({rx, asyncio.create_task(ws.done.wait())},
                           return_when=asyncio.FIRST_COMPLETED)
        rx.cancel()
        if rx.done() and not rx.cancelled() and rx.exception():
            raise rx.exception()

    asyncio.run(go())
    return ws.sent, audits, typed


def _said(frames):
    return [f["text"] for f in frames if f["type"] == "transcript"]


def test_a_plain_reply_is_one_request_and_a_turn(monkeypatch):
    s = _Fake([("Two panes are idle.", [])])
    frames, audits, _ = _turn(s, "what's up", monkeypatch)
    assert _said(frames) == ["Two panes are idle."]
    assert [f["type"] for f in frames][-1] == "turn_complete"
    assert s.history == [("user", "what's up"), ("model", "Two panes are idle.", [])]
    assert audits == []


def test_a_tool_result_goes_back_until_the_model_answers_in_text(monkeypatch):
    monkeypatch.setattr(L.agent_history, "offered", lambda: True)
    monkeypatch.setattr(L.agent_history, "resolve", lambda q: [])
    s = _Fake([("", [("find_sessions", {"query": "trust copy"})]), ("None found.", [])])
    frames, audits, _ = _turn(s, "find my codex session", monkeypatch)
    assert s.history == [
        ("user", "find my codex session"),
        ("model", "", ["find_sessions"]),
        ("results", [("find_sessions", "ok")]),
        ("model", "None found.", []),
    ]
    assert _said(frames) == ["None found."]
    assert audits == ["live_find_sessions"]  # once per call, from the one choke point
    assert s._usage == [20, 4, 0, 0, 10, 0]  # summed across the turn's two requests


@pytest.mark.parametrize("ok", [True, False])
def test_a_pane_change_waits_for_send_or_cancel(monkeypatch, ok):
    s = _Fake([("", [("type_in_pane", {"pane_id": "%1", "text": "rebase"})]),
               ("Sent." if ok else "Left it.", [])])
    frames, audits, typed = _turn(s, "tell work to rebase", monkeypatch, ok=ok)
    kinds = [f["type"] for f in frames]
    assert kinds.index("propose") < kinds.index("decided")
    assert typed == ([("%1", "rebase", True, True)] if ok else [])
    assert s.history[2] == ("results", [("type_in_pane", "done" if ok else "declined")])
    assert audits == ["live_type_in_pane"]


def test_pane_updates_ride_in_front_of_the_next_turn_and_stay_bounded(monkeypatch):
    s = _Fake([("ok", [])])
    for i in range(C.CONTEXT_KEPT + 2):
        asyncio.run(s.send_context(f"[tmux update] {i}"))
    _turn(s, "and now?", monkeypatch)
    sent = s.history[0][1].split("\n\n")
    assert sent[-1] == "and now?" and len(sent) == C.CONTEXT_KEPT + 1
    assert sent[0] == "[tmux update] 2"  # the oldest were dropped, not the newest
    assert not s._context


def test_a_failed_call_costs_the_turn_not_the_conversation(monkeypatch):
    s = _Fake([RuntimeError("503"), ("back", [])])
    frames, _, _ = _turn(s, "one", monkeypatch)
    assert _said(frames) == ["(The model call failed; try again.)"]
    s._inbox = asyncio.Queue()  # the next turn runs on a new event loop in this harness
    frames, _, _ = _turn(s, "two", monkeypatch)
    assert _said(frames) == ["back"] and s.history[0] == ("user", "one")
    assert s.history[1] == ("model", C._FAILED, [])  # closed, so "two" cannot resume "one"


def test_a_refused_entry_ends_the_session_with_the_reason(monkeypatch):
    err = RuntimeError("nope")
    err.status_code = 404
    with pytest.raises(P.Unreachable, match="HTTP 404"):
        _turn(_Fake([err]), "hi", monkeypatch)


def test_chat_entries_connect_to_the_chat_adapter(monkeypatch):
    async def go():
        async with P.connect(_FLASH, "sys") as s:
            return s

    s = asyncio.run(go())
    assert isinstance(s, C._Gemini) and s.system == "sys"


def test_gemini_wire_shapes(monkeypatch):
    """The model's content goes back whole (thought signatures), calls keep their ids, and
    answers go back as function responses in one user turn."""
    t = C.llm.genai_types()
    fc = t.FunctionCall(id="f1", name="find_sessions", args={"query": "x"})
    content = t.Content(role="model", parts=[t.Part(text="looking", thought=True),
                                             t.Part(text="Looking."), t.Part(function_call=fc)])
    usage = Ns(prompt_token_count=100, cached_content_token_count=40,
               candidates_token_count=7, thoughts_token_count=3)
    seen = {}

    async def generate_content(model, contents, config):
        seen.update(model=model, n=len(contents), tools=config.tools)
        return Ns(candidates=[Ns(content=content)], usage_metadata=usage)

    client = Ns(aio=Ns(models=Ns(generate_content=generate_content)))
    monkeypatch.setattr(C.llm, "_client", lambda: client)
    s = C._Gemini(_FLASH, "sys")
    s._user("find it")
    reply, calls, split = asyncio.run(s._complete())
    assert (reply, split) == ("Looking.", P.Split(60, 10, 0, 0, 40, 0))
    assert [(c.id, c.name, c.args) for c in calls] == [("f1", "find_sessions", {"query": "x"})]
    assert seen["model"] == "gemini-3-flash-preview" and s.history[-1] is content
    s._results([(calls[0], {"status": "ok"})])
    (part,) = s.history[-1].parts
    assert (part.function_response.id, part.function_response.response) == ("f1", {"status": "ok"})


def test_claude_wire_shapes():
    """Tools go as input_schema, the assistant content is appended as returned, and the
    answers are tool_result blocks keyed by the tool_use id."""
    blocks = [Ns(type="thinking"), Ns(type="text", text="Checking."),
              Ns(type="tool_use", id="tu1", name="find_sessions", input={"query": "x"})]
    usage = Ns(input_tokens=5, cache_creation_input_tokens=100, cache_read_input_tokens=900,
               output_tokens=20)
    seen = {}

    async def create(**kw):
        seen.update(kw)
        return Ns(content=blocks, usage=usage)

    s = C._Claude(P._CHAT_DEFAULT[1], "sys", Ns(beta=Ns(messages=Ns(create=create))))
    s._user("find it")
    reply, calls, split = asyncio.run(s._complete())
    assert (reply, split) == ("Checking.", P.Split(105, 20, 0, 0, 900, 0))
    assert seen["model"] == "claude-sonnet-5-5" and seen["system"] == "sys"
    assert seen["tools"][0].keys() == {"name", "description", "input_schema"}
    assert s.history[-1] == {"role": "assistant", "content": blocks}
    s._results([(calls[0], {"status": "ok"})])
    assert s.history[-1]["content"] == [
        {"type": "tool_result", "tool_use_id": "tu1", "content": '{"status": "ok"}'}]


def test_a_model_that_never_stops_calling_tools_is_stopped(monkeypatch):
    monkeypatch.setattr(L.agent_history, "offered", lambda: True)
    monkeypatch.setattr(L.agent_history, "resolve", lambda q: [])
    s = _Fake([("", [("find_sessions", {"query": "x"})])] * C.STEPS)
    frames, audits, _ = _turn(s, "loop", monkeypatch)
    assert len(audits) == C.STEPS and not s.script
    assert _said(frames) == [C._STOPPED] and frames[-1]["type"] == "turn_complete"
    assert s.history[-1] == ("model", C._STOPPED, [])  # the chain ends before the next turn


def test_an_empty_response_still_answers_the_turn(monkeypatch):
    frames, _, _ = _turn(_Fake([("", [])]), "hi", monkeypatch)
    assert _said(frames) == [C._EMPTY]


def test_quiet_after_acting_is_asked_once_for_the_outcome(monkeypatch):
    s = _Fake([("", [("type_in_pane", {"pane_id": "%1", "text": "go"})]), ("", []),
               ("Told it to go.", [])])
    frames, _, typed = _turn(s, "tell it to go", monkeypatch, ok=True)
    assert typed and _said(frames) == ["Told it to go."]
    assert s.history[3:5] == [("model", "", []), ("user", C._OUTCOME)]


def test_still_quiet_after_acting_shows_no_placeholder(monkeypatch):
    s = _Fake([("", [("type_in_pane", {"pane_id": "%1", "text": "go"})]), ("", []), ("", [])])
    frames, _, typed = _turn(s, "tell it to go", monkeypatch, ok=True)
    assert typed and _said(frames) == [] and not s.script  # asked once, not again
    assert frames[-1]["type"] == "turn_complete"


def test_a_turns_replies_stay_apart_when_the_client_joins_them(monkeypatch):
    monkeypatch.setattr(L.agent_history, "offered", lambda: True)
    monkeypatch.setattr(L.agent_history, "resolve", lambda q: [])
    s = _Fake([("I'll check.", [("find_sessions", {"query": "x"})]), ("None found.", [])])
    frames, _, _ = _turn(s, "find it", monkeypatch)
    assert "".join(_said(frames)) == "I'll check. None found."


def test_old_turns_are_dropped_whole_from_the_front(monkeypatch):
    monkeypatch.setattr(L.agent_history, "offered", lambda: True)
    monkeypatch.setattr(L.agent_history, "resolve", lambda q: [])
    s = _Fake([])
    for i in range(C.TURNS_KEPT + 3):
        s.script = [("", [("find_sessions", {"query": "x"})]), (f"r{i}", [])]
        s._inbox = asyncio.Queue()  # each turn runs on a new event loop in this harness
        _turn(s, f"t{i}", monkeypatch)
    users = [h[1] for h in s.history if h[0] == "user"]
    assert users == [f"t{i}" for i in range(3, C.TURNS_KEPT + 3)]
    assert s.history[0] == ("user", "t3") and len(s.history) == 4 * C.TURNS_KEPT
