"""Prompt-eval harness: regression-test the production classifier prompt against a
corpus of blessed pane samples, and A/B models, with one pass/fail signal.

WHY this exists: prompt edits (#85's question-overfire fix) and model swaps (the
3.1-lite → 3.5-lite benchmark) were validated ad-hoc — hand-eyeballed JSON over a
throwaway script each time. This codifies that into standing infra: a committed corpus
+ a scoring model + one command that exits non-zero on any regression, so a prompt
change or model swap gets the same check every time and CI can gate on it.

WHAT it does: for each sample it runs the SAME code path production uses —
`openbus.classify.classify(pane, text, llm_fn)` with `llm_fn` calling the real Vertex
model under `openbus/parser_prompt.txt` — so the candidate output includes the
waiting_on / activity overrides that actually drive the UI badge, not just raw model
JSON. It then scores the candidate against the sample's blessed `expected`:

  STRUCTURED fields (exact match) — these drive the badge and behavior, so brittleness
  is correct here: `tool`, `activity`, `waiting_on`, plus the PRESENCE/shape of
  `question` (present-or-absent, and if present its `answer_style`), `rewind`, `tasks`,
  `copyables`, and — only where a sample names it — a RENDERABLE `tables`.
  A single structured mismatch fails the sample.

  FREE-TEXT fields (LLM-as-judge) — `headline` is prose; exact-match would be noise.
  A second Vertex call (temperature 0) rules PASS/FAIL on whether the candidate
  headline captures the SAME situation as the expected one, given the screen.
  Concrete expected tables also require every referenced edit to survive in the output.

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

from openbus.classify import classify
from openbus.tmux import Pane

SAMPLES_DIR = Path(__file__).parent / "samples"

# Fields whose EXACT value drives the UI badge / behavior — scored strictly. `question`,
# `rewind`, `tasks` are scored by PRESENCE (+ question.answer_style) separately below,
# because their free-text bodies are prose the judge handles.
_STRUCT_SCALAR = ("tool", "activity", "waiting_on")
# `session` becomes the pane's NAME on the phone, so a sample that pins it wants an exact
# match — but it is read off agent chrome that most screens don't show, so it is scored
# only when a sample states an expectation (including an explicit null for "must omit").
# Silently ignoring it would let a naming regression pass with the expectation in place.
_STRUCT_OPTIONAL = ("session", "agents")


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


def _keymap(km: dict | None) -> tuple:
    """What the cursor walk actually reads off a keymap: the three keys it may press and
    whether type-to-filter was advertised. Both sides go through this so a model that
    omits `search` scores the same as a sample that writes `false`, and so a keymap
    carrying extra prose doesn't fail on fields nothing consumes."""
    # isinstance, not `or {}`: classify() pipes model JSON through unvalidated (see
    # classify.py), so a malformed keymap can be a string or a list, and .get would raise
    # — taking down the whole eval run instead of recording one structured mismatch.
    km = km if isinstance(km, dict) else {}
    # `is True`, not bool(): the field is declared boolean but the candidate is raw model
    # output, and bool("false") is True — which would let a malformed keymap score as
    # matching a pinned `search: true` and walk straight through the gate. Omitted and
    # explicitly false still agree, which is the point of normalizing at all.
    return (km.get("next"), km.get("prev"), km.get("select"), km.get("search") is True)


def _shape(field: dict | list | None, extra: tuple[str, ...] = ()) -> object:
    """The structural signature we score `question` on: present-or-absent, and if a
    question, its answer_style (menu vs text vs cursor — the phone sends a keystroke,
    typed text, or a whole arrow walk, so this is behavior, not prose). Body text is
    left to the judge.

    ABSENT (missing or explicit null) → None. A PRESENT question — even an empty `{}`,
    which the web UI's `if (s.question)` treats as truthy and would try (and fail) to
    render — is ("present", answer_style): so a spurious bare `{}` still scores as a
    question mismatch against an expected-absent, rather than slipping through.

    `extra` names further fields to fold in, and comes from what the SAMPLE declares —
    the same "takes no position" rule as _STRUCT_OPTIONAL, one level down. It exists for
    `selected` and `keymap`: answer_style alone says a cursor picker was recognized, but
    the walk that drives it is steered by the anchor and the advertised bindings, and a
    model that says "cursor" with a wrong anchor selects the WRONG SESSION — silently,
    and with an eval that was still green. Scored only where a sample asks for it, since
    most screens have nothing to say about either."""
    if field is None:
        return None
    if isinstance(field, dict):
        vals = tuple(_keymap(field.get(k)) if k == "keymap" else field.get(k) for k in extra)
        return ("present", field.get("answer_style"), *vals)
    return ("present", None) if field else None


