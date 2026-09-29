"""classify() is now a raw-JSON pipe: it returns the LLM's dict (plus pane_id/label),
with a waiting-override for question/rewind and a no-LLM heuristic fallback."""

from openbus.classify import bootstrap, classify
from openbus.tmux import Pane


def test_opaque_session_identifiers_are_not_titles():
    for name in ("01a0e9d1-093d-7c10-84f4-133c9544c971", " ABCDEF0123456789 "):
        result = classify(_pane(), "text", _llm({"session": name, "activity": "idle"}))
        assert "session" not in result
    for name in ("gpt-5 migration", "airbyte-value-population", "Review 4955", "deadbeef"):
        capture = f"› input\n{name} · gpt-6-sol · ~/src/app"
        result = classify(_pane("node"), capture, _llm({
            "tool": "codex", "session": name, "activity": "idle",
        }))
        assert result["session"] == name


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
                "working_prs": [{"repo": "querystory/tmux-rc", "number": 245}],
                "events": [{"text": "Merged PR #24"}, {"junk": 1}, "nope"],
            }
        ),
    )
    assert r["name"] == "tmux-rc overhaul"
    assert r["summary"] == "shipping PRs #24 and #26"
    assert r["events"] == [{"text": "Merged PR #24", "historical": True}]
    assert r["working_prs"] == [{"repo": "querystory/tmux-rc", "number": 245}]


def test_bootstrap_rejects_junk():
    assert bootstrap(_pane(), "…", _llm(["not a dict"])) is None
    assert bootstrap(_pane(), "…", _llm({"summary": 3})) is None
    assert bootstrap(_pane(), "…", lambda s, t: None) is None


def test_bootstrap_rejects_non_title_names():
    for name in ("~/src/app", "/src/app", "docs/metadata-design",
                 "01a0e9d1-093d-7c10-84f4-133c9544c971"):
        result = bootstrap(_pane("node"), "ordinary history", _llm({
            "summary": "Working on parser behavior", "name": name,
        }))
        assert result["name"] is None


def test_bootstrap_prompt_explains_opencode_model_identity():
    seen = {}

    def llm(system, text):
        seen["prompt"] = system
        return {"summary": "OpenCode session"}

    bootstrap(_pane(cmd="opencode"), "Claude Opus 5.5\nOpenCode 1.18.32", llm)
    assert "OpenCode can run Claude, GPT, or Gemini models" in seen["prompt"]


def test_payload_leads_with_foreground_process():
    seen = {}

    def llm(system, text):
        seen["text"] = text
        return {"tool": "shell", "activity": "idle"}

    classify(_pane(cmd="python3"), "some screen", llm)
    first_line = seen["text"].splitlines()[0]
    assert "foreground process" in first_line and "python3" in first_line


def test_foreground_agent_process_beats_selected_model_identity():
    r = classify(
        _pane(cmd="opencode"),
        "Build · Claude Opus 5.5 · Amazon Bedrock\nOpenCode 1.18.32",
        _llm({"tool": "claude", "activity": "idle", "model": "Claude Opus 5.5",
              "tasks": [{"text": "A conversational bullet", "done": False}]}),
    )
    assert r["tool"] == "opencode"
    assert r["model"] == "Claude Opus 5.5"  # backend metadata remains intact
    assert "tasks" not in r


def test_opencode_keeps_an_explicit_task_plan():
    tasks = [{"text": "Add the regression test", "done": False}]
    r = classify(
        _pane(cmd="opencode"),
        "Plan\n☐ Add the regression test\n\nOpenCode 1.18.32",
        _llm({"tool": "opencode", "activity": "running", "tasks": tasks}),
    )
    assert r["tasks"] == tasks


def test_opencode_stale_checklist_does_not_validate_current_bullets():
    r = classify(
        _pane(cmd="opencode"),
        (
            "Plan\n☐ Old implementation step\n\n"
            "Review complete:\n- Logs still use HTTP\nOpenCode 1.18.32"
        ),
        _llm({"tool": "opencode", "activity": "idle",
              "tasks": [{"text": "Logs still use HTTP", "done": False}]}),
    )
    assert "tasks" not in r


