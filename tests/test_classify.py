"""Heuristic classifier regression tests (no network / no LLM)."""

from termiphone.classify import classify
from termiphone.tmux import Pane


def _pane(cmd="bash", win="bash"):
    return Pane("work", "0", win, "0", "%0", cmd, "t")


def test_shell_idle():
    text = "shapor@host:~/src$ "
    s = classify(_pane(), text, prev_text=text, idle_seconds=30)
    assert s.tool == "shell"
    assert s.activity == "idle"


def test_yes_no_prompt():
    text = "Editing models.py\nDo you want to proceed? [y/N]"
    s = classify(_pane("claude", "claude"), text, prev_text="old", idle_seconds=0)
    assert s.tool == "claude"
    assert s.activity == "waiting"
    assert s.question and s.question.options == ["yes", "no"]


def test_live_menu_detected():
    text = "Which approach?\n  1. Refactor\n  2. New file\n  3. Skip\nEnter choice:"
    s = classify(_pane("claude"), text, prev_text="old", idle_seconds=0)
    assert s.activity == "waiting"
    assert s.question.prompt == "Which approach?"
    assert s.question.options == ["Refactor", "New file", "Skip"]


def test_answered_menu_not_waiting():
    # Output below the menu ⇒ the prompt was already answered.
    text = (
        "Which approach?\n  1. Refactor\n  2. New file\n  3. Skip\n"
        "Enter choice: 1\n>>> PROCEEDING WITH 1 <<<"
    )
    s = classify(_pane("claude"), text, prev_text="old", idle_seconds=0)
    assert s.question is None
    assert s.activity != "waiting"


def test_context_pct():
    text = "Context left: 47%\nAnalyzing codebase..."
    s = classify(_pane("claude"), text, prev_text="old", idle_seconds=0)
    assert s.context_pct == 47
    assert s.activity == "running"


def test_heuristic_cases_never_call_llm():
    def boom(*_):
        raise AssertionError("LLM must not be called for clean heuristic input")

    text = "Which approach?\n  1. A\n  2. B\nEnter choice:"
    classify(_pane("claude"), text, prev_text="old", idle_seconds=0, llm_fn=boom)
