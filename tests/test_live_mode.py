"""Live Mode: prompt-context assembly and the type_in_pane dispatch guardrails.

The session/transport itself is exercised live (it's a bidi stream to Vertex); what
must be pinned in tests is everything that decides WHAT the model sees and WHETHER a
tool call may touch a real terminal — the context builder and the reject paths."""

import asyncio
import logging
import time
from types import SimpleNamespace

import pytest

import openbus.live as L
import openbus.live_providers as P


def _run(coro):
    return asyncio.run(coro)


class _Watcher:
    def __init__(self):
        self.snapshots = {
            "%1": [{"id": "s1", "text": "one\ntwo\nthree", "ts": 1.0}],
            "%2": [{"id": "s2", "text": "idle-shell-screen", "ts": 1.0}],
        }
        self.reparsed = []

    def digest(self):
        return [
            {"pane_id": "%1", "label": "work", "window_index": "3", "tool": "claude",
             "activity": "waiting", "tmux_active": True,
             "headline": "asking about tests", "summary": "ran the suite",
             "prs": [{"repo": "querystory/qs-app", "number": 4955}],
             "cwd": "/repo/worktree",
             "question": "Run them? 1) yes 2) no", "history": []},
            {"pane_id": "%2", "label": "shell", "window_index": "4", "tool": "shell",
             "activity": "idle", "tmux_active": False,
             "headline": None, "summary": None, "question": None, "history": []},
        ]

    def snapshot_text(self, pane_id, snap_id):
        for s in self.snapshots.get(pane_id, []):
            if s["id"] == snap_id:
                return s["text"]
        return None

    def request_reparse(self, pane_id):
        self.reparsed.append(pane_id)


class _FC:
    def __init__(self, name="type_in_pane", args=None, id="call-1"):
        self.name, self.args, self.id = name, args, id


class _Session:
    def __init__(self):
        self.responses = []

    async def send_tool_result(self, call, payload):
        self.responses.append((call, payload))


class _WS:
    def __init__(self):
        self.sent = []

    async def send_json(self, obj):
        self.sent.append(obj)


_METER = L._Meter("s1", "tester", P._DEFAULT[0])  # a tool call only reads it


@pytest.mark.parametrize("debug", [False, True])
def test_receiver_preserves_transcripts_and_debug_logging(monkeypatch, caplog, debug):
    class Session:
        async def events(self):
            yield P.Event("transcript", role="user", text="private user words")
            yield P.Event("transcript", role="model", text="private model words")
            yield P.Event("interrupted")

    monkeypatch.setattr(L.telemetry, "QSDEBUG", debug)
    caplog.set_level(logging.INFO, logger=L.logger.name)
    ws = _WS()
    meter = L._Meter("s", "a", P._DEFAULT[0])
    _run(L._receiver(ws, Session(), _Watcher(), meter))
    assert ws.sent == [
        {"type": "transcript", "role": "user", "text": "private user words"},
        {"type": "transcript", "role": "model", "text": "private model words"},
        {"type": "interrupted"},
    ]
    assert meter._transcript() == "user: private user words\nmodel: private model words"
    assert ("private user words" in caplog.text) is debug
    assert ("private model words" in caplog.text) is debug


def test_pane_context_carries_state_and_screens():
    w = _Watcher()
    ctx = L._pane_context(w, screens="all")
    # User-facing identity (window number + name) leads; the %id is the tool-call handle.
    # Name falls back to label here (stub has no self-published title).
    assert 'window 3 "work" (id=%1) — claude — waiting' in ctx
    assert "ACTIVE" in ctx  # the focused pane is flagged for "here"/"this" resolution
    assert "PENDING QUESTION: Run them? 1) yes 2) no" in ctx
    assert "PRs this pane has worked on: querystory/qs-app#4955" in ctx
    assert 'cwd: "/repo/worktree"' in ctx
    assert "one\ntwo\nthree" in ctx and "idle-shell-screen" in ctx  # all screens ride along
    # digest-only updates omit every screen
    none = L._pane_context(w, screens="none")
    assert "one\ntwo\nthree" not in none and "idle-shell-screen" not in none
    # "active" carries ONLY the focused pane's screen — the fix for typing blind
    active = L._pane_context(w, screens="active")
    assert "one\ntwo\nthree" in active  # %1 is tmux_active
    assert "idle-shell-screen" not in active  # %2 is not, so its screen stays out


def test_pane_block_names_window_prefers_title_hides_id_role():
    # Heading leads with window number + best-first name (title over label), and the %id
    # is present only as the tool-call handle — never as the spoken identity.
    titled = L._pane_block(
        {"pane_id": "%16", "window_index": "9", "title": "Resolve PR 78",
         "label": "work", "tool": "claude", "activity": "idle"}, None)
    assert 'window 9 "Resolve PR 78" (id=%16)' in titled
    # No self-published title ⇒ fall back to the window label.
    untitled = L._pane_block(
        {"pane_id": "%2", "window_index": "2", "title": None, "label": "shell",
         "tool": "shell", "activity": "running"}, None)
    assert 'window 2 "shell" (id=%2)' in untitled


def test_pane_block_idle_age_suffix():
    # Idle panes carry their AGE ("idle for 2d") — the prompt's targeting ladder says a
    # long-idle pane is rarely where a new instruction is destined, so the model needs
    # the number. Non-idle panes and idle panes without a reading stay unsuffixed.
    aged = L._pane_block(
        {"pane_id": "%3", "window_index": "3", "title": "old", "tool": "claude",
         "activity": "idle", "idle_seconds": 2 * 86400}, None)
    assert "— idle for 2d" in aged.splitlines()[0]
    fresh = L._pane_block(
        {"pane_id": "%4", "window_index": "4", "title": "busy", "tool": "claude",
         "activity": "running", "idle_seconds": 999}, None)
    assert "for" not in fresh.splitlines()[0]
    unknown = L._pane_block(
        {"pane_id": "%5", "window_index": "5", "title": "n/a", "tool": "claude",
         "activity": "idle"}, None)
    assert unknown.splitlines()[0].endswith("— idle")
    # unit ladder: coarsest useful unit at each scale
    assert L._fmt_age(40) == "40s" and L._fmt_age(720) == "12m"
    assert L._fmt_age(3 * 3600 + 5) == "3h" and L._fmt_age(86400 * 2 + 30) == "2d"


def test_pane_block_sanitizes_name_quotes_and_newlines():
    # A title with quotes/newlines must not unbalance the heading's quoting or split it.
    b = L._pane_block(
        {"pane_id": "%1", "window_index": "0", "title": 'fix "the" bug\nnow',
         "tool": "claude", "activity": "idle"}, None)
    head = b.splitlines()[0]
    assert head == '## window 0 "fix the bug now" (id=%1) — claude — idle'


def test_system_prompt_has_rules_and_panes():
    p = L._system_prompt(_Watcher())
    assert "type_in_pane" in p  # the tool contract is in the instructions
    assert "# Panes (live state)" in p and 'window 3 "work" (id=%1)' in p


def test_cwd_cannot_introduce_fake_pane_prompt_lines():
    block = L._pane_block({"pane_id": "%1", "cwd": '/repo/"\n## fake pane\r\t'}, None)
    assert block.splitlines()[1] == 'cwd: "/repo/\\"\\n## fake pane\\r\\t"'
    assert len(block.splitlines()) == 2