def test_opencode_standalone_checkboxes_own_their_line_and_done_state():
    tasks = [{"text": t, "done": False} for t in ("Ship it", "Test it", "Drop it", "Prose")]
    r = classify(
        _pane(cmd="opencode"),
        "Plan\n[✓] Ship it\n[•] Test it\n~[ ] Drop it~\n[ ]\nProse\n\nOpenCode 1.18.32",
        _llm({"tool": "opencode", "activity": "running", "tasks": tasks}),
    )
    assert r["tasks"] == [{"text": "Ship it", "done": True}, tasks[1]]


def test_opencode_interrupt_spinner_forces_running():
    r = classify(
        _pane(cmd="opencode"),
        "┃  Build · Claude Opus 5.5 · Amazon Bedrock\n▰▰▰▰▰▰ esc interrupt",
        _llm({"tool": "claude", "activity": "idle", "model": "Claude Opus 5.5"}),
    )
    assert r["tool"] == "opencode"
    assert r["activity"] == "running"


def test_opencode_stale_interrupt_row_does_not_override_idle_footer():
    r = classify(
        _pane(cmd="opencode"),
        "▰▰▰▰ esc interrupt\nPrevious turn complete\nOpenCode 1.18.32",
        _llm({"tool": "opencode", "activity": "idle"}),
    )
    assert r["activity"] == "idle"


def test_payload_supplies_repository_for_semantic_pr_classification():
    seen = {}

    def llm(_system, text):
        seen["text"] = text
        return {"tool": "codex", "activity": "running"}

    classify(_pane(cmd="codex"), "working", llm, repository="querystory/tmux-rc")
    assert "GitHub repository is 'querystory/tmux-rc'" in seen["text"].splitlines()[0]


def test_working_prs_are_validated_bounded_and_deduped():
    raw = [
        *[{"repo": repo, "number": 1} for repo in ("../bad", "./repo", "org/.", "org/..")],
        {"repo": "org/" + "a" * 257, "number": 1},
        {"repo": "querystory/qs-app", "number": "1" * 5000},
        {"repo": "querystory/qs-app", "number": "²"},
        {"repo": "querystory/qs-app", "number": 2**53},
        {"repo": "querystory/qs-app", "number": str(2**53)},
        {"repo": "querystory/qs-app", "number": 4955},
        {"repo": "QUERYSTORY/qs-app", "number": "4955"},
        {"repo": "no-owner", "number": 2},
        {"repo": "querystory/qs-app", "number": True},
        {"repo": "querystory/qs-app", "number": 0},
        "junk",
    ] + [{"repo": "querystory/tmux-rc", "number": n} for n in range(1, 12)]
    r = classify(_pane(), "…", _llm({"activity": "running", "working_prs": raw}))
    assert r["working_prs"][0] == {"repo": "querystory/qs-app", "number": 4955}
    assert len(r["working_prs"]) == 8
    assert len({(p["repo"].lower(), p["number"]) for p in r["working_prs"]}) == 8
    assert "working_prs" not in classify(
        _pane(), "…", _llm({"activity": "idle", "working_prs": "all PRs"})
    )


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
    assert r["pane_id"] == "%0" and r["label"] == "work:0"  # merged in (session:window)


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


def test_agents_count_only_observed_busy_workers():
    subs = [{"state": state} for state in
            ("running", "done", "waiting", "idle", "compacting", "unknown")]
    subs.extend([{}, {"state": "Running"}, "malformed"])
    r = classify(_pane(), "…", _llm({"activity": "running", "subagents": subs}))
    assert r["agents"] == 2


def test_agents_count_defaults_zero_without_subagents():
    assert classify(_pane(), "…", _llm({"activity": "running"}))["agents"] == 0
    # A non-list subagents (model glitch) must not leak through as a count.
    assert classify(_pane(), "…", _llm({"subagents": "oops"}))["agents"] == 0


def test_no_llm_fallback_idle_shell():
    r = classify(_pane("bash"), "user@host:~/proj$ ", llm_fn=None)
    assert r["tool"] == "shell" and r["activity"] == "idle"


