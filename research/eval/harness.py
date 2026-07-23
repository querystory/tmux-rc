"""Prompt-eval harness: regression-test the production classifier prompt against a
corpus of blessed pane samples, and A/B models, with one pass/fail signal.

WHY this exists: prompt edits (#85's question-overfire fix) and model swaps (the
3.1-lite → 3.5-lite benchmark) were validated ad-hoc — hand-eyeballed JSON over a
throwaway script each time. This codifies that into standing infra: a committed corpus
+ a scoring model + one command that exits non-zero on any regression, so a prompt
change or model swap gets the same check every time and CI can gate on it.

WHAT it does: for each sample it runs the SAME code path production uses —
`daemon.classify.classify(pane, text, llm_fn)` with `llm_fn` calling the real Vertex
model under `daemon/parser_prompt.txt` — so the candidate output includes the
waiting_on / activity overrides that actually drive the UI badge, not just raw model
JSON. It then scores the candidate against the sample's blessed `expected`:

  STRUCTURED fields (exact match) — these drive the badge and behavior, so brittleness
  is correct here: `tool`, `activity`, `waiting_on`, plus the PRESENCE/shape of
  `question` (present-or-absent, and if present its `answer_style`), `rewind`, `tasks`.
  A single structured mismatch fails the sample.

  FREE-TEXT fields (LLM-as-judge) — `headline` is prose; exact-match would be noise.
  A second Vertex call (temperature 0) rules PASS/FAIL on whether the candidate
  headline captures the SAME situation as the expected one, given the screen.

  A sample PASSES only if structured fields match AND the judge agrees. Both signals
  are surfaced so a failure tells you which half broke.

This module holds the reusable machinery; `__main__.py` is the CLI. The single
model-call helper (`run_classifier`) is the DRY consolidation of the three prior
one-offs (research/probe.py `_parse`, validate_question_overfire `_classify`, the 3.5
bench) — one place that assembles the production payload and calls the model.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from daemon.classify import classify
from daemon.tmux import Pane

SAMPLES_DIR = Path(__file__).parent / "samples"

# Fields whose EXACT value drives the UI badge / behavior — scored strictly. `question`,
# `rewind`, `tasks` are scored by PRESENCE (+ question.answer_style) separately below,
# because their free-text bodies are prose the judge handles.
_STRUCT_SCALAR = ("tool", "activity", "waiting_on")


@dataclass
class Sample:
    name: str
    description: str
    current_command: str
    capture: str
    expected: dict

    @classmethod
    def load(cls, path: Path) -> Sample:
        d = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            name=path.stem,
            description=d.get("description", ""),
            current_command=d.get("current_command", "bash"),
            capture=d["capture"],
            expected=d["expected"],
        )


def load_corpus(only: str | None = None) -> list[Sample]:
    """All samples in samples/ (or just `only`), sorted by name for a stable table."""
    paths = sorted(SAMPLES_DIR.glob("*.json"))
    samples = [Sample.load(p) for p in paths]
    if only:
        samples = [s for s in samples if s.name == only]
        if not samples:
            raise SystemExit(f"no sample named {only!r} in {SAMPLES_DIR}")
    return samples


def run_classifier(sample: Sample, llm_fn) -> dict:
    """Run ONE sample through the production classify() path. `llm_fn(system, text)`
    is the model call (the harness injects a real-Vertex one; tests can inject a stub).
    A synthetic Pane carries the sample's foreground process so the `[tmux: …]` prefix
    and tool-anchoring behave exactly as in production."""
    pane = Pane(
        session="eval",
        window_index="0",
        window_name="eval",
        pane_index="0",
        id="%0",
        current_command=sample.current_command,
        title=sample.name,
    )
    return classify(pane, sample.capture, llm_fn=llm_fn)


# ── scoring ────────────────────────────────────────────────────────────────────────


def _shape(field: dict | list | None) -> object:
    """The structural signature we score `question` on: present-or-absent, and if a
    question, its answer_style (menu vs text — the phone sends a keystroke vs typed
    text, so this is behavior, not prose). Body text is left to the judge."""
    if not field:
        return None
    if isinstance(field, dict):
        return field.get("answer_style")
    return bool(field)


def score_structured(candidate: dict, expected: dict) -> tuple[bool, list[str]]:
    """Compare the STRUCTURED fields. Returns (ok, mismatches) — a human-readable diff
    line per field that disagrees. `waiting_on` is only meaningful when waiting, so it's
    compared as absent==absent there. `question` compares by shape (presence +
    answer_style); `rewind`/`tasks` by presence only."""
    diffs = []
    for k in _STRUCT_SCALAR:
        c, e = candidate.get(k), expected.get(k)
        if c != e:
            diffs.append(f"{k}: got {c!r} want {e!r}")
    # question — presence + answer_style
    cq, eq = _shape(candidate.get("question")), _shape(expected.get("question"))
    if cq != eq:
        diffs.append(f"question: got {cq!r} want {eq!r}")
    for k in ("rewind", "tasks"):
        c, e = bool(candidate.get(k)), bool(expected.get(k))
        if c != e:
            diffs.append(f"{k} present: got {c} want {e}")
    return (not diffs), diffs


# ── LLM-as-judge (free-text) ─────────────────────────────────────────────────────────

_JUDGE_SYSTEM = (
    "You are a strict grader for a terminal-pane classifier. You are given the SCREEN "
    "the classifier saw, the EXPECTED classification (blessed by a human), and the "
    "CANDIDATE classification a model produced. Judge ONLY the free-text 'headline' "
    "field: does the candidate headline describe the SAME situation on the screen as "
    "the expected one? Be lenient on wording, phrasing, and length — different words "
    "for the same situation PASS. Fail only if the candidate headline is about a "
    "materially DIFFERENT situation, is misleading, or contradicts the screen. If "
    "neither side has a headline, PASS. Reply with compact JSON only: "
    '{"verdict":"PASS"|"FAIL","reason":"<one short line>"}.'
)


def judge_freetext(sample: Sample, candidate: dict, llm_fn) -> tuple[bool, str]:
    """Second opinion on the prose. Returns (passed, reason). `llm_fn(system, text)`
    returns the judge's parsed JSON dict; a malformed/None reply is treated as a FAIL
    (visible, not silently swallowed) with the reason noting it."""
    payload = json.dumps(
        {
            "screen": sample.capture,
            "expected_headline": sample.expected.get("headline"),
            "candidate_headline": candidate.get("headline"),
        },
        ensure_ascii=False,
    )
    reply = llm_fn(_JUDGE_SYSTEM, payload)
    if not isinstance(reply, dict) or reply.get("verdict") not in ("PASS", "FAIL"):
        return False, f"judge returned no verdict: {str(reply)[:80]}"
    return reply["verdict"] == "PASS", str(reply.get("reason", ""))[:120]


# ── one-sample evaluation ────────────────────────────────────────────────────────────


@dataclass
class Result:
    name: str
    passed: bool
    struct_ok: bool
    judge_ok: bool
    struct_diffs: list[str]
    judge_reason: str
    candidate: dict


def evaluate(sample: Sample, classify_llm, judge_llm) -> Result:
    """Full evaluation of one sample: run the classifier, score structured fields, and
    (only if it's worth the judge call — always, so the judge signal is present even
    when structured already failed) run the free-text judge. Pass = both."""
    candidate = run_classifier(sample, classify_llm)
    struct_ok, diffs = score_structured(candidate, sample.expected)
    judge_ok, reason = judge_freetext(sample, candidate, judge_llm)
    return Result(
        name=sample.name,
        passed=struct_ok and judge_ok,
        struct_ok=struct_ok,
        judge_ok=judge_ok,
        struct_diffs=diffs,
        judge_reason=reason,
        candidate=candidate,
    )


def format_table(results: list[Result]) -> str:
    """A per-sample PASS/FAIL table + totals, for the CLI. Structured and judge columns
    are separate so a failure points at which half broke."""
    lines = [f"{'sample':30} {'struct':>7} {'judge':>6}  verdict", "-" * 60]
    for r in results:
        lines.append(
            f"{r.name:30} {'ok' if r.struct_ok else 'FAIL':>7} "
            f"{'ok' if r.judge_ok else 'FAIL':>6}  {'PASS' if r.passed else 'FAIL'}"
        )
        if not r.struct_ok:
            for d in r.struct_diffs:
                lines.append(f"    struct: {d}")
        if not r.judge_ok:
            lines.append(f"    judge: {r.judge_reason}")
    passed = sum(r.passed for r in results)
    lines.append("-" * 60)
    lines.append(f"{passed}/{len(results)} passed")
    return "\n".join(lines)