def test_explicit_pr_target_precedes_conversational_continuity():
    prompt = L._system_prompt(_Watcher())
    window = prompt.index("1. A window the user names")
    pr = prompt.index("2. If the user names a repository and PR number")
    continuity = prompt.index("3. Otherwise the conversation you're already in")
    active = prompt.index("4. Otherwise ACTIVE")
    assert window < pr < continuity < active
    assert "belongs in matching window B, not A" in prompt
    assert "If no association matches, inspect current pane context or ask which window" in prompt


def _dispatch(fc, monkeypatch, watcher=None, meter=_METER, panes=()):
    w = watcher or _Watcher()
    ws, session = _WS(), _Session()
    typed = []
    monkeypatch.setattr(L.tmux, "send_keys", lambda *a, **k: typed.append(a))
    monkeypatch.setattr(L.tmux, "list_panes", lambda: list(panes))  # never the real tmux
    monkeypatch.setattr(L.telemetry, "emit_action", lambda **k: None)
    _run(L._handle_tool_call(ws, session, fc, w, meter))
    return w, ws, session, typed


def test_typing_dispatches_and_logs(monkeypatch):
    fc = _FC(args={"pane_id": "%1", "text": "1", "press_enter": True})
    w, ws, session, typed = _dispatch(fc, monkeypatch)
    assert typed == [("%1", "1", True, True)]  # literal send_keys, with Enter
    assert w.reparsed == ["%1"]  # the keystrokes trigger a fresh parse
    assert any(m["type"] == "typed" and m["label"] == "work" for m in ws.sent)
    call, payload = session.responses[0]
    assert call.id == "call-1"  # the call (with its id) rides back to the provider
    assert payload == {"status": "done", "pane": "work"}
    # the tool result never carries screen content (echo-loop guard)
    assert "screen" not in str(payload)


@pytest.mark.parametrize("text", [False, True])
def test_open_pane_offers_a_button_and_never_touches_the_pane(monkeypatch, text):
    """open_pane only changes the view: no consent card even in a text session, nothing
    typed, and the client is told which pane, named the way the model names it."""
    w, ws, session, typed = _dispatch(
        _FC(name="open_pane", args={"pane_id": "%1"}), monkeypatch,
        meter=L._Meter("s1", "tester", P._DEFAULT[0], text=text))
    assert ws.sent == [
        {"type": "open_pane", "pane_id": "%1", "label": 'window 3 "work"', "auto": False}]
    assert session.responses[0][1] == {  # "done" read as "opened" to the model
        "status": "button_shown", "pane": 'window 3 "work"',
        "reason": "the user taps it to open; nothing is open yet"}
    assert typed == [] and w.reparsed == []


def test_open_pane_finds_a_window_opened_before_the_watcher_saw_it(monkeypatch):
    """resume_session returns a pane id the digest may not hold yet; tmux vouches for it,
    and it is named as the watcher would: the agent's own title over tmux's "claude"."""
    fresh = L.tmux.Pane("work", "7", "claude", "0", "%40", "claude", "auth fix")
    _, ws, _, _ = _dispatch(_FC(name="open_pane", args={"pane_id": "%40"}), monkeypatch,
                            panes=[fresh])
    assert ws.sent == [
        {"type": "open_pane", "pane_id": "%40", "label": 'window 7 "auth fix"', "auto": False}]


@pytest.mark.parametrize(
    "args", [{"pane_id": "%9"}, {"pane_id": ["%1"]}, {"pane_id": "%1", "x": 1}, "oops",
             {"name": "work", "pane_id": "%1"}, {"name": 3}])
def test_open_pane_refuses_an_unknown_pane_or_malformed_call(monkeypatch, args):
    _, ws, session, _ = _dispatch(_FC(name="open_pane", args=args), monkeypatch)
    assert ws.sent == [] and session.responses[0][1]["status"] == "rejected"


class _Fleet(_Watcher):
    """The two field misses, fictionalised: an exact title the model passed over for a
    pane that merely shared a word, and a run-together title lost to the tool column."""

    def digest(self):
        def pane(pid, win, label, title, tool):
            return {"pane_id": pid, "window_index": win, "label": label, "title": title,
                    "tool": tool, "activity": "idle"}
        return [
            pane("%11", "9", "app-0:9", "billing write back | billing-capture-inbox", "node"),
            pane("%12", "3", "❋ acmelinux codex", "acmelinux codex installation", "codex"),
            pane("%13", "10", "misc:10", "release notifications", "omp"),
            pane("%14", "13", "omp-history", "Follow review instructions", "omp"),
            pane("%10", "25", "❋ slack inbox", "✳ slack inbox", "claude"),
            pane("%15", "6", "misc:6", "Café 認証 修正", "claude"),
            pane("%16", "25", "other:25", "auth fix", "codex"),  # same number
            pane("%17", "4", "misc:4", "release 25 notes", "claude"),  # number in title
            *({**pane(p, w, "x", t, "codex"), "cwd": "~/src/sales-kit/"} for p, w, t in (
                ("%19", "19", "pipeline fix"), ("%20", "20", "event | sales-kit"),
                ("%28", "28", "copy tweaks"))),  # one repo: the title alone must not win
        ]


@pytest.mark.parametrize(("name", "pane_id"), [
    ("slack inbox merge", "%10"),      # a stray word: "inbox" alone must not win
    ("acme linux OMP session", "%12"),  # "acme linux" is "acmelinux"; one tool word loses
    ("window 25 slack", "%10"),        # the number filters, the words choose
    ("window 25 auth", "%16"),
    ("café 認証", "%15"),             # names are not only ASCII
    ("cafe\u0301 認証", "%15"),       # nor in one Unicode form
])
def test_open_pane_by_name_opens_the_window_that_clearly_matches(monkeypatch, name, pane_id):
    _, ws, session, _ = _dispatch(_FC(name="open_pane", args={"name": name}), monkeypatch,
                                  watcher=_Fleet())
    assert [m["pane_id"] for m in ws.sent] == [pane_id]
    assert session.responses[0][1]["status"] == "button_shown"


@pytest.mark.parametrize(("name", "status", "candidates"), [
    ("the omp pane", "ambiguous", ["%13", "%14"]),
    ("window 25", "ambiguous", ["%10", "%16"]),  # never %17, whose title says 25
    ("deploy dashboard", "no_match", []),
    ("sales kit open session", "ambiguous", ["%19", "%20", "%28"]),
])
def test_open_pane_by_name_offers_candidates_rather_than_guess(
        monkeypatch, name, status, candidates):
    _, ws, session, _ = _dispatch(_FC(name="open_pane", args={"name": name}), monkeypatch,
                                  watcher=_Fleet())
    result = session.responses[0][1]
    assert result["status"] == status  # and a button for each candidate
    assert [c["pane_id"] for c in result.get("candidates", [])] == candidates
    assert [m["pane_id"] for m in ws.sent] == candidates