def test_failed_parse_holds_the_last_known_activity_instead_of_guessing():
    """A failed parse read NOTHING off the screen, so it must not invent a state. The
    old fallback said "running" for anything that wasn't a bare shell prompt — which on
    an agent TUI is everything — so a silent 429 stamped a finished agent "Running", and
    the watcher's fingerprint retired the screen so it never re-parsed. A pane whose
    screen never changes again wore that badge forever."""
    pane = _pane("node")
    # Nothing known yet: "unknown" (reads as stale in the UI), never a fabricated "running".
    assert classify(pane, "streaming output...\nmore", llm_fn=None)["activity"] == "unknown"
    # Known state carries forward, in both directions — the last thing we actually read.
    for prev in ("running", "idle", "waiting"):
        r = classify(pane, "streaming output...\nmore", llm_fn=None, prev_activity=prev)
        assert r["activity"] == prev
    # A bare shell prompt is still readable without the model, so it still wins.
    assert classify(_pane(), "user@host:~$ ", llm_fn=None, prev_activity="running")[
        "activity"
    ] == "idle"


def test_failed_parse_is_flagged_so_the_watcher_can_retry_the_screen():
    """The watcher retires a screen by advancing its fingerprint, and never re-parses an
    unchanged one. So a failed parse has to say so, or the failure is committed as if it
    were a reading."""
    assert classify(_pane("node"), "x", llm_fn=None)["parse_ok"] is False
    assert "parse_ok" not in classify(_pane("node"), "x", _llm({"activity": "idle"}))


def test_copyables_capped_and_malformed_dropped():
    # copyables ride EVERY state poll, so classify caps count/size and drops junk
    # rather than repairing it — a clipped paste is worse than no paste.
    r = classify(
        _pane(),
        "fix: unwrap the thing\npast the malformed ones",
        _llm(
            {
                "activity": "idle",
                "copyables": [
                    {"label": "Commit message", "text": "fix: unwrap the thing"},
                    {"label": "no text field"},
                    {"label": "too long", "text": "x" * 4001},
                    {"label": "empty", "text": ""},
                    {"label": "blank", "text": "   \n  "},
                    "not a dict",
                    {"label": "fourth", "text": "past the malformed ones"},
                ],
            }
        ),
    )
    # Only the two well-formed entries survive. The trailing one is KEPT: validation runs
    # before the 3-item cap, so the malformed entries above it don't consume slots.
    assert r["copyables"] == [
        {"label": "Commit message", "text": "fix: unwrap the thing"},
        {"label": "fourth", "text": "past the malformed ones"},
    ]


