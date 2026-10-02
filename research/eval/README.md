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
"a server logging `gemini-…` lines is NOT the Gemini CLI" trap is exercised).

## The scoring model

Exact JSON equality is too brittle — headlines are free prose that varies run to run.
So we split the fields by how the UI uses them:

**Structured fields — exact match (strict).** These drive the badge and behavior, so
strictness is correct:
- `tool`, `activity`, `waiting_on` — compared by value.
- `parse_ok`, `model` — opt-in checks for accepted classifications and model preservation.
  An omitted `parse_ok` means success, matching the watcher.
- `agents` and `subagent_states` — opt-in worker checks. When supplied, compare the busy
  background count and the sorted multiset of worker states (including duplicates).
- `working_prs` — opt-in exact comparison of semantic `owner/name#number` associations.
  An explicit empty list pins the important negative case: PRs may be visible without
  this session actually working on any of them.
- `question` — compared by *shape*: present-or-absent, and if present its
  `answer_style` (`menu` vs `text` — the phone sends a keystroke vs typed text, so this
  is behavior). The prompt body is prose, left to the judge.
- `question.reply_buttons` — opt-in: the model-written reply buttons must be an accept
  ("Yes…") then a decline ("No…"); their wording is not pinned.
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
A boolean `tables` expectation checks presence only. An expected table list also
sends the expected and candidate tables to the prose judge, which must confirm every
referenced edit is represented. Sample 16 uses this to reject unrelated tables.

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
| `16_question_refers_to_list` | question naming "edits 1-4" → the list travels with it (`tables`) |
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
| `28_opencode_claude_model` | OpenCode using Claude/Bedrock → tool=opencode, not claude |
| `29_opencode_interrupt_spinner` | OpenCode `esc interrupt` spinner → running, not idle |
| `54_claude_user_turn_is_not_question` | the user's own ❯ turn above a live spinner → running, no question |
| `55_claude_turn_ends_with_user_handoff` | finished turn handing the user a blocked `! command` → text user-wait |
| `56_claude_turn_ends_with_decision` | finished turn asking the user to decide; "1 shell still running" is not work |
| `57_codex_turn_aborted_by_provider_error` | Codex "■ …at capacity" above an empty input → text user-wait offering "try again" |
| `60_omp_bun_idle` | omp under `bun`, idle `π >` status row → tool=omp, idle despite finished job rows |
| `61_omp_running_subagents` | omp waiting on two task subagents → running parent, two running workers |
| `62_omp_ask_picker` | omp `ask` cursor picker → user-wait with the selected option |

Sample 16 records a known prompt-compliance failure. On 2026-09-18, an authorized
Vertex run using the production prompt and `gemini-3.1-flash-lite` for both classifier
and content judge **failed both checks**: the candidate omitted the edits table, and
the judge reported that the required table was missing. The four-edit expectation
remains desired behavior, not a passing production baseline. This case detects the
bug; a production prompt/classifier fix is still needed.

### Committed vs local — what's repo-safe

The corpus is **committed** and runnable by anyone/CI. tmux-rc dogfooding content is
fine verbatim: cwd like `~/src/qs/termiphone`, tmux-rc code/output, PR numbers — the repo
watches itself. The `samples/.gitignore` guards against dropping **raw scratch captures**
(`.txt`, `.png` from `probe.py --save`) into the corpus dir. When you turn a real capture
into a `.json` sample, only scrub content from **another codebase** (proprietary source
that happened to be on screen) or an **actual secret/token/credential**.

Samples here are a mix of **real tmux-rc captures** (the working/running case) and
**faithful synthetic reproductions** (the permission boxes, rewind picker, external-wait,
the log-trap) — synthesized where the only real example would drag in another codebase's
content, or where hand-authoring reproduces the pattern more cleanly than hunting a live
occurrence.

## Adding a sample

1. Capture or author the pane text. If real, scrub only other-codebase content / secrets
   (see above).
2. Decide the blessed `expected` — the structured fields the UI must get right, plus a
   headline that captures the situation.
3. Write `samples/NN_short_name.json` in the shape above.
4. Run `python -m research.eval --sample NN_short_name` and confirm it passes (or, if it
   reveals a real prompt gap, that's a finding — file it like #85).

## Running it

The production parser composes `openbus/parser_prompt.txt` with the Codex, Claude,
and Gemini fragments beside it. Eval uses that same loader. For `--prompt` A/B runs,
provide the composed text, not a template containing fragment placeholders.
LLM captures label `[visible screen]` after history; synthetic samples should place
that boundary explicitly when testing scrollback behavior. The capture layer splits
physical tmux ranges before joining wrapped rows, not by counting joined text lines.

Needs the three Vertex vars in the environment:

```
export GOOGLE_CLOUD_PROJECT=… VERTEX_AI_REGION_GEMINI=… GOOGLE_APPLICATION_CREDENTIALS=…
python -m research.eval
```

(`set -a; . ./.env; set +a` loads them from the repo `.env`; the unquoted OTEL-token
line errors under `source` — export the three vars directly if so.)
