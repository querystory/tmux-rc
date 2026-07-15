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
_MENU_ITEM_RE = re.compile(r"^\s*[❯>›]?\s*(\d+)[.)]\s+(.*\S)\s*$")
# A selection cursor on a menu item — a strong "live picker" signal prose never has.
_CURSOR_RE = re.compile(r"^\s*[❯›>]\s*\d")
# Claude Code's context-window status line, e.g. "Context left: 47%".
_CONTEXT_RE = re.compile(r"context[^0-9%]{0,20}(\d{1,3})\s*%", re.IGNORECASE)
# A generic shell prompt at the tail (user@host:path$ / #).
_SHELL_PROMPT_RE = re.compile(r"[\w.-]+@[\w.-]+.*[$#]\s*$")


# Signatures of an agent in the transcript, so a pane keeps its identity even when
# the agent shells out (foreground command briefly becomes git/grep/bash).
_CLAUDE_SIG = re.compile(
    r"claude code|✳|✻|✶|✷|context left|bypass permissions|accept edits|shift\+tab to cycle",
    re.IGNORECASE,
)


def _detect_tool(pane: Pane, text: str) -> Tool:
    """Identify the tool. Prefer the running command, then agent signatures in the
    transcript. Returns "unknown" for a bare subprocess so the caller's sticky logic
    can keep a previously-known agent identity instead of flapping to "shell"."""
    cmd = pane.current_command.lower()
    if "claude" in cmd:
        return "claude"
    if "codex" in cmd:
        return "codex"
    if "gemini" in cmd:
        return "gemini"
    low = text.lower()
    if _CLAUDE_SIG.search(text) or "anthropic" in low:
        return "claude"
    if "codex" in low:
        return "codex"
    if cmd in ("bash", "zsh", "sh", "fish"):
        return "shell"
    return "unknown"


def _detect_question(text: str) -> Question | None:
    """Detect a prompt genuinely awaiting input. Deliberately CONSERVATIVE: a full
    agent transcript scrolls prose that ends in '?' or has '1.'/'2.' numbering, which
    must NOT be mistaken for a live prompt. We only fire on a numbered menu whose block
    reaches the last line, or a y/N affordance ON the last line. Ambiguous framed
    prompts are left to the LLM pass (see _has_frame)."""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return None
    last = lines[-1]

    # Numbered menu. Two ways to qualify as a LIVE picker (vs. scrollback numbering in
    # a transcript): the block reaches the bottom (within a line), OR one of the items
    # carries a selection cursor (❯/›/>), which prose never has. Either way, reject if
    # a shell prompt appears below (that means it was already answered).
    menu_idxs = [i for i, ln in enumerate(lines) if _MENU_ITEM_RE.match(ln)]
    last_menu_idx = menu_idxs[-1] if menu_idxs else -1
    has_cursor = any(_CURSOR_RE.match(lines[i]) for i in menu_idxs)
    near_bottom = last_menu_idx >= len(lines) - 2
    if last_menu_idx != -1 and (near_bottom or has_cursor):
        below = lines[last_menu_idx + 1 :]
        if not any(_SHELL_PROMPT_RE.search(ln) for ln in below):
            options = []
            i = last_menu_idx
            while i >= 0 and _MENU_ITEM_RE.match(lines[i]):
                options.insert(0, _MENU_ITEM_RE.match(lines[i]).group(2))
                i -= 1
            if len(options) >= 2:  # a single "1." line is not a menu
                prompt = _clean_prompt(lines[i]) if i >= 0 else "Select an option"
                return Question(prompt=prompt, options=options)

    # y/N only when it's the affordance on the very last line.
    if _YN_RE.search(last):
        return Question(prompt=_clean_prompt(last), options=["yes", "no"])
    return None


def _clean_prompt(line: str) -> str:
    """Trim a trailing shell prompt that bled onto the same line as a prompt (common
    when a tool prints a question without a newline before the shell redraws)."""
    return _SHELL_PROMPT_RE.sub("", line).strip() or line.strip()


def _detect_context_pct(text: str) -> int | None:
    m = _CONTEXT_RE.search(text)
    if not m:
        return None
    pct = int(m.group(1))
    return pct if 0 <= pct <= 100 else None


# Editor / multiplexer chrome that is NOT the agent's state (vim mode, tmux footer).
_NOISE_RE = re.compile(r"--\s*(INSERT|NORMAL|VISUAL|REPLACE)\s*--|shift\+tab to cycle", re.I)


