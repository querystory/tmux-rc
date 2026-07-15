"""classify() is now a raw-JSON pipe: it returns the LLM's dict (plus pane_id/label),
with a waiting-override for question/rewind and a no-LLM heuristic fallback."""

from daemon.classify import classify
from daemon.tmux import Pane


def _pane(cmd="bash"):
    return Pane("work", "0", "bash", "0", "%0", cmd, "t", "/home/x/proj")


def _llm(payload):
    return lambda system, text: payload


def test_pipes_llm_json_through():
    r = classify(_pane(), "…", _llm({
        "tool": "claude", "activity": "running", "headline": "Editing models.py",
        "model": "Opus 4.8", "notable": ["ran tests", "8 passed"],
    }))
    assert r["tool"] == "claude" and r["headline"] == "Editing models.py"
    assert r["notable"] == ["ran tests", "8 passed"]  # passed straight through
    assert r["pane_id"] == "%0" and r["label"] == "work"  # merged in (session name)


def test_question_forces_waiting():
    r = classify(_pane(), "…", _llm({"activity": "running",
                 "question": {"prompt": "Proceed?", "options": ["yes", "no"]}}))
    assert r["activity"] == "waiting"


def test_rewind_forces_waiting():
    r = classify(_pane(), "…", _llm({"activity": "running",
                 "rewind": {"entries": [{"text": "x", "selected": True}]}}))
    assert r["activity"] == "waiting"


def test_no_llm_fallback_idle_shell():
    r = classify(_pane("bash"), "shapor@host:~/proj$ ", llm_fn=None)
    assert r["tool"] == "shell" and r["activity"] == "idle"


def test_no_llm_fallback_running():
    r = classify(_pane("node"), "streaming output...\nmore", llm_fn=None)
    assert r["activity"] == "running"
