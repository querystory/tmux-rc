"""classify() is now a raw-JSON pipe: it returns the LLM's dict (plus pane_id/label),
with a waiting-override for question/rewind and a no-LLM heuristic fallback."""

from daemon.classify import bootstrap, classify
from daemon.tmux import Pane


def _pane(cmd="bash"):
    return Pane("work", "0", "bash", "0", "%0", cmd, "t", "/home/x/proj")


def _llm(payload):
    return lambda system, text: payload


def test_bootstrap_shapes_result_and_flags_history():
    r = bootstrap(_pane(), "…", _llm({
        "name": "  tmux-rc overhaul  ",
        "summary": " shipping PRs #24 and #26 ",
        "events": [{"text": "Merged PR #24"}, {"junk": 1}, "nope"],
    }))
    assert r["name"] == "tmux-rc overhaul"
    assert r["summary"] == "shipping PRs #24 and #26"
    assert r["events"] == [{"text": "Merged PR #24", "historical": True}]


def test_bootstrap_rejects_junk():
    assert bootstrap(_pane(), "…", _llm(["not a dict"])) is None
    assert bootstrap(_pane(), "…", _llm({"summary": 3})) is None
    assert bootstrap(_pane(), "…", lambda s, t: None) is None


def test_payload_leads_with_foreground_process():
    seen = {}

    def llm(system, text):
        seen["text"] = text
        return {"tool": "shell", "activity": "idle"}

    classify(_pane(cmd="python3"), "some screen", llm)
    first_line = seen["text"].splitlines()[0]
    assert "foreground process" in first_line and "python3" in first_line


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
