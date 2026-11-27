"""The Live/Responses boundary must not duplicate terminal actions or lose billing."""

import asyncio
import base64
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest
from fastapi import WebSocketDisconnect

from openbus import gpt_live as G
from openbus import live as L
from openbus import live_record


class Watcher:
    def __init__(self):
        self.snapshots = {"%1": [{"text": "$ "}]}

    def digest(self):
        return [
            {
                "pane_id": "%1",
                "window_index": "1",
                "label": "shell",
                "tool": "shell",
                "activity": "idle",
                "tmux_active": True,
            }
        ]

    def request_reparse(self, pane):
        pass

    def state_version(self):
        return 1

    async def wait_for_state_change(self, *a, **kw):
        await asyncio.Event().wait()


class Wire:
    def __init__(self, events=()):
        self.events = list(events)
        self.sent = []
        self.continued = asyncio.Event()

    async def send(self, data):
        event = json.loads(data)
        self.sent.append(event)
        if event["type"] == "response.create":
            self.continued.set()

    async def __aiter__(self):
        for event in self.events:
            yield json.dumps(event)


class Browser:
    def __init__(self):
        self.messages = []

    async def send_json(self, message):
        self.messages.append(message)


def session(events=(), *, closed=True):
    meter = L._Meter("test", "test", G.ENTRY)
    meter.usage = G.Usage(G.BACKEND)
    events = [*events, {"type": "session.closed"}] if closed else events
    return G.Session(Wire(events), Browser(), Watcher(), meter)


async def drain(s):
    """Read a session to its end: the provider's own session.closed, which nothing asked for."""
    with pytest.raises(G.ProviderError, match="session_closed_by_provider"):
        await s.receive()


def response(kind, **fields):
    return {
        "type": "response.event",
        "delegation_id": "delegation-1",
        "event": {"type": kind, **fields},
    }


def call(cid="call-1", args=None):
    return {
        "type": "function_call",
        "call_id": cid,
        "name": "type_in_pane",
        "arguments": json.dumps(args or {"pane_id": "%1", "text": "echo hello"}),
    }


def test_shared_audio_forwarder_reaches_the_adapter():
    """The adapter borrows live._forward_client wholesale rather than growing a second mic
    path, so it has to answer to the seam's verb BY NAME: a method spelled anything else
    is an AttributeError on the first spoken frame, and no test of the adapter's own
    methods would ever reach it. Odd-length frames are dropped rather than forwarded —
    half a PCM16 sample shifts every sample after it."""
    script = [
        {"action": "audio", "data": base64.b64encode(b"\x01\x02\x03\x04").decode()},
        {"action": "audio", "data": base64.b64encode(b"\x01").decode()},  # odd: dropped
        {"action": "stop"},
    ]

    class Mic(Browser):
        async def receive_json(self):
            return script.pop(0)

    s = session()
    asyncio.run(L._forward_client(Mic(), s, s.meter))
    assert [e["type"] for e in s.ws.sent] == ["session.input_audio.append"]
    assert base64.b64decode(s.ws.sent[0]["audio"]) == b"\x01\x02\x03\x04"


def test_typed_turn_goes_to_the_backend():
    """GPT-Live's frontend takes no text, so a typed turn is a backend user item and a
    continue; the frontend then voices the result like any other."""
    s = session()
    asyncio.run(s.send_text("find it"))
    assert [e["type"] for e in s.ws.sent] == ["response.item.create", "response.create"]
    assert s.ws.sent[0]["item"]["content"] == [{"type": "input_text", "text": "find it"}]


def test_a_pasted_image_goes_to_the_backend_with_its_turn():
    """The live-delegation guide routes images to the Responses backend, which is where
    typed turns already go: one user item, image first, then the continue."""
    s = session()
    assert s.images
    asyncio.run(s.send_text("read it", [("image/png", b"PNG")]))
    assert [e["type"] for e in s.ws.sent] == ["response.item.create", "response.create"]
    assert s.ws.sent[0]["item"]["content"] == [
        {"type": "input_image", "image_url": "data:image/png;base64,UE5H"},
        {"type": "input_text", "text": "read it"}]


