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
        self.script, self.offered = list(script), []

    def _user(self, text, images=()):
        self.history.append(("user", text, *images))

    def _model(self, text):
        self.history.append(("model", text, []))

    async def _complete(self, *, tools=True):
        self.offered.append(tools)
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
    assert s.history[2] == ("results", [("type_in_pane", "sent" if ok else "declined")])
    assert audits == ["live_type_in_pane"]


def test_a_new_message_supersedes_an_unanswered_card(monkeypatch):
    """Typing instead of tapping Send/Cancel must not leave the turn stuck on the card with
    the new message queued silently behind it: the card is declined as superseded, the
    model is told why, and the new message is answered as its own turn."""
    from tests.test_live_mode import _ScriptedWS

    s = _Fake([("", [("type_in_pane", {"pane_id": "%1", "text": "rebase"}),
                     ("press_key", {"pane_id": "%1", "key": "Enter"})]),
               ("Left it.", []), ("Got it.", [])])
    meter = L._Meter("s", "a", _FLASH, text=True)
    typed, answers = [], []
    monkeypatch.setattr(L.telemetry, "audit", lambda *a, **k: None)
    monkeypatch.setattr(L.tmux, "send_keys", lambda *a, **k: typed.append(a))
    monkeypatch.setattr(L.tmux, "pane_pid", lambda pane: "42")
    record = s.send_tool_result
    monkeypatch.setattr(s, "send_tool_result", lambda c, p: (answers.append(p), record(c, p))[1])

    class Browser(_Browser):
        async def send_json(self, obj):
            self.sent.append(obj)
            # The user types past the first card (and only that one).
            if obj["type"] == "propose" and [f["type"] for f in self.sent].count("propose") == 1:
                await L._forward_client(_ScriptedWS([{"action": "text", "text": "test"},
                                                     {"action": "stop"}]), s, meter)
            if self.sent.count({"type": "turn_complete"}) == 2:
                self.done.set()

    ws = Browser(meter)

    async def go():
        rx = asyncio.create_task(L._receiver(ws, s, _Watcher(), meter))
        await s.send_text("tell work to rebase, then press enter")
        await asyncio.wait_for(ws.done.wait(), 5)
        rx.cancel()

    asyncio.run(go())
    # Both cards, the one typed past and the next call in that turn, read "Cancelled — you
    # sent a new message" on the client, and neither holds the new message up.
    assert [f["ok"] for f in ws.sent if f["type"] == "decided"] == [None, None]
    assert answers == [{"status": "declined", "reason": L._DECLINED["superseded"]}] * 2
    assert typed == [] and meter.approvals == {} and not meter.superseded
    assert ("user", "test") in s.history  # delivered, and answered as its own turn
    assert _said(ws.sent) == ["Left it.", "Got it."]


def test_a_card_already_tapped_is_not_superseded():
    """A Send tapped just before the message, its turn still unwinding, stands."""
    from tests.test_live_mode import _ScriptedWS

    meter = L._Meter("s", "a", _FLASH, text=True)

    async def go():
        meter.approvals["p"] = tapped = asyncio.get_running_loop().create_future()
        tapped.set_result(True)
        await L._forward_client(_ScriptedWS([{"action": "text", "text": "test"},
                                             {"action": "stop"}]), _Fake([]), meter)

    asyncio.run(go())
    assert not meter.superseded


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
        seen.update(model=model, n=len(contents), tools=config.tools, cfg=config)
        return Ns(candidates=[Ns(content=content)], usage_metadata=usage)

    client = Ns(aio=Ns(models=Ns(generate_content=generate_content)))
    monkeypatch.setattr(C.llm, "_client", lambda: client)
    s = C._Gemini(_FLASH, "sys")
    s._user("find it", [("image/png", b"PNG")])
    image, text = s.history[0].parts
    assert (image.inline_data.mime_type, image.inline_data.data, text.text) == (
        "image/png", b"PNG", "find it")
    reply, calls, split = asyncio.run(s._complete())
    assert (reply, split) == ("Looking.", P.Split(60, 10, 0, 0, 40, 0))
    assert [(c.id, c.name, c.args) for c in calls] == [("f1", "find_sessions", {"query": "x"})]
    assert seen["model"] == "gemini-3-flash-preview" and s.history[-1] is content
    s._results([(calls[0], {"status": "ok"})])
    (part,) = s.history[-1].parts
    asyncio.run(s._complete(tools=False))
    assert seen["cfg"].tool_config.function_calling_config.mode == "NONE"
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
    s._user("find it", [("image/png", b"PNG")])
    assert s.history[0]["content"] == [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "UE5H"}},
        {"type": "text", "text": "find it"}]
    reply, calls, split = asyncio.run(s._complete())
    assert (reply, split) == ("Checking.", P.Split(105, 20, 0, 0, 900, 0))
    assert seen["model"] == "claude-sonnet-5-5" and seen["system"] == "sys"
    assert seen["tools"][0].keys() == {"name", "description", "input_schema"}
    assert seen["tool_choice"] == {"type": "auto"}
    assert s.history[-1] == {"role": "assistant", "content": blocks}
    s._results([(calls[0], {"status": "ok"})])
    assert s.history[-1]["content"] == [
        {"type": "tool_result", "tool_use_id": "tu1", "content": '{"status": "ok"}'}]
    asyncio.run(s._complete(tools=False))
    assert seen["tool_choice"] == {"type": "none"}