@pytest.mark.parametrize("ok", [True, False])
def test_text_session_runs_a_pane_action_only_once_the_user_approves(monkeypatch, ok):
    """In a text session a pane-changing call is proposed, not run: it types only on the
    user's Send, and Cancel answers the model that the user declined. Both are audited."""
    meter = L._Meter("s1", "tester", P._DEFAULT[0], text=True)

    class Tap(_WS):  # the browser: taps Send or Cancel on the card it is shown
        async def send_json(self, obj):
            await super().send_json(obj)
            if obj["type"] == "propose":
                client = _ScriptedWS([{"action": "approve", "id": obj["id"], "ok": ok},
                                      {"action": "stop"}])
                await L._forward_client(client, None, meter)

    typed, audits, bound = [], [], []
    monkeypatch.setattr(L.tmux, "send_keys", lambda *a, **k: (typed.append(a), bound.append(k)))
    monkeypatch.setattr(L.tmux, "pane_pid", lambda pane: "4242")
    monkeypatch.setattr(L.telemetry, "audit", lambda *a, **k: audits.append({**k, "pane": a[1]}))
    ws, session = Tap(), _Session()
    fc = _FC(args={"pane_id": "%1", "text": "rebase onto main"})
    _run(L._handle_tool_call(ws, session, fc, _Watcher(), meter))

    assert ws.sent[0]["type"] == "propose"
    # Named as the list names it, with the id the card's Open button navigates to.
    assert ws.sent[0]["text"] == 'Send to window 3 "work": rebase onto main'
    assert ws.sent[0]["pane_id"] == "%1"
    assert ws.sent[1] == {"type": "decided", "id": ws.sent[0]["id"], "ok": ok}  # then final
    assert typed == ([("%1", "rebase onto main", True, True)] if ok else [])
    assert session.responses[0][1] == (
        {"status": "done", "pane": "work"} if ok
        else {"status": "declined", "reason": "the user declined"})
    assert audits[-1]["consent"] == ("approved" if ok else "declined")
    assert audits[-1]["pane"] == "%1"  # recorded even when declined
    # Bound to the pane incarnation the card showed, so a recycled %1 is refused.
    assert bound == ([{"expected_pid": "4242"}] if ok else [])
    assert meter.approvals == {}


@pytest.mark.parametrize("viewing", [True, False])
def test_card_waiting_unseen_pushes_once(monkeypatch, viewing):
    """A card the user isn't looking at (sheet minimized, page hidden) pushes "Chat needs
    you" once it has waited a moment; one in view stays quiet. Either way the push stops
    with the answer, so a card notifies at most once."""
    meter = L._Meter("s1", "tester", P._DEFAULT[0], text=True)
    meter.unseen_since = None if viewing else time.monotonic()
    pushed = []

    class Push:
        def chat(self, text):
            pushed.append(text)

    meter.push = Push()

    class Answer(_WS):  # taps Cancel well after a push would have gone out
        async def send_json(self, obj):
            await super().send_json(obj)
            if obj["type"] == "propose":
                asyncio.get_running_loop().call_later(
                    0.2, meter.approvals[obj["id"]].set_result, False)  # noqa: FBT003 - a Future result

    monkeypatch.setattr(L, "_NUDGE_TICK", 0.01)
    monkeypatch.setattr(L.push, "SETTLE_SECONDS", 0.03)
    monkeypatch.setattr(L.tmux, "pane_pid", lambda pane: "4242")
    monkeypatch.setattr(L.telemetry, "audit", lambda *a, **k: None)
    _run(L._handle_tool_call(Answer(), _Session(), _FC(args={"pane_id": "%1", "text": "ls"}),
                             _Watcher(), meter))
    assert pushed == ([] if viewing else ['Send to window 3 "work": ls'])


def test_client_reports_whether_the_chat_is_in_view():
    """Unseen time runs from the moment the chat left view, not from a poll tick, and a
    repeated "out of view" keeps that moment; coming back into view clears it."""
    meter = L._Meter("s1", "tester", P._DEFAULT[0], text=True)
    off, stop = {"action": "viewing", "on": False}, {"action": "stop"}
    _run(L._forward_client(_ScriptedWS([off, stop]), None, meter))
    since = meter.unseen_since
    assert since is not None
    _run(L._forward_client(_ScriptedWS([off, stop]), None, meter))
    assert meter.unseen_since == since
    _run(L._forward_client(_ScriptedWS([{"action": "viewing", "on": True}, stop]), None, meter))
    assert meter.unseen_since is None


def _chat(session="chat"):
    return L._Meter(session, "tester", P._DEFAULT[0], text=True)


_UNANSWERED = object()


async def _drop_while_proposed(monkeypatch, session="chat", *, answer=_UNANSWERED,
                               how="cancel"):
    """Propose a type_in_pane, then drop the connection before anything ran: before any
    answer, or with `answer` in hand, `how`: the call cancelled with its "decided" in flight,
    that send raising, or ("race") cancelled before the answer it was just given wakes it.
    Returns the card's id."""
    shown, meter = asyncio.Event(), _chat(session)

    class Shown(_WS):
        async def send_json(self, obj):
            await super().send_json(obj)
            if obj["type"] == "propose" and answer is not _UNANSWERED and how != "race":
                meter.approvals[obj["id"]].set_result(answer)
                return
            shown.set()
            if obj["type"] == "decided":
                if how == "raise":
                    raise ConnectionError
                await asyncio.Event().wait()  # the socket died under it

    monkeypatch.setattr(L.tmux, "pane_pid", lambda pane: "4242")
    ws = Shown()
    call = asyncio.create_task(L._handle_tool_call(
        ws, _Session(), _FC(args={"pane_id": "%1", "text": "rebase"}), _Watcher(), meter))
    await shown.wait()
    if how == "race":
        meter.approvals[ws.sent[0]["id"]].set_result(answer)
    call.cancel()
    await asyncio.gather(call, return_exceptions=True)
    return ws.sent[0]["id"]


@pytest.mark.parametrize("drop", [None, "cancel", "raise"])
@pytest.mark.parametrize("ok", [True, False])
def test_a_card_survives_its_connection_dropping(monkeypatch, ok, drop):
    """A phone drops the socket on every lock: the card stays up, so the same chat
    reconnecting can still Send it, bound to the pane process it showed, or Cancel it,
    even when a tap or its answer was lost in a drop, and over a second drop. It runs
    once: a resent tap is told the answer again."""
    L._parked.clear()
    typed, audits, refreshes = [], [], []
    monkeypatch.setattr(L.tmux, "send_keys", lambda *a, **k: typed.append((a, k)))
    monkeypatch.setattr(L, "_background", lambda task: (refreshes.append(task), task.cancel()))
    monkeypatch.setattr(L.telemetry, "audit", lambda *a, **k: audits.append(k))

    async def go():
        proposal = await _drop_while_proposed(
            monkeypatch, **({"answer": True, "how": drop} if drop else {}))
        assert audits[-1]["consent"] == "parked"

        class Drops(_ScriptedWS):
            async def send_json(self, obj):
                raise ConnectionError  # this socket died too

        with pytest.raises(ConnectionError):
            await L._forward_client(Drops([{"action": "approve", "id": proposal, "ok": ok}]),
                                    _Session(), _chat())
        assert [k[2] for k in L._parked] == [proposal]
        tap = {"action": "approve", "id": proposal, "ok": ok}
        ws = _ScriptedWS([tap, tap, {"action": "stop"}])  # the second: its "decided" was lost
        await L._forward_client(ws, _Session(), _chat())
        return proposal, ws

    proposal, ws = _run(go())
    assert ws.sent[0] == ws.sent[-1] == {"type": "decided", "id": proposal, "ok": ok}
    assert typed == ([(("%1", "rebase", True, True), {"expected_pid": "4242"})] if ok else [])
    assert audits[-1]["consent"] == ("approved" if ok else "declined")
    assert L._parked == {}  # answered once
    assert refreshes == []  # no update tells the new model of an action it never took