def test_copyables_reemitted_minimally():
    # The model's dict is not passed through: an invented key would ride every poll for
    # free, and an enormous label is payload too (the client's 60 cap is only display).
    r = classify(
        _pane(),
        "paste me\nno label at all",
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


def test_copyables_validated_before_capping():
    # Malformed entries must not burn one of the 3 slots: the prompt orders copyables
    # most-pasteable-first, so capping the raw list would drop a good trailing entry.
    r = classify(
        _pane(),
        "first\nsecond\nthird",
        _llm(
            {
                "activity": "idle",
                "copyables": [
                    "junk",
                    {"label": "no text"},
                    {"label": "a", "text": "first"},
                    {"label": "b", "text": "second"},
                    {"label": "c", "text": "third"},
                ],
            }
        ),
    )
    assert [c["text"] for c in r["copyables"]] == ["first", "second", "third"]


def test_copyables_all_malformed_omits_field():
    # Nothing survived — omit the field rather than shipping an empty list (the prompt
    # says omit when there's nothing, and the UI keys off presence).
    r = classify(
        _pane(),
        "…",
        _llm({"activity": "idle", "copyables": [{"label": "x"}, "junk"]}),
    )
    assert "copyables" not in r


def test_copyables_non_list_dropped():
    r = classify(_pane(), "…", _llm({"activity": "idle", "copyables": "nope"}))
    assert "copyables" not in r


def test_copyable_duplicating_a_link_is_dropped():
    # A copy row holding a URL the card already renders as a tap-to-open link chip is a
    # duplicate affordance. Text that merely CONTAINS a URL still copies.
    r = classify(
        _pane(),
        "https://github.com/o/r/pull/5\ncurl https://github.com/o/r/pull/5 -H accept:json",
        _llm(
            {
                "activity": "idle",
                "links": [{"href": "https://github.com/o/r/pull/5", "text": "PR #5"}],
                "copyables": [
                    {"label": "PR URL", "text": "https://github.com/o/r/pull/5"},
                    {"label": "PR URL padded", "text": "  https://github.com/o/r/pull/5  "},
                    {
                        "label": "curl using it",
                        "text": "curl https://github.com/o/r/pull/5 -H accept:json",
                    },
                ],
            }
        ),
    )
    assert r["copyables"] == [
        {"label": "curl using it", "text": "curl https://github.com/o/r/pull/5 -H accept:json"}
    ]


def test_background_terminal_hint_is_scoped_to_current_section():
    from openbus.classify import parser_prompt

    prompts = []

    def capture_prompt(system, _text):
        prompts.append(system)
        return {"tool": "codex", "activity": "idle"}

    classify(_pane("node"), "Background terminals:\n exec session 1: tail -f log", capture_prompt)
    assert "AGENT COUNTS:" in prompts[-1]
    classify(_pane("node"), "  1 background terminal running · /ps to view", capture_prompt)
    assert "AGENT COUNTS:" in prompts[-1]
    classify(_pane("node"), "Background agents:\n review: running", capture_prompt)
    assert prompts[-1] == parser_prompt()
    classify(_pane("node"), "Ready", capture_prompt, prior=["Background terminals:\n old command"])
    assert prompts[-1] == parser_prompt()


def test_codex_title_requires_current_ui_evidence():
    capture = ("› Ask Codex to do anything\n\n"
               "01a0e9d1-093d-7c10-84f4-133c9544c971 · gpt-6-astra medium · "
               "Context 52% left · ~/src/tmux-rc · Ready")
    result = classify(_pane("node"), capture, _llm({
        "tool": "codex", "session": "Track PRs per live session",
    }))
    assert "session" not in result
    assert result["label"] == _pane("node").label


def test_status_fields_are_not_session_title_evidence():
    for name in ("gpt-5.5", "xhigh fast", "fast", "~/src/app"):
        result = classify(_pane("node"), "gpt-5.5 xhigh fast · ~/src/app", _llm({
            "tool": "codex", "session": name, "activity": "idle",
        }))
        assert "session" not in result


def test_session_grounding_preserves_footer_and_rename_titles():
    for capture in (
        "old output\n\n› input\n\nReview 4955 · gpt-6-sol · ~/src/app",
        "• Thread renamed to Review 4955\n" + "output\n" * 10,
    ):
        result = classify(_pane("node"), capture, _llm({"tool": "codex", "session": "Review 4955"}))
        assert result["session"] == "Review 4955"


def test_null_session_does_not_trigger_retry():
    calls = []
    def read(_prompt, text):
        calls.append(text)
        return {"tool": "codex", "session": None, "activity": "idle"}
    result = classify(_pane("node"), "gpt-6-sol · ~/src/app", read)
    assert result["session"] is None
    assert len(calls) == 1


def test_failed_identity_retry_does_not_retire_the_screen():
    for retry in (None, {}, {"session": "Still another title"}):
        replies = iter([{"tool": "codex", "session": "Other title"}, retry])
        result = classify(_pane("codex"), "› input\nReview 4955 · gpt-6-sol · ~/src/app",
                          lambda _prompt, _text, replies=replies: next(replies))
        assert "session" not in result
        assert result["parse_ok"] is False


def test_shell_drops_scrolled_agent_title_without_retry():
    calls = []
    def read(_prompt, text):
        calls.append(text)
        return {"tool": "shell", "session": "Fix login redirects", "activity": "idle"}
    result = classify(_pane(), "Thread renamed to Fix login redirects\nuser@host:~$ ", read)
    assert "session" not in result
    assert len(calls) == 1


def test_returned_shell_prompt_overrides_old_agent_identity_and_actions():
    result = classify(_pane("bash"), "Thread renamed to Fix login redirects\nuser@host:~$ ", _llm({
        "tool": "codex", "session": "Fix login redirects", "activity": "waiting",
        "question": {"prompt": "Old approval?"}, "rewind": {"rows": ["old"]},
    }))
    assert result["tool"] == "shell" and result["activity"] == "idle"
    assert not any(key in result for key in ("session", "question", "rewind", "waiting_on"))


def test_visible_opencode_spinner_wins_over_stale_question_retry():
    calls = []
    def read(_prompt, text):
        calls.append(text)
        if len(calls) == 1:
            return {"tool": "opencode", "question": {"prompt": "Old approval?"}}
        return {"tool": "opencode", "activity": "idle"}
    capture = "Old approval?\n\x1e[visible screen]\x1f\n■⬝ esc interrupt"
    result = classify(_pane("opencode"), capture, read)
    assert len(calls) == 2
    assert "question" not in result
    assert result["activity"] == "running"


def test_failed_question_retry_does_not_retire_the_screen():
    for retry in (None, {}, {"question": {"prompt": "Still an old question?"}}):
        replies = iter([{"tool": "codex", "question": {"prompt": "Old approval?"},
                         "rewind": {"rows": ["old turn"]}, "headline": "Old menu"}, retry])
        result = classify(_pane("codex"), "Old approval?\n\x1e[visible screen]\x1f\n› Ready",
                          lambda _prompt, _text, replies=replies: next(replies))
        assert "question" not in result
        assert not any(key in result for key in ("rewind", "waiting_on", "headline"))
        assert result["activity"] == "unknown"
        assert result["parse_ok"] is False


def test_question_retry_cannot_resurrect_stale_rewind():
    replies = iter([
        {"tool": "codex", "question": {"prompt": "Old approval?"}},
        {"tool": "codex", "activity": "idle",
         "rewind": {"entries": [{"text": "old turn"}]}},
    ])
    result = classify(
        _pane("node"), "Old approval?\n\x1e[visible screen]\x1f\n› Ready",
        lambda _prompt, _text: next(replies),
    )
    assert not any(key in result for key in ("question", "rewind", "waiting_on"))
    assert result["activity"] == "unknown" and result["parse_ok"] is False


def test_non_codex_session_requires_visible_evidence():
    for tool in ("opencode", "gemini"):
        capture = ("\x1e[visible screen]\x1f\n› Ready\n"
                   "{'session': 'Unrelated title'}")
        result = classify(_pane(tool), capture, _llm({
            "tool": tool, "session": "Unrelated title", "activity": "idle",
        }))
        assert "session" not in result


def test_session_grounding_rejects_paths_and_quoted_output():
    for name in ("~/src/app", "/src/app", "app", "other-pane"):
        capture = "{'session': 'other-pane'}\n\noutput\n\n› input\n\n~/src/app · gpt-6-sol"
        result = classify(_pane("node"), capture, _llm({"tool": "codex", "session": name}))
        assert "session" not in result
    capture = ("› input\n\nairbyte-value-population · gpt-5.6-sol medium · "
               "docs/query-result-metadata-design · ~/src/app · Ready")
    result = classify(_pane("node"), capture, _llm({
        "tool": "codex", "session": "docs/query-result-metadata-design",
    }))
    assert "session" not in result


def test_short_capture_does_not_promote_tool_output_to_status_evidence():
    for capture in (
        "{'session': 'other-pane'}\noutput\n› input\n~/src/app · gpt-6-sol",
        "{'session': 'other-pane'}\noutput\n~/src/app · gpt-6-sol",
        "log: Thread renamed to other-pane\n› input\n~/src/app · gpt-6-sol",
        ("› old input\n\x1e[visible screen]\x1f\n{'session': 'other-pane'}\n"
         "Inspecting parser\nWorking\n~/src/app · gpt-6-sol"),
        ("\x1e[visible screen]\x1f\nDone\n"
         "{'footer': 'other-pane · gpt-6-sol · Ready'}"),
        ("\x1e[visible screen]\x1f\n› Ask Codex to do anything\n"
         "{'session': 'other-pane'}\ngpt-6-sol · ~/src/app"),
        ("Thread renamed to other-pane\n\x1e[visible screen]\x1f\n"
         "› Ask Codex to do anything\ngpt-6-sol · ~/src/app"),
    ):
        result = classify(_pane("node"), capture, _llm({"tool": "codex", "session": "other-pane"}))
        assert "session" not in result


def test_claude_title_above_status_bar_is_preserved():
    capture = ("› input\n──────────────────── Fix login redirects\n"
               "~/src/app · Opus 5.5 · 30% context")
    result = classify(_pane("claude"), capture, _llm({
        "tool": "claude", "session": "Fix login redirects",
    }))
    assert result["session"] == "Fix login redirects"


def test_stale_question_is_reread_from_visible_screen_only():
    calls = []
    def read(_prompt, text):
        calls.append(text)
        if len(calls) == 1:
            return {"tool": "codex", "activity": "waiting", "question": {"prompt": "Update now?"}}
        assert "Update now?" not in text
        return {"tool": "codex", "activity": "idle", "headline": "Ready for a request"}
    capture = "Update now?\n\x1e[visible screen]\x1f\n› Ask Codex to do anything"
    result = classify(_pane("node"), capture, read)
    assert len(calls) == 2
    assert "question" not in result and "waiting_on" not in result
    assert result["activity"] == "idle"


def test_visible_question_is_preserved_without_retry():
    calls = []
    def read(_prompt, text):
        calls.append(text)
        return {"tool": "codex", "question": {"prompt": "Allow this command?"}}
    capture = ("history\n\x1e[visible screen]\x1f\nAllow this command?\n"
               "1. Yes\n2. No\n[visible screen]")
    result = classify(_pane("node"), capture, read)
    assert len(calls) == 1
    assert result["question"]["prompt"] == "Allow this command?"
    assert result["activity"] == "waiting"


def test_copyable_table_rows_are_not_duplicated():
    for payload in ("12  Open\n13  Merged", "12 Open", "13 Merged"):
        result = classify(_pane(), "12 Open\n13 Merged", _llm({
            "tables": [{"headers": ["PR", "State"], "rows": [["12", "Open"], ["13", "Merged"]]}],
            "copyables": [{"label": "PR list", "text": payload}],
        }))
        assert "copyables" not in result
        assert len(result["tables"]) == 1


def test_copyables_require_payload_in_current_viewport():
    for capture, expected in (
        ("git commit -m stale\n\x1e[visible screen]\x1f\nUnrelated output", False),
        ("history\n\x1e[visible screen]\x1f\ngit commit -m stale", True),
    ):
        result = classify(_pane(), capture, _llm({
            "copyables": [{"label": "Command", "text": "git commit -m stale"}],
        }))
        assert bool(result.get("copyables")) is expected


def test_scrolled_rename_is_evidence_for_initial_read_and_retry():
    capture = ("• Thread renamed to Fix login redirects\n\x1e[visible screen]\x1f\n"
               "Done\n\n› Ask Codex to do anything\n\ngpt-6-sol · ~/src/app")
    for initial in ("Fix login redirects", "wrong quoted name"):
        calls = []
        def read(_prompt, text, calls=calls, initial=initial):
            calls.append(text)
            if len(calls) == 1:
                assert "• Thread renamed to Fix login redirects" in text
            else:
                assert text.endswith("• Thread renamed to Fix login redirects")
            return {"tool": "codex", "session": initial if len(calls) == 1
                    else "Fix login redirects", "activity": "idle"}
        result = classify(_pane("node"), capture, read)
        assert result["session"] == "Fix login redirects"
        assert len(calls) == (1 if initial == "Fix login redirects" else 2)


def test_cursor_search_binding_requires_visible_footer_evidence():
    for capture, expected in (
        ("Resume session\nType to search · Esc to cancel", True),
        ("Type to search\n\x1e[visible screen]\x1f\nResume session\nEsc to cancel", False),
        ("Resume session\nSearch is not available", False),
    ):
        result = classify(_pane("node"), capture, _llm({
            "tool": "claude", "question": {"prompt": "Resume session",
            "answer_style": "cursor", "keymap": {"select": "Enter", "search": False}},
        }))
        assert result["question"]["keymap"]["search"] is expected


def test_menu_command_becomes_context_not_paste_action():
    result = classify(_pane(), "Allow?\ngit apply patch.diff", _llm({
        "question": {"prompt": "Allow?", "answer_style": "menu", "options": ["Yes", "No"]},
        "copyables": [{"label": "Command", "text": "git apply patch.diff"}],
    }))
    assert "copyables" not in result
    assert result["tables"][0]["rows"] == [["git apply patch.diff"]]
    assert result["question"]["options"] == ["Yes", "No"]


def test_copyable_cannot_cut_a_summary_out_of_prose():
    result = classify(_pane(), "• Tests reject malformed input. No PR was opened.", _llm({
        "copyables": [{"label": "Summary", "text": "Tests reject malformed input."}],
    }))
    assert "copyables" not in result


def test_copyable_can_quote_inline_code_and_wrapped_box():
    result = classify(_pane(), "Run `git status` first.\n│ A wrapped │\n│ message.  │", _llm({
        "copyables": [{"text": "git status"}, {"text": "A wrapped message."}],
    }))
    assert [c["text"] for c in result["copyables"]] == ["git status", "A wrapped message."]