def test_a_model_that_never_stops_calling_tools_answers_without_them(monkeypatch):
    """The last step offers no tools, so the turn ends on an answer, not a dead end."""
    monkeypatch.setattr(L.agent_history, "offered", lambda: True)
    monkeypatch.setattr(L.agent_history, "resolve", lambda q: [])
    s = _Fake([("", [("find_sessions", {"query": "x"})])] * (C.STEPS - 1) + [("None.", [])])
    frames, audits, _ = _turn(s, "loop", monkeypatch)
    assert len(audits) == C.STEPS - 1 and not s.script
    assert s.offered == [True] * (C.STEPS - 1) + [False]
    assert _said(frames) == ["None."] and frames[-1]["type"] == "turn_complete"
    assert s.history[-2] == ("user", C._LAST)


def test_a_call_after_tools_are_withdrawn_is_refused_not_run(monkeypatch):
    monkeypatch.setattr(L.agent_history, "offered", lambda: True)
    monkeypatch.setattr(L.agent_history, "resolve", lambda q: [])
    s = _Fake([("", [("find_sessions", {"query": "x"})])] * C.STEPS)
    frames, audits, _ = _turn(s, "loop", monkeypatch)
    assert len(audits) == C.STEPS - 1 and not s.script
    assert s.history[-2:] == [("results", [("find_sessions", "rejected")]), ("model", C._DONE, [])]
    assert frames[-1]["type"] == "turn_complete"


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
    assert s.history[-1] == ("model", C._DONE, [])  # the next turn doesn't answer _OUTCOME
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


class _Client:
    """The browser end of _forward_client: hands over scripted frames, then stop."""

    def __init__(self, *frames):
        self.frames, self.sent = [*frames, {"action": "stop"}], []

    async def receive_json(self):
        return self.frames.pop(0)

    async def send_json(self, obj):
        self.sent.append(obj)


def test_a_turn_past_the_queue_is_refused_not_held():
    """Turns wait behind a slow answer only up to TURNS_QUEUED; the next is refused (and
    flagged, so the client drops its thumbnails) instead of piling images up in memory."""
    s = _Fake([])
    frames = [{"action": "text", "text": f"t{i}"} for i in range(C.TURNS_QUEUED + 1)]
    ws = _Client(*frames)
    asyncio.run(L._forward_client(ws, s, L._Meter("s", "a", _FLASH, text=True)))
    assert s._inbox.qsize() == C.TURNS_QUEUED
    assert ws.sent[-1] == {"type": "error", "refused": True,
                           "message": "Still answering earlier turns; not sent"}


_TYPE = ("", [("type_in_pane", {"pane_id": "%1", "text": "rebase"})])


