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
from pathlib import Path

from .tmux import Pane

# Cheap fast-path only (NOT semantic parsing): a bare shell prompt at the tail lets the
# watcher/fallback call an obviously-idle shell "idle" without an LLM call.
_SHELL_PROMPT_RE = re.compile(r"[\w.-]+@[\w.-]+.*[$#]\s*$")

# The production parser prompt lives in parser_prompt.txt (a load-bearing ~120-line
# artifact — kept as its own file so it can be edited/diffed as prose, not wrangled
# inside a Python string). Stable between edits ⇒ hits Gemini's context cache.
# research/probe.py loads the SAME file so the two never drift. Re-read on mtime
# change: an import-time constant silently served STALE prompts after edits, because
# uvicorn's stat reloader only watches *.py (reload_includes needs watchfiles).
_PROMPT_PATH = Path(__file__).with_name("parser_prompt.txt")
_prompt_cache: tuple[float, str] = (0.0, "")


def parser_prompt() -> str:
    global _prompt_cache
    mtime = _PROMPT_PATH.stat().st_mtime_ns  # ns: coarse mtime can miss rapid edits
    if mtime != _prompt_cache[0]:
        _prompt_cache = (mtime, _PROMPT_PATH.read_text(encoding="utf-8").strip())
    return _prompt_cache[1]


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


def _with_recent_events(text: str, recent: list[str]) -> str:
    """Append the events we ALREADY reported for this pane, so the model emits only
    genuinely NEW events instead of restating ongoing work in different words each
    parse. This deduplicates the activity log at the source (the model knows what it
    already said) rather than the client guessing whether two phrasings mean the same."""
    if not recent:
        return text
    already = "\n".join(f"- {e}" for e in recent[-20:])
    return (
        f"{text}\n\n[events already reported for this pane — do NOT repeat these or "
        f"restate the same action in different words; only add events for genuinely "
        f"new activity since them]\n{already}"
    )


def classify(
    pane: Pane, text: str, llm_fn=None, prior: list[str] | None = None,
    recent_events: list[str] | None = None,
) -> dict:
    """Parse `pane` into a plain dict for the UI. `llm_fn(system, text) -> dict|None`
    is the Gemini parser. `prior` = recent prior captures (continuity); `recent_events`
    = events already reported (so the model doesn't repeat them). Returns the model's
    JSON with pane_id/label merged in; on no/failed LLM a minimal heuristic dict."""
    payload = _with_recent_events(_with_prior(text, prior or []), recent_events or [])
    result = llm_fn(parser_prompt(), payload) if llm_fn else None
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
    # Prefer the agent's own session name (read from the pane by the LLM, e.g.
    # "tmux-rc-dev") over the tmux-derived label — it's what the user recognizes.
    sess = result.get("session")
    result["label"] = str(sess)[:40] if isinstance(sess, str) and sess.strip() else pane.label
    return result
