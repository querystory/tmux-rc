"""Turn raw pane text into a PaneState.

Heuristics run first and always (free, instant). The LLM is a lazy fallback, invoked
only when heuristics can't produce a clean status phrase or can't parse a detected
prompt — and only when the pane changed. See docs/DESIGN.md for the rationale.
"""

from __future__ import annotations

import re

from .models import PaneState, Question, Tool
from .tmux import Pane

# --- prompt / waiting detection -------------------------------------------------

# Tail patterns that indicate the pane is blocked waiting for a human answer.
_YN_RE = re.compile(r"\[y/n\]|\(y/n\)|\byes/no\b|\[Y/n\]|\[y/N\]", re.IGNORECASE)
_QUESTION_TAIL_RE = re.compile(r"\?\s*$")
# A numbered menu: lines like "1. Foo" / "2) Bar" / "❯ 1. Baz".
_MENU_ITEM_RE = re.compile(r"^\s*[❯>]?\s*(\d+)[.)]\s+(.*\S)\s*$")
# Claude Code's context-window status line, e.g. "Context left: 47%".
_CONTEXT_RE = re.compile(r"context[^0-9%]{0,20}(\d{1,3})\s*%", re.IGNORECASE)
# A generic shell prompt at the tail (user@host:path$ / #).
_SHELL_PROMPT_RE = re.compile(r"[\w.-]+@[\w.-]+.*[$#]\s*$")


def _detect_tool(pane: Pane, text: str) -> Tool:
    """Prefer the running command; fall back to banners in the pane text."""
    cmd = pane.current_command.lower()
    if "claude" in cmd:
        return "claude"
    if "codex" in cmd:
        return "codex"
    if "gemini" in cmd:
        return "gemini"
    low = text.lower()
    if "claude code" in low or "anthropic" in low:
        return "claude"
    if "codex" in low:
        return "codex"
    if cmd in ("bash", "zsh", "sh", "fish"):
        return "shell"
    return "unknown"


def _detect_question(text: str) -> Question | None:
    """Look at the last non-empty lines for a prompt awaiting input."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    tail = "\n".join(lines[-6:])
    last = lines[-1]

    # Numbered menu: collect consecutive trailing "N. option" lines.
    options: list[str] = []
    for ln in lines[-12:]:
        m = _MENU_ITEM_RE.match(ln)
        if m:
            options.append(m.group(2))
    if options:
        # Prompt = the last line before the menu that isn't itself a menu item.
        prompt = next(
            (ln.strip() for ln in reversed(lines) if not _MENU_ITEM_RE.match(ln)),
            "Select an option",
        )
        return Question(prompt=prompt, options=options)

    if _YN_RE.search(tail):
        return Question(prompt=last.strip(), options=["yes", "no"])
    if _QUESTION_TAIL_RE.search(last):
        return Question(prompt=last.strip(), options=[])
    return None


def _detect_context_pct(text: str) -> int | None:
    m = _CONTEXT_RE.search(text)
    if not m:
        return None
    pct = int(m.group(1))
    return pct if 0 <= pct <= 100 else None


def _short_status(text: str) -> str:
    """Last meaningful line, trimmed — the cheap 'what's happening' phrase. Skips
    lines that are pure box-drawing / punctuation (Claude Code's UI frames), which
    carry no information and should defer to the LLM."""
    for ln in reversed(text.splitlines()):
        s = ln.strip()
        if s and _has_words(s):
            return s[:120]
    return ""


def _has_words(s: str) -> bool:
    """True if the line contains real alphanumeric content (not just box art)."""
    return bool(re.search(r"[A-Za-z0-9]", s))


def _has_frame(text: str) -> bool:
    """True if recent lines contain box-drawing characters — a framed TUI prompt
    (e.g. Claude Code's confirmation dialog) that line-based rules likely miss."""
    return bool(re.search(r"[│╭╮╰╯─┌┐└┘|]", "\n".join(text.splitlines()[-8:])))


def classify(
    pane: Pane,
    text: str,
    prev_text: str | None,
    idle_seconds: int,
    llm_fn=None,
) -> PaneState:
    """Build a PaneState. `llm_fn(system, text) -> dict|None` is the lazy pass; when
    None, heuristics-only. `idle_seconds` is how long the text has been unchanged."""
    tool = _detect_tool(pane, text)
    question = _detect_question(text)
    context_pct = _detect_context_pct(text)
    status = _short_status(text)
    changed = text != prev_text

    if question is not None:
        activity = "waiting"
    elif not changed and _SHELL_PROMPT_RE.search(text):
        activity = "idle"
    elif changed:
        activity = "running"
    else:
        activity = "idle"

    state = PaneState(
        pane_id=pane.id,
        label=pane.label,
        tool=tool,
        activity=activity,
        idle_seconds=idle_seconds,
        status_line=status,
        context_pct=context_pct,
        question=question,
    )

    # Lazy LLM pass: only when it can actually add value. Fires when the pane changed
    # and heuristics are weak — no clean status, an unparsed prompt, or a framed
    # (box-drawing) UI where an in-frame prompt likely hides from line-based rules.
    weak = (
        not status
        or (question is not None and not question.options)
        or (question is None and _has_frame(text))
    )
    if changed and weak and llm_fn is not None:
        _apply_llm(state, text, llm_fn)

    return state


_LLM_SYSTEM = (
    "You read a snapshot of a terminal pane and report its state as compact JSON. "
    "Output ONLY an object with keys: "
    '"status_line" (a short phrase, <=80 chars, describing what is happening, e.g. '
    '"editing models.py" or "running tests 14/52"), '
    '"activity" (one of "running","idle","waiting"), and optionally '
    '"question" (an object {"prompt": string, "options": [string,...]}) when the '
    "terminal is blocked waiting for the user to choose or confirm. Omit question "
    "if nothing is waiting. Options should be the literal choices the user can pick."
)


def _apply_llm(state: PaneState, text: str, llm_fn) -> None:
    """Merge an LLM classification into a heuristic PaneState, without overwriting
    good heuristic values with empty ones."""
    result = llm_fn(_LLM_SYSTEM, text[-4000:])
    if not isinstance(result, dict):
        return
    if s := result.get("status_line"):
        state.status_line = str(s)[:120]
    if a := result.get("activity"):
        if a in ("running", "idle", "waiting"):
            state.activity = a
    q = result.get("question")
    if isinstance(q, dict) and q.get("prompt"):
        state.question = Question(
            prompt=str(q["prompt"]),
            options=[str(o) for o in q.get("options", []) if o],
        )
        state.activity = "waiting"