@pytest.mark.parametrize("how", ["cancel", "race"])
def test_a_superseded_card_stays_superseded_over_a_drop(monkeypatch, how):
    """A new message answered the card, and its "decided" died with the socket: a later
    Send is told so, and never runs what the user moved on from."""
    L._parked.clear()
    typed = []
    monkeypatch.setattr(L.tmux, "send_keys", lambda *a, **k: typed.append(a))
    monkeypatch.setattr(L.telemetry, "audit", lambda *a, **k: None)

    async def go():
        proposal = await _drop_while_proposed(monkeypatch, answer=None, how=how)
        ws = _ScriptedWS([{"action": "approve", "id": proposal, "ok": True}, {"action": "stop"}])
        await L._forward_client(ws, _Session(), _chat())
        return proposal, ws

    proposal, ws = _run(go())
    assert ws.sent == [{"type": "decided", "id": proposal, "ok": None}]
    assert typed == [] and L._parked == {}


@pytest.mark.parametrize("back", [None, "shown", "hidden", "expired"])
def test_a_card_parked_by_a_drop_still_pushes(monkeypatch, back):
    """Locking the phone drops the socket, the moment a push matters most: the parked card
    counts as out of view and still pushes once, on the wait it began with, even over a
    reconnect that stays hidden. A reconnect that shows it again, or expiry, stops it."""
    L._parked.clear()
    pushed = []
    monkeypatch.setattr(L, "_chats", {})
    monkeypatch.setattr(L, "_NUDGE_TICK", 0.01)
    monkeypatch.setattr(L.push, "SETTLE_SECONDS", 0.2)
    monkeypatch.setattr(L, "PARKED_SECONDS", 0.02 if back == "expired" else 60)
    monkeypatch.setattr(L.tmux, "pane_pid", lambda pane: "4242")
    monkeypatch.setattr(L.telemetry, "audit", lambda *a, **k: None)
    meter = _chat()
    meter.push = SimpleNamespace(chat=pushed.append)  # in view when proposed

    async def go():
        shown = asyncio.Event()

        class Shown(_WS):
            async def send_json(self, obj):
                await super().send_json(obj)
                shown.set()

        call = asyncio.create_task(L._handle_tool_call(
            Shown(), _Session(), _FC(args={"pane_id": "%1", "text": "ls"}), _Watcher(), meter))
        await shown.wait()
        call.cancel()  # the socket dropped: the card is parked
        await asyncio.gather(call, return_exceptions=True)
        await asyncio.sleep(0.15)
        if back in {"shown", "hidden"}:  # the same chat reconnects and reports its view
            again = _chat()
            L._connect(again)
            view = {"action": "viewing", "on": back == "shown"}
            await L._forward_client(_ScriptedWS([view, {"action": "stop"}]), None, again)
        await asyncio.sleep(0.12)  # past the first wait, short of a restarted one

    _run(go())
    assert pushed == ([] if back in {"shown", "expired"} else ['Send to window 3 "work": ls'])
    assert L._parked == {} if back == "expired" else len(L._parked) == 1
    L._parked.clear()


def test_a_reconnected_phone_learns_what_became_of_its_untapped_cards(monkeypatch):
    """Sync settles the cards answered meanwhile (one a new message superseded, its
    "decided" lost in a drop) and the ones the daemon no longer has, and leaves the ones
    still waiting, parked or live, open. Each parked card a new message claims is recorded
    before any is sent, so a socket dying mid-way loses none."""
    L._parked.clear()
    monkeypatch.setattr(L.telemetry, "audit", lambda *a, **k: None)

    class Drops(_ScriptedWS):
        async def send_json(self, obj):
            if obj["type"] == "decided":
                raise ConnectionError  # this socket died on the first answer

    async def go():
        first, second, waiting = [await _drop_while_proposed(monkeypatch) for _ in range(3)]
        with pytest.raises(ConnectionError):
            await L._forward_client(Drops([{"action": "text", "text": "never mind"}]),
                                    _TypedSession(), _chat())
        await _drop_while_proposed(monkeypatch)  # parked after the new message
        meter = _chat()
        meter.approvals["live"] = asyncio.get_running_loop().create_future()
        later = next(k[2] for k in L._parked)
        ws = _ScriptedWS([{"action": "sync", "ids": [first, second, waiting, later, "live",
                                                     "restarted"]}, {"action": "stop"}])
        await L._forward_client(ws, _TypedSession(), meter)
        return ws, first, second, waiting

    ws, first, second, waiting = _run(go())
    superseded = [{"type": "decided", "id": p, "ok": None} for p in (first, second, waiting)]
    assert ws.sent == [*superseded, {"type": "expired", "id": "restarted"}]
    L._parked.clear()


def test_a_parked_card_expires_for_real(monkeypatch):
    """Another chat's card, one parked past PARKED_SECONDS, or one lost to a restart is
    answered "expired"; a new message supersedes the chat's parked cards."""
    L._parked.clear()
    monkeypatch.setattr(L.telemetry, "audit", lambda *a, **k: None)

    async def go():
        other, stale, current = [await _drop_while_proposed(monkeypatch, s)
                                 for s in ("other", "chat", "chat")]
        key = next(k for k in L._parked if k[2] == stale)
        L._parked[key] = (L._parked[key][0] - L.PARKED_SECONDS - 1, *L._parked[key][1:])
        L._park(stale, L._parked.pop(key))  # parking sweeps: a card past its time is not kept
        assert stale not in {k[2] for k in L._parked}
        ws = _ScriptedWS([*({"action": "approve", "id": p, "ok": True}
                            for p in (other, stale, "restarted")),
                          {"action": "text", "text": "never mind"}, {"action": "stop"}])
        await L._forward_client(ws, _TypedSession(), _chat())
        return ws, other, stale, current

    ws, other, stale, current = _run(go())
    assert ws.sent[:4] == [*({"type": "expired", "id": p} for p in (other, stale, "restarted")),
                           {"type": "decided", "id": current, "ok": None}]
    assert [k[1] for k in L._parked] == ["other"]
    L._parked.clear()


def test_approval_is_refused_when_the_pane_had_no_process_to_bind(monkeypatch):
    """A failed pid lookup must not approve an unguarded send: it binds to "", which no
    live pane matches, so send_keys's identity check refuses it."""
    meter = L._Meter("s1", "tester", P._DEFAULT[0], text=True)

    class Approve(_WS):
        async def send_json(self, obj):
            await super().send_json(obj)
            if obj["type"] == "propose":
                meter.approvals[obj["id"]].set_result(True)

    bound = []
    monkeypatch.setattr(L.tmux, "send_keys", lambda *a, **k: bound.append(k))
    monkeypatch.setattr(L.tmux, "pane_pid", lambda pane: None)
    monkeypatch.setattr(L.telemetry, "audit", lambda *a, **k: None)
    _run(L._handle_tool_call(Approve(), _Session(), _FC(args={"pane_id": "%1", "text": "x"}),
                             _Watcher(), meter))
    assert bound == [{"expected_pid": ""}]


