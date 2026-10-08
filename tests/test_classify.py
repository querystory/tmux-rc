"""classify() is now a raw-JSON pipe: it returns the LLM's dict (plus pane_id/label),
with a waiting-override for question/rewind and a no-LLM heuristic fallback."""

import pytest

from openbus import classify as classify_mod
from openbus import tmux
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


def _pane(cmd="bash", title="t"):
    return Pane("work", "0", "bash", "0", "%0", cmd, title, "/home/x/proj")


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
    assert "OpenCode and omp can run Claude, GPT, or Gemini models" in seen["prompt"]


def test_payload_leads_with_foreground_process():
    seen = {}

    def llm(system, text):
        seen["text"] = text
        return {"tool": "shell", "activity": "idle"}

    classify(_pane(cmd="python3"), "some screen", llm)
    first_line = seen["text"].splitlines()[0]
    assert "foreground process" in first_line and "python3" in first_line


def test_no_llm_skips_prompt_composition(monkeypatch):
    def fail():
        raise AssertionError("no-LLM mode must not compose the parser prompt")
    monkeypatch.setattr("openbus.classify.parser_prompt", fail)
    assert classify(_pane("node"), "plain output")["tool"] == "unknown"


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


@pytest.mark.parametrize(("cmd", "title", "tool"), [
    ("omp", "t", "omp"),  # the native binary names itself
    ("bun", "π ⠋ Fix the parser", "omp"),  # a bun install is proven by omp's title
    ("bun", "π: titles off", "omp"),
    ("bun", "π", "omp"),  # titles off and no session label yet
    ("bun", "π calculator", "opencode"),  # a word after π is not a state separator
    ("bun", "πr² calculator", "opencode"),  # no omp separator: the model's read stands
    ("bash", "π > stale title", "opencode"),  # omp has exited; its title lingers
])
def test_omp_identity_comes_from_process_or_title(cmd, title, tool):
    r = classify(_pane(cmd, title), "…", _llm({"tool": "opencode", "activity": "running"}))
    assert r["tool"] == tool


@pytest.mark.parametrize(("spend", "cost"), [
    ("S0.09 (+0.18)", "$0.09 (+$0.18) (sub)"),  # subscription spend plus subagent spend
    ("$0.05", "$0.05"),  # metered
    ("(sub)", "$9"),  # subscription with no spend yet: the row says nothing, model stands
])
@pytest.mark.parametrize("bar", ["▶────4%────╎──272K─", "▶4%────╎──272K─"])  # low %: no dashes
def test_omp_status_row_sets_cost_and_context(spend, cost, bar):
    row = f" ⠋ 1m 3s > ◒ GPT-5.5 > 📁 ~/src > {spend} {bar}◀ 👥 2"
    parsed = {"tool": "omp", "cost": "$9", "working": {"verb": "Delegating"}}
    r = classify(_pane("bun", "π ⠋ x"), f"↻ Delegating\n{row}", _llm(parsed))
    assert (r["cost"], r["context_pct"], r["agents"]) == (cost, 4, 2)
    assert r["working"] == {"verb": "Delegating", "elapsed": "1m 3s"}


@pytest.mark.parametrize(("child", "tool"), [
    ("bun\0/home/x/.bun/bin/omp\0--model\0x\0", "omp"),  # `omp …; exec bash` wrapper
    ("/usr/local/bin/omp\0", "omp"),
    ("vim\0notes.txt\0", "opencode"),  # omp has exited; its title lingers
])
def test_omp_behind_a_shell_is_proven_by_a_live_omp_process(monkeypatch, child, tool):
    proc = {("10", "cmdline"): "bash\0", ("10", "task/10/children"): "11 ",
            ("11", "cmdline"): child}
    for module in (classify_mod, tmux):  # the process walk lives in tmux
        monkeypatch.setattr(module, "proc_read", lambda pid, name: proc.get((pid, name), ""))
    pane = Pane("work", "0", "bash", "0", "%0", "bash", "π ⠧ agent-history-omp", "/x", pid="10")
    r = classify(pane, "…", _llm({"tool": "opencode", "activity": "running"}))
    assert r["tool"] == tool


@pytest.mark.parametrize("parsed", [
    {"tool": "omp", "activity": "waiting", "waiting_on": "external"},
    {"tool": "omp", "activity": "idle", "subagents": [{"label": "a", "state": "running"}]},
    None,  # failed parse: the title alone is the read
])
def test_omp_idle_title_retires_stale_job_rows(parsed):
    r = classify(
        _pane("bun", "π > omp-play"),
        "ⓘ waiting on 1 of 2 jobs 1 done\n π > ◒ GPT-5.5 > 🌳 tmux-rc",
        _llm(parsed),
    )
    assert (r["activity"], r.get("waiting_on"), r.get("parse_ok"), r["agents"]) == (
        "idle", None, None, 0)


