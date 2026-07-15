"""LLM-first classification, raw-JSON pipe.

The LLM is the parser AND the schema. We send the visible pane text to Gemini Flash
Lite with one layered prompt and pass its JSON straight through to the UI — no typed
schema in the middle. New prompt fields light up in the frontend with no backend
change. Research (see research/README.md) showed text-only beats image on accuracy,
cost, and latency across working / rewind-picker / error screens, so text is the
hot-path input; the image switch stays wired but off.

`classify` returns a plain dict: the model's JSON, plus watcher-managed fields
(pane_id, label, idle_seconds, snapshot_id, updated_at). On no/failed LLM it returns a
minimal dict (idle vs running) so the pipe never breaks.
"""

from __future__ import annotations

import re

from .tmux import Pane

# Cheap fast-path only (NOT semantic parsing): a bare shell prompt at the tail lets the
# watcher/fallback call an obviously-idle shell "idle" without an LLM call.
_SHELL_PROMPT_RE = re.compile(r"[\w.-]+@[\w.-]+.*[$#]\s*$")

# The production parser prompt. Validated in research/probe.py. General TUI patterns
# first, then a Claude Code section described semantically (not by exact strings), and
# a JSON shape the UI renders directly. Static ⇒ hits Gemini's context cache.
PARSER_PROMPT = """
You are the parser behind a phone dashboard that mirrors what's happening in a
developer's terminal panes so they can watch and control coding agents from their
phone. You are given one or more recent snapshots of ONE terminal pane as text, in
time order and labeled ("[earlier frame -N]" … "[current frame]"). Report the state
for the CURRENT frame; use the earlier frames only for continuity (stay consistent as
output trickles in — don't re-decide from scratch) and to describe what just happened
in "notable". A pane is a fixed-width character grid; treat alignment/columns as
meaningful.

Describe what is happening in a way that lets the phone UI light up usefully. Reason
about GENERAL terminal patterns by how they look/behave, NOT by exact wording (which
changes across versions and tools):
- Is the pane actively working (a spinner, a "thinking" word, an elapsed timer,
  streaming output), waiting for the user to answer something, or idle at a prompt?
- Is there a SELECTION MENU — a list of choices with one marked by a cursor/highlight
  (❯, ›, >, an arrow, a bold or colored line) that you'd move through with up/down?
- Is there an INPUT PROMPT — a yes/no confirmation, a numbered choice, or a free-text
  field the user must fill?

This is very often CLAUDE CODE (an AI coding agent). Recognize it by its style: a
rounded input box drawn with box characters, and a two-line STATUS LINE near the
bottom that typically shows — in order — the working directory and git branch, the
model name (e.g. "Opus 4.8"), a context-window percentage, an elapsed session time, a
session cost in dollars, and counts of prompts/tools; plus a permission-mode indicator
("plan mode", "accept edits", "bypass permissions"). While working it shows a whimsical
gerund + elapsed time + tokens streamed (e.g. "Shimmying… (6m 55s · ↓18.1k tokens)").
After Esc-Esc it shows a REWIND picker: a header about restoring to a previous point,
then a scrollable list of past user messages (each may note "No code changes" or a
file/diff stat like "app.js +18 -8"), one selected, with "N more above/below" markers.
It may also show a task/TODO checklist. Ignore editor/multiplexer chrome that isn't the
agent's own state (vim's "-- INSERT --", tmux's mode footer).

Return ONLY compact JSON. Include a field only if you can determine it — do not guess:
{
  "tool": "claude"|"codex"|"gemini"|"shell"|"unknown",
  "activity": "running"|"waiting"|"idle",
  "headline": "one short human sentence for the top of the card: what is happening / "
              "what is being worked on (prefer the task over echoing a spinner word)",
  "model": "...", "context_pct": <int>, "cost": "$...", "mode": "plan|accept-edits|bypass|normal",
  "working": {"verb": "...", "elapsed": "...", "tokens": "..."},
  "question": {"prompt": "...", "options": ["..."], "answer_style": "text"|"menu"},
  "rewind": {"entries": [{"text": "...", "note": "...", "selected": true}], "more_above": <int>, "more_below": <int>},
  "tasks": [{"text": "...", "done": true|false}],
  "events": [
    {"text": "WHAT happened, human phrase — e.g. 'Reworked the file list into an activity view'",
     "file": {"path": "...", "added": <int>, "removed": <int>},   // OPTIONAL: if this event was a file edit
     "meta": "OPTIONAL short side-note for non-file events, e.g. 'ran 3 commands', 'exit 1'"}
  ]
}
"events" is the activity feed — a list of "what the thing did", newest-relevant first.
The PRIMARY content of each event is "text" (what happened, plain language). Attach
OPTIONAL metadata: "file" when the event was editing a file (path + lines +/-), or
"meta" for a short programmatic side-note (command count, exit code). The UI shows
`text` as the main line and file/meta as a small right-justified side-note. So a file
edit is just an event whose metadata is a file diff — do NOT emit a bare "added 4 lines
to X"; make text say what changed and put the numbers in "file". Do NOT put things that
have their own dedicated field (mode, model, cost, context %, tool, tasks, question,
rewind) into events.
Include "question" only when genuinely blocked awaiting input; "rewind" only when the
restore picker is shown; "tasks" only when a checklist is visible.

"question.answer_style" tells the phone HOW to send an answer — this matters a lot:
- "text": the agent asked a plain natural-language question and is waiting for a typed
  reply (NOT a widget). Options you list are suggested answers; the phone will TYPE the
  chosen option's text and press Enter. Use this for Claude Code's normal conversational
  questions.
- "menu": the terminal is showing an actual interactive selection widget (a numbered
  list you press a digit for, or a cursor list you arrow through) — the options map to
  keystrokes, not typed text. Use this only for a real on-screen menu/picker.
When unsure, prefer "text" (typing the answer is safe; sending a stray digit is not).
""".strip()