def _short_status(text: str) -> str:
    """Last meaningful line, trimmed — the cheap 'what's happening' phrase. Skips
    box-drawing frames and editor/multiplexer chrome (vim '-- INSERT --', tmux
    footers), which carry no info and should defer to the LLM."""
    for ln in reversed(text.splitlines()):
        s = ln.strip()
        if s and _has_words(s) and not _NOISE_RE.search(s):
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
    force_llm: bool = False,
) -> PaneState:
    """Build a PaneState. `llm_fn(system, text) -> dict|None` is the lazy pass; when
    None, heuristics-only. `idle_seconds` is how long the text has been unchanged.
    `force_llm` asks for a proactive pass even when heuristics look fine (see below)."""
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

    # Lazy LLM pass. Two triggers:
    #   1. The pane changed and heuristics are weak — no clean status, an unparsed
    #      prompt, or a framed (box-drawing) UI likely hiding an in-frame prompt.
    #   2. force_llm — the watcher asks for a proactive pass (e.g. a pane that has
    #      been idle a while and hasn't been LLM-classified on its current content).
    #      When in doubt on a stable screen, a cheap pass beats a wrong heuristic.
    weak = (
        not status
        or (question is not None and not question.options)
        or (question is None and _has_frame(text))
    )
    if llm_fn is not None and (force_llm or (changed and weak)):
        _apply_llm(state, text, llm_fn)

    return state


_LLM_SYSTEM = (
    "You read a snapshot of a terminal pane running a coding agent (usually Claude "
    "Code) or a shell, and report its state as compact JSON. Coding agents render a "
    "status line at the bottom (model, context %, cost, session stats) and, while "
    "working, a line like '✳ Cultivating… (11m46s · ↓13.3k tokens)'. IGNORE editor/"
    "multiplexer chrome such as vim's '-- INSERT --' or tmux mode footers — that is "
    "not the agent's state. Output ONLY a JSON object with these keys (omit any you "
    "cannot determine, do not guess):\n"
    '"status_line": short phrase (<=80 chars) of what is happening now. Prefer '
    "summarizing WHAT is being worked on over echoing the spinner word — e.g. "
    "\"Cultivating — editing classify.py\" beats \"Cultivating…\". For a plain shell, "
    'describe the running or last command and whether it finished, e.g. "running '
    'make test (14/52)", "git rebase in progress", "$ idle at prompt".\n'
    '"activity": one of "running" (spinner/Thinking/streaming/a working line with an '
    'elapsed timer), "waiting" (blocked, needs the user to type or choose), "idle" '
    "(bare shell prompt, nothing happening).\n"
    '"model": the model name if shown, e.g. "Sonnet 5", "Opus 4.8".\n'
    '"context_pct": integer percent of context shown (e.g. 78 from "78% ctx").\n'
    '"cost": dollar cost if shown, e.g. "$10.64".\n'
    '"mode": one of "plan","accept-edits","bypass","normal" — from indicators like '
    '"plan mode on", "accept edits on", "bypass permissions on"; else "normal".\n'
    '"working_verb": the whimsical gerund shown while working, e.g. "Cultivating".\n'
    '"elapsed": elapsed working time if shown, e.g. "11m46s".\n'
    '"tokens": tokens streamed if shown, e.g. "13.3k".\n'
    '"agents": integer count of running sub-agents/parallel tasks if the agent shows '
    "them (e.g. a task list or 'N agents running'); else 0.\n"
    '"question": {"prompt": string, "options": [string,...]} — include this ONLY if '
    "the agent has STOPPED and is presenting an interactive prompt the user must "
    "answer RIGHT NOW: a boxed confirmation dialog, a selection list with a "
    "highlighted cursor (❯) on the choices, or an explicit input line at the very "
    "bottom awaiting text. This is rare. Do NOT invent a question from ordinary "
    "output: an agent's prose that merely contains a question mark, a bulleted or "
    "numbered list inside its reasoning/plan, or a completed menu with output below "
    "it is NOT a question — omit the key and use activity 'running' or 'idle'. When "
    "in doubt, OMIT question."
)

_MODES = {"plan", "accept-edits", "bypass", "normal"}


def _apply_llm(state: PaneState, text: str, llm_fn) -> None:
    """Merge an LLM classification into a heuristic PaneState, without overwriting
    good heuristic values with empty ones."""
    result = llm_fn(_LLM_SYSTEM, text[-4000:])
    if not isinstance(result, dict):
        return
    if s := result.get("status_line"):
        state.status_line = str(s)[:120]
    if (a := result.get("activity")) in ("running", "idle", "waiting"):
        state.activity = a
    if m := result.get("model"):
        state.model = str(m)[:40]
    if isinstance(result.get("context_pct"), int):
        pct = result["context_pct"]
        state.context_pct = pct if 0 <= pct <= 100 else None
    if c := result.get("cost"):
        state.cost = str(c)[:16]
    if (mode := result.get("mode")) in _MODES:
        state.mode = mode
    for field in ("working_verb", "elapsed", "tokens"):
        if v := result.get(field):
            setattr(state, field, str(v)[:24])
    if isinstance(result.get("agents"), int) and result["agents"] > 0:
        state.agents = result["agents"]
    q = result.get("question")
    if isinstance(q, dict) and q.get("prompt"):
        state.question = Question(
            prompt=str(q["prompt"]),
            options=[str(o) for o in q.get("options", []) if o],
        )
        state.activity = "waiting"