def score_structured(candidate: dict, expected: dict) -> tuple[bool, list[str]]:
    """Compare the STRUCTURED fields. Returns (ok, mismatches) — a human-readable diff
    line per field that disagrees. `waiting_on` is only meaningful when waiting, so it's
    compared as absent==absent there. `question` compares by shape (presence +
    answer_style); `rewind`/`tasks`/`copyables` by presence. `tables` is presence too but
    OPT-IN — scored only on a sample whose `expected` names it, and counted present only
    when the phone could actually draw it (a table object carrying rows)."""
    diffs = []
    for k in _STRUCT_SCALAR:
        c, e = candidate.get(k), expected.get(k)
        if c != e:
            diffs.append(f"{k}: got {c!r} want {e!r}")
    for k in _STRUCT_OPTIONAL:
        if k not in expected:
            continue  # sample takes no position on this field
        c, e = candidate.get(k), expected.get(k)
        if c != e:
            diffs.append(f"{k}: got {c!r} want {e!r}")
    if "subagent_states" in expected:
        subs = candidate.get("subagents")
        states = (sorted(str(a.get("state")) for a in subs if isinstance(a, dict))
                  if isinstance(subs, list) else [])
        if states != sorted(expected["subagent_states"]):
            diffs.append(f"subagent_states: got {states!r} want {expected['subagent_states']!r}")
    # question — presence + answer_style, plus whichever cursor fields the sample pins
    want_q = expected.get("question")
    extra = tuple(k for k in ("selected", "keymap") if isinstance(want_q, dict) and k in want_q)
    cq, eq = _shape(candidate.get("question"), extra), _shape(want_q, extra)
    if cq != eq:
        diffs.append(f"question: got {cq!r} want {eq!r}")
    # Presence-only fields. `copyables` is here rather than compared by content: the
    # LABEL is free prose and the TEXT is a verbatim payload whose exact whitespace we
    # don't want to bless brittlely — what must not regress is "the model noticed there
    # was something paste-worthy on this screen (or correctly noticed there wasn't)".
    presence = {k: candidate.get(k) for k in ("rewind", "tasks", "copyables")}
    # `tables` is presence too, but OPT-IN — scored only on a sample that names it. Most
    # screens have no table and take no position, so scoring it everywhere would newly fail
    # existing samples on a field they were never blessed against. Naming it is the claim,
    # and the claim is "the question refers to a list, so the list has to travel with it or
    # the phone asks about items it never showed". So score what the PHONE would RENDER,
    # not bare truthiness: app.js draws a table only where `rows` is an ARRAY (and an empty
    # one carries no list), and nothing validates this field — the model answers under a
    # mime type, not a schema — so a string, a {} or `rows: "1. do the thing"` would
    # otherwise score as a list that travelled when the phone would show none.
    if "tables" in expected:
        tables = candidate.get("tables")
        valid = tables is None or (isinstance(tables, list) and all(
            isinstance(table, dict)
            and (table.get("headers") is None or (
                isinstance(table["headers"], list)
                and all(isinstance(cell, str) for cell in table["headers"])
            ))
            and isinstance(table.get("rows"), list)
            and all(isinstance(row, list) and all(isinstance(cell, str) for cell in row)
                    for row in table["rows"])
            for table in tables
        ))
        if not valid:
            diffs.append("tables: malformed headers, rows, or cells")
        presence["tables"] = valid and any(
            row for table in (tables or []) for row in table["rows"]
        )
    for k, got in presence.items():
        c, e = bool(got), bool(expected.get(k))
        if c != e:
            diffs.append(f"{k} present: got {c} want {e}")
    return (not diffs), diffs


# ── LLM-as-judge (free-text) ─────────────────────────────────────────────────────────

_JUDGE_SYSTEM = (
    "You are a strict grader for a terminal-pane classifier. You are given the SCREEN "
    "the classifier saw, the EXPECTED classification (blessed by a human), and the "
    "CANDIDATE classification a model produced. Judge the free-text 'headline' "
    "field: does the candidate headline describe the SAME situation on the screen as "
    "the expected one? Be lenient on wording, phrasing, and length — different words "
    "for the same situation PASS. Fail only if the candidate headline is about a "
    "materially DIFFERENT situation, is misleading, or contradicts the screen. If "
    "neither side has a headline, that check passes. When expected_tables is provided, "
    "ALSO check that candidate_tables contains every expected edit with its meaning "
    "intact. Ignore wording and table layout, but FAIL for missing edits or unrelated "
    "rows substituted for the referenced edits. Both headline and table content must "
    "pass. Reply with compact JSON only: "
    '{"verdict":"PASS"|"FAIL","reason":"<one short line>"}.'
)


def judge_freetext(sample: Sample, candidate: dict, llm_fn) -> tuple[bool, str]:
    """Second opinion on the prose. Returns (passed, reason). `llm_fn(system, text)`
    returns the judge's parsed JSON dict; a malformed/None reply is treated as a FAIL
    (visible, not silently swallowed) with the reason noting it."""
    fields = {
        "screen": sample.capture,
        "expected_headline": sample.expected.get("headline"),
        "candidate_headline": candidate.get("headline"),
    }
    # A boolean asserts presence only; concrete expected tables also assert meaning.
    if isinstance(sample.expected.get("tables"), list):
        fields["expected_tables"] = sample.expected["tables"]
        fields["candidate_tables"] = candidate.get("tables")
    payload = json.dumps(fields, ensure_ascii=False)
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
            lines.extend(f"    struct: {d}" for d in r.struct_diffs)
        if not r.judge_ok:
            lines.append(f"    judge: {r.judge_reason}")
    passed = sum(r.passed for r in results)
    lines.append("-" * 60)
    lines.append(f"{passed}/{len(results)} passed")
    return "\n".join(lines)