def test_proposal_says_when_approving_will_not_press_enter(monkeypatch):
    meter = L._Meter("s1", "tester", P._DEFAULT[0], text=True)

    class Cancel(_WS):
        async def send_json(self, obj):
            await super().send_json(obj)
            if obj["type"] == "propose":
                meter.approvals[obj["id"]].set_result(False)

    monkeypatch.setattr(L.telemetry, "audit", lambda *a, **k: None)
    monkeypatch.setattr(L.tmux, "pane_pid", lambda pane: "4242")
    class Titled(_Watcher):  # an unnamed window, whose list label is a bare tmux address
        def digest(self):
            return [{**super().digest()[0], "title": "Prevent leaks", "label": "misc-1:3"}]

    ws = Cancel()
    fc = _FC(args={"pane_id": "%1", "text": "draft", "press_enter": False})
    _run(L._handle_tool_call(ws, _Session(), fc, Titled(), meter))
    assert ws.sent[0]["text"] == 'Type (no Enter) into window 3 "Prevent leaks" (misc-1:3): draft'


def test_text_session_prompt_labels_relays_as_typed():
    assert "(via voice)" in L._system_prompt(_Watcher())
    typed = L._system_prompt(_Watcher(), text=True)
    assert "(via voice)" not in typed and "(via text)" in typed


def test_press_key_dispatches_named_key(monkeypatch):
    # press_key sends a named key non-literally with no auto-Enter — the way to reach
    # Escape / C-c / arrows that type_in_pane can't.
    fc = _FC(name="press_key", args={"pane_id": "%1", "key": "Escape"})
    w, ws, session, typed = _dispatch(fc, monkeypatch)
    assert typed == [("%1", "Escape", False, False)]  # not literal, no trailing Enter
    assert w.reparsed == ["%1"]
    assert any(m["type"] == "typed" and m["text"] == "[Escape]" for m in ws.sent)
    assert session.responses[0][1] == {"status": "done", "pane": "work"}
    # The model sees readable names; tmux gets its canonical ones.
    fc = _FC(name="press_key", args={"pane_id": "%1", "key": "PageUp"})
    assert _dispatch(fc, monkeypatch)[3] == [("%1", "PPage", False, False)]


def test_press_key_rejects_unknown_key(monkeypatch):
    # Only whitelisted keys — an arbitrary chord must not reach the terminal.
    for key in ("C-x", "F1", "rm -rf", ""):
        fc = _FC(name="press_key", args={"pane_id": "%1", "key": key})
        _, _, session, typed = _dispatch(fc, monkeypatch)
        assert typed == []
        assert session.responses[0][1]["status"] == "rejected"


def test_unknown_pane_is_rejected(monkeypatch):
    fc = _FC(args={"pane_id": "%99", "text": "hi"})
    _, ws, session, typed = _dispatch(fc, monkeypatch)
    assert typed == [] and ws.sent == []
    assert session.responses[0][1]["status"] == "rejected"


def test_echoed_or_malformed_call_is_rejected(monkeypatch):
    # The model sometimes parrots our FunctionResponse back as a new call with extra
    # args — that must never reach a terminal.
    for args in (
        {"pane_id": "%1", "text": "x", "status": "typed"},  # extra arg
        {"pane_id": "%1", "text": {"command": "run"}},
        {"pane_id": "%1", "text": 42},
        {"pane_id": 1, "text": "run"},
        {"pane_id": ["%1"], "text": "run"},
        {"pane_id": "%1", "text": "   "},                   # blank text
        {"pane_id": "%1", "text": "x", "press_enter": "false"},  # non-bool: must not coerce
        {"pane_id": "%1", "text": "x", "press_enter": 1},   # non-bool int
    ):
        _, _, session, typed = _dispatch(_FC(args=args), monkeypatch)
        assert typed == []
        assert session.responses[0][1]["status"] == "rejected"
    # Non-dict args (the model returned a bare string/list) must be rejected, not crash.
    _, _, session, typed = _dispatch(_FC(args="oops"), monkeypatch)
    assert typed == []
    assert session.responses[0][1]["status"] == "rejected"



def test_press_key_requires_string_target_and_key(monkeypatch):
    for args in ({"pane_id": 1, "key": "Enter"},
                 {"pane_id": {"id": "%1"}, "key": "Enter"},
                 {"pane_id": "%1", "key": ["Enter"]}):
        _, _, session, typed = _dispatch(_FC(name="press_key", args=args), monkeypatch)
        assert typed == []
        assert session.responses[0][1]["status"] == "rejected"


def test_context_updater_skips_timeouts(monkeypatch):
    # wait_for_state_change returns the current version even on timeout; only a real
    # version advance may produce an ambient update — no 30s heartbeat (issue #45's
    # lesson applies to Live Mode context too).
    class _W(_Watcher):
        def __init__(self):
            super().__init__()
            self.script = [1, 1, 2]  # timeout, timeout, real change
            self.v = 1

        def state_version(self):
            return self.v

        async def wait_for_state_change(self, since, timeout):
            if not self.script:
                await asyncio.Event().wait()  # park forever; test cancels us
            self.v = self.script.pop(0)
            return self.v

    class _S:
        def __init__(self):
            self.sent = []

        async def send_context(self, text):
            self.sent.append(text)

    monkeypatch.setattr(L, "UPDATE_MIN_SECONDS", 0)

    async def run():
        w, s = _W(), _S()
        task = asyncio.create_task(L._context_updater(s, w))
        await asyncio.sleep(0.05)
        task.cancel()
        return s.sent

    sent = _run(run())
    assert len(sent) == 1 and "[tmux update]" in sent[0]


# --- _run_session teardown / reconnect ---------------------------------------
#
# The regression these guard: on a clean stop, _run_session's finally cancels the
# receiver + context-updater side tasks, and that cancellation must be ABSORBED —
# pre-fix the CancelledError escaped contextlib.suppress(Exception) (it's a
# BaseException in 3.12) and surfaced as a bare "Exception in ASGI application".


class _Connect:
    """Fake provider-session context manager. `boom` (if set) is raised
    on __aenter__ to simulate a connect that fails before the session is up."""

    def __init__(self, session, boom=None):
        self._session, self._boom = session, boom

    async def __aenter__(self):
        if self._boom is not None:
            raise self._boom
        return self._session

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    """Stands in for live_providers.connect and counts connect attempts. `connects` is
    one _Connect (or callable returning one) per expected attempt."""

    def __init__(self, connects):
        self._connects = list(connects)
        self.attempts = 0

    def connect(self, model, system_prompt):
        self.attempts += 1
        cm = self._connects.pop(0)
        return cm() if callable(cm) else cm


class _ScriptedWS(_WS):
    """A _WS whose receive_json replays a script: a dict is returned, an Exception is
    raised (to drive WebSocketDisconnect / EOF paths)."""

    def __init__(self, script):
        super().__init__()
        self.script = list(script)

    async def receive_json(self):
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if item is _SILENT:
            await asyncio.Event().wait()  # a live browser saying nothing (e.g. during a backoff)
        return item


_SILENT = object()


async def _park(*args, **kwargs):
    # Stand in for the receiver/context-updater: run until cancelled, so the finally's
    # cancel() actually produces a CancelledError for the drain to absorb.
    await asyncio.Event().wait()


def _statuses(ws):
    return [m["status"] for m in ws.sent if m.get("type") == "status"]


