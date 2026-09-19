"""The Live/Responses boundary must not duplicate terminal actions or lose billing."""

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import WebSocketDisconnect

from openbus import gpt_live as G
from openbus import live as L


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
    meter = L._Meter("test", "test")
    meter.usage = G.Usage(G.BACKEND)
    events = [*events, {"type": "session.closed"}] if closed else events
    return G.Session(Wire(events), Browser(), Watcher(), "test", meter)


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


def test_custom_backend_requires_explicit_rates(monkeypatch):
    for name in ("INPUT", "CACHED", "OUTPUT"):
        monkeypatch.delenv(f"TMUXRC_GPT_LIVE_{name}_PER_M", raising=False)
    with pytest.raises(G.ProviderError, match="set_tmuxrc_gpt_live_input_per_m"):
        G.Usage("another-model")


def test_completed_calls_survive_empty_response_output_and_all_results_precede_continue(
    monkeypatch,
):
    # Import the optional SDK before starting the one-second protocol deadline.
    L.llm.genai_types()
    typed = []
    monkeypatch.setattr(L.tmux, "send_keys", lambda *a: typed.append(a))
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
        await s.receive()
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
    asyncio.run(s.receive())
    assert s.work.empty()
    assert s.browser.messages[-1]["type"] == "error"


def test_duplicate_tool_call_stops_without_typing():
    async def run():
        s = session()
        s.seen_calls.add("call-1")
        s.work.put_nowait([call()])
        with pytest.raises(RuntimeError, match="duplicate input"):
            await s.execute()
        assert not s.ws.sent

    asyncio.run(run())


def test_context_is_quiet_bounded_deduplicated_and_backend_keeps_full_screen():
    async def run():
        s = session()
        s.watcher.digest = lambda: [{"pane_id": "%1", "label": "界" * 1000}]
        text = "[tmux update] " + "界" * 4000
        content = SimpleNamespace(parts=[SimpleNamespace(text=text)])
        await s.send_client_content(turns=content)
        await s.send_client_content(turns=content)
        s.watcher.digest = list
        await s.send_client_content(turns=content)
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
    asyncio.run(s.receive())
    assert [m.get("role") for m in s.browser.messages[:3]] == ["user", "model", "user"]
    assert s.browser.messages[2]["new_segment"]
    assert s.browser.messages[3]["sample_rate"] == 16000
    assert s.work.empty() and not s.ws.sent


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
    meter = L._Meter("test", "test")

    async def run():
        try:
            await G.run_session(Client(), Watcher(), "test", meter)
        except WebSocketDisconnect:
            assert disconnected

    asyncio.run(run())
    assert meter.usage.final and meter.usage.seconds == 10
    assert wire.sent[0]["type"] == "session.start"
    assert wire.sent[-1]["type"] == "session.close"
    assert meter.details["provider"] == "openai"


def test_picker_key_gating_and_route_selection(monkeypatch):
    from fastapi.testclient import TestClient

    from openbus import server

    monkeypatch.setenv("TMUXRC_LIVE_MODE", "1")
    monkeypatch.setattr(L, "LIVE_MODEL", "gemini-live-2.5-flash-native-audio")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert all(m["label"] != G.LABEL for m in server.get_version()["live_models"])
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    assert any(m["label"] == G.LABEL for m in server.get_version()["live_models"])
    called = []

    async def run(*args):
        called.append("gpt")

    monkeypatch.setattr(G, "run_session", run)
    monkeypatch.setattr(server.app.state, "watcher", Watcher(), raising=False)
    monkeypatch.setattr(L._Meter, "finish", lambda s: None)
    with (
        TestClient(server.app).websocket_connect(
            "/api/live-mode?model=GPT-Live%201"
        ) as ws,
        pytest.raises(WebSocketDisconnect),
    ):
        ws.receive_json()
    assert called == ["gpt"]


@pytest.mark.parametrize("code,expected", [
    ("invalid_api_key", "invalid_api_key"), ("secret context!", "unknown_error"),
    (None, "unknown_error"),
])
def test_provider_errors_expose_only_sanitized_code(code, expected):
    s = session([{"type": "error", "error": {"code": code, "message": "private terminal context"}}])
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
        asyncio.run(G.run_session(Browser(), Watcher(), "test", L._Meter("test", "test")))


@pytest.mark.parametrize("default,selection,key", [
    (G.MODEL, "Gemini Live", True),
    (G.MODEL, "Default", False),
    ("gemini-live-2.5-flash-native-audio", G.LABEL, False),
])
def test_unavailable_model_never_connects(monkeypatch, default, selection, key):
    from urllib.parse import urlencode

    from fastapi.testclient import TestClient

    from openbus import server

    monkeypatch.setenv("TMUXRC_LIVE_MODE", "1")
    monkeypatch.setattr(L, "LIVE_MODEL", default)
    if key:
        monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    else:
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(server.app.state, "watcher", Watcher(), raising=False)
    monkeypatch.setattr(L._Meter, "finish", lambda s: None)

    async def unexpected(*args):
        pytest.fail("Unavailable model must not connect")

    monkeypatch.setattr(G, "run_session", unexpected)
    monkeypatch.setattr(L, "_run_session", unexpected)
    url = "/api/live-mode?" + urlencode({"model": selection})
    with TestClient(server.app).websocket_connect(url) as ws:
        assert "unavailable" in ws.receive_json()["message"].lower()