def test_a_reconnect_restores_the_screen_transcript_as_quoted_context(monkeypatch):
    """A restart loses the history while the client still shows it, and the user answers what
    they see: "yes" must reach the model with the question it answers, as text that carries
    no call, result or approval, and the card says the context was restored."""
    s = _Fake([_TYPE, ("Sent yes to work.", [])])
    kept = s.restore([{"role": "user", "text": "which session had the oom?"},
                      {"role": "model", "text": "The rate-limit one. Want me to reopen it?"},
                      {"role": "approve", "text": "ok"}, "junk"])
    assert kept == 2  # a role the client never sends, or no row at all, is dropped
    frames, _, typed = _turn(s, "yes", monkeypatch, ok=True)
    first = s.history[0][1]
    assert first.startswith("[Restored conversation]") and first.endswith("\n\nyes")
    assert "Assistant: The rate-limit one. Want me to reopen it?" in first
    card = next(f for f in frames if f["type"] == "propose")
    assert card["warn"] == C.CAUTION_RESTORED
    assert typed == [("%1", "rebase", True, True)]
    assert s.restore([{"role": "user", "text": "late"}]) == 0  # never over live history


def test_restored_text_is_bounded_to_its_tail():
    s = _Fake([])
    s.restore([{"role": "user", "text": "x" * C.RESTORED_CHARS}, {"role": "model", "text": "end?"}])
    assert "(earlier turns omitted)" in s._restored and s._restored.count("x") < C.RESTORED_CHARS
    assert s.asked
    s = _Fake([])
    s.restore([{"role": "model", "text": "Reopen it? " + "x" * C.RESTORED_CHARS}])
    assert not s.asked  # the question was cut, so it anchors nothing


def test_a_refused_bare_yes_never_ends_silent(monkeypatch):
    """No card and no typed row show for the refusal, so a model quiet after it doesn't
    leave the turn empty: the turn asks what to do itself."""
    s = _Fake([_TYPE, ("", []), ("", [])])
    frames, _, typed = _turn(s, "yes", monkeypatch, ok=True)
    assert typed == [] and _said(frames) == [C._UNANCHORED]


def test_a_bare_yes_with_nothing_asked_never_reaches_a_pane(monkeypatch):
    """With no question from the assistant in this conversation, "yes" has no referent: the
    pane change is refused before any card, and the model is told to ask instead."""
    s = _Fake([_TYPE, ("What should I say yes to?", [])])
    frames, audits, typed = _turn(s, "Yes, ok - do it! Thanks", monkeypatch, ok=True)
    assert typed == [] and not any(f["type"] == "propose" for f in frames)
    assert s.history[2] == ("results", [("type_in_pane", "declined")])
    assert audits == ["live_type_in_pane"]
    assert s.asked  # the model asked back, so the next "yes" has its referent


@pytest.mark.parametrize(("text", "bare"), [
    ("Yes, that's fine", True), ("that’s right, go ahead", True), ("let's go", True),
    ("yes, please do that", True),
    ("yes, that's the auth fix", False), ("yes, rebase it", False)])
def test_agreement_is_matched_on_its_words(text, bare):
    assert C.only_agrees(text) is bare


def test_a_question_anchors_only_the_next_message_sent():
    """A "yes" queued while a turn runs was written before that turn's reply: a question
    in the reply must not anchor it after the fact."""
    async def go():
        s = _Fake([])
        s.asked = True
        await s.send_text("tell work to rebase")
        await s.send_text("yes")  # sent while that turn runs, before its reply
        return [s._inbox.get_nowait()[2] for _ in range(2)]
    assert asyncio.run(go()) == [True, False]


@pytest.mark.parametrize(("reply", "asks"), [
    ("Reopen it? It is idle.", True), ("Reopen it?**", True),
    ("See https://example.com/x?id=1 for it.", False), ("Done.", False)])
def test_only_a_sentence_ending_in_a_question_mark_asks(reply, asks):
    assert bool(C._ASKS.search(reply)) is asks


def test_a_bare_yes_to_a_question_is_carded_with_a_warning(monkeypatch):
    s = _Fake([_TYPE, ("Sent yes to work.", [])])
    s.asked = True  # the last reply asked something (set at turn end, as tested above)
    frames, _, typed = _turn(s, "ok", monkeypatch, ok=True)
    assert next(f for f in frames if f["type"] == "propose")["warn"] == C.CAUTION_BARE.format("ok")
    assert typed == [("%1", "rebase", True, True)]


def _users(s):
    return [h[1] for h in s.history if h[0] == "user"]


