"""The OpenAI Realtime adapter against a scripted WebSocket: every wire event we depend on
maps to the provider-neutral Event live.py consumes, the three sends carry exactly what
the protocol needs (24 kHz audio, a reply-less context item, a tool result that asks for
the follow-up turn at the right moment), and the resampler is length- and value-correct.
The real API is exercised by research/live-eval/harness_openai.py."""

import asyncio
import base64
import json
import struct

import pytest

import daemon.live_providers as P


class _WS:
    """Scripted server: `script` is what the server sends; `sent` collects what we send."""

    def __init__(self, script=()):
        self.script = [json.dumps(m) for m in script]
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.script:
            raise StopAsyncIteration
        return self.script.pop(0)


def _run(coro):
    return asyncio.run(coro)


async def _drain(session):
    return [e async for e in session.events()]


def _pcm(*samples):
    return struct.pack(f"<{len(samples)}h", *samples)


def test_resampler_length_and_values():
    # 2 in → 3 out; a constant stays constant; a ramp interpolates linearly.
    assert P.resample_16k_to_24k(_pcm(100, 100, 100, 100)) == _pcm(
        100, 100, 100, 100, 100, 100
    )
    ramp = P.resample_16k_to_24k(_pcm(0, 300, 600, 900))
    assert struct.unpack("<6h", ramp) == (
        0,
        200,
        400,
        600,
        800,
        900,
    )  # last holds (no successor)
    assert len(P.resample_16k_to_24k(b"\0" * 4096 * 2)) == 6144 * 2
    assert (
        P.resample_16k_to_24k(b"\1\0") == b"\1\0"
    )  # too short to interpolate: pass through


@pytest.mark.parametrize("pcm", [b"", b"\xff", _pcm(123), _pcm(123) + b"\xff", _pcm(0, 300) + b"\xff"])
def test_resampler_discards_incomplete_samples(pcm):
    normalized = pcm[:len(pcm) & ~1]
    result = P.resample_16k_to_24k(pcm)
    assert result == P.resample_16k_to_24k(normalized)
    assert len(result) % 2 == 0


@pytest.mark.parametrize("pcm", [b"", b"\xff"])
def test_send_audio_skips_frames_without_a_complete_sample(pcm):
    ws = _WS()
    _run(P._OpenAISession(ws).send_audio(pcm))
    assert ws.sent == []


def test_send_audio_resamples_and_base64s():
    ws = _WS()
    _run(P._OpenAISession(ws).send_audio(_pcm(0, 300, 600, 900)))
    (m,) = ws.sent
    assert m["type"] == "input_audio_buffer.append"
    assert base64.b64decode(m["audio"]) == P.resample_16k_to_24k(_pcm(0, 300, 600, 900))


def test_send_context_is_a_system_item_with_no_response():
    ws = _WS()
    _run(P._OpenAISession(ws).send_context("[tmux update] x"))
    assert [m["type"] for m in ws.sent] == [
        "conversation.item.create"
    ]  # no response.create
    item = ws.sent[0]["item"]
    assert item["role"] == "system" and item["content"] == [
        {"type": "input_text", "text": "[tmux update] x"}
    ]


def test_tool_result_requests_follow_up_now_or_after_active_response():
    call = P.ToolCall("call_1", "type_in_pane", {"pane_id": "%1", "text": "y"})
    # Idle: output item then response.create straight away.
    ws = _WS()
    _run(P._OpenAISession(ws).send_tool_result(call, {"status": "done"}))
    assert [m["type"] for m in ws.sent] == [
        "conversation.item.create",
        "response.create",
    ]
    assert ws.sent[0]["item"] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": json.dumps({"status": "done"}),
    }
    # Mid-response: deferred until that response's done, else the API rejects it.
    ws = _WS([{"type": "response.created"}, {"type": "response.done", "response": {}}])
    s = P._OpenAISession(ws)

    async def scenario():
        gen = s.events()
        # The script has response.created first; events() consumes it silently and yields
        # nothing until response.done. Interleave: send the tool result after created.
        ws.script = [json.dumps({"type": "response.created"})]
        assert [e async for e in gen] == []  # created consumed, no yield
        assert s._active is True
        await s.send_tool_result(call, {"status": "done"})
        assert [m["type"] for m in ws.sent] == ["conversation.item.create"]  # deferred
        ws.script = [json.dumps({"type": "response.done", "response": {}})]
        kinds = [e.kind async for e in s.events()]
        assert kinds == ["usage", "turn_complete"]
        assert [m["type"] for m in ws.sent][-1] == "response.create"  # released at done

    _run(scenario())