@pytest.mark.parametrize("parsed", [
    {"tool": "omp", "activity": "idle"},
    {"tool": "omp", "activity": "waiting", "waiting_on": "external"},  # its own jobs
    None,  # failed parse
])
def test_omp_working_title_is_running(parsed):
    r = classify(_pane("bun", "π ⠋ Fix the parser"), "…", _llm(parsed))
    assert (r["activity"], r.get("parse_ok")) == ("running", None)


@pytest.mark.parametrize(("parsed", "prev", "accepted"), [
    ({"tool": "omp"}, None, True),  # the parse missed the question
    (None, "running", True),  # failed parse: surface the new wait
    (None, "waiting", False),  # failed parse: keep the card that has the answer controls
])
def test_omp_attention_title_is_a_user_wait(parsed, prev, accepted):
    r = classify(_pane("bun", "π ! Pick a color"), "…", _llm(parsed), prev_activity=prev)
    assert (r["activity"], r["waiting_on"]) == ("waiting", "user")
    assert r.get("parse_ok", True) is accepted


def test_omp_idle_title_keeps_a_closing_question():
    question = {"prompt": "Should I merge this?", "answer_style": "text"}
    r = classify(
        _pane("bun", "π > omp-play"),
        "Done. Should I merge this?\n π > ◒ GPT-5.5 > 🌳 tmux-rc",
        _llm({"tool": "omp", "activity": "idle", "question": question,
              "subagents": [{"label": "a", "state": "running"}]}),
    )
    assert (r["activity"], r["waiting_on"], r["question"]["prompt"], r["agents"]) == (
        "waiting", "user", "Should I merge this?", 0)


@pytest.mark.parametrize(("state", "activity", "active"), [
    ("⠋", "running", False),
    (">", "idle", False),
    ("!", "waiting", True),
])
def test_omp_old_ask_cannot_override_current_input_state(state, activity, active):
    prompt = "Which color do you prefer?"
    screen = (
        f"\x1e[visible screen]\x1f\n? Ask\n{prompt}\n○ Red\n● Green\n\n"
        "Read web/m/app.js\nTable rendering uses headers and rows.\n\n"
        "  ⎋ Reading table renderer\n ⠋ 2m > ◒ GPT-5.5 > 🌳 tmux-rc\n╰─\n"
    )
    if active:
        screen = _sample("62_omp_ask_picker")
    # The provider repeats the same wrong receipt on a bounded retry.
    result = classify(_pane("bun", f"π {state} Table renderer"), screen, _llm({
        "tool": "omp", "activity": "waiting", "waiting_on": "user",
        "question": {"prompt": prompt, "answer_style": "cursor", "options": ["Red", "Green"]},
        "tables": [{"headers": ["Option"], "rows": [["Red"], ["Green"]]}],
    }))
    assert result["activity"] == activity
    assert result.get("parse_ok", True) is True
    if active:
        assert result["waiting_on"] == "user" and result["question"]["prompt"] == prompt
    else:
        assert not any(result.get(key) for key in ("question", "waiting_on", "tables"))


@pytest.mark.parametrize("state", ["⠋", ">"])
@pytest.mark.parametrize("question", [{"answer_style": "cursor"}, {"prompt": None}, "Pick one?"])
def test_omp_malformed_question_is_rejected_without_raising(state, question):
    screen = "\x1e[visible screen]\x1f\n? Ask\nPick one?\n● Red\n\nRead web/m/app.js\n"
    result = classify(_pane("bun", f"π {state} Table renderer"), screen, _llm({
        "tool": "omp", "activity": "waiting", "waiting_on": "user", "question": question,
    }))
    assert not result.get("question")


_RECEIPT = "? Ask\nWhich color do you prefer?\n○ Red\n● Green\n\nRead web/m/app.js\n\n"
_LIVE_ASK = "╭─ Ask ──╮\n│ Which shade? │\n├──┤\n│ ❯ ○ Light │\n╰──╯\n"


@pytest.mark.parametrize("style", ["text", None])
@pytest.mark.parametrize(("title", "screen", "prompt"), [
    ("π > Colors", _RECEIPT + " π > ◒ GPT-5.5\n", "Which color do you prefer?"),
    ("π > Colors", "61_omp_running_subagents", "Which color do you prefer?"),  # boxed
    ("t", "67_omp_queued_messages_with_ask", "Are the regressions passing?"),  # no omp title
    ("π ! Ask Preferred Color Choice", "67_omp_queued_messages_with_ask",
     "Ask Preferred Color Choice"),  # the ⎋ activity row's session label
    ("π ! Ask Shade", _RECEIPT + _LIVE_ASK, "Which color do you prefer?"),
    ("π ! Ask Preferred Color Choice", "67_omp_queued_messages_with_ask",
     "Are the regressions passing?"),
    ("π > Colors", "67_omp_queued_messages_with_ask", "Are the regressions passing?"),
])
def test_omp_receipts_and_queued_input_are_not_questions(title, screen, prompt, style):
    if screen[:3] in ("61_", "67_"):
        screen = _sample(screen)
    result = classify(_pane("omp", title), f"\x1e[visible screen]\x1f\n{screen}", _llm({
        "tool": "omp", "activity": "waiting", "waiting_on": "user",
        "question": {"prompt": prompt, "answer_style": style},
    }))
    assert (result.get("question") or {}).get("prompt") != prompt