def _talk(s, monkeypatch, *turns):
    """Typed turns in a row, each (text, the user's answer to any card)."""
    for text, ok in turns:
        s._inbox = asyncio.Queue()  # each turn runs on a new event loop in this harness
        _turn(s, text, monkeypatch, ok=ok)


def test_the_pane_a_chat_is_on_leads_its_next_turn(monkeypatch):
    """"Ask the agent" right after talking about a pane means that pane, so each turn says which
    pane the conversation is on: the last one a reply named or an action reached. A declined
    action reached nothing, so it moves nothing."""
    s = _Fake([("Work is waiting to run its tests.", []),
               ("", [("type_in_pane", {"pane_id": "%2", "text": "status?"})]), ("Not sent.", []),
               ("", [("type_in_pane", {"pane_id": "%2", "text": "status?"})]), ("Sent.", []),
               ("Ok.", [])])
    _talk(s, monkeypatch, ("what is work doing", None), ("ask the agent for its status", False),
          ("send it to the shell then", True), ("thanks", None))
    work, shell = (L._FOCUS.format(L._pane_name(d), d["pane_id"]) for d in _Watcher().digest())
    first, *rest = _users(s)
    assert "[Current pane" not in first
    assert [u.split("\n\n")[0] for u in rest if u != C._OUTCOME] == [work, work, shell]


def test_a_reply_to_the_assistants_question_is_marked_as_one(monkeypatch):
    """With a pane's PENDING QUESTION also in view, "no, for the other one" was once read as
    declining that pane's command: the turn says it follows the assistant's own question."""
    s = _Fake([("Should I send it elsewhere?", []), ("Ok.", []), ("Ok.", [])])
    _talk(s, monkeypatch, ("status of all workstreams", None), ("no for the other one", None),
          ("thanks", None))
    assert [C.OPEN_QUESTION in u for u in _users(s)] == [False, True, False]
    assert _users(s)[1].endswith(f"{C.OPEN_QUESTION}\n\nno for the other one")


_PANES = [{"pane_id": "%1", "title": "tmuxrc opus 5.5 main session", "window_index": "2"},
          {"pane_id": "%5", "title": "fable session monitor", "window_index": "7"},
          {"pane_id": "%6", "label": "main", "window_index": "12"}]


@pytest.mark.parametrize(("reply", "pane"), [
    ("The fable session monitor is investigating a 500.", "%5"),
    ("Main is idle, and Fable Session Monitor waits on you.", "%5"),  # the last named
    ("Sending to tmuxrc opus 5.5 main session.", "%1"),  # not "main", inside it
    ("main is idle", "%6"), ("window 7 is idle", "%5"), ("window 1 is idle", None),
    ("Nothing to report.", None)])
def test_a_reply_names_a_pane_by_title_or_number(reply, pane):
    assert (L._named(_PANES, reply) or {}).get("pane_id") == pane


_ASKING = {"pane_id": "%5", "question": "Run gcloud logging read? 1) Yes 2) Yes, always 3) No"}
_MENU = {**_ASKING, "menu": ["Yes", "Yes, always", "No"]}