def test_events_map_to_neutral_kinds_and_usage_accumulates():
    audio = base64.b64encode(b"\1\2\3\4").decode()
    usage1 = {
        "input_token_details": {
            "text_tokens": 100,
            "audio_tokens": 50,
            "cached_tokens": 40,
            "cached_tokens_details": {"text_tokens": 30, "audio_tokens": 10},
        },
        "output_token_details": {"text_tokens": 10, "audio_tokens": 200},
    }
    usage2 = {
        "input_token_details": {"text_tokens": 120, "audio_tokens": 30},
        "output_token_details": {"text_tokens": 5, "audio_tokens": 100},
    }
    ws = _WS(
        [
            {"type": "session.created"},
            {"type": "response.created"},
            {"type": "response.output_audio.delta", "delta": audio},
            {"type": "response.output_audio_transcript.delta", "delta": "Typing "},
            {
                "type": "conversation.item.input_audio_transcription.completed",
                "transcript": "say yes",
            },
            {
                "type": "response.function_call_arguments.done",
                "call_id": "c9",
                "name": "type_in_pane",
                "arguments": json.dumps({"pane_id": "%1", "text": "y"}),
            },
            {"type": "error", "error": {"message": "benign"}},
            {"type": "response.done", "response": {"usage": usage1}},
            {"type": "input_audio_buffer.speech_started"},
            {"type": "response.done", "response": {"usage": usage2}},
        ]
    )
    evs = _run(_drain(P._OpenAISession(ws)))
    kinds = [e.kind for e in evs]
    assert kinds == [
        "audio",
        "transcript",
        "transcript",
        "tool_call",
        "usage",
        "turn_complete",
        "interrupted",
        "usage",
        "turn_complete",
    ]
    assert evs[0].data == b"\1\2\3\4"
    assert (evs[1].role, evs[1].text) == ("model", "Typing ")
    assert (evs[2].role, evs[2].text) == ("user", "say yes")
    assert evs[3].call == P.ToolCall(
        "c9", "type_in_pane", {"pane_id": "%1", "text": "y"}
    )
    # Per-response usage is SUMMED into connection totals; cached input moves out of the
    # text/audio input buckets into its own two (usage2 reports no cache: all uncached).
    assert evs[4].usage == P.Split(70, 10, 40, 200, 30, 10)
    assert evs[7].usage == P.Split(190, 15, 70, 300, 30, 10)
    assert ws.sent == []  # a benign error is logged, never answered or fatal


def test_unparseable_tool_arguments_pass_through_for_rejection():
    ws = _WS(
        [
            {
                "type": "response.function_call_arguments.done",
                "call_id": "c",
                "name": "press_key",
                "arguments": "{not json",
            }
        ]
    )
    (ev,) = _run(_drain(P._OpenAISession(ws)))
    assert ev.call.args == "{not json"  # live._handle_tool_call rejects non-dict args


def test_openai_endpoint_shapes(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    url, hdr = P.openai_endpoint(P.LiveModel("x", "gpt-realtime-2.1-mini", "openai"))
    assert url == "wss://api.openai.com/v1/realtime?model=gpt-realtime-2.1-mini"
    assert hdr == {"Authorization": "Bearer sk-test"}
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "az-test")
    for ep in (
        "https://foo.cognitiveservices.azure.com/",
        "foo.cognitiveservices.azure.com",
        "https://foo.openai.azure.com/openai/v1/",
    ):
        monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", ep)
        url, hdr = P.openai_endpoint(
            P.LiveModel("x", "gpt-realtime-2.1", "azure-openai")
        )
        assert (
            url
            == f"wss://{ep.replace('https://', '').split('/')[0]}/openai/v1/realtime?model=gpt-realtime-2.1"
        )
        assert hdr == {"api-key": "az-test"}


def test_openai_backends_gate_on_their_keys(monkeypatch):
    table = [
        {"label": "GPT", "model": "gpt-realtime-2.1", "backend": "openai"},
        {
            "label": "GPT (Azure)",
            "model": "gpt-realtime-2.1",
            "backend": "azure-openai",
        },
    ]
    monkeypatch.setenv("TMUXRC_LIVE_MODELS", json.dumps(table))
    for v in ("OPENAI_API_KEY", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT"):
        monkeypatch.delenv(v, raising=False)
    assert P.available() == []
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")  # key alone is not enough for Azure
    assert [m.label for m in P.available()] == ["GPT"]
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "h")
    assert [m.label for m in P.available()] == ["GPT", "GPT (Azure)"]
    assert (
        P.find("GPT").hint == "OpenAI · $3/$12 per 1M audio"
    )  # no rates given → 2.5's card, visibly


def test_handshake_rejection_is_unreachable_not_retryable(monkeypatch):
    import websockets

    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "k")
    monkeypatch.setenv(
        "AZURE_OPENAI_ENDPOINT", "https://foo.cognitiveservices.azure.com"
    )

    class _Resp:
        status_code = 400

    class _Failing:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            raise websockets.exceptions.InvalidStatus(_Resp())

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(websockets, "connect", _Failing)
    m = P.LiveModel("GPT (Azure)", "gpt-realtime-2.1", "azure-openai")

    async def go():
        async with P.connect(m, "prompt"):
            pass

    try:
        _run(go())
        raise AssertionError("expected Unreachable")
    except P.Unreachable as e:
        assert (
            str(e)
            == "deployment 'gpt-realtime-2.1' not found on foo.cognitiveservices.azure.com"
        )