def _obvious_idle(text: str) -> bool:
    for ln in reversed(text.splitlines()):
        if ln.strip():
            return bool(_SHELL_PROMPT_RE.search(ln))
    return False


def _with_prior(text: str, prior: list[str]) -> str:
    """Prepend recent prior captures (oldest→newest) as labeled context, so the model
    sees the trajectory. This keeps classification STABLE when content trickles in one
    line at a time (no re-deciding from scratch each frame → no flicker) and gives it
    material to describe what JUST happened. Prompt tokens are cheap, so this is nearly
    free. The LAST block is the current screen — the one to report state for."""
    if not prior:
        return text
    blocks = [f"[earlier frame -{len(prior) - i}]\n{p}" for i, p in enumerate(prior)]
    blocks.append(f"[current frame — report state for THIS one]\n{text}")
    return "\n\n".join(blocks)


def classify(pane: Pane, text: str, llm_fn=None, prior: list[str] | None = None) -> dict:
    """Parse `pane` into a plain dict for the UI. `llm_fn(system, text) -> dict|None`
    is the Gemini parser. `prior` is recent prior captures (oldest→newest) for
    continuity. Returns the model's JSON with pane_id/label merged in; on no/failed
    LLM, a minimal heuristic dict (idle vs running) so the pipe never breaks."""
    result = llm_fn(PARSER_PROMPT, _with_prior(text, prior or [])) if llm_fn else None
    if not isinstance(result, dict):
        result = {
            "tool": "shell" if pane.current_command in ("bash", "zsh", "sh", "fish") else "unknown",
            "activity": "idle" if _obvious_idle(text) else "running",
        }
    # A detected question/rewind means the pane is waiting, regardless of what the
    # model put in "activity" — this is the one bit of logic we keep out of the model.
    if result.get("question") or result.get("rewind"):
        result["activity"] = "waiting"
    result["pane_id"] = pane.id
    result["label"] = pane.label
    return result