@pytest.mark.parametrize(("request_", "name", "args", "pane", "refused"), [
    # Relaying a message never answers the pane's prompt on the way: no key, no menu row.
    ("no for the fable one", "press_key", {"key": "Escape"}, _ASKING, True),
    ("no for the fable one", "press_key", {"key": "Down"}, _ASKING, True),
    ("no for the fable one", "type_in_pane", {"text": "3"}, _ASKING, True),
    ("no for the fable one", "type_in_pane", {"text": "No"}, _ASKING, True),
    ("no for the fable one", "type_in_pane", {"text": "From the user: status?"}, _ASKING, False),
    ("ask the agent", "type_in_pane", {"text": "make test"}, {"pane_id": "%9"}, False),
    # Asked for, by the key's job or the answer's verb or number, it goes to the card.
    ("decline it", "press_key", {"key": "Escape"}, _ASKING, False),
    ("decline it", "press_key", {"key": "Enter"}, _ASKING, True),  # the wrong key's job
    ("pick 3", "type_in_pane", {"text": "3"}, _ASKING, False),
    ("ask window 3 for its status", "type_in_pane", {"text": "3"}, _ASKING, True),
    ("approve the command", "type_in_pane", {"text": "1"}, _ASKING, False),
    # A menu takes no message at all: its Enter would pick the highlighted row ("1. Yes").
    ("ask it for status", "type_in_pane", {"text": "From the user: status?"}, _MENU, "menu"),
    ("pick 3", "type_in_pane", {"text": "3"}, _MENU, False),
    ("send it the screenshot", "send_image_to_pane", {"image_number": 1}, _MENU, "menu"),
    ("no for the fable one", "type_in_pane", {"text": "3"}, _MENU, True),
    # A cursor list searches what's typed: even an asked-for row goes by keys.
    ("pick 3", "type_in_pane", {"text": "3"}, {**_MENU, "answer_style": "cursor"}, "menu"),
    ("pick 3", "press_key", {"key": "Down"}, {**_MENU, "answer_style": "cursor"}, False),
    ("pick 3", "press_key", {"key": "Enter"}, {**_MENU, "answer_style": "cursor"}, False),
    # Negated, it asks for nothing; a No named with a verb is a row, not a negation.
    ("don't decline it", "press_key", {"key": "Escape"}, _ASKING, True),
    ("no, decline it", "press_key", {"key": "Escape"}, _ASKING, True),
    ("pick No", "type_in_pane", {"text": "3"}, _ASKING, False),
    # A yes to the assistant's own question carries that question's words.
    (("Want me to cancel its command?", "yes"), "press_key", {"key": "Escape"}, _ASKING, False),
    (("Pick 3. No in fable?", "yes"), "type_in_pane", {"text": "3"}, _ASKING, False)])
def test_a_panes_prompt_is_answered_only_when_asked(request_, name, args, pane, refused):
    s = Ns(unanchored=False, request=request_ if isinstance(request_, tuple) else ("", request_),
           turn_pane=None)
    want = refused if isinstance(refused, str) else "unasked_answer" if refused else None
    assert L._refusal(s, name, {**args, "pane_id": pane["pane_id"]}, [pane]) == want


def test_a_turn_knows_the_question_it_answers(monkeypatch):
    s = _Fake([("Want me to cancel its command?", []), ("Ok.", []), ("Ok.", [])])
    _talk(s, monkeypatch, ("status", None), ("yes", None))
    assert s.request == ("Want me to cancel its command?", "yes")
    _talk(s, monkeypatch, ("and now", None))
    assert s.request == ("", "and now")
    s.script = [("Want me to cancel its command?", []), ("Ok.", [])]
    _talk(s, monkeypatch, ("status", None), ("no", None))
    assert s.request == ("", "no")  # a no carries none of the question's words


def test_a_queued_message_keeps_the_pane_shown_when_it_was_sent():
    async def go():
        s = _Fake([])
        s.focus = ("%1", "[Current pane: a]")
        await s.send_text("first")
        s.focus = ("%2", "[Current pane: b]")  # the first turn's reply moved it afterwards
        await s.send_text("second")
        return [s._inbox.get_nowait()[3] for _ in range(2)]
    assert asyncio.run(go()) == [("%1", "[Current pane: a]"), ("%2", "[Current pane: b]")]


def test_a_reconnect_restores_the_pane_the_replies_and_sends_were_about(monkeypatch):
    s = _Fake([])
    frames = [{"action": "resume", "transcript": [
        {"role": "model", "text": "Shell is idle."}, {"role": "user", "text": "and work?"},
        {"role": "model", "text": "Work is waiting to run its tests."},
        {"role": "typed", "text": "shell (%2): make test"}, {"role": "model", "text": "Sent."}]},
        {"action": "stop"}]
    from tests.test_live_mode import _ScriptedWS
    asyncio.run(L._forward_client(_ScriptedWS(frames), s, L._Meter("s", "a", _FLASH, text=True),
                                  _Watcher()))
    shell = _Watcher().digest()[1]  # the send came after the reply that named work
    assert s.focus == ("%2", L._FOCUS.format(L._pane_name(shell), "%2"))
    assert s.restored_replies() == []  # handed over once: a later resume replays nothing