def test_omp_rejected_queue_text_keeps_the_whole_viewport_for_the_retry():
    seen = []
    classify(_pane("bun", "π ⠦ Follow PR293 Copilot Review"),
             _sample("66_omp_queued_user_question"),
             lambda system, text: seen.append(text) or {
                 "tool": "omp", "activity": "waiting", "waiting_on": "user",
                 "question": {"prompt": "Did you test the omp history search stuff"}})
    retry = seen[-1]
    assert "Completed Ask receipt" not in retry and "Reviewing the documentation" in retry


@pytest.mark.parametrize(("closing", "kept"), [
    ("", False), ("\nDone. Should I merge this?\n", True),
])
def test_omp_receipt_header_above_the_visible_boundary(closing, kept):
    capture = _sample("61_omp_running_subagents")
    cut = capture.index("\n", capture.index("? Ask"))  # the header scrolled into history
    screen = f"{capture[:cut]}\n\x1e[visible screen]\x1f{capture[cut:]}{closing}"
    prompt = "Should I merge this?" if kept else "Which color do you prefer?"
    result = classify(_pane("omp", "π > Colors"), screen, _llm({
        "tool": "omp", "activity": "waiting",
        "question": {"prompt": prompt, "answer_style": "text"},
    }))
    assert ((result.get("question") or {}).get("prompt") == prompt) is kept


@pytest.mark.parametrize(("prompt", "kept"), [
    ("Are the regressions passing?", False), ("Which color do you prefer?", True),
])
def test_omp_queue_heading_above_the_visible_boundary(prompt, kept):
    capture = _sample("67_omp_queued_messages_with_ask").replace("\x1e[visible screen]\x1f\n", "")
    cut = capture.index("\n", capture.index("After yield"))  # the heading scrolled off
    screen = f"{capture[:cut]}\n\x1e[visible screen]\x1f{capture[cut:]}"
    result = classify(_pane("omp", "π ! Ask Preferred Color Choice"), screen, _llm({
        "tool": "omp", "activity": "waiting",
        "question": {"prompt": prompt, "answer_style": "text"},
    }))
    assert ((result.get("question") or {}).get("prompt") == prompt) is kept


def test_screen_inferred_omp_drops_a_rejected_questions_tables():
    screen = "\x1e[visible screen]\x1f\n" + _sample("61_omp_running_subagents")
    result = classify(_pane("bash", "t"), screen, _llm({
        "tool": "omp", "activity": "waiting", "waiting_on": "user",
        "question": {"prompt": "Which color do you prefer?", "answer_style": "text"},
        "tables": [{"headers": ["Option"], "rows": [["Red"], ["Green"]]}],
    }))
    assert not result.get("question") and not result.get("tables")


def test_omp_idle_session_label_is_not_a_question():
    label = "Ask Preferred Color Choice"
    result = classify(
        _pane("bun", "π > omp-play"), _sample("60_omp_bun_idle"),
        _llm({"tool": "omp", "activity": "waiting", "waiting_on": "user",
              "question": {"prompt": label, "answer_style": "text"}}),
    )
    assert result["activity"] == "idle"
    assert not any(result.get(key) for key in ("question", "waiting_on"))


@pytest.mark.parametrize(
    ("checked", "unchecked"), [("☑", "☐"), ("[x]", "[ ]"), ("\uf14a", "\uf096")],
)
def test_omp_ask_radios_do_not_become_tasks(checked, unchecked):
    screen = (
        "Ask\n○ Keep the fix scoped\n● Also fix table extraction\n\nTodo\n"
        f"├─ {checked} Inspect parser\n└─ {unchecked} Fix table extraction\n"
        " ⠋ 2m > ◒ GPT-5.5 > 🌳 tmux-rc\n"
    )
    tasks = [
        {"text": text, "done": done} for text, done in (
            ("Keep the fix scoped", False), ("Also fix table extraction", True),
            ("Inspect parser", False), ("Fix table extraction", True),
        )
    ]
    result = classify(_pane("bun", "π ⠋ Fix extraction"), screen, _llm({
        "tool": "omp", "activity": "running", "tasks": tasks,
    }))
    assert result["tasks"] == [
        {"text": "Inspect parser", "done": True},
        {"text": "Fix table extraction", "done": False},
    ]


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