def test_tools_come_from_the_shared_table_unconverted():
    """One table, every provider. live_providers.TOOLS is already plain JSON Schema — the
    seam chose that format precisely because no backend needs it translated — so the
    adapter only wraps each entry, and the google-genai enum walk this used to do is gone
    rather than rewritten."""
    defs = G.tool_definitions()
    shared = L.live_providers.tools()
    assert [d["name"] for d in defs] == [t["name"] for t in shared]
    for d, t in zip(defs, shared, strict=True):
        assert d["type"] == "function" and d["parameters"] == t["parameters"]


def test_the_paid_smoke_script_still_builds_a_meter():
    """research/live-eval/smoke_gpt_live.py is billable and opt-in, so nothing in CI runs it
    — which is exactly why a constructor change can rot it unseen. Compile it and check the
    one call the seam changed, rather than discovering the TypeError with a live session and
    a bill attached."""
    path = Path(__file__).resolve().parent.parent / "research" / "live-eval" / "smoke_gpt_live.py"
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")
    assert 'live._Meter("gpt-live-smoke", "smoke", gpt_live.ENTRY)' in source


def test_usage_duration_snapshots_backend_cache_and_duplicate_completion():
    usage = G.Usage(G.BACKEND)
    for seconds in (12, 24, 24):
        usage.update({"type": "session.usage.updated", "usage": {"seconds": seconds}})
    event = response(
        "response.completed",
        response={
            "id": "r1",
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 100,
                "input_tokens_details": {"cached_tokens": 800},
            },
        },
    )
    usage.update(event)
    usage.update(event)
    assert usage.in_tokens == 1000 and usage.out_tokens == 100
    assert usage.cost() == pytest.approx(
        0.02 + (200 * 0.2 + 800 * 0.02 + 100 * 1.2) / 1e6
    )
    assert not usage.final
    usage.update({"type": "session.closed", "usage": {"seconds": 25}})
    assert usage.seconds == 25 and usage.final