def test_run_session_clean_stop_absorbs_cancellation(monkeypatch):
    # THE regression: a client "stop" ends the session without _run_session raising —
    # the side-task CancelledError is drained, not leaked.
    monkeypatch.setattr(L, "_receiver", _park)
    monkeypatch.setattr(L, "_context_updater", _park)
    client = _FakeClient([_Connect(_Session())])
    monkeypatch.setattr(L.live_providers, "connect", client.connect)

    ws = _ScriptedWS([{"action": "stop"}])
    # must not raise
    _run(L._run_session(ws, _Watcher(), L._Meter("s", "a", P._DEFAULT[0])))

    assert client.attempts == 1  # clean stop → no reconnect
    assert _statuses(ws)[-1:] == ["listening"] or "listening" in _statuses(ws)


def test_text_session_sends_typed_turns_as_user_turns(monkeypatch):
    """A typed turn reaches the provider through the
    user-turn verb (not the reply-less context path), echoed to the browser as the user's
    transcript and kept in the meter's. A blank one goes nowhere; an oversized one is
    refused rather than cut."""
    class Typed(_Session):
        def __init__(self):
            super().__init__()
            self.texts = []

        async def send_text(self, text):
            self.texts.append(text)

        async def send_context(self, text):
            raise AssertionError("a typed turn is not ambient context")

    monkeypatch.setattr(L, "_receiver", _park)
    monkeypatch.setattr(L, "_context_updater", _park)
    session = Typed()
    client = _FakeClient([_Connect(session)])
    monkeypatch.setattr(L.live_providers, "connect", client.connect)
    ws = _ScriptedWS([{"action": "text", "text": "  find my codex session "},
                      {"action": "text", "text": " "},
                      {"action": "text", "text": "x" * (L.TYPED_TURN_CHARS + 1)},
                      {"action": "stop"}])
    meter = L._Meter("s", "a", P._DEFAULT[0], text=True)
    _run(L._run_session(ws, _Watcher(), meter))

    assert session.texts == ["find my codex session"]
    assert {"type": "transcript", "role": "user", "text": "find my codex session",
            "new_segment": True, "images": 0} in ws.sent
    assert {"type": "error", "message": "Too long; not sent", "refused": True} in ws.sent
    assert "user: find my codex session" in meter._transcript()


def test_run_session_reconnects_once_after_a_drop(monkeypatch):
    # A first connect that errors before the session is up reconnects exactly once,
    # then the second connect runs to a clean stop.
    monkeypatch.setattr(L, "_receiver", _park)
    monkeypatch.setattr(L, "_context_updater", _park)
    hold = L._hold
    monkeypatch.setattr(L, "_hold", lambda ws, seconds: hold(ws, 0.01))  # collapse the backoff
    client = _FakeClient([
        _Connect(_Session(), boom=RuntimeError("gemini drop")),
        _Connect(_Session()),
    ])
    monkeypatch.setattr(L.live_providers, "connect", client.connect)

    ws = _ScriptedWS([_SILENT, {"action": "stop"}])  # silent through the backoff, then stop
    _run(L._run_session(ws, _Watcher(), L._Meter("s", "a", P._DEFAULT[0])))

    assert client.attempts == 2  # exactly one reconnect
    assert _statuses(ws).count("reconnecting") == 1


def test_run_session_reconnects_when_the_provider_stream_ends(monkeypatch):
    """A provider that drops surfaces as the RECEIVER ending, and a phone that is muted or
    backgrounded sends no audio frame for the mic pump to trip over. Awaiting the pump
    alone left the session parked in "listening" against a dead socket, with the reconnect
    loop one frame away and never entered — so the runner races the two, and a side task
    that simply ends is treated as the end of the connection it was reading."""
    seen = []

    async def receiver(*args, **kwargs):
        seen.append(1)
        if len(seen) == 1:
            return  # first connection: the provider's event stream just stopped
        await asyncio.Event().wait()  # second: a healthy session, until the client stops

    monkeypatch.setattr(L, "_receiver", receiver)
    monkeypatch.setattr(L, "_context_updater", _park)
    hold = L._hold
    monkeypatch.setattr(L, "_hold", lambda ws, seconds: hold(ws, 0.01))  # collapse the backoff
    client = _FakeClient([_Connect(_Session()), _Connect(_Session())])
    monkeypatch.setattr(L.live_providers, "connect", client.connect)

    # A browser that says nothing at all until it finally stops: the EOF has to be noticed
    # without its help. wait_for so a runner that goes back to awaiting only the pump fails
    # here instead of hanging the suite.
    ws = _ScriptedWS([_SILENT, _SILENT, {"action": "stop"}])
    _run(asyncio.wait_for(
        L._run_session(ws, _Watcher(), L._Meter("s", "a", P._DEFAULT[0])), 5
    ))

    assert client.attempts == 2  # the dead connection was noticed and replaced
    assert _statuses(ws).count("reconnecting") == 1


def test_run_session_websocket_disconnect_does_not_reconnect(monkeypatch):
    # A gone client (WebSocketDisconnect from receive_json) propagates out — there is
    # nothing to reconnect to, so no second connect attempt is made.
    monkeypatch.setattr(L, "_receiver", _park)
    monkeypatch.setattr(L, "_context_updater", _park)
    client = _FakeClient([_Connect(_Session())])
    monkeypatch.setattr(L.live_providers, "connect", client.connect)

    ws = _ScriptedWS([L.WebSocketDisconnect(code=1006)])
    try:
        _run(L._run_session(ws, _Watcher(), L._Meter("s", "a", P._DEFAULT[0])))
        raised = False
    except L.WebSocketDisconnect:
        raised = True

    assert raised  # the disconnect surfaces, not swallowed as a reconnectable drop
    assert client.attempts == 1


class _Detail:
    def __init__(self, modality, n):
        self.modality, self.token_count = modality, n


class _Usage:
    """Mimics Gemini Live usage_metadata: cumulative session totals, with per-modality
    breakdowns splitting audio from text."""

    def __init__(self, prompt, resp, audio_in=0, audio_out=0, cached=0, audio_cached=0):
        from google.genai import types
        self.prompt_token_count = prompt
        self.response_token_count = resp
        self.prompt_tokens_details = [_Detail(types.Modality.AUDIO, audio_in)] if audio_in else []
        self.response_tokens_details = (
            [_Detail(types.Modality.AUDIO, audio_out)] if audio_out else []
        )
        self.cached_content_token_count = cached
        self.cache_tokens_details = (
            [_Detail(types.Modality.AUDIO, audio_cached)] if audio_cached else []
        )


def test_live_usage_splits_modalities_and_costs():
    u = L._LiveUsage(P._RATES_25)
    u.set(P.gemini_usage(_Usage(prompt=1000, resp=500, audio_in=800, audio_out=400)))
    # prompt 1000 = 800 audio + 200 text; response 500 = 400 audio + 100 text.
    assert (u.audio_in, u.split.text_in) == (800, 200)
    assert (u.split.audio_out, u.split.text_out) == (400, 100)
    assert u.in_tokens == 1000 and u.out_tokens == 500 and u.cached == 0
    r = P._RATES_25
    expected = (
        200 / 1e6 * r.text_in + 100 / 1e6 * r.text_out
        + 800 / 1e6 * r.audio_in + 400 / 1e6 * r.audio_out
    )
    assert abs(u.cost() - expected) < 1e-12


