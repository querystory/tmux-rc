"""classify() is now a raw-JSON pipe: it returns the LLM's dict (plus pane_id/label),
with a waiting-override for question/rewind and a no-LLM heuristic fallback."""

from daemon.classify import bootstrap, classify
from daemon.tmux import Pane


def _pane(cmd="bash"):
    return Pane("work", "0", "bash", "0", "%0", cmd, "t", "/home/x/proj")


def _llm(payload):
    return lambda system, text: payload


def test_bootstrap_shapes_result_and_flags_history():
    r = bootstrap(
        _pane(),
        "…",
        _llm(
            {
                "name": "  tmux-rc overhaul  ",
                "summary": " shipping PRs #24 and #26 ",
                "events": [{"text": "Merged PR #24"}, {"junk": 1}, "nope"],
            }
        ),
    )
    assert r["name"] == "tmux-rc overhaul"
    assert r["summary"] == "shipping PRs #24 and #26"
    assert r["events"] == [{"text": "Merged PR #24", "historical": True}]


def test_bootstrap_rejects_junk():
    assert bootstrap(_pane(), "…", _llm(["not a dict"])) is None
    assert bootstrap(_pane(), "…", _llm({"summary": 3})) is None
    assert bootstrap(_pane(), "…", lambda s, t: None) is None


def test_payload_leads_with_foreground_process():
    seen = {}

    def llm(system, text):
        seen["text"] = text
        return {"tool": "shell", "activity": "idle"}

    classify(_pane(cmd="python3"), "some screen", llm)
    first_line = seen["text"].splitlines()[0]
    assert "foreground process" in first_line and "python3" in first_line


def test_pipes_llm_json_through():
    r = classify(
        _pane(),
        "…",
        _llm(
            {
                "tool": "claude",
                "activity": "running",
                "headline": "Editing models.py",
                "model": "Opus 4.8",
                "notable": ["ran tests", "8 passed"],
            }
        ),
    )
    assert r["tool"] == "claude" and r["headline"] == "Editing models.py"
    assert r["notable"] == ["ran tests", "8 passed"]  # passed straight through
    assert r["pane_id"] == "%0" and r["label"] == "work"  # merged in (session name)


def test_question_forces_waiting():
    r = classify(
        _pane(),
        "…",
        _llm(
            {
                "activity": "running",
                "question": {"prompt": "Proceed?", "options": ["yes", "no"]},
            }
        ),
    )
    assert r["activity"] == "waiting"
    assert r["waiting_on"] == "user"  # a question is a user-facing affordance


def test_rewind_forces_waiting():
    r = classify(
        _pane(),
        "…",
        _llm(
            {
                "activity": "running",
                "rewind": {"entries": [{"text": "x", "selected": True}]},
            }
        ),
    )
    assert r["activity"] == "waiting"
    assert r["waiting_on"] == "user"


def test_waiting_defaults_to_user():
    # Model says "waiting" with no waiting_on → default to the safe actionable "user".
    r = classify(_pane(), "…", _llm({"activity": "waiting"}))
    assert r["waiting_on"] == "user"


def test_waiting_on_external_passes_through():
    r = classify(_pane(), "…", _llm({"activity": "waiting", "waiting_on": "external"}))
    assert r["activity"] == "waiting" and r["waiting_on"] == "external"


def test_question_overrides_stray_external():
    # A question is unambiguously a user-wait even if the model mislabels waiting_on.
    r = classify(
        _pane(),
        "…",
        _llm(
            {
                "activity": "running",
                "waiting_on": "external",
                "question": {"prompt": "Proceed?", "options": []},
            }
        ),
    )
    assert r["activity"] == "waiting" and r["waiting_on"] == "user"


def test_non_waiting_gets_no_waiting_on():
    r = classify(_pane(), "…", _llm({"activity": "running"}))
    assert "waiting_on" not in r  # only meaningful for waiting panes


def test_stray_waiting_on_dropped_when_not_waiting():
    # The model may emit waiting_on on a non-waiting pane; classify must drop it so a
    # stale value never leaks to the UI (contract: meaningful only when activity==waiting).
    r = classify(_pane(), "…", _llm({"activity": "running", "waiting_on": "external"}))
    assert "waiting_on" not in r


def test_agents_count_matches_ui_not_done_rule():
    # The dock badge count must agree with subagentsView, which pulses on state != "done".
    # So any non-"done" state (running, missing, paused, a stray uppercase) counts as one
    # running agent; only exactly "done" is excluded.
    subs = [
        {"state": "running"},
        {"state": "done"},
        {},  # missing state → running
        {"state": "paused"},  # not "done" → still counted (matches the pulse)
        {"state": "Running"},  # stray case → not "done" → counted
    ]
    r = classify(_pane(), "…", _llm({"activity": "running", "subagents": subs}))
    assert r["agents"] == 4


def test_agents_count_defaults_zero_without_subagents():
    assert classify(_pane(), "…", _llm({"activity": "running"}))["agents"] == 0
    # A non-list subagents (model glitch) must not leak through as a count.
    assert classify(_pane(), "…", _llm({"subagents": "oops"}))["agents"] == 0


def test_no_llm_fallback_idle_shell():
    r = classify(_pane("bash"), "shapor@host:~/proj$ ", llm_fn=None)
    assert r["tool"] == "shell" and r["activity"] == "idle"


def test_no_llm_fallback_running():
    r = classify(_pane("node"), "streaming output...\nmore", llm_fn=None)
    assert r["activity"] == "running"


def test_copyables_capped_and_malformed_dropped():
    # copyables ride EVERY state poll, so classify caps count/size and drops junk
    # rather than repairing it — a clipped paste is worse than no paste.
    r = classify(
        _pane(),
        "…",
        _llm(
            {
                "activity": "idle",
                "copyables": [
                    {"label": "Commit message", "text": "fix: unwrap the thing"},
                    {"label": "no text field"},
                    {"label": "too long", "text": "x" * 4001},
                    {"label": "empty", "text": ""},
                    "not a dict",
                    {"label": "fourth", "text": "over the cap of 3"},
                ],
            }
        ),
    )
    assert r["copyables"] == [{"label": "Commit message", "text": "fix: unwrap the thing"}]


def test_copyables_reemitted_minimally():
    # The model's dict is not passed through: an invented key would ride every poll for
    # free, and an enormous label is payload too (the client's 60 cap is only display).
    r = classify(
        _pane(),
        "…",
        _llm(
            {
                "activity": "idle",
                "copyables": [
                    {"label": "L" * 500, "text": "paste me", "junk": "x" * 9000},
                    {"text": "no label at all"},
                ],
            }
        ),
    )
    assert r["copyables"] == [
        {"label": "L" * 200, "text": "paste me"},
        {"label": "", "text": "no label at all"},
    ]


def test_copyables_non_list_dropped():
    r = classify(_pane(), "…", _llm({"activity": "idle", "copyables": "nope"}))
    assert "copyables" not in r
