"""LLM-first classifier tests. The LLM is the parser, so we test the mapping from its
JSON onto PaneState (via a stub llm_fn) and the no-LLM fallback — NOT regex parsing."""

from termiphone.classify import classify
from termiphone.tmux import Pane


def _pane(cmd="bash", win="bash"):
    return Pane("work", "0", win, "0", "%0", cmd, "t", "/home/x/proj")


def _llm(payload):
    """A stub parser that returns a fixed JSON payload."""
    return lambda system, text: payload


def test_maps_status_and_metadata():
    s = classify(
        _pane(),
        "…",
        _llm({
            "tool": "claude", "activity": "running", "status_line": "Editing models.py",
            "model": "Opus 4.8", "context_pct": 47, "cost": "$10.64", "mode": "bypass",
            "working_verb": "Cultivating", "elapsed": "11m46s", "tokens": "13.3k", "agents": 2,
        }),
    )
    assert s.tool == "claude" and s.activity == "running"
    assert s.status_line == "Editing models.py"
    assert s.model == "Opus 4.8" and s.context_pct == 47 and s.cost == "$10.64"
    assert s.mode == "bypass" and s.working_verb == "Cultivating" and s.agents == 2


def test_question_sets_waiting():
    s = classify(_pane(), "…", _llm({"activity": "running",
                 "question": {"prompt": "Proceed?", "options": ["yes", "no"]}}))
    assert s.activity == "waiting"
    assert s.question and s.question.options == ["yes", "no"]


def test_rewind_sets_waiting_and_entries():
    s = classify(_pane(), "…", _llm({"rewind": {
        "entries": [
            {"text": "did a thing", "note": "No code changes", "selected": False},
            {"text": "did another", "note": "app.js +18 -8", "selected": True},
        ],
        "more_above": 40, "more_below": 1,
    }}))
    assert s.activity == "waiting"
    assert s.rewind and len(s.rewind.entries) == 2
    assert s.rewind.entries[1].selected and s.rewind.more_above == 40


def test_invalid_enum_ignored():
    # A bogus activity/mode from the LLM must not corrupt the state.
    s = classify(_pane(), "…", _llm({"activity": "bogus", "mode": "nonsense",
                                     "status_line": "hi"}))
    assert s.activity != "bogus"  # left at default
    assert s.mode != "nonsense"
    assert s.status_line == "hi"


def test_no_llm_fallback_idle_shell():
    # No parser available: crude idle/running guess only, no fake semantic parsing.
    s = classify(_pane(cmd="bash"), "shapor@host:~/proj$ ", llm_fn=None)
    assert s.tool == "shell"
    assert s.activity == "idle"
    assert s.question is None and s.rewind is None


def test_no_llm_fallback_running():
    s = classify(_pane(cmd="node"), "Some streaming output...\nmore output", llm_fn=None)
    assert s.activity == "running"