def test_live_usage_prices_cached_input_at_the_cached_rate():
    # OpenAI-shaped card: cached input ~30× cheaper. prompt 1000 = 800 audio (300 of it
    # cached) + 200 text (100 cached): the cached share leaves the uncached buckets.
    rates = P.Split(0.6, 2.4, 10.0, 20.0, 0.06, 0.3)
    u = L._LiveUsage(rates)
    u.set(P.gemini_usage(_Usage(prompt=1000, resp=0, audio_in=800, cached=400, audio_cached=300)))
    assert u.split == (100, 0, 500, 0, 100, 300)
    assert u.in_tokens == 1000 and u.audio_in == 800 and u.cached == 400
    assert abs(u.cost() - (100 * 0.6 + 500 * 10 + 100 * 0.06 + 300 * 0.3) / 1e6) < 1e-12
    # Gemini's default card has no cached discount, so the same tokens cost the same as
    # uncached — the split is informational there, never a silent undercount.
    g = L._LiveUsage(P._RATES_25)
    g.set(u.split)
    assert abs(g.cost() - (200 * 0.5 + 800 * 3) / 1e6) < 1e-12


def test_live_usage_is_cumulative_not_summed():
    # Live sends running totals per message — later messages overwrite, never add.
    u = L._LiveUsage(P._RATES_25)
    u.set(P.gemini_usage(_Usage(prompt=100, resp=50)))
    u.set(P.gemini_usage(_Usage(prompt=300, resp=120)))
    assert u.in_tokens == 300 and u.out_tokens == 120


def test_meter_emits_per_turn_and_folds_into_totals(monkeypatch):
    from openbus import llm
    emitted = []
    monkeypatch.setattr(L.telemetry, "emit_live_turn", lambda **k: emitted.append(k))
    folded = {}
    monkeypatch.setattr(llm, "record_live_usage", lambda **k: folded.update(k))
    m = L._Meter("sess-abc", "user@example.com", P._DEFAULT[0])
    m.usage.set(P.gemini_usage(_Usage(prompt=200, resp=80, audio_in=150, audio_out=60)))
    m.note("user: what's running")
    m.end_turn()
    m.finish()
    assert [e["final"] for e in emitted] == [False, True]  # one per-turn, one final
    assert (emitted[0]["turns"], emitted[0]["session"], emitted[0]["provider"]) == (
        1, "sess-abc", "vertex",
    )
    assert emitted[-1]["cost"] == m.usage.cost() and emitted[-1]["cached_tokens"] == 0
    # Session cost is folded into the status-bar totals exactly once, at finish().
    assert folded == {"in_tokens": 200, "out_tokens": 80, "cost": m.usage.cost()}


def test_connect_snapshot_screen_budget():
    """A big fleet must not blow the session's setup limit: the connect snapshot's
    screen text is bounded fleet-wide (the real 24-pane deck hit ~36k tokens and
    Gemini 1007-closed every session at first audio). Digests always survive."""
    w = _Watcher()
    # ~4.7k raw chars — deliberately OVER SCREEN_TAIL_CHARS, so _screen_tail's own cap
    # is exercised too: each pane contributes exactly one capped tail to the budget.
    big = ("x" * 79 + "\n") * (L.SCREEN_TAIL_LINES - 1)
    w.snapshots = {f"%{i}": [{"id": "s", "text": big, "ts": 1.0}] for i in range(30)}

    def digest():
        return [{"pane_id": f"%{i}", "label": f"p{i}", "window_index": str(i),
                 "tool": "claude", "activity": "idle" if i else "running",
                 "tmux_active": i == 29,  # active pane sorted LAST in digest order
                 "idle_seconds": i * 100,
                 "headline": f"headline-{i}", "summary": None, "question": None,
                 "history": []} for i in range(30)]
    w.digest = digest

    ctx = L._pane_context(w, screens="all")
    # Every pane keeps its digest block, budget or not.
    for i in range(30):
        assert f"headline-{i}" in ctx
    # Total screen payload is bounded: budget, plus at most one tail of overshoot,
    # plus a marker token per tail — tail_marked() may prefix ⟪dim⟫/⟪placeholder⟫ to
    # reopen a marked run, so a tail can exceed SCREEN_TAIL_CHARS by a token's width.
    screens = ctx.count("screen:\n")
    assert screens < 30, "budget did not drop any screens"
    MARKER_SLACK = 32 * 30  # generous per-pane allowance for reopen tokens
    assert len(ctx) <= L.SCREEN_BUDGET_CHARS + L.SCREEN_TAIL_CHARS + 30 * 400 + MARKER_SLACK
    # Priority: the ACTIVE pane always keeps its screen, however it sorts in digest
    # order; the longest-idle pane is the first to lose its own.
    active_block = ctx.split("## window 29")[1]
    assert "screen:" in active_block.split("##")[0]
    idlest_block = ctx.split("## window 28")[1]  # idle_seconds=2800, the stalest
    assert "screen:" not in idlest_block.split("##")[0]


@pytest.mark.parametrize("name", ["rm_rf", ["type_in_pane"]])
def test_unknown_tool_is_rejected_as_such(monkeypatch, name):
    fc = _FC(name=name, args={"pane_id": "%1", "text": "x"})
    _, _, session, typed = _dispatch(fc, monkeypatch)
    assert typed == []
    assert session.responses[0][1] == {"status": "rejected", "reason": "unknown tool"}


def test_audit_line_cannot_be_forged(caplog):
    caplog.set_level(logging.INFO, logger="openbus.server.audit")
    forged = "\nAUDIT kill_window pane=%2"
    L.telemetry.audit("x", "%1" + forged, "me" + forged, "w" + forged, outcome="error" + forged)
    assert len(caplog.text.splitlines()) == 1


class _TypedSession:
    """Records what reaches a session's typed-turn verb."""

    def __init__(self, images=True):
        self.turns, self.images = [], images

    async def send_text(self, text, images=()):
        self.turns.append((text, list(images)))


def test_a_pasted_image_reaches_the_session_with_its_turn():
    """Base64 on the socket, bytes at the session; the echo counts the images so the client
    can put its thumbnails on it; an image-only turn is still a turn."""
    session, ws = _TypedSession(), _ScriptedWS([
        {"action": "text", "text": " look ", "images": [{"mime": "image/png", "data": "UE5H"}]},
        {"action": "text", "images": [{"mime": "image/jpeg", "data": "SlBH"}]},
        {"action": "text", "text": "plain"}, {"action": "stop"}])
    _run(L._forward_client(ws, session, L._Meter("s", "a", P._DEFAULT[0], text=True)))
    # Each image is numbered in the turn text, so the model can name it in a tool call.
    assert session.turns == [
        ("look\n(image 1 attached)", [("image/png", b"PNG")]),
        ("(image 2 attached)", [("image/jpeg", b"JPG")]), ("plain", [])]
    assert [(f["text"], f["images"]) for f in ws.sent] == [("look", 1), ("", 1), ("plain", 0)]


_UNREADABLE = "Could not read that image; not sent"
_TYPE = "Images go as PNG, JPEG, WebP or GIF; not sent"


