"""A/B validation for the question-overfire fix (fix/waiting-overfire-on-agent-question).

Runs the affected + control pane captures, plus a genuine tool-held permission prompt,
through the REAL Vertex client under BASELINE vs PATCHED system prompt. Because
Flash-Lite varies run-to-run even at temperature 0, it reports the RATE at which a
`question` fires over N trials (default 5; pass an int arg to change). The `question`
object is what daemon/classify.py force-promotes to activity=waiting/waiting_on=user, so
its fire-rate IS the amber-badge signal. Proves:
  - AFFECTED panes lose the spurious `question` (agent's own trailing "?").
  - CONTROL panes are unchanged.
  - a GENUINE permission prompt STILL fires its `question` under the patched prompt.

A minimal DRY extension of research/probe.py — reuses the real daemon.llm client and
runs against FROZEN pane captures under research/samples/ (so both the baseline and the
patched call see the identical text — live panes drift between calls). Regenerate the
captures with `daemon.tmux.capture_pane(pid, mark_dim=True)` if you want a fresh set.
Run from the worktree root with the three Vertex env vars set:
    python -m research.validate_question_overfire
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from daemon.llm import _MODEL, _client

_HERE = Path(__file__).parent
_SAMPLES = _HERE / "samples"
_PROMPT = _HERE.parent / "daemon" / "parser_prompt.txt"
# PATCHED = the working-tree prompt (what ships). BASELINE = the same file at origin/main,
# read from git so there's no committed duplicate to drift — the A/B is prompt-vs-prompt.
PATCHED = _PROMPT.read_text(encoding="utf-8").strip()
_baseline_proc = subprocess.run(
    ["git", "show", "origin/main:daemon/parser_prompt.txt"],
    capture_output=True, text=True, cwd=_HERE.parent,
)
BASELINE = _baseline_proc.stdout.strip()
if _baseline_proc.returncode != 0 or not BASELINE:
    # An empty/failed baseline (no origin/main, unfetched, wrong cwd) would silently make
    # the A/B compare PATCHED against nothing — fail fast rather than report noise.
    raise SystemExit(
        "could not load baseline prompt from origin/main:daemon/parser_prompt.txt "
        f"(git rc={_baseline_proc.returncode}): {_baseline_proc.stderr.strip()[:200]}\n"
        "run `git fetch origin` from the worktree first."
    )

AFFECTED = ["%3", "%46", "%53", "%57"]
CONTROLS = ["%49", "%24", "%54", "%52", "%48", "%16"]


def _capture(pid: str) -> str:
    """A pane's frozen capture, if saved locally. Real captures are NOT committed (they
    hold live pane content); regenerate them with
    `daemon.tmux.capture_pane(pid, mark_dim=True)` before running against live panes."""
    return (_SAMPLES / f"pane_{pid.replace('%', 'pct')}.txt").read_text(encoding="utf-8")

# A genuine tool-held Claude Code permission box — the model MUST still fire a question
# for this under the patched prompt (the fix must not suppress a real prompt).
GENUINE_PROMPT_FIXTURE = _HERE / "samples" / "genuine_permission_prompt.txt"


def _classify(text: str, prompt: str) -> dict:
    from google.genai import types

    resp = _client().models.generate_content(
        model=_MODEL,
        contents=[text],
        config=types.GenerateContentConfig(
            system_instruction=prompt,
            response_mime_type="application/json",
            temperature=0.0,
        ),
    )
    try:
        return json.loads(resp.text)
    except json.JSONDecodeError as e:
        # A validation harness must NOT swallow a bad parse: counting it as "no question"
        # would silently deflate the fire-rate and fake a passing result. Surface it.
        raise RuntimeError(
            f"model returned non-JSON (len {len(resp.text or '')}): {(resp.text or '')[:200]!r}"
        ) from e


def _has_q(d: dict) -> bool:
    return bool(d.get("question"))


# Flash-Lite has run-to-run variance even at temperature 0, so a single sample is noise.
# `trials` is the number of samples per prompt; the reported RATE at which a `question`
# fires is the honest way to A/B a nondeterministic model. Default 5, overridable via argv
# (parsed in main() so importing this module has no argv side effects).
DEFAULT_TRIALS = 5


def _q_rate(text: str, prompt: str, trials: int) -> int:
    return sum(_has_q(_classify(text, prompt)) for _ in range(trials))


def _run_one(label: str, text: str, trials: int) -> tuple:
    b = _q_rate(text, BASELINE, trials)
    p = _q_rate(text, PATCHED, trials)
    print(f"  {label:34} baseline q {b}/{trials}   patched q {p}/{trials}")
    return label, b, p


def main() -> None:
    trials = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_TRIALS
    rows = []
    print(f"question-fire rate over {trials} trials each (temperature 0):\n")

    print("AFFECTED (agent's own trailing '?', idle editable box — should DROP the question):")
    for pid in AFFECTED:
        if (_SAMPLES / f"pane_{pid.replace('%', 'pct')}.txt").exists():
            rows.append(("AFFECTED", *_run_one(f"AFFECTED {pid}", _capture(pid), trials)))
        else:
            print(f"  AFFECTED {pid:24} (no local capture — skipped)")

    print("\nCONTROLS (should be UNCHANGED):")
    for pid in CONTROLS:
        if (_SAMPLES / f"pane_{pid.replace('%', 'pct')}.txt").exists():
            rows.append(("CONTROL", *_run_one(f"CONTROL {pid}", _capture(pid), trials)))
        else:
            print(f"  CONTROL {pid:24} (no local capture — skipped)")

    if GENUINE_PROMPT_FIXTURE.exists():
        print("\nGENUINE (tool-held permission box — MUST still fire the question):")
        text = GENUINE_PROMPT_FIXTURE.read_text(encoding="utf-8")
        rows.append(("GENUINE", *_run_one("GENUINE permission-prompt fixture", text, trials)))

    print("\n\n===== SUMMARY TABLE =====")
    print(f"{'group':9} {'pane':34} {'baseline q':>12} {'patched q':>11}  verdict")
    for group, label, b, p in rows:
        bs = f"{b}/{trials}"
        ps = f"{p}/{trials}"
        if group == "AFFECTED":
            verdict = "FIXED (q rate down)" if (b > 0 and p < b) else "no over-fire this sample"
        elif group == "CONTROL":
            verdict = "unchanged"
        else:  # GENUINE
            verdict = "still fires q" if p == trials else "!! SUPPRESSED"
        print(f"{group:9} {label:34} {bs:>12} {ps:>11}  {verdict}")


if __name__ == "__main__":
    sys.exit(main())
