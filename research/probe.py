"""Research harness: how well does Flash Lite "see" a terminal pane?

Captures the current (or a target) tmux pane three ways — plain text, rendered PNG,
and text+image — sends each to Flash Lite with the same parser prompt, and prints the
JSON each returns plus token counts. Lets us compare input modes empirically before
committing the architecture.

Usage (from repo root, venv active, GOOGLE_CLOUD_PROJECT set):
    python -m research.probe [pane_target]     # e.g. %0  or  session:win
    python -m research.probe --save NAME        # also save the capture+png to research/samples/
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from daemon import tmux
from daemon.llm import _MODEL, _client
from daemon.render import render_png

SAMPLES = Path(__file__).parent / "samples"

# The parser prompt under test. Describes the medium, teaches general TUI patterns,
# then gives a Claude Code section (semantic, not exact-string), and asks for UI JSON.
PROMPT = """
You are the parser behind a phone dashboard that mirrors what's happening in a
developer's terminal panes so they can watch and control coding agents from their
phone. You are given a snapshot of ONE terminal pane — as text, as an image, or both.
It is a fixed-width character grid; treat alignment/columns and (if an image) color
and bold as meaningful signal.

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
rounded input box drawn with box characters, clay/orange accents, and a two-line
STATUS LINE near the bottom that typically shows — in order — the working directory
and git branch, the model name (e.g. "Opus 4.8"), a context-window percentage, an
elapsed session time, a session cost in dollars, and counts of prompts/tools; plus a
permission-mode indicator ("plan mode", "accept edits", "bypass permissions"). While
working it shows a whimsical gerund + elapsed time + tokens streamed (e.g.
"Shimmying… (6m 55s · ↓18.1k tokens)"). After Esc-Esc it shows a REWIND picker: a
header about restoring to a previous point, then a scrollable list of past user
messages (each may note "No code changes" or a file/diff stat like "app.js +18 -8"),
one selected, with "N more above/below" markers. It may also show a task/TODO
checklist. Ignore editor/multiplexer chrome that isn't the agent's own state (vim's
"-- INSERT --", tmux's mode footer).

Return ONLY compact JSON. Include a field only if you can determine it — do not guess:
{
  "tool": "claude"|"codex"|"gemini"|"shell"|"unknown",
  "activity": "running"|"waiting"|"idle",
  "headline": "one short human sentence for the top of the card: what is happening / "
              "what is being worked on (prefer the task over echoing a spinner word)",
  "model": "...", "context_pct": <int>, "cost": "$...", "mode": "plan|accept-edits|bypass|normal",
  "working": {"verb": "...", "elapsed": "...", "tokens": "..."},
  "question": {"prompt": "...", "options": ["..."]},
  "rewind": {"entries": [{"text": "...", "note": "...", "selected": true}], "more_above": <int>, "more_below": <int>},
  "tasks": [{"text": "...", "done": true|false}],
  "notable": ["short bullets of anything else useful to show — errors (note if red), "
              "test results, files changed, etc."]
}
""".strip()


def _capture_ansi(pane_id: str) -> str:
    return subprocess.run(
        ["tmux", "capture-pane", "-p", "-e", "-J", "-t", pane_id, "-S", "-50"],
        capture_output=True, text=True,
    ).stdout


# Gemini 3.x Flash Lite pricing (USD per 1M tokens). Update if it changes; used only
# for a rough per-call cost estimate in this research harness.
_IN_PER_M, _OUT_PER_M = 0.10, 0.40


def _parse(parts) -> dict:
    """Run one parse and return quality + tokens + latency + cost estimate."""
    import time

    from google.genai import types

    client = _client()
    in_tokens = client.models.count_tokens(model=_MODEL, contents=parts).total_tokens
    t0 = time.time()
    resp = client.models.generate_content(
        model=_MODEL, contents=parts,
        config=types.GenerateContentConfig(
            system_instruction=PROMPT, response_mime_type="application/json", temperature=0.0
        ),
    )
    latency = time.time() - t0
    out_tokens = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
    cost = in_tokens / 1e6 * _IN_PER_M + out_tokens / 1e6 * _OUT_PER_M
    try:
        result = json.loads(resp.text)
    except Exception:
        result = {"_raw": resp.text}
    return {"json": result, "in": in_tokens, "out": out_tokens, "latency": latency, "cost": cost}


def main() -> None:
    from google.genai import types

    save_name = None
    argv = sys.argv[1:]
    if "--save" in argv:
        i = argv.index("--save")
        save_name = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]  # drop --save and its value
    target = argv[0] if argv else None

    pane = tmux.find_pane(target)
    if pane is None:
        print("no pane found")
        return
    plain = tmux.capture_pane(pane.id)
    ansi = _capture_ansi(pane.id)
    png = render_png(ansi)

    if save_name:
        SAMPLES.mkdir(parents=True, exist_ok=True)
        (SAMPLES / f"{save_name}.txt").write_text(plain)
        (SAMPLES / f"{save_name}.png").write_bytes(png)
        print(f"saved sample '{save_name}' to {SAMPLES}\n")

    img = types.Part.from_bytes(data=png, mime_type="image/png")
    modes = {
        "TEXT      ": [plain],
        "IMAGE     ": [img],
        "TEXT+IMAGE": [plain, img],
    }
    print(f"pane {pane.id} ({pane.label})  —  PNG {len(png) // 1024}KB\n")
    rows = []
    for name, parts in modes.items():
        m = _parse(parts)
        rows.append((name, m))
        print(f"===== {name}  ({m['in']} in / {m['out']} out tok · "
              f"{m['latency']:.1f}s · ${m['cost'] * 1000:.3f}/1k calls) =====")
        print(json.dumps(m["json"], indent=2, ensure_ascii=False))
        print()
    print("===== SUMMARY =====")
    print(f"{'mode':12} {'in':>6} {'out':>5} {'latency':>8} {'$/1k calls':>11}")
    for name, m in rows:
        print(f"{name:12} {m['in']:>6} {m['out']:>5} {m['latency']:>7.1f}s {m['cost'] * 1000:>10.3f}")


if __name__ == "__main__":
    main()
