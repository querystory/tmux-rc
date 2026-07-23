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