def test_a_handoff_and_the_backends_reply_are_recorded_and_end_the_turn(monkeypatch, tmp_path):
    """A recording shows when the voice handed off and what the backend answered. Live has
    no spoken turn boundary, so a response with no tool call to continue ends the turn:
    the audit's turns count what the session did, never 0."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setenv("TMUXRC_LIVE_RECORD", "1")
    s = session([
        {"type": "session.delegation.created", "delegation": {"id": "delegation-1"}},
        response("response.created", response={"id": "r1"}),
        response("response.output_item.done", item=call()),
        response("response.completed", response={"id": "r1", "output": []}),
        response("response.completed", response={"id": "r1", "output": []}),  # repeated
        response("response.created", response={"id": "r2"}),
        response("response.output_text.delta", delta="Typed it "),
        response("response.output_text.delta", delta="in the shell."),
        response("response.completed", response={"id": "r2", "output": []}),
    ])
    s.meter.rec = live_record.start("abcdef01", G.ENTRY)
    asyncio.run(drain(s))
    assert s.meter.turns == 1  # the call's continuation is the same turn, a repeat no turn
    s.meter.rec.close()
    s.meter.rec.thread.join()
    [folder] = (tmp_path / "tmux-rc/live-audio").iterdir()
    events = [json.loads(x) for x in (folder / "events.jsonl").read_text().splitlines()]
    handoff, first, reply = (e for e in events if e["kind"] in ("handoff", "backend"))
    assert handoff["provider_event"]["delegation"] == {"id": "delegation-1"}
    assert (first["text"], reply["text"]) == ("", "Typed it in the shell.")
    assert reply["outcome"] == "response.completed" and reply["t_ms"] >= handoff["t_ms"]


def test_custom_backend_requires_explicit_rates(monkeypatch):
    for name in ("INPUT", "CACHED", "OUTPUT"):
        monkeypatch.delenv(f"TMUXRC_GPT_LIVE_{name}_PER_M", raising=False)
    with pytest.raises(G.ProviderError, match="set_tmuxrc_gpt_live_input_per_m"):
        G.Usage("another-model")


def test_completed_calls_survive_empty_response_output_and_all_results_precede_continue(
    monkeypatch,
):
    typed = []
    monkeypatch.setattr(L.tmux, "send_keys", lambda *a, **k: typed.append(a))
    monkeypatch.setattr(L.tmux, "server_uid", lambda: "test")
    monkeypatch.setattr(L.telemetry, "emit_action", lambda **kw: None)
    monkeypatch.setattr(L.telemetry, "emit_live_turn", lambda **kw: None)

    async def run():
        s = session(
            [
                response("response.created", response={"id": "r1"}),
                response("response.function_call_arguments.done", arguments="{}"),
                response("response.output_item.done", item=call()),
                response(
                    "response.output_item.done",
                    item=call("call-2", {"pane_id": "%99", "text": "bad"}),
                ),
                response("response.completed", response={"id": "r1", "output": []}),
            ]
        )
        await drain(s)
        worker = asyncio.create_task(s.execute())
        await asyncio.wait_for(s.ws.continued.wait(), 1)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        # Clean up the shared handler's delayed refresh without touching the network.
        for task in list(L._tasks):
            task.cancel()
        await asyncio.gather(*list(L._tasks), return_exceptions=True)
        return s

    s = asyncio.run(run())
    assert typed == [("%1", "echo hello", True, True)]
    assert [e["type"] for e in s.ws.sent] == [
        "response.item.create",
        "response.item.create",
        "response.create",
    ]
    assert json.loads(s.ws.sent[1]["item"]["output"])["status"] == "rejected"
    assert not any(m["type"] == "turn_complete" for m in s.browser.messages)


@pytest.mark.parametrize("terminal", ["response.failed", "response.incomplete"])
def test_unsuccessful_responses_never_execute_collected_calls(terminal, monkeypatch):
    monkeypatch.setattr(L.telemetry, "emit_live_turn", lambda **kw: None)
    s = session(
        [
            response("response.output_item.done", item=call()),
            response(terminal, response={"id": "r1", "output": []}),
        ]
    )
    asyncio.run(drain(s))
    assert s.work.empty()
    assert s.browser.messages[-1]["type"] == "error"


def test_duplicate_tool_call_stops_without_typing():
    async def run():
        s = session()
        s.seen_calls.add("call-1")
        s.work.put_nowait(("delegation-1", [call()]))
        with pytest.raises(RuntimeError, match="duplicate input"):
            await s.execute()
        assert not s.ws.sent

    asyncio.run(run())


def test_a_fleet_snapshot_replaces_the_backends_pane_state_instead_of_piling_up():
    """Appended, every snapshot stayed in the backend's conversation and each later request
    re-read them all. Each one now rebuilds its instructions around the panes as they are."""
    async def run():
        s = session()
        for label in ("orchard", "quarry"):
            s.watcher.digest = lambda label=label: [{"pane_id": "%1", "label": label}]
            await s.send_context(f"[tmux update] current pane state: {label}")
        return s.ws.sent

    sent = asyncio.run(run())
    assert not any(e["type"] == "response.item.create" for e in sent)
    prompts = [e["session"]["delegation"]["responses"]["instructions"]
               for e in sent if e["type"] == "session.update"]
    assert len(prompts) == 2 and all(p.endswith(G.BACKEND_PROMPT) for p in prompts)
    assert '"quarry"' in prompts[1] and '"orchard"' not in prompts[1]


def test_a_repeated_snapshot_still_updates_state_it_does_not_show(monkeypatch):
    """A scratch page or an inactive pane's screen is in the backend's prompt but not in
    the snapshot text, so a repeat of that text must still carry a change to either."""
    pages = iter(["", "orchard-plan"])
    monkeypatch.setattr(G.live, "_scratch_index", lambda w: next(pages))

    async def run():
        s = session()
        for _ in range(2):
            await s.send_context("[tmux update] current pane state: same")
        return [e for e in s.ws.sent if e["type"] == "session.update"]

    updates = asyncio.run(run())
    assert len(updates) == 2
    assert "orchard-plan" in updates[1]["session"]["delegation"]["responses"]["instructions"]


def test_context_is_quiet_bounded_deduplicated_and_backend_keeps_full_screen():
    async def run():
        s = session()
        s.watcher.digest = lambda: [{"pane_id": "%1", "label": "界" * 1000}]
        text = "[tmux update] " + "界" * 4000
        await s.send_context(text)
        await s.send_context(text)
        s.watcher.digest = list
        await s.send_context(text)
        return s, text

    s, text = asyncio.run(run())
    assert s.ws.sent[0]["item"]["content"][0]["text"] == text
    assert sum(e["type"] == "response.item.create" for e in s.ws.sent) == 1
    hints = [e for e in s.ws.sent if e["type"] == "session.thinking.append"]
    assert len(hints) == 2  # one change, one removal; no repeat
    assert len(hints[0]["content"].encode()) <= 480
    assert "no longer present" in hints[1]["content"]
    assert all(e.get("delegation_id") is None for e in hints)
    assert not any(e["type"] == "response.create" for e in s.ws.sent)


def test_an_idle_panes_age_ticking_sends_no_hint_and_each_hint_is_recorded():
    """Only news reaches the voice: a pane idle a minute longer is not a change. Each hint
    sent is in the recording, to match against a transcript the provider attributes to
    the user."""
    async def run():
        s = session()
        events = []
        s.meter.rec.event = lambda kind, **fields: events.append((kind, fields))
        pane = {"pane_id": "%1", "label": "orchard tests", "activity": "idle"}
        for idle, headline in ((120, "waiting"), (180, "waiting"), (240, "tests pass")):
            s.watcher.digest = lambda idle=idle, headline=headline: [
                {**pane, "idle_seconds": idle, "headline": headline}]
            await s.send_context("[tmux update] something")
        return s, events

    s, events = asyncio.run(run())
    hints = [e["content"] for e in s.ws.sent if e["type"] == "session.thinking.append"]
    assert len(hints) == 2 and "tests pass" in hints[1]
    assert not any("idle for" in h for h in hints)
    assert [f["text"] for k, f in events if k == "hint"] == hints


def test_panes_as_they_stand_at_the_start_are_never_hinted():
    # A hint per pane on a big fleet froze GPT-Live's captions and buried its instructions.
    async def run():
        s = session()
        await s.send_context("[tmux update] current pane state: a")
        quiet = list(s.ws.sent)
        s.watcher.digest = lambda: [*Watcher().digest(), {"pane_id": "%2", "label": "orchard"}]
        await s.send_context("[tmux update] current pane state: b")
        return quiet, s.ws.sent[len(quiet):]

    quiet, later = asyncio.run(run())
    assert not any(e["type"] == "session.thinking.append" for e in quiet)
    assert [e["content"] for e in later if e["type"] == "session.thinking.append"] == [
        '[tmux update] ## "orchard" (id=%2) — unknown']


def test_continuous_transcripts_keep_roles_and_gaps_without_triggering_tools():
    s = session(
        [
            {
                "type": "session.input_transcript.delta",
                "delta": "hello",
                "start_ms": 0,
                "end_ms": 500,
            },
            {
                "type": "session.output_transcript.delta",
                "delta": "yes",
                "start_ms": 300,
                "end_ms": 600,
            },
            {
                "type": "session.input_transcript.delta",
                "delta": "again",
                "start_ms": 2500,
                "end_ms": 3000,
            },
            {"type": "session.output_audio.delta", "delta": "AAA="},
        ]
    )
    asyncio.run(drain(s))
    assert [m.get("role") for m in s.browser.messages[:3]] == ["user", "model", "user"]
    assert s.browser.messages[2]["new_segment"]
    assert s.browser.messages[3]["sample_rate"] == 16000
    assert s.work.empty() and not s.ws.sent
    assert list(s.meter.heard) == ["hello", "again"]  # what a relay's label is checked against


@pytest.mark.parametrize("disconnected", [False, True])
def test_stop_or_phone_disconnect_collects_final_usage(monkeypatch, disconnected):
    class Connection(Wire):
        def __init__(self):
            super().__init__()
            self.closed = asyncio.Event()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def recv(self):
            return json.dumps({"type": "session.started"})

        async def send(self, data):
            await super().send(data)
            if self.sent[-1]["type"] == "session.close":
                self.closed.set()

        async def __aiter__(self):
            await self.closed.wait()
            yield json.dumps({"type": "session.closed", "usage": {"seconds": 10}})

    class Client(Browser):
        async def receive_json(self):
            if disconnected:
                raise WebSocketDisconnect
            return {"action": "stop"}

    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    wire = Connection()
    monkeypatch.setattr(G.websockets, "connect", lambda *a, **kw: wire)
    meter = L._Meter("test", "test", G.ENTRY)

    async def run():
        try:
            await G.run_session(Client(), Watcher(), meter)
        except WebSocketDisconnect:
            assert disconnected

    asyncio.run(run())
    assert meter.usage.final and meter.usage.seconds == 10
    assert wire.sent[0]["type"] == "session.start"
    assert wire.sent[0]["session"]["model"] == G.ENTRY.model  # the picked entry's id
    assert wire.sent[-1]["type"] == "session.close"
    assert meter.model.backend == "openai"


@pytest.mark.parametrize("entry", G.ENTRIES, ids=lambda e: e.model)
def test_picker_key_gating_and_route_selection(monkeypatch, entry):
    from fastapi.testclient import TestClient

    from openbus import server

    monkeypatch.setenv("TMUXRC_LIVE_MODE", "1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert all(m["label"] != entry.label for m in server.get_version()["live_models"])
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    assert any(m["label"] == entry.label for m in server.get_version()["live_models"])
    called = []

    async def run(browser, watcher, meter):
        called.append(meter.model)

    monkeypatch.setattr(G, "run_session", run)
    monkeypatch.setitem(server.app.state._state, "watcher", Watcher())
    monkeypatch.setattr(L._Meter, "finish", lambda s: None)
    with (
        TestClient(server.app).websocket_connect(
            "/api/live-mode?" + urlencode({"model": entry.label})
        ) as ws,
        pytest.raises(WebSocketDisconnect),
    ):
        ws.receive_json()
    assert called == [entry]  # the adapter, carrying the model id the user picked


@pytest.mark.parametrize("error,expected", [
    ({"code": "invalid_api_key", "message": "private terminal context"}, "invalid_api_key"),
    ({"code": "secret context!"}, "unknown_error"), ({}, "unknown_error"),
    ("private terminal context", "unknown_error"), (None, "unknown_error"),
])
def test_provider_errors_expose_only_sanitized_code(error, expected):
    s = session([{"type": "error", "code": "invalid_api_key", "error": error}])
    with pytest.raises(G.ProviderError, match=r"^GPT-Live: " + expected + "$"):
        asyncio.run(s.receive())
    assert not s.ws.sent


def test_startup_error_preserves_code(monkeypatch):
    class Connection(Wire):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def recv(self):
            return json.dumps({"type": "error", "error": {
                "code": "model_not_found", "message": "private context",
            }})

    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    monkeypatch.setattr(G.websockets, "connect", lambda *a, **kw: Connection())
    with pytest.raises(G.ProviderError, match=r"^GPT-Live: model_not_found$"):
        asyncio.run(G.run_session(Browser(), Watcher(), L._Meter("test", "test", G.ENTRY)))


# Selection is by LABEL against the offered menu now, not by a TMUXRC_LIVE_MODEL default:
# live_providers.find() answers for the table's entries and GPT-Live is routed by its own
# label, only when its key is present. The safety property these cases were written for is
# unchanged — a pick the server never offered must not open a session.
@pytest.mark.parametrize("selection,key", [
    (G.ENTRY.label, False),          # GPT-Live with no key: offered by neither source
    ("No Such Model", False),  # a label from neither source
    ("No Such Model", True),
])
def test_unavailable_model_never_connects(monkeypatch, selection, key):
    from fastapi.testclient import TestClient

    from openbus import server

    monkeypatch.setenv("TMUXRC_LIVE_MODE", "1")
    if key:
        monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setitem(server.app.state._state, "watcher", Watcher())
    monkeypatch.setattr(L._Meter, "finish", lambda s: None)

    async def unexpected(*args):
        pytest.fail("Unavailable model must not connect")

    monkeypatch.setattr(G, "run_session", unexpected)
    monkeypatch.setattr(L, "_run_session", unexpected)
    # Refused BEFORE the accept: a 1008 close carrying the reason, not an accepted socket
    # that then apologises. Nothing was offered, so there is no session to open and no mic
    # to stream — closing first is what makes "never connects" true rather than merely said.
    url = "/api/live-mode?" + urlencode({"model": selection})
    with (
        pytest.raises(WebSocketDisconnect) as refused,
        TestClient(server.app).websocket_connect(url) as ws,
    ):
        ws.receive_json()
    assert refused.value.code == 1008


def test_provider_diagnostic_reaches_browser(monkeypatch):
    from fastapi.testclient import TestClient

    from openbus import server

    monkeypatch.setenv("TMUXRC_LIVE_MODE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    monkeypatch.setitem(server.app.state._state, "watcher", Watcher())
    monkeypatch.setattr(L._Meter, "finish", lambda s: None)

    async def fail(*args):
        raise G.ProviderError({"error": {"code": "invalid_api_key", "message": "private context"}})

    monkeypatch.setattr(G, "run_session", fail)
    with TestClient(server.app).websocket_connect("/api/live-mode?model=GPT-Live%201") as ws:
        assert ws.receive_json() == {"type": "error", "message": "GPT-Live: invalid_api_key"}


@pytest.mark.parametrize("status,code", [
    (401, "invalid_api_key"), (403, "permission_denied"), (404, "endpoint_not_found"),
    (429, "rate_limit_exceeded"), (503, "handshake_failed"),
])
def test_http_handshake_error_is_sanitized(monkeypatch, status, code):
    from websockets.datastructures import Headers
    from websockets.http11 import Response

    class Connection:
        async def __aenter__(self):
            raise G.websockets.exceptions.InvalidStatus(
                Response(status, "private reason", Headers(), body=b"private body")
            )

        async def __aexit__(self, *args):
            pass

    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    monkeypatch.setattr(G.websockets, "connect", lambda *a, **kw: Connection())
    with pytest.raises(G.ProviderError, match=r"^GPT-Live: " + code + "$"):
        asyncio.run(G.run_session(Browser(), Watcher(), L._Meter("test", "test", G.ENTRY)))


@pytest.mark.parametrize("rate", ["bad", "nan", "inf", "-1"])
def test_invalid_backend_rates_have_safe_diagnostics(monkeypatch, rate):
    monkeypatch.setenv("TMUXRC_GPT_LIVE_INPUT_PER_M", rate)
    with pytest.raises(G.ProviderError, match=r"^GPT-Live: set_tmuxrc_gpt_live_input_per_m$"):
        G.Usage(G.BACKEND)


def test_full_tool_queue_reports_overload_without_running_more_actions():
    s = session([
        response("response.output_item.done", item=call()),
        response("response.completed", response={"id": "r1", "output": []}),
    ])
    for _ in range(s.work.maxsize):
        s.work.put_nowait(("delegation-1", [call()]))
    with pytest.raises(G.ProviderError, match=r"^GPT-Live: terminal_queue_full$"):
        asyncio.run(s.receive())
    assert not s.ws.sent
    assert s.work.qsize() == s.work.maxsize


def test_invalid_configuration_still_marks_openai_telemetry(monkeypatch):
    """Even a session that dies in configuration is billed to the right provider — and it
    must be EMITTABLE, which is the part only a real turn boundary used to exercise. The
    adapter rides the seam's one emit path, so its stand-in entry supplies model/provider
    and `details` may carry only fields that path has no parameter for: a `details` key
    shadowing a real parameter is a duplicate-kwarg TypeError, and a bare string left in
    meter.model an AttributeError — both invisible until a live call."""
    emitted = []
    monkeypatch.setattr(L.telemetry, "emit_live_turn", lambda **k: emitted.append(k))
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    monkeypatch.setenv("TMUXRC_GPT_LIVE_INPUT_PER_M", "invalid")
    meter = L._Meter("test", "test", G.ENTRY)
    with pytest.raises(G.ProviderError):
        asyncio.run(G.run_session(Browser(), Watcher(), meter))
    assert meter.model is G.ENTRY  # the adapter must not replace it with a bare id
    assert meter.details == {
        "backend_model": G.BACKEND, "voice_seconds": 0.0, "usage_final": False,
    }
    meter._emit(final=True)
    assert emitted[0]["provider"] == "openai"
    assert emitted[0]["model"] == G.ENTRY.model
    assert emitted[0]["backend_model"] == G.BACKEND


def test_concurrent_snapshot_dedup_survives_post_action_context():
    async def run():
        s = session()
        original_send = s.ws.send

        async def slow_send(data):
            await asyncio.sleep(0)
            await original_send(data)

        s.ws.send = slow_send
        snapshot = "[tmux update] current pane state: same screen"
        action = "[tmux update] shell (%1) after your input: new screen"
        await asyncio.gather(s.send_context(snapshot), s.send_context(snapshot))
        await s.send_context(action)
        await s.send_context(snapshot)
        updates = [e for e in s.ws.sent if e["type"] in ("session.update", "response.item.create")]
        assert len(updates) == 2
        assert not [e for e in s.ws.sent if e["type"] == "session.thinking.append"]

    asyncio.run(run())


def test_provider_eof_is_a_drop():
    s = session(closed=False)
    with pytest.raises(G.ProviderError, match="connection_closed_without_session_closed"):
        asyncio.run(s.receive())
    assert not s.meter.usage.final


def test_an_unasked_session_closed_is_an_error_not_a_stop():
    """A provider ending the session must not look like the user's stop, which deletes the
    parked cards and closes 1000 so the client gives up. Its usage still counts as final."""
    s = session()
    asyncio.run(drain(s))
    assert s.meter.usage.final


def test_provider_eof_propagates_from_running_session(monkeypatch):
    class Connection(Wire):
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def recv(self):
            return json.dumps({"type": "session.started"})

    class Client(Browser):
        async def receive_json(self):
            await asyncio.Event().wait()

    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    wire = Connection()
    monkeypatch.setattr(G.websockets, "connect", lambda *a, **kw: wire)
    meter = L._Meter("test", "test", G.ENTRY)
    with pytest.raises(G.ProviderError, match="connection_closed_without_session_closed"):
        asyncio.run(G.run_session(Client(), Watcher(), meter))
    assert not meter.usage.final
    assert wire.sent[-1]["type"] == "session.close"


def test_a_missing_key_is_an_error_not_a_stop(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(L.live_providers.Unreachable, match="OPENAI_API_KEY"):
        asyncio.run(G.run_session(Browser(), Watcher(), L._Meter("test", "test", G.ENTRY)))


def test_one_delegation_types_into_one_pane(monkeypatch):
    """GPT-Live's request is its delegation, not the transcript, which streams meanwhile."""
    typed = []
    monkeypatch.setattr(L.tmux, "send_keys", lambda *a, **k: typed.append(a))
    monkeypatch.setattr(L.tmux, "server_uid", lambda: "test")
    monkeypatch.setattr(L.telemetry, "emit_action", lambda **kw: None)
    monkeypatch.setattr(L, "_labels", lambda w: {"%1": "shell", "%2": "docs"})

    async def run():
        s = session()
        worker = asyncio.create_task(s.execute())
        for batch in [("delegation-1", [call("c1", {"pane_id": "%1", "text": "w"})]),
                      ("delegation-2", [call("c2", {"pane_id": "%2", "text": "x"})]),
                      ("delegation-1", [call("c3", {"pane_id": "%2", "text": "y"})])]:
            s.work.put_nowait(batch)
            await asyncio.wait_for(s.ws.continued.wait(), 1)  # the batch's continuation
            s.ws.continued.clear()
        for task in [worker, *L._tasks]:
            task.cancel()
        await asyncio.gather(worker, *list(L._tasks), return_exceptions=True)
        return s

    s = asyncio.run(run())
    assert typed == [("%1", "w", True, True), ("%2", "x", True, True)]  # interleaved: A, B, A
    assert json.loads(s.ws.sent[-2]["item"]["output"])["status"] == "rejected"


