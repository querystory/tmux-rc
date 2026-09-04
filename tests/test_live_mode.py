"""Live Mode: prompt-context assembly and the type_in_pane dispatch guardrails.

The session/transport itself is exercised live (it's a bidi stream to Vertex); what
must be pinned in tests is everything that decides WHAT the model sees and WHETHER a
tool call may touch a real terminal — the context builder and the reject paths."""

import asyncio

import daemon.live as L
import daemon.live_providers as P


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


def test_pane_context_carries_state_and_screens():
    w = _Watcher()
    ctx = L._pane_context(w, screens="all")
    # User-facing identity (window number + name) leads; the %id is the tool-call handle.
    # Name falls back to label here (stub has no self-published title).
    assert 'window 3 "work" (id=%1) — claude — waiting' in ctx
    assert "ACTIVE" in ctx  # the focused pane is flagged for "here"/"this" resolution
    assert "PENDING QUESTION: Run them? 1) yes 2) no" in ctx
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


def _dispatch(fc, monkeypatch, watcher=None):
    w = watcher or _Watcher()
    ws, session = _WS(), _Session()
    typed = []
    monkeypatch.setattr(L.tmux, "send_keys", lambda *a: typed.append(a))
    monkeypatch.setattr(L.telemetry, "emit_action", lambda **k: None)
    _run(L._handle_tool_call(ws, session, fc, w, "tester"))
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


def test_press_key_dispatches_named_key(monkeypatch):
    # press_key sends a named key non-literally with no auto-Enter — the way to reach
    # Escape / C-c / arrows that type_in_pane can't.
    fc = _FC(name="press_key", args={"pane_id": "%1", "key": "Escape"})
    w, ws, session, typed = _dispatch(fc, monkeypatch)
    assert typed == [("%1", "Escape", False, False)]  # not literal, no trailing Enter
    assert w.reparsed == ["%1"]
    assert any(m["type"] == "typed" and m["text"] == "[Escape]" for m in ws.sent)
    assert session.responses[0][1] == {"status": "done", "pane": "work"}


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

    def connect(self, system_prompt):
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
        return item


async def _park(*args, **kwargs):
    # Stand in for the receiver/context-updater: run until cancelled, so the finally's
    # cancel() actually produces a CancelledError for the drain to absorb.
    await asyncio.Event().wait()


async def _instant_sleep(*args, **kwargs):
    # Collapse the reconnect backoff so the test doesn't actually wait seconds.
    return None


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
    _run(L._run_session(ws, _Watcher(), "tester", L._Meter("s", "a")))  # must not raise

    assert client.attempts == 1  # clean stop → no reconnect
    assert _statuses(ws)[-1:] == ["listening"] or "listening" in _statuses(ws)


def test_run_session_reconnects_once_after_a_drop(monkeypatch):
    # A first connect that errors before the session is up reconnects exactly once,
    # then the second connect runs to a clean stop.
    monkeypatch.setattr(L, "_receiver", _park)
    monkeypatch.setattr(L, "_context_updater", _park)
    monkeypatch.setattr(L.asyncio, "sleep", _instant_sleep)  # collapse reconnect backoff
    client = _FakeClient([
        _Connect(_Session(), boom=RuntimeError("gemini drop")),
        _Connect(_Session()),
    ])
    monkeypatch.setattr(L.live_providers, "connect", client.connect)

    ws = _ScriptedWS([{"action": "stop"}])
    _run(L._run_session(ws, _Watcher(), "tester", L._Meter("s", "a")))

    assert client.attempts == 2  # exactly one reconnect
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
        _run(L._run_session(ws, _Watcher(), "tester", L._Meter("s", "a")))
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
    def __init__(self, prompt, resp, audio_in=0, audio_out=0):
        from google.genai import types
        self.prompt_token_count = prompt
        self.response_token_count = resp
        self.prompt_tokens_details = [_Detail(types.Modality.AUDIO, audio_in)] if audio_in else []
        self.response_tokens_details = [_Detail(types.Modality.AUDIO, audio_out)] if audio_out else []


def test_live_usage_splits_modalities_and_costs():
    u = L._LiveUsage()
    u.set(P.gemini_usage(_Usage(prompt=1000, resp=500, audio_in=800, audio_out=400)))
    # prompt 1000 = 800 audio + 200 text; response 500 = 400 audio + 100 text.
    assert (u.audio_in, u.text_in) == (800, 200)
    assert (u.audio_out, u.text_out) == (400, 100)
    assert u.in_tokens == 1000 and u.out_tokens == 500
    expected = (200 / 1e6 * L._LIVE_TEXT_IN_PER_M + 100 / 1e6 * L._LIVE_TEXT_OUT_PER_M
                + 800 / 1e6 * L._LIVE_AUDIO_IN_PER_M + 400 / 1e6 * L._LIVE_AUDIO_OUT_PER_M)
    assert abs(u.cost() - expected) < 1e-12


def test_live_usage_is_cumulative_not_summed():
    # Live sends running totals per message — later messages overwrite, never add.
    u = L._LiveUsage()
    u.set(P.gemini_usage(_Usage(prompt=100, resp=50)))
    u.set(P.gemini_usage(_Usage(prompt=300, resp=120)))
    assert u.in_tokens == 300 and u.out_tokens == 120


def test_meter_emits_per_turn_and_folds_into_totals(monkeypatch):
    import daemon.llm as llm
    emitted = []
    monkeypatch.setattr(L.telemetry, "emit_live_turn", lambda **k: emitted.append(k))
    folded = {}
    monkeypatch.setattr(llm, "record_live_usage", lambda **k: folded.update(k))
    m = L._Meter("sess-abc", "user@example.com")
    m.usage.set(P.gemini_usage(_Usage(prompt=200, resp=80, audio_in=150, audio_out=60)))
    m.note("user: what's running")
    m.end_turn()
    m.finish()
    assert [e["final"] for e in emitted] == [False, True]  # one per-turn, one final
    assert emitted[0]["turns"] == 1 and emitted[0]["session"] == "sess-abc"
    assert emitted[-1]["cost"] == m.usage.cost()
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