def test_only_latest_rename_is_session_evidence():
    capture = ("• Thread renamed to Old task\n• Thread renamed to New task\n"
               "\x1e[visible screen]\x1f\n› Ask Codex to do anything\n"
               "gpt-6-sol · ~/src/app · Ready")
    old = classify(_pane("node"), capture, _llm({"tool": "codex", "session": "Old task"}))
    new = classify(_pane("node"), capture, _llm({"tool": "codex", "session": "New task"}))
    assert "session" not in old
    assert new["session"] == "New task"


def test_session_grounding_preserves_bracketed_footer_title():
    capture = "› input\n\n[PR 123] Fix login · gpt-6-sol · ~/src/app"
    result = classify(_pane("node"), capture, _llm({
        "tool": "codex", "session": "PR 123",
    }))
    assert result["session"] == "[PR 123] Fix login"


def test_null_session_does_not_trigger_retry():
    calls = []
    def read(_prompt, text):
        calls.append(text)
        return {"tool": "codex", "session": None, "activity": "idle"}
    result = classify(_pane("node"), "gpt-6-sol · ~/src/app", read)
    assert result["session"] is None
    assert len(calls) == 1


def test_failed_identity_retry_preserves_valid_current_state():
    for retry in (None, {}, {"session": "Still another title"}):
        replies = iter([{"tool": "codex", "session": "Other title", "activity": "idle",
                         "headline": "Review complete", "model": "gpt-6-sol"}, retry])
        result = classify(_pane("codex"), "› input\nReview 4955 · gpt-6-sol · ~/src/app",
                          lambda _prompt, _text, replies=replies: next(replies))
        assert "session" not in result
        assert result.get("parse_ok") is not False
        assert result["activity"] == "idle"
        assert result["headline"] == "Review complete"
        assert result["model"] == "gpt-6-sol"


def test_uuid_footer_does_not_freeze_completed_model_selection():
    uuid = "01a0e9d1-093d-7c10-84f4-133c9544c971"
    capture = ("Select Model and Effort\n1. GPT-6-Sol\n2. GPT-6-Astra\n"
               "\x1e[visible screen]\x1f\nWorked for 8m 32s · 5:16 PM\n"
               f"› Ask Codex to do anything\n{uuid} · GPT-6.1-Sol medium · ~/src/tmux-rc")
    result = classify(_pane("codex"), capture, _llm({
        "tool": "codex", "session": uuid, "activity": "idle", "model": "GPT-6.1-Sol",
    }))
    assert "session" not in result
    assert "question" not in result
    assert result.get("parse_ok") is not False
    assert result["activity"] == "idle"
    assert result["model"] == "GPT-6.1-Sol"


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
        ("\x1e[visible screen]\x1f\n› Ask Codex to do anything\n"
         "other-pane · gpt-6-sol · Ready\ngpt-6-sol · ~/src/app · Ready"),
    ):
        result = classify(_pane("node"), capture, _llm({"tool": "codex", "session": "other-pane"}))
        assert "session" not in result


def test_prose_with_model_and_ready_is_not_status_evidence():
    capture = "› Ask Codex to do anything\noutput: other-pane · gpt-6-sol · ~/src/app · Ready"
    result = classify(_pane("node"), capture, _llm({
        "tool": "codex", "session": "other-pane", "activity": "idle",
    }))
    assert "session" not in result

    title = "Fix: login redirects"
    result = classify(_pane("node"), f"› input\n{title} · gpt-6-sol · ~/src/app", _llm({
        "tool": "codex", "session": title, "activity": "idle",
    }))
    assert result["session"] == title


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


