"""Offline guards for the prompt-eval harness (research/eval) — the SCORING logic and
CORPUS integrity, with NO Vertex calls (stub llm_fns), so `make test` / CI protect the
harness itself without needing credentials or network. The live model runs are the
harness's job; these tests keep its plumbing honest."""

from research.eval.harness import (
    Sample,
    evaluate,
    load_corpus,
    score_structured,
)

_VALID_ACTIVITY = {"running", "waiting", "idle", "compacting"}
_VALID_TOOL = {"claude", "codex", "gemini", "shell", "unknown"}


def _sample(**expected) -> Sample:
    return Sample(
        name="t", description="", current_command="node",
        capture="●\n❯", expected=expected,
    )


def test_structured_exact_match_passes():
    ok, diffs = score_structured(
        {"tool": "claude", "activity": "idle"},
        {"tool": "claude", "activity": "idle"},
    )
    assert ok and diffs == []


def test_structured_scalar_mismatch_fails():
    ok, diffs = score_structured({"activity": "waiting"}, {"activity": "idle"})
    assert not ok and any("activity" in d for d in diffs)


def test_question_scored_by_shape_not_prose():
    # Same shape (menu) with wildly different prompt text still matches — prose is the
    # judge's job, not the structured check.
    ok, _ = score_structured(
        {"question": {"prompt": "Run tests?", "answer_style": "menu"}},
        {"question": {"answer_style": "menu"}},
    )
    assert ok
    # answer_style mismatch DOES fail (text vs menu is behavior).
    ok, diffs = score_structured(
        {"question": {"answer_style": "text"}},
        {"question": {"answer_style": "menu"}},
    )
    assert not ok and any("question" in d for d in diffs)


def test_question_presence_mismatch_fails():
    # A spurious question (the #85 over-fire) must be caught: candidate has one, expected
    # has none.
    ok, diffs = score_structured(
        {"question": {"answer_style": "text"}}, {"activity": "idle"}
    )
    assert not ok and any("question" in d for d in diffs)


def test_empty_question_dict_counts_as_present():
    # A bare {} question is truthy to the web UI (renders a broken question view), so it
    # must NOT score as absent: candidate {} vs expected-absent is a mismatch.
    ok, diffs = score_structured({"question": {}}, {"activity": "idle"})
    assert not ok and any("question" in d for d in diffs)


def test_rewind_and_tasks_scored_by_presence():
    ok, _ = score_structured({"rewind": {"entries": []}}, {"rewind": {"present": True}})
    assert ok
    ok, diffs = score_structured({}, {"tasks": [{"text": "x"}]})
    assert not ok and any("tasks" in d for d in diffs)


def test_tables_is_scored_only_where_a_sample_names_it():
    """Opt-in presence: a sample that names `tables` asserts it either way, and one that
    doesn't stays unconstrained — otherwise adding the field would fail every existing
    sample on a claim it never made."""
    table = [{"headers": ["#", "edit"], "rows": [["1", "resolve the conflict"]]}]
    assert not score_structured({}, {"tables": True})[0]          # wanted, absent
    assert score_structured({"tables": table}, {"tables": True})[0]
    assert not score_structured({"tables": table}, {"tables": False})[0]  # unwanted
    # Silent sample: the model may emit tables or not, and neither is a regression.
    assert score_structured({"tables": table}, {})[0]
    assert score_structured({}, {})[0]


def test_an_unrenderable_tables_value_does_not_count_as_the_list():
    """Nothing validates the model's `tables` (JSON mime type, no schema), and the phone
    draws only table objects carrying rows. A truthy-but-undrawable value would otherwise
    score as "the list travelled" while the screen still shows the question alone."""
    for junk in ("1. resolve the conflict", {}, [{}], [{"rows": []}], [{"title": "edits"}],
                 [{"rows": "1. resolve the conflict"}], [{"rows": ["not a row"]}],
                 [{"rows": [{"text": "edit 1"}]}], [{"rows": [[]]}], [{"rows": {"1": "resolve"}}]):
        ok, diffs = score_structured({"tables": junk}, {"tables": True})
        assert not ok and any("tables" in d for d in diffs), junk


def test_evaluate_pass_requires_both_struct_and_judge():
    s = _sample(tool="claude", activity="idle", headline="idle at prompt")
    good = lambda system, text: {"tool": "claude", "activity": "idle", "headline": "at the box"}  # noqa: E731
    judge_pass = lambda system, text: {"verdict": "PASS", "reason": "same"}  # noqa: E731
    judge_fail = lambda system, text: {"verdict": "FAIL", "reason": "different"}  # noqa: E731

    assert evaluate(s, good, judge_pass).passed
    r = evaluate(s, good, judge_fail)
    assert not r.passed and r.struct_ok and not r.judge_ok  # judge alone can fail it


def test_judge_no_verdict_is_a_fail_not_a_pass():
    s = _sample(tool="claude", activity="idle", headline="x")
    good = lambda system, text: {"tool": "claude", "activity": "idle", "headline": "x"}  # noqa: E731
    r = evaluate(s, good, lambda system, text: None)  # malformed judge reply
    assert not r.judge_ok


def test_corpus_loads_and_is_well_formed():
    corpus = load_corpus()
    assert len(corpus) >= 10  # ~10-15 curated samples
    for s in corpus:
        assert s.capture.strip(), f"{s.name}: empty capture"
        assert s.expected.get("tool") in _VALID_TOOL, f"{s.name}: bad tool"
        assert s.expected.get("activity") in _VALID_ACTIVITY, f"{s.name}: bad activity"
        # waiting_on is meaningful only on a waiting pane.
        if s.expected.get("waiting_on"):
            assert s.expected["activity"] == "waiting", f"{s.name}: waiting_on off a non-wait"
            assert s.expected["waiting_on"] in ("user", "external")


def test_referenced_edits_reach_the_content_judge():
    """An unrelated but renderable table must not satisfy the referenced-list case."""
    import json

    from research.eval.harness import judge_freetext

    sample = next(s for s in load_corpus() if s.name == "16_question_refers_to_list")
    rows = sample.expected["tables"][0]["rows"]
    assert len(rows) == 4
    unrelated = {"headline": sample.expected["headline"],
                 "tables": [{"rows": [["1", "Rewrite packaging"]]}]}

    def judge(system, text):
        payload = json.loads(text)
        assert payload["expected_tables"] == sample.expected["tables"]
        assert payload["candidate_tables"] == unrelated["tables"]
        assert "missing edits" in system
        return {"verdict": "FAIL", "reason": "The four requested edits are missing"}

    assert not judge_freetext(sample, unrelated, judge)[0]