@pytest.mark.parametrize(("image", "accepts", "reason"), [
    ({"mime": "image/svg+xml", "data": "UE5H"}, True, _TYPE),  # not a pane-paste type
    ({"mime": "image/png", "data": "not base64!"}, True, _UNREADABLE),
    ({"mime": "image/png", "data": ""}, True, _TYPE),
    ({"mime": ["image/png"], "data": "UE5H"}, True, _TYPE),  # not even a string
    ([{"mime": "image/png", "data": "UE5H"}] * (L.CHAT_IMAGES + 1), True,
     f"At most {L.CHAT_IMAGES} images a turn; not sent"),
    ({}, True, _UNREADABLE),  # not a list at all
    ([{"mime": "image/png", "data": "A" * (L.CHAT_IMAGE_BYTES // 3 * 4 + 4)}], True,
     "Images are over 8 MB together; not sent"),
    # The cause users actually hit: a small screenshot pasted to a voice model that can't
    # see it must say so, not read as a size or type limit.
    ({"mime": "image/png", "data": "UE5H"}, False,
     "This voice model can't take images; switch to Chat to send them"),
])
def test_an_image_the_turn_cannot_carry_refuses_it_and_says_why(image, accepts, reason):
    session = _TypedSession(images=accepts)
    images = image if isinstance(image, list) or image == {} else [image]
    ws = _ScriptedWS([{"action": "text", "text": "look", "images": images}, {"action": "stop"}])
    _run(L._forward_client(ws, session, L._Meter("s", "a", P._DEFAULT[0])))
    assert session.turns == []
    assert ws.sent == [{"type": "error", "message": reason, "refused": True}]


def _forwarding(monkeypatch, *, ok, text=True, images=((("image/png", b"PNG")),)):
    """A chat meter holding `images`, a browser tapping `ok` on the card, and the pane
    delivery faked at server.attach_image."""
    from openbus import server

    meter = L._Meter("s1", "tester", P._DEFAULT[0], text=text)
    meter.keep_images(list(images))
    events, audits = [], []

    class Tap(_WS):
        async def send_json(self, obj):
            await super().send_json(obj)
            if obj["type"] == "propose":
                meter.approvals[obj["id"]].set_result(ok)

    async def attach(pane_id, pid, data, mime, caption):
        events.append(("attach", pane_id, pid, data, mime, caption))
        return "/tmp/x.png", "path"

    monkeypatch.setattr(server, "attach_image", attach)
    monkeypatch.setattr(L.tmux, "send_keys", lambda *a, **k: events.append(("keys", a, k)))
    monkeypatch.setattr(L.tmux, "pane_pid", lambda pane: "4242")
    monkeypatch.setattr(L.telemetry, "audit", lambda *a, **k: audits.append(k))
    return meter, Tap(), events, audits


def _forward(meter, ws, **args):
    session = _Session()
    fc = _FC(name="send_image_to_pane", args={"pane_id": "%1", **args})
    _run(L._handle_tool_call(ws, session, fc, _Watcher(), meter))
    return session.responses[0][1]


def test_forwarding_an_image_waits_for_send_and_binds_to_the_pane(monkeypatch):
    meter, ws, events, audits = _forwarding(monkeypatch, ok=True)
    assert _forward(meter, ws, image_number=1, caption="what is this?") == {
        "status": "done", "pane": "work"}
    card = ws.sent[0]
    assert card["text"] == 'Send image 1 to window 3 "work": what is this?'
    assert card["image"] == "data:image/png;base64,UE5H"  # the thumbnail the user approves
    assert events == [  # delivered once, bound to the pid the card showed, caption in the draft
        ("attach", "%1", "4242", b"PNG", "image/png", "what is this?")]
    assert len(audits) == 1 and audits[0]["consent"] == "approved"
    assert audits[0]["detail"] == "image/png 3B into work via path"
    assert b"PNG" not in repr(audits).encode()  # type and size, never the bytes


def test_cancelling_an_image_card_sends_nothing(monkeypatch):
    meter, ws, events, audits = _forwarding(monkeypatch, ok=False)
    assert _forward(meter, ws) == {"status": "declined", "reason": "the user declined"}
    assert events == [] and audits[0]["consent"] == "declined"


@pytest.mark.parametrize("args", [{"image_number": 2}, {"image_number": True}, {"caption": 5}])
def test_forwarding_refuses_a_missing_image_or_bad_args(monkeypatch, args):
    meter, ws, events, _ = _forwarding(monkeypatch, ok=True)
    assert _forward(meter, ws, **args)["status"] == "rejected"
    assert events == []


def test_forwarding_refuses_when_no_image_was_attached(monkeypatch):
    meter, ws, events, _ = _forwarding(monkeypatch, ok=True, images=())
    assert _forward(meter, ws)["status"] == "rejected"
    assert events == []


def test_forwarding_works_from_a_voice_session_too(monkeypatch):
    """A voice model that takes images can hand one on, without a card like its other
    actions, and audited like them."""
    meter, ws, events, audits = _forwarding(monkeypatch, ok=True, text=False)
    assert _forward(meter, ws)["status"] == "done"
    assert len(events) == 1 and len(audits) == 1 and "consent" not in audits[0]


def test_images_stay_numbered_until_no_request_can_show_their_turn():
    meter = L._Meter("s1", "tester", P._DEFAULT[0], text=True)
    meter.keep_images([("image/png", b"a"), ("image/png", b"b")])
    meter.keep_images([("image/jpeg", b"c")])
    assert meter.image(2) == (2, "image/png", b"b")
    assert meter.image(None) == (3, "image/jpeg", b"c")  # the latest
    for _ in range(L.TURNS_KEPT + L.TURNS_QUEUED - 1):
        meter.keep_images([])  # still shown: the queued turns are not in the history yet
    assert meter.image(1) == (1, "image/png", b"a")
    meter.keep_images([])  # now past anything the chat model can still show
    assert meter.image(1) is None


def test_an_omitted_image_number_is_pinned_to_the_image_on_the_card(monkeypatch):
    """A turn pasted while the card waits must not change which image Send delivers."""
    meter, ws, events, _ = _forwarding(monkeypatch, ok=True)
    send = ws.send_json

    async def paste_meanwhile(obj):
        if obj["type"] == "propose":
            meter.keep_images([("image/jpeg", b"NEW")])
        await send(obj)

    ws.send_json = paste_meanwhile
    _forward(meter, ws)
    assert events[0][3:5] == (b"PNG", "image/png")


def test_typing_at_a_password_prompt_says_why_it_was_refused(monkeypatch):
    """send_keys refuses model text at a password prompt; the model is told why, so it can
    send the user to the app's password field rather than retry. The refused text is
    probably the password, so its audit record never carries it, even under QSDEBUG."""
    def refuse(*a, **k):
        raise L.tmux.PasswordPromptError(L.tmux.AT_PASSWORD)

    audits = []
    monkeypatch.setattr(L.tmux, "send_keys", refuse)
    monkeypatch.setattr(L.telemetry, "emit_action", lambda **k: None)
    monkeypatch.setattr(L.telemetry, "QSDEBUG", True)
    monkeypatch.setattr(L.telemetry, "audit", lambda *a, **k: audits.append((a, k)))
    fc = _FC(args={"pane_id": "%2", "text": "hunter2"})
    session = _Session()
    _run(L._handle_tool_call(_WS(), session, fc, _Watcher(), _METER))
    assert "password field" in str(session.responses)
    assert audits and "hunter2" not in str(audits)
