# Prompt-eval harness

A standing regression test for the pane classifier. Runs the production classifier
prompt against a committed corpus of blessed pane samples, scores each candidate output
against a curated expected, and exits non-zero if any sample regresses. One command
gives a pass/fail signal for a **prompt edit** or a **model swap**.

```
python -m research.eval                                   # whole corpus, production prompt + model
python -m research.eval --sample 03_overfire_are_you_good # one sample
python -m research.eval --model gemini-3.5-flash-lite     # A/B a different model
python -m research.eval --prompt path/to/candidate.txt    # A/B a candidate prompt edit
```

Exit code 0 = all pass, 1 = at least one failed — so it drops into CI and into an A/B
loop unchanged.

## Why this exists

Two things this session were validated by hand, each with a throwaway script:

- **#85** — the classifier fired a spurious `question` (the amber "tap me, I need you"
  badge) whenever the agent's OWN turn ended in a "?" ("…or are you good here?"). The
  fix was checked with an ad-hoc fire-rate script over live captures.
- **The 3.1-lite → 3.5-lite benchmark (#81)** — "is the newer model at least as good?"
  was answered by eyeballing 24 screens' JSON in a one-off `probe.py` extension.

Both are the same shape: *run the production prompt over a set of screens, compare the
output to what it should be.* This harness makes that a repeatable artifact instead of a
script you rewrite each time — so the next prompt edit or model swap gets the same
check, and the corpus grows with each case we hit.

It **consolidates** the shared machinery the three prior one-offs each re-implemented
(`research/probe.py`'s `_parse`, `research/validate_question_overfire.py`'s `_classify`,
the bench's model loop): one place — `harness.run_classifier` + `__main__._vertex_caller`
— that assembles the production payload and calls the model. `probe.py` (input-mode
exploration) and `validate_question_overfire.py` (fire-rate A/B) stay as-is; this is the
standing pass/fail harness they were each reaching toward.

## What it runs — the REAL production path

Each sample goes through `openbus.classify.classify(pane, text, llm_fn)` — the exact
function the daemon calls — with `llm_fn` hitting the real Vertex model under
`openbus/parser_prompt.txt` (temperature 0, JSON mime, same as `openbus.llm.classify_text`).
That matters: `classify()` applies the `waiting_on` / `activity` overrides
(question/rewind → `waiting`/`user`; drop stray `waiting_on` off non-waiting panes) that
actually drive the phone UI. We score the **post-override** output — what the UI sees —
not raw model JSON.

A synthetic `Pane` carries each sample's `current_command` so the `[tmux: foreground
process is …]` prefix and tool-anchoring behave as in production (this is how the
"a server logging `gemini-…` lines is NOT the Gemini CLI" trap is exercised). A sample's
`processes` replays the pane's process tree, so an agent installed through npm, whose
foreground is only `node`, is pinned by the script node runs exactly as the daemon does.

## The scoring model

Exact JSON equality is too brittle — headlines are free prose that varies run to run.
So we split the fields by how the UI uses them:

**Structured fields — exact match (strict).** These drive the badge and behavior, so
strictness is correct:
- `tool`, `activity`, `waiting_on` — compared by value.
- `parse_ok`, `model`, `cost` — opt-in checks for accepted classifications and status-row
  readouts (model preservation, cost normalized to dollars).
  An omitted `parse_ok` means success, matching the watcher.
- `agents` and `subagent_states` — opt-in worker checks. When supplied, compare the busy
  background count and the sorted multiset of worker states (including duplicates).
- `status_entries_exclude` — opt-in list of status readouts that must not come back as
  chips, because a field of their own already shows them (context, cost).
- `subagents_include` — opt-in list of partial worker rows; each must exactly match the
  given fields of some parsed row (e.g. a workflow row's `label` and `progress`).
- `working_prs` — opt-in exact comparison of semantic `owner/name#number` associations.
  An explicit empty list pins the important negative case: PRs may be visible without
  this session actually working on any of them. An entry marked `"optional": true` may
  be reported or left out, for a PR whose association the model can fairly miss.
- `question` — compared by *shape*: present-or-absent, and if present its
  `answer_style` (`menu` vs `text` — the phone sends a keystroke vs typed text, so this
  is behavior). The prompt body is prose, left to the judge.
- `question.items` — opt-in exact list: the numbered or lettered rows above a typed
  question that its prompt names ("do 1 now?", "A or B?"), which classify() reads off the
  screen.
- `question.subject` — opt-in exact string: the paragraph or heading that leads into
  those rows, which says what the question decides.
- `question.context` — opt-in exact string: the widget rows classify() reads around a
  menu's prompt (deterministic, so pinned exactly).
- `copyable_texts` — opt-in exact list of copyable texts, for payloads lifted from the
  agent's transcript (`reply`), which are deterministic.
- `question.ask` — opt-in, judged: the plain "The agent wants to … Continue?" restatement
  of an approval prompt must carry the same most consequential effect as the expected
  one, name its destructive steps (and a description/command mismatch), and quote no
  command text. Pinned `null`, it asserts the menu is not restated at all (a numbered
  choice that is not an approval keeps its own question).
- `question.reply_buttons` — opt-in: the model-written reply buttons must be an accept
  ("Yes…") then a decline ("No…"); their wording is not pinned. Pinned `false`, there
  must be none (a handed-over `! command` is not a question to reply to).
- `question.prompt_excludes` — opt-in list of strings the question's prompt must not
  contain (text drawn beside the message, such as a side panel's rows).
- `links` — opt-in list of URLs, each of which must be offered whole among the candidate's
  link hrefs (extra links are fine): a URL the terminal wrapped must not arrive as a fragment.
- `rewind`, `tasks`, `copyables` — compared by *presence* only.
- `tables` — presence too, but **opt-in**: scored only on a sample whose `expected`
  names it. Most screens have no table and take no position, so scoring it everywhere
  would fail existing samples on a field they were never blessed against. "Present"
  means *renderable* — valid table objects carrying at least one nonempty array row —
  because nothing validates the model's shape here and a truthy string or `{}` would
  otherwise score as a list that never reached the screen.

All supplied tables must have string-valued cells in array rows and array headers
(or omitted/null headers);
one valid table cannot hide malformed siblings or rows that break the desktop renderer.
A `tables` expectation checks presence only.

A single structured mismatch fails the sample.

**Free-text — LLM-as-judge (lenient on wording).** `headline` is a mobile-notification
sentence; two different phrasings of the same situation are both correct. A second
Vertex call (temperature 0) is given the screen + expected headline + candidate headline
and rules `PASS`/`FAIL` on whether the candidate describes the **same situation**. It's
lenient on wording, strict on "is this about a materially different thing / misleading?".

**A sample PASSES only if structured matches AND the judge agrees.** Both columns show
in the table, so a failure tells you which half broke — a structured drift (a badge
regression) reads differently from a judge FAIL (the headline wandered).

### A note on nondeterminism

Flash-Lite varies run to run even at temperature 0. A borderline sample can flip between
runs (a headline that drifts, a `tasks` list the model sometimes emits for a status
list). Keep expected values on the fields that are genuinely determined by the screen,
and author captures so the load-bearing signal is unambiguous — the corpus is a
regression gate, not a coin flip. If a sample flakes, tighten the capture rather than
loosen the score.

## The corpus

`samples/*.json` — one file per sample, bundling everything so a case is atomic:

```json
{
  "description": "why this sample exists / what it asserts",
  "current_command": "node",          // the pane's tmux foreground process
  "repository": "owner/name",         // optional local GitHub repository context
  "capture": "…the pane text…",       // what the model sees
  "prior": ["…an earlier frame…"],    // optional: earlier captures, sent as production does
  "reply": "…agent message…",         // optional: the agent's last message, from its transcript
  "processes": ["node …/bin/codex"],  // optional: argv under the pane, for node/bun-hosted agents
  "expected": { "tool": "claude", "activity": "idle", "headline": "…" }
}
```

Coverage highlights — every state, every tool, and the affordances this session
actually hit:

| sample | asserts |
|---|---|
| `01_claude_running` | Claude working (spinner + gerund) → running |
| `02_claude_idle` | Claude at an empty box → idle, no question |
| `03_overfire_are_you_good` | **#85**: agent's own "…are you good here?" → NOT a question |
| `04_overfire_or_if_youve_seen` | **#85**: multi-clause rhetorical "?" → NOT a question |
| `05_claude_permission_box` | genuine tool-held permission box → question (menu), user-wait |
| `06_codex_permission_box` | Codex numbered prompt → tool=codex (not claude) + menu wait |
| `07_waiting_external_subagents` | blocked on background subagents → waiting_on=external |
| `08_waiting_external_ci` | polling PR/CI checks → waiting_on=external |
| `09_claude_rewind` | Esc-Esc rewind picker → rewind present, user-wait |
| `10_claude_compacting` | compaction progress → activity=compacting (not running) |
| `11_shell_idle` | bare shell prompt → tool=shell, idle |
| `12_shell_server_gemini_trap` | server log mentioning `gemini-…` → tool=shell (not gemini) |
| `13_gemini_idle` | Gemini CLI's own chrome → tool=gemini |
| `14_copyable_commit_and_command` | drafted commit text and a command to run elsewhere → copyables |
| `15_codex_thread_title` | Codex status-bar thread title → session name, not model/mode/cwd |
| `16_question_refers_to_list` | question naming "edits 1-4" → the four rows travel with it (`question.items`), no table |
| `17_claude_resume_cursor_picker` | `/resume` cursor picker → user-wait with selected row and keymap |
| `18_claude_done_marker_with_draft` | completed turn plus unsent draft → idle, not running |
| `19_codex_orchestrating_claude` | Claude chrome inside Codex capture output → tool=codex |
| `20_shell_server_codex_trap` | Python logs mentioning Codex/OpenAI models → tool=shell |
| `21_openai_model_claude_chrome` | OpenAI model in ambiguous Claude-like chrome → tool=codex |
| `22_background_worker_states` | mixed worker states and parent wait → external-wait + exact states |
| `23_background_shell_is_not_agent` | Codex background shell terminal → no subagent |
| `24_agents_and_background_terminal` | real agents plus shell terminal → count only coding workers |
| `25_working_pr_review` | active review work associates the pane with that PR |
| `26_pr_list_is_not_work` | a visible `gh pr list` does not create associations |
| `101_bare_pr_numbers_outside_a_checkout` | synthetic: a pane outside any checkout → only the PR written with its repo; bare `#N`s get no repo guessed from the folder or another mention |
| `104_bare_pr_numbers_beside_another_repos_link` | synthetic: outside a checkout, bare `#N`s beside a status-line link to another repo → only the PR whose github.com URL is on screen |
| `28_opencode_claude_model` | OpenCode using Claude/Bedrock → tool=opencode, not claude |
| `29_opencode_interrupt_spinner` | OpenCode `esc interrupt` spinner → running, not idle |
| `54_claude_user_turn_is_not_question` | the user's own ❯ turn above a live spinner → running, no question |
| `55_claude_turn_ends_with_user_handoff` | finished turn handing the user a blocked `! command` → text user-wait |
| `56_claude_turn_ends_with_decision` | finished turn asking the user to decide; "1 shell still running" is not work |
| `57_codex_turn_aborted_by_provider_error` | Codex "■ …at capacity" above an empty input → text user-wait offering "try again" |
| `60_omp_bun_idle` | omp under `bun`, idle `π >` status row → tool=omp, idle despite finished job rows; `S`-prefixed subscription cost → dollars |
| `61_omp_running_subagents` | omp waiting on two task subagents → running parent, two running workers |
| `62_omp_ask_picker` | omp `ask` cursor picker → user-wait with the selected option |
| `63_omp_folder_segment_running` | synthetic: omp outside a repo (`📁 <path>` segment) with a `↻` activity line → running, title not a question |
| `64_omp_wrapped_by_bash` | omp behind a bash wrapper (`current_command=bash`) → tool=omp from its chrome, running with two workers |
| `65_omp_gutter_code_is_not_question` | omp screen + one earlier frame: a question string inside a line-numbered Edit diff (`+246│`) is code, not a prompt → running, no question |
| `66_omp_queued_user_question` | omp's outgoing numbered Steering message → running, no question, even without dim styling |
| `67_omp_queued_messages_with_ask` | Steering/After yield queues beside a genuine Ask picker → preserve the actual cursor question |
| `68_omp_answered_ask_followed_by_work` | answered omp Ask receipt followed by Read/Grep work → running, no stale question or decision table |
| `88_claude_wrapped_login_link_beside_options` | a wrapped OSC 8 login URL beside a prose option list → the whole URL in `links`, even though grounding rejects the misread question |
| `90_claude_question_names_list_items` | a closing "do 1 now and prepare the list for 2?" under a numbered list → those two rows in `question.items`, not the unnamed third |
| `92_claude_question_names_lettered_options` | a bare "Go with A or B?" under lettered options → the A and B rows in `question.items`, and the line leading into them in `question.subject` |
| `93_claude_decision_heading_above_options` | a heading over bulleted "Option A:" / "Option B —" rows, asked "option A or B?" → the heading as `question.subject`, both rows in `question.items` |
| `100_claude_handoff_beside_suggested_command` | a `! command` handoff in Claude's right-barred transcript, its greyed suggestion in the box → text user-wait quoting the agent, no reply buttons |
| `101_claude_bare_command_after_colon_one_line` | a one-line message handing over a bare `! command` after a colon, no backticks → text user-wait offering that command |
| `102_claude_bare_command_after_colon_wrapped` | a long bare `! command` after a colon, wrapped over screen rows but one row ending the transcript → offered whole, not just a copyable |
| `103_claude_offer_outside_auto_needs_you` | synthetic: a finished turn outside auto mode handing over a `! command` (offers) with no question → user-wait (Needs you), not idle |
| `105_claude_side_panel_beside_offer` | a `! command` handoff after a colon with Claude's changes panel split into its `[side panel]` block → the command offered, a question prompt quoting none of the panel |

Sample 16 originally exposed a table-extraction failure: the prompt said to carry a
question's supporting list but also to omit tables unless an actual table was shown.
Rewording that helped for a while, but the parser kept dropping the list (sample 90 is
a live case where it returned no table at all), so classify() now reads the rows off the
screen into `question.items`, and drops the parser's own copy of them (a table keyed by
their numbers, or the rows offered as answer buttons): which rows a prompt names is a
fact about the screen, not a judgement. Deleting the prompt's table instruction was
tried and rejected: the parser then filed the list as `tasks`, and the reworded prompt
flipped the unrelated sample 47.

### Committed vs local — what's repo-safe

The corpus is **committed** and public, so every sample is invented (AGENTS.md: "This
repository is public"). A real capture, even renamed or redacted, still describes the work it
came from: use it only to learn the shape that triggers the bug, then rebuild that shape from
scratch around an unrelated topic, as short as it can be. The `samples/.gitignore` guards
against dropping **raw scratch captures** (`.txt`, `.png` from `probe.py --save`) into the
corpus dir, and `tests/test_public_data.py` rejects the usual tells.

## Adding a sample

1. Author the pane text from scratch (see above): a local capture may show you the shape
   that triggers the bug, but none of its text goes in the sample.
2. Decide the blessed `expected` — the structured fields the UI must get right, plus a
   headline that captures the situation.
3. Write `samples/NN_short_name.json` in the shape above.
4. Run `python -m research.eval --sample NN_short_name` and confirm it passes (or, if it
   reveals a real prompt gap, that's a finding — file it like #85).

## Running it

The production parser composes `openbus/parser_prompt.txt` with the Codex, Claude,
Gemini, and omp fragments beside it. Eval uses that same loader. For `--prompt` A/B runs,
provide the composed text, not a template containing fragment placeholders.
LLM captures label `[visible screen]` after history; synthetic samples should place
that boundary explicitly when testing scrollback behavior. The capture layer splits
physical tmux ranges before joining wrapped rows, not by counting joined text lines.

omp's current title state is supplied to the model separately from transcript text.
Working/idle titles retire completed Ask cards; `!` preserves the active Ask/approval
dialog, including an unsubmitted highlighted option. The right-aligned session label
above the idle footer is not a question. Rejected receipts get one bounded re-read
with their selected answer retained only as task context. Later work after submission
retires an earlier question; scroll position alone does not prove an answer.
Ask radio options are not checklist tasks; genuine visible checkbox rows retain
their rendered completion state.
Stopped provider errors use their visible error text as the headline, not obsolete
progress from before the failure.

Needs the three Vertex vars in the environment:

```
export GOOGLE_CLOUD_PROJECT=… VERTEX_AI_REGION_GEMINI=… GOOGLE_APPLICATION_CREDENTIALS=…
python -m research.eval
```

(`set -a; . ./.env; set +a` loads them from the repo `.env`; the unquoted OTEL-token
line errors under `source` — export the three vars directly if so.)

## Inline replies experiment (closing-question buttons: separate call vs the main parse)

**Question.** A turn that ends on "Should I …?" gets accept/decline reply buttons from a
small extra model call (design A, merged in #282), because the parser prompt deliberately
tells the main parse NOT to emit a `question` for a closing offer above an empty input.
Could the main parse return the buttons in the same response (design B: one new optional
output field, `closing_replies`, plus three comment lines in the schema; `question`
semantics untouched), so the extra call and its cache can be deleted? Branch
`exp/inline-closing-replies` is B; main is A; C is main with the call switched off.

**Method.** `closing_questions/` holds 32 synthetic screens (Claude and Codex chrome,
auto mode on and off): single-step yes/no, gated multi-step, either/or, wh-questions,
open-ended requests, destructive wording, code identifiers, a wrapped paragraph, a bold
question, a `! command` handoff, a provider error, and offers that must stay idle (not auto
mode, user already typed, old question already answered, Codex). Each carries a
`closing_replies` block (kind, and the step words a checkpoint button may name).
`python -m research.eval.closing_replies run --tag X --out X.json --existing` (run it in a tree
with #282 for A, add `--no-replies` there for C, in the experiment branch for B) pushes them, plus the 62 regular samples,
through the production `classify()` five times per condition (gemini-3.1-flash-lite,
temperature 0, the production Vertex path) and records every call's tokens and latency,
the parse's raw output, a deterministic rubric, and a judge-model verdict on button
quality. The rubric: accept first and decline second for yes/no, "Yes, but check with me
before X" only for steps the question names (gated), no buttons for open-ended, either/or
and wh questions, and the idle screens stay idle. The rubric is stricter than the judge,
and both are reported.

**Results (5 runs per case; counts are passes over runs).**

| | A separate call | B inline | C no buttons |
|---|---|---|---|
| yes/no, rubric (of 50) | 40 | 35 | 20 |
| yes/no, judge (of 50) | 50 | 40 | 30 |
| gated multi-step, rubric (of 20) | 13 | 0 | 0 |
| gated, judge (of 20) | 20 | 10 | 20 |
| either/or, wh, open-ended: no buttons (of 55) | 35 | 32 | 41 |
| offers that must stay idle (of 30) | 30 | 30 | 30 |
| regular corpus structured pass (of 310) | 305 | 300 | 295 |
| regular corpus failures | 16 | 16, 59 | 16, 58, 59 |
| screen runs proposing buttons with no question (of 470) | 0 | 35 | 0 |
| first-parse input tokens (mean) | 8706 | 8794 (+88, +1.0%) | 8706 |
| first-parse output tokens (mean) | 155.8 | 156.8 | 154.6 |
| first-parse latency (mean) | 1.51 s | 1.50 s | 1.44 s |
| extra call | 101 over 5 runs, 145 in, 33 out tokens, 0.90 s mean, 1.18 s p90 | none | none |
| production lines (openbus/) vs pre-#282 | +86 net | +66 net (20 fewer than A, including the 4-line prompt field) | 0 |
| all three: closing samples matching their expected tool/activity/question shape (of 160) | 160 | 160 | 160 |

**What the numbers say.**

- Idle detection did not move in any design: the offers that must stay idle stayed idle
  every run, so B does not threaten the idle/closing-offer rule. Sample 16 is the known
  failure.
- B's buttons are worse where it matters. On the real gated capture (sample 58) B returned
  accept and decline and never the per-step checkpoints A gives; on the single-step
  sample 59 it invented a checkpoint ("Yes, but check with me before sending") on every
  run, which fails that sample's accept-then-decline check. Across the four gated synthetic
  cases B produced no usable checkpoint button in 20 runs; it offers partial-scope options
  ("Commit and push only") instead. A single schema comment carrying a conditional rule
  (checkpoint only steps the question names) is not followed by this model; the separate
  call, whose whole input is the question, is.
- B proposes buttons on screens where no closing question is detected (35 of 470 screen runs, one count per run even where `classify()` re-read a slice:
  the not-auto-mode offers, the user-already-typed draft, Codex, and regular samples
  03, 04 and 16). `classify()` discards them, so nothing wrong reaches the phone, but they are
  paid-for output tokens.
- Both designs hand out buttons for some either/or and wh questions, which the spec says
  must be empty; A is a little better on that (35 vs 32 of 55) and A's cache means that
  is one call per question. Where the main parse itself emits a `question` with options
  (about 40% of yes/no screens), those win in every design, so that behaviour is
  independent of this choice.
- Cost is a wash. B adds about 330 bytes of prompt, +88 input tokens per parse (+1.0%).
  Local telemetry shows roughly 4k to 14k parses per busy day at about 15k input tokens
  and about $0.003 each, so B costs on the order of 0.4 to 1.2 million extra input tokens
  and a few tens of cents a day across the fleet. A costs about $0.0001 and 0.9 s per
  distinct closing question, a few cents a day, and the latency is on a path where the
  card has already rendered (the buttons appear on the next tick).
- Run-to-run variance at temperature 0 was low: 4 of 32 cases flipped under A, 1 under B
  and 1 under C.
- Caveat: in this corpus a rejected session name made `classify()` re-read a slice on 73%
  of screens under every condition. That retry costs the same everywhere, so the
  comparison holds, but absolute token and latency figures are inflated by it.

**Decision: keep A, delete nothing.** B saves about 20 production lines and the call, but
loses the checkpoint buttons, regresses the existing sample 59, and spends tokens on every
parse to save a cheap call that only fires on a detected closing question. A hybrid (ask
B inline, fall back to the call when the field is empty) does not help, because B's
failures are wrong options, not empty ones. Revisit if the model or the field's wording
improves enough for B to pass the gated cases; rerun the harness above to check.
