"""429/backoff behavior of the LLM error path (openbus/llm.py).

These exercise _handle_llm_error and the backoff gate without any network: the
google.genai import happens lazily inside the try, and the gate returns before it.
"""

import json
import time

import pytest

from openbus import llm


def _reset():
    llm._backoff.update(delay=0.0, until=0.0)
    llm.last_error["msg"] = None


def test_429_arms_backoff_and_short_message():
    _reset()
    msg = llm._handle_llm_error(Exception("429 RESOURCE_EXHAUSTED. try later"))
    assert "429" in msg and "backing off" in msg
    assert llm._backoff["until"] > time.time()
    assert "Traceback" not in msg  # single-line, not a dump


def test_backoff_doubles_then_caps():
    _reset()
    delays = []
    for _ in range(5):
        llm._handle_llm_error(Exception("RESOURCE_EXHAUSTED"))
        delays.append(llm._backoff["delay"])
    assert delays == [15.0, 30.0, 60.0, 120.0, 120.0]


def test_gate_skips_vertex_call_while_backing_off(monkeypatch):
    _reset()
    llm._backoff.update(delay=15.0, until=time.time() + 30)
    called = []
    monkeypatch.setattr(llm, "_client", lambda: called.append(1))
    assert llm.classify_text("system", "pane text") is None
    assert not called, "must not touch Vertex during backoff"
    assert "429" in llm.last_error["msg"]


def test_success_message_shape_for_auth_and_timeout():
    _reset()
    assert "auth expired" in llm._handle_llm_error(
        Exception("Reauthentication is needed. Please run gcloud ...")
    )
    assert llm._backoff["until"] == 0.0  # auth errors must NOT arm the 429 backoff
    assert "timed out" in llm._handle_llm_error(TimeoutError("read timed out"))


def test_unknown_error_passes_through_truncated():
    _reset()
    msg = llm._handle_llm_error(ValueError("x" * 500))
    assert msg == "x" * 200
    assert llm._backoff["until"] == 0.0


def test_parse_json_salvages_first_object_from_trailing_data():
    # flash-lite quirk: valid object then a duplicated block ("Extra data" from loads)
    assert llm._parse_json('{"a": 1}\n{"a": 1}') == {"a": 1}


def test_parse_json_raises_on_garbage():
    with pytest.raises(json.JSONDecodeError):
        llm._parse_json("not json at all")


def test_malformed_json_is_expected_single_line_error():
    _reset()
    msg = llm._handle_llm_error(json.JSONDecodeError("Extra data", "{}", 2))
    assert msg.startswith("model returned malformed JSON:")
    assert "Extra data" in msg
    assert llm._backoff["until"] == 0.0  # not a quota event; must not arm the backoff