@pytest.mark.parametrize("row", [
    '│+246│····prompt = "Which color?"', " ├─   *65│Which color?", "  12│Which color?",
])
def test_question_behind_line_number_gutter_is_file_content(row):
    replies = iter([{"tool": "omp", "activity": "waiting", "question": {"prompt": "Which color?"}},
                    {"tool": "omp", "activity": "running"}])
    capture = f"\x1e[visible screen]\x1f\n{row}\n ⠙ 3m > ◒ GPT-5.5"
    result = classify(_pane("bun"), capture, lambda *_: next(replies))
    assert "question" not in result and result["activity"] == "running"


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
    for payload in (
        "PR  State\n12  Open\n13  Merged",
        "12  Open\n13  Merged",
        "12 Open",
        "13 Merged",
    ):
        result = classify(_pane(), "PR State\n12 Open\n13 Merged", _llm({
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


def test_action_and_identity_retry_keeps_scrolled_rename_evidence():
    calls = []
    def read(_prompt, text):
        calls.append(text)
        if len(calls) == 1:
            return {"tool": "codex", "session": "Wrong title", "activity": "waiting",
                    "question": {"prompt": "Old approval?"}}
        assert "› Ready" in text and "• Thread renamed to Fix login redirects" in text
        return {"tool": "codex", "session": "Fix login redirects", "activity": "idle"}
    capture = ("Old approval?\n• Thread renamed to Fix login redirects\n"
               "\x1e[visible screen]\x1f\n› Ready\ngpt-6-sol · ~/src/app")
    result = classify(_pane("node"), capture, read)
    assert result["session"] == "Fix login redirects" and result["activity"] == "idle"
    assert "question" not in result and len(calls) == 2


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


def _sample(name, field="capture"):
    import json
    from pathlib import Path
    path = Path(__file__).parents[1] / "research/eval/samples" / f"{name}.json"
    return json.loads(path.read_text(encoding="utf-8"))[field]


def test_menu_context_is_read_off_its_own_widget():
    """The bare "Do you want to proceed?" gets the widget's rows (tool, description,
    command, blocking notice); the samples pin each layout's exact string end to end."""
    def ask(capture, prompt, style="menu", **extra):
        return classify(_pane("node"), capture, _llm({"tool": "claude", "question": {
            "prompt": prompt, "answer_style": style, "options": ["Yes", "No"], **extra}}))
    proceed, run = "Do you want to proceed?", "Would you like to run the following command?"
    for name, prompt in (("69_claude_permission_context", proceed),
                         ("05_claude_permission_box", proceed), ("06_codex_permission_box", run),
                         ("75_codex_permission_prompt_in_command", run),
                         ("76_codex_numbered_rows_in_command", run),
                         ("78_claude_box_row_ends_in_glyph", proceed),
                         ("79_codex_unframed_rows_keep_glyphs", run),
                         ("80_codex_numbered_row_then_prompt_in_command", run)):
        got = ask(_sample(name), prompt)["question"].get("context")
        assert got == _sample(name, "expected")["question"]["context"]
    # Unframed (no edge), a command's own "│" or "━━━" row is content, not frame.
    unframed = _sample("79_codex_unframed_rows_keep_glyphs")
    for row in ("    │ build report\n", "    ━━━━━━━━━━━━\n"):
        assert ask(unframed.replace(row, ""), run)["question"]["context"] != (
            ask(unframed, run)["question"]["context"])
    # A command's own "1." row is not option 1: commands sharing that prefix stay distinct.
    numbered = _sample("76_codex_numbered_rows_in_command")
    assert ask(numbered.replace("rm -rf build", "rm -rf src"), run)["question"]["context"] != (
        ask(numbered, run)["question"]["context"])
    # A cursor picker is a plain choice: its prompt boxed earlier in the transcript is no
    # widget to read, nor an approval to restate.
    picker = ask(_sample("62_omp_ask_picker"), "Which color do you prefer?", style="cursor")
    assert "context" not in picker["question"]
    # Indentation inside the command is content; only the widget's margin goes.
    nested = ("\x1e[visible screen]\x1f\n───\n │ python - <<EOF\n │ if x:\n │     go()\n"
              " │ EOF\n\n Go?")
    assert ask(nested, "Go?")["question"]["context"] == "python - <<EOF\nif x:\n    go()\nEOF"
    spaced = nested.replace("if x:\n", "if x:\n │\n │ ━━━\n")  # blank rows and rule text too
    want = "python - <<EOF\nif x:\n\n━━━\n    go()\nEOF"
    assert ask(spaced, "Go?")["question"]["context"] == want
    # Only a ╭ box's own right border goes: a row's trailing "│" is content, so commands
    # differing by one never share an identity.
    box = "\x1e[visible screen]\x1f\n╭────╮\n│ echo a │ │\n│ Go?      │\n│ 1. Yes   │\n╰────╯"
    assert ask(box, "Go?")["question"]["context"] == "echo a │"
    assert ask(nested.replace(" EOF\n", " EOF │\n"), "Go?")["question"]["context"].endswith("EOF │")
    # The edge may sit a full 16 rows above the prompt's own row.
    edge = "\x1e[visible screen]\x1f\n───\n Bash command\n" + "\n" * 14 + " Do you want to proceed?"
    assert ask(edge, "Do you want to proceed?")["question"]["context"] == "Bash command"
    # No widget edge close above: those rows are the conversation, and the model's own
    # context/ask are never passed through.
    plain = "\x1e[visible screen]\x1f\n● Ran the build.\nDo you want to proceed?\n❯ 1. Yes\n  2. No"
    q = ask(plain, "Do you want to proceed?", context="made up", ask="made up")
    q = q["question"]
    assert not {"context", "ask"} & q.keys()
    # A command the model also offered as a copyable shows once, in the widget's own rows.
    held = classify(_pane("node"), _sample("69_claude_permission_context"), _llm({
        "tool": "claude", "copyables": [{"label": "Command", "text": "for p in 4100 4200; do "
                                         "kill $(ss -ltnp | grep \":$p \" | grep -o 'pid=[0-9]*'"
                                         " | cut -d= -f2); done"}],
        "question": {"prompt": "Do you want to proceed?", "answer_style": "menu"}}))
    assert "tables" not in held and "copyables" not in held
    assert "context" not in ask(_sample("69_claude_permission_context"),
                                "Do you want to proceed?", style="text")["question"]


def test_widget_ask_is_restated_once_per_widget():
    classify_mod._asks.clear()
    capture, calls = _sample("69_claude_permission_context"), []
    def restate(reply):
        return lambda system, text: calls.append(text) or reply
    def ask(capture, replies_fn):
        return classify(_pane("node"), capture, _llm({"tool": "claude", "question": {
            "prompt": "Do you want to proceed?", "answer_style": "menu",
            "options": ["Yes", "No"]}}), replies_fn=replies_fn)["question"]
    good = restate({"ask": "The agent wants to kill what listens on 4100 and 4200. Continue?"})
    q = ask(capture, good)
    assert q["ask"].startswith("The agent wants to kill")
    # "Latest blocked action: [Git Destructive]" names an EARLIER action: no tag from it.
    assert "flag" not in q
    assert "cut -d= -f2); done" in calls[0] and "Options: Yes / No" in calls[0]  # uncut
    ask(capture, good)
    assert len(calls) == 1  # cached per widget...
    ask(capture.replace("4200", "4300"), good)
    ask(capture.replace("ss -ltnp", "ss  -ltnp"), good)
    assert len(calls) == 3  # ...so a new command, even by whitespace, is a new ask
    # A failed call, or one off the asked-for shape, leaves the bare prompt (never the
    # raw rows) and is retried next time.
    assert "ask" not in ask(capture.replace("4100", "3"), restate({"ask": "Sure, kill them."}))
    off_shape = restate({"ask": "The agent is unable to summarize this?"})
    assert "ask" not in ask(capture.replace("4100", "4"), off_shape)
    # A numbered choice that is no approval keeps its own question, with no call made.
    choice = classify(_pane("node"), _sample("77_claude_numbered_choice_not_approval"),
                      _llm({"tool": "claude", "question": {
                          "prompt": "Which environment should I deploy to?",
                          "answer_style": "menu", "options": ["Staging", "Production"]}}),
                      replies_fn=good)["question"]
    assert choice["context"] == "Deploy target" and "ask" not in choice
    assert len(calls) == 5  # unchanged
    bad = classify(_pane("node"), capture, _llm({"tool": "claude", "question": {
        "prompt": "Do you want to proceed?", "answer_style": "menu", "options": 1}}),
        replies_fn=good)["question"]  # malformed options: no restatement, no crash
    assert bad["context"] and "ask" not in bad and len(calls) == 5
    failed = capture.replace("4100", "2")
    assert "ask" not in ask(failed, restate(None))
    ask(failed, restate(None))
    assert len(calls) == 7


def test_users_own_turn_under_a_live_spinner_is_not_a_question():
    capture = _sample("54_claude_user_turn_is_not_question")
    user_turn = "should we do that yet or wait a bit longer?"
    calls = []
    def read(_prompt, _text):
        calls.append(1)
        return {"tool": "claude", "activity": "waiting", "question": {"prompt": user_turn}}
    result = classify(_pane("claude"), capture, read)
    assert len(calls) == 2  # rejected, then re-read once
    assert "question" not in result and "waiting_on" not in result
    assert result["activity"] == "running"  # the live spinner is authoritative
    assert "parse_ok" not in result  # so the watcher accepts it over the stale card
    # Either signal alone rejects it: the ❯ row, or a live spinner below agent text.
    agent_text = "\x1e[visible screen]\x1f\n● Push now?\n\n✶ Pushing… (3s · esc to interrupt)"
    # Claude chrome quoted in another tool's pane says nothing about that tool's question.
    codex = classify(_pane("codex"), agent_text, _llm({"question": {"prompt": "Push now?"}}))
    assert codex["question"]["prompt"] == "Push now?"
    for screen, prompt in ((capture.split("· Befuddling")[0], user_turn),
                           (agent_text, "Push now?")):
        result = classify(_pane("claude"), screen, _llm({
            "tool": "claude", "activity": "idle", "question": {"prompt": prompt}}))
        assert "question" not in result


def test_finished_turn_blocked_on_the_user_is_a_text_wait():
    cases = {
        "55_claude_turn_ends_with_user_handoff": "The PRD's local fix is committed",
        "56_claude_turn_ends_with_decision": "- Live text mode prototype: should I start it",
        "57_codex_turn_aborted_by_provider_error": "Selected model is at capacity.",
    }
    for name, prompt in cases.items():
        # The model's "running" (background shells/monitors) and "idle" both lose.
        tool = name.split("_")[1]
        result = classify(_pane("node"), _sample(name), _llm({"tool": tool, "activity": "running"}))
        assert result["question"]["prompt"].startswith(prompt)
        assert result["question"]["answer_style"] == "text"
        assert result["activity"] == "waiting" and result["waiting_on"] == "user"
    assert result["question"]["options"] == ["try again"]


def test_finished_turn_is_not_blocked_once_answered_or_outside_auto_mode():
    decision = _sample("56_claude_turn_ends_with_decision")
    error = _sample("57_codex_turn_aborted_by_provider_error")
    for tool, screen in (
        ("claude", decision.replace("auto mode on", "accept edits on")),  # an optional offer
        ("claude", decision.replace("\n❯\n", "\n❯ yes start it\n")),  # already answered
        ("codex", error.replace("⟪placeholder⟫Ask Codex to do anything⟪/placeholder⟫",
                                "try again")),
        ("codex", error.replace("■ Selected model is at capacity. Please try a different model.",
                                "■ Conversation interrupted - tell the model what to do.")),
    ):
        result = classify(_pane(tool), screen, _llm({"activity": "idle"}))
        assert "question" not in result
    assert classify(_pane("claude"), decision.replace("auto mode on", "plan mode on"), _llm({
        "tool": "claude", "activity": "running"}))["activity"] == "idle"  # the turn is over
    # A boxed or indented prompt row with text counts as typed, and a model question
    # from the finished turn is answered once the user types after it.
    insert = decision.replace("⏵⏵ auto mode on", "-- INSERT -- ⏵⏵ auto mode on")
    assert classify(_pane("claude"), insert, _llm({}))["question"]["answer_style"] == "text"
    prose = decision.replace("✻ Worked for 40s", "* Wait for 2s").replace("✻ Cooked", "* Cooked")
    assert classify(_pane("claude"), prose, _llm({"activity": "running"}))["activity"] == "running"
    quoted = decision.replace("auto mode on", "accept edits on").replace(
        "Still open:", "● The footer says ⏵⏵ auto mode on when enabled.\n  Still open:")
    assert "question" not in classify(_pane("claude"), quoted, _llm({}))
    newer = decision.replace("\n❯\n", "\n❯ run tests\n\n● Running tests\n")
    assert classify(_pane("claude"), newer, _llm({"activity": "running"}))["activity"] == "running"
    empty_box = decision.replace("\n❯\n", "\n│ ❯                │\n")
    assert classify(_pane("claude"), empty_box, _llm({}))["question"]["answer_style"] == "text"
    for row in ("│ ❯ yes start it │", "  ❯ yes start it"):
        answered = decision.replace("\n❯\n", f"\n{row}\n")
        ask = "should I start it in the background"
        result = classify(_pane("claude"), answered, _llm({"question": {"prompt": ask}}))
        assert "question" not in result
    failed = classify(_pane("claude"), decision.replace("auto mode on", "plan mode on"),
                      lambda _s, _t: None, prev_activity="running")
    assert failed["activity"] == "idle" and "parse_ok" not in failed


def test_claude_api_error_ending_the_turn_offers_a_retry():
    screen = ("\x1e[visible screen]\x1f\n● Fixing the parser.\n"
              "  ⎿  API Error: 529 overloaded_error\n\n❯\n  ~/src/app · Opus 5.5")
    result = classify(_pane("claude"), screen, _llm({"tool": "claude", "activity": "idle"}))
    assert result["question"] == {"prompt": "API Error: 529 overloaded_error",
                                  "answer_style": "text", "options": ["try again"]}
    # Deterministic chrome is a read of the screen even when the model call failed.
    assert classify(_pane("claude"), screen, lambda _s, _t: None)["question"] == result["question"]
    assert "parse_ok" not in classify(_pane("claude"), screen, lambda _s, _t: None)
    stale = classify(_pane("claude"), screen, _llm({"question": {"prompt": "Fixing the parser."}}))
    assert stale["question"]["options"] == ["try again"]  # the error ended the turn after it
    menu = {"prompt": "Close them?", "answer_style": "menu", "options": ["Yes", "No"]}
    decision = classify(_pane("claude"), _sample("56_claude_turn_ends_with_decision"),
                        _llm({"question": menu}))
    assert decision["question"]["answer_style"] == "text"
    only_command = ("\x1e[visible screen]\x1f\n● ! git push\n✻ Worked for 3s\n❯\n"
                    "⏵⏵ auto mode on")
    assert classify(_pane("claude"), only_command, _llm({}))["question"]["prompt"] == "! git push"
    bare = classify(_pane("claude"), screen.replace("  ⎿  API Error: 529", "API Error handling"),
                    _llm({}))
    assert "question" not in bare  # only the ⎿ result marker is provider-error chrome
    shell = classify(_pane("bash"), "■ Build failed\nuser@host:~$ ", _llm({"activity": "idle"}))
    assert "question" not in shell and shell["activity"] == "idle"
    # Another tool's chrome is output, not this pane's turn.
    claude = classify(_pane("claude"), "● Ran make\n■ Build failed\n\n❯", _llm({}))
    codex = classify(_pane("codex"), screen.replace("  ⎿  ", "✻ Worked for 3s\n"), _llm({}))
    assert "question" not in claude and "question" not in codex


def _auto_screen(message):
    return f"\x1e[visible screen]\x1f\n● {message}\n✻ Baked for 16s\n\n❯\n  ⏵⏵ auto mode on"


@pytest.mark.parametrize("message", [
    "Done.\n  Should I send this to\n  Copilot, then merge\n  after approval?",
    ("Done.\n\n  - first item\n  - second item\n\n  Should I send this to\n"
     "  Copilot, then merge after approval?"),
    ("Done.\n  - item one. Should I send this to\n"
     "    Copilot, then merge after approval?"),
    "Done.\n  **Should I send this to Copilot, then merge after approval?**",
])
def test_wrapped_closing_question_is_read_whole(message):
    result = classify(_pane("claude"), _auto_screen(message), _llm({"tool": "claude"}))
    prompt = result["question"]["prompt"]
    assert "Should I send this to Copilot, then merge after approval?" in prompt
    assert "first item" not in prompt
    assert "options" not in result["question"]  # no replies_fn: no buttons


@pytest.fixture
def replies():
    classify_mod._replies.clear()
    calls = []

    def make(answer):
        def fn(system, text):
            assert system == classify_mod._REPLIES_SYSTEM
            calls.append(text)
            return answer
        return fn

    yield make, calls
    classify_mod._replies.clear()


ASK = "Should I send it to Copilot, then merge?"


def _ask(replies_fn, model_q=None, message=ASK):
    result = classify(_pane("claude"), _auto_screen(message),
                      _llm({"tool": "claude", "question": model_q}), replies_fn=replies_fn)
    return result["question"]


def test_closing_question_gets_model_replies_accept_then_decline_once(replies):
    make, calls = replies
    answer = {"options": ["Yes, do both", "Yes, but check before merging", "Hold off", ASK]}
    for _ in range(2):
        assert _ask(make(answer))["options"] == [
            "Yes, do both", "Hold off", "Yes, but check before merging"]
    assert calls == [ASK]  # one call per distinct question, and it sees only the question


def test_open_ended_or_failed_replies_show_no_buttons(replies):
    make, calls = replies
    for answer in (None, {"options": "junk"}, ["Yes"], {}, {"options": []}):
        assert "options" not in _ask(make(answer), message="Which env should I use?")
    assert len(calls) == 5  # failures retry; the empty list is an answer and is cached
    asked = "Which env should I use?"
    assert "options" not in _ask(make({"options": ["Yes", "No"]}), message=asked)
    assert len(calls) == 5  # ... so the cached empty answer stood


def test_model_options_for_the_same_question_skip_the_extra_call(replies):
    make, calls = replies
    own = {"prompt": ASK.lower(), "options": ["go", "wait"]}
    assert _ask(make({"options": ["x", "y"]}), own)["options"] == ["go", "wait"]
    other = {"prompt": "Pick a color", "options": ["red"]}
    assert _ask(make({"options": ["Yes", "No"]}), other)["options"] == ["Yes", "No"]
    assert calls == [ASK]


def test_decline_survives_the_cap_and_a_lone_option_is_unusable(replies):
    make, calls = replies
    five = {"options": ["Yes, all", "Check a", "Check b", "Check c", "No"]}
    assert _ask(make(five))["options"] == ["Yes, all", "No", "Check a", "Check b"]
    assert "options" not in _ask(make({"options": ["Yes"]}), message="Should I stop?")
    assert "options" not in _ask(make({"options": ["Yes"]}), message="Should I stop?")
    assert "options" not in _ask(make({"options": ["\x1b", ""]}), message="Should I stop?")
    assert len(calls) == 4  # neither the lone option nor all-junk was cached


def test_reply_options_are_sanitized(replies):
    make, _ = replies
    raw = ["Yes", "yes", "ok\x1b[A", "no\n", "x" * 61, 7, ASK, "Maybe", "Later", "Never", "No"]
    assert _ask(make({"options": raw}))["options"] == ["Yes", "No", "Maybe", "Later"]


def test_provider_error_retry_is_deterministic(replies):
    make, calls = replies
    screen = ("\x1e[visible screen]\x1f\n● Fixing.\n  ⎿  API Error: 529 overloaded_error\n\n❯\n")
    out = classify(_pane("claude"), screen, _llm({}), replies_fn=make({"options": ["a", "b"]}))
    assert out["question"]["options"] == ["try again"] and not calls