def test_provider_diagnostic_reaches_browser(monkeypatch):
    from fastapi.testclient import TestClient

    from openbus import server

    monkeypatch.setenv("TMUXRC_LIVE_MODE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    monkeypatch.setattr(server.app.state, "watcher", Watcher(), raising=False)
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
        asyncio.run(G.run_session(Browser(), Watcher(), "test", L._Meter("test", "test")))


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
        s.work.put_nowait([call()])
    with pytest.raises(G.ProviderError, match=r"^GPT-Live: terminal_queue_full$"):
        asyncio.run(s.receive())
    assert not s.ws.sent
    assert s.work.qsize() == s.work.maxsize


def test_invalid_configuration_still_marks_openai_telemetry(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    monkeypatch.setenv("TMUXRC_GPT_LIVE_INPUT_PER_M", "invalid")
    meter = L._Meter("test", "test")
    with pytest.raises(G.ProviderError):
        asyncio.run(G.run_session(Browser(), Watcher(), "test", meter))
    assert meter.model == G.MODEL
    assert meter.details == {
        "provider": "openai", "backend_model": G.BACKEND,
        "voice_seconds": 0.0, "usage_final": False,
    }


def test_concurrent_snapshot_dedup_survives_post_action_context():
    async def run():
        s = session()
        original_send = s.ws.send

        async def slow_send(data):
            await asyncio.sleep(0)
            await original_send(data)

        s.ws.send = slow_send
        snapshot = SimpleNamespace(parts=[
            SimpleNamespace(text="[tmux update] current pane state: same screen"),
        ])
        action = SimpleNamespace(parts=[
            SimpleNamespace(text="[tmux update] shell (%1) after your input: new screen"),
        ])
        await asyncio.gather(
            s.send_client_content(turns=snapshot), s.send_client_content(turns=snapshot),
        )
        await s.send_client_content(turns=action)
        await s.send_client_content(turns=snapshot)
        updates = [e for e in s.ws.sent if e["type"] == "response.item.create"]
        assert len(updates) == 2
        assert len([e for e in s.ws.sent if e["type"] == "session.thinking.append"]) == 1

    asyncio.run(run())


@pytest.mark.parametrize("default", ["gemini-live-2.5-flash-native-audio", G.MODEL])
def test_adapter_import_failure_reaches_browser(monkeypatch, default):
    import builtins

    from fastapi.testclient import TestClient

    from openbus import server

    original = builtins.__import__

    def missing(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 1 and "gpt_live" in fromlist:
            raise ImportError("optional adapter dependency missing")
        return original(name, globals, locals, fromlist, level)

    monkeypatch.setenv("TMUXRC_LIVE_MODE", "1")
    monkeypatch.setattr(server.app.state, "watcher", Watcher(), raising=False)
    monkeypatch.setattr(L._Meter, "finish", lambda s: None)
    monkeypatch.setattr(builtins, "__import__", missing)
    monkeypatch.setenv("OPENAI_API_KEY", "test-not-a-key")
    monkeypatch.setattr(L, "LIVE_MODEL", default)
    called = []

    async def gemini(*args):
        called.append("gemini")

    monkeypatch.setattr(L, "_run_session", gemini)
    client = TestClient(server.app)
    version = client.get("/api/version")
    assert version.status_code == 200
    models = version.json()["live_models"]
    assert models == ([] if default == G.MODEL else [
        {"label": "Gemini Live", "value": "", "hint": "Vertex"},
    ])
    assert version.json()["live_enabled"] == bool(models)
    with client.websocket_connect("/api/live-mode?model=GPT-Live%201") as ws:
        assert ws.receive_json() == {"type": "error", "message": "live session failed"}
    if default != G.MODEL:
        with (
            client.websocket_connect("/api/live-mode") as ws,
            pytest.raises(WebSocketDisconnect),
        ):
            ws.receive_json()
        assert called == ["gemini"]


@pytest.mark.parametrize("closed", [False, True])
def test_provider_eof_requires_session_closed(closed):
    s = session(closed=closed)
    if closed:
        asyncio.run(s.receive())
        assert s.meter.usage.final
    else:
        with pytest.raises(G.ProviderError, match="connection_closed_without_session_closed"):
            asyncio.run(s.receive())
        assert not s.meter.usage.final


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
    meter = L._Meter("test", "test")
    with pytest.raises(G.ProviderError, match="connection_closed_without_session_closed"):
        asyncio.run(G.run_session(Client(), Watcher(), "test", meter))
    assert not meter.usage.final
    assert wire.sent[-1]["type"] == "session.close"