@pytest.mark.parametrize(("question", "menu"), [
    ({"prompt": "Run it?", "answer_style": "menu", "options": ["Yes", "No"]}, ["Yes", "No"]),
    ({"prompt": "Pick", "answer_style": "cursor"}, []),
    ({"prompt": "Run it?", "answer_style": "menu", "options": 1}, []),
    ({"prompt": "Run it?", "answer_style": "menu", "options": "Yes"}, []),
    ({"prompt": "Which branch?", "answer_style": "text", "options": ["main"]}, None),
    ("a bare string", None), (None, None)])
def test_only_a_widget_question_is_a_menu(question, menu):
    from openbus.watcher import Watcher
    assert Watcher._question_menu({"question": question}) == menu


def test_a_name_two_panes_share_moves_nothing():
    twins = [{"pane_id": "%1", "title": "tests", "window_index": "1"},
             {"pane_id": "%2", "title": "tests", "window_index": "1"}]
    assert L._named(twins, "tests is waiting") is None
    assert L._named(twins, "window 1 is waiting") is None


def test_a_resumed_pane_becomes_the_current_one_before_the_watcher_sees_it(monkeypatch):
    async def resume(*a, **k):
        return {"status": "opened", "pane_id": "%9", "window": "auth fix"}
    monkeypatch.setattr(L.agent_history, "offered", lambda: True)
    monkeypatch.setitem(L._HISTORY_TOOLS, "resume_session", resume)
    s = _Fake([("", [("resume_session", {"session_id": "x"})]), ("Reopened.", []), ("Ok.", [])])
    _talk(s, monkeypatch, ("resume the auth fix", True), ("tell it to rebase", None))
    assert _users(s)[-1].startswith(L._FOCUS.format(L._pane_name({"title": "auth fix"}), "%9"))


_FABLE = {"pane_id": "%7", "title": "fable session monitor", "window_index": "5",
          "prs": [{"repo": "q/fable", "number": 412}]}


@pytest.mark.parametrize(("request_", "turn_pane", "refused"), [
    ("ask the agent for all 8 workstream status", "%3", True),  # the conversation is on %3
    ("ask the agent for all 8 workstream status", "%7", False),
    ("ask the agent for all 8 workstream status", None, False),  # no conversation yet
    ("now tell fable session monitor to stop", "%3", False),  # named by title
    ("and window 5?", "%3", False),  # or number
    ("address review on PR 412", "%3", False),  # or a PR it worked on
    ("and the tmuxrc opus 5.5 main session?", "%3", True),  # naming another pane
    ("ask window 2 about PR 412", "%3", True)])  # a named pane wins over a PR number
def test_a_send_off_the_current_pane_needs_the_user_to_name_it(request_, turn_pane, refused):
    s = Ns(unanchored=False, request=("", request_), turn_pane=turn_pane)
    args = {"pane_id": "%7", "text": "From the user: status?"}
    assert (L._refusal(s, "type_in_pane", args, [_FABLE, _PANES[0]]) == "elsewhere") is refused


def test_an_open_button_moves_no_focus(monkeypatch):
    s = _Fake([("", [("open_pane", {"pane_id": "%2"})]), ("Here.", []), ("Ok.", [])])
    _talk(s, monkeypatch, ("where's that one", None), ("and now", None))
    assert "[Current pane" not in _users(s)[-1]


@pytest.mark.parametrize("row", ["shell (%2): make test", "shell: make test",
                                 "other:1 (%2) (not submitted): ls"])
def test_a_restored_send_names_its_pane_by_id_or_name(row):
    assert L._restored_pane(_Watcher().digest(), "typed", row)["pane_id"] == "%2"


def test_a_name_or_pr_two_panes_share_unlocks_neither():
    twins = [{"pane_id": "%1", "title": "main", "prs": [{"number": 7}]},
             {"pane_id": "%2", "title": "tmuxrc main", "prs": [{"number": 7}]}]
    s = Ns(unanchored=False, request=("", "tell tmuxrc main about PR 7"), turn_pane="%9")
    assert L._refusal(s, "type_in_pane", {"pane_id": "%1", "text": "hi"}, twins) == "elsewhere"
    assert L._refusal(s, "type_in_pane", {"pane_id": "%2", "text": "hi"}, twins) is None