def test_two_composer_turns_are_two_requests(monkeypatch):
    """A composer turn has no delegation id, so its request is the meter's: a turn typed once
    the last one has finished is a new one and can reach another pane. One typed while an
    earlier turn is still going (its calls, or the continuation they asked for, even after a
    later turn's own reply has ended) shares its request: fail closed."""
    typed = []
    monkeypatch.setattr(L.tmux, "send_keys", lambda *a, **k: typed.append(a))
    monkeypatch.setattr(L.tmux, "server_uid", lambda: "test")
    monkeypatch.setattr(L.telemetry, "emit_action", lambda **kw: None)
    monkeypatch.setattr(L, "_labels", lambda w: {"%1": "shell", "%2": "docs"})

    def composer(kind, **fields):
        return {**response(kind, **fields), "delegation_id": None}

    async def turn(s, n, pane, *, finish=True):
        await s.send_text("now there")
        s.ws.continued.clear()  # the turn's own response.create
        await L._transcript(s.browser, s.meter, "user", "now there")
        s.ws.events = [composer("response.created", response={"id": f"r{n}"}),
                       composer("response.output_item.done", item=call(f"c{n}", {
                           "pane_id": pane, "text": "x"})),
                       composer("response.completed", response={"id": f"r{n}"}),
                       {"type": "session.closed"}]
        await drain(s)
        await asyncio.wait_for(s.ws.continued.wait(), 1)  # its calls ran; asked to go on
        s.ws.continued.clear()
        if finish:  # the continuation ends the turn, calling nothing more
            s.ws.events = [composer("response.completed", response={"id": f"r{n}+"}),
                           {"type": "session.closed"}]
            await drain(s)

    async def run():
        s = session()
        worker = asyncio.create_task(s.execute())
        await turn(s, 1, "%1")
        await turn(s, 2, "%2")  # the first turn is over: a request of its own
        await turn(s, 3, "%1", finish=False)
        await s.send_text("and here")  # before that continuation's response even starts
        assert not s.meter.answered
        s.ws.events = [composer("response.completed", response={"id": "r4"}),  # it answers
                       {"type": "session.closed"}]
        await drain(s)
        await s.send_text("and there")  # the third turn's continuation is still to come
        assert not s.meter.answered
        for n in ("3+", "5"):  # that continuation, then the last turn's reply, end both
            s.ws.events = [composer("response.completed", response={"id": f"r{n}"}),
                           {"type": "session.closed"}]
            await drain(s)
        await s.send_text("now somewhere else")
        assert s.meter.answered
        for task in [worker, *L._tasks]:
            task.cancel()
        await asyncio.gather(worker, *list(L._tasks), return_exceptions=True)

    asyncio.run(run())
    assert [t[0] for t in typed] == ["%1", "%2", "%1"]
