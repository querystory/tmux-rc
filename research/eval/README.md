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

Each sample goes through `daemon.classify.classify(pane, text, llm_fn)` — the exact
function the daemon calls — with `llm_fn` hitting the real Vertex model under
`daemon/parser_prompt.txt` (temperature 0, JSON mime, same as `daemon.llm.classify_text`).
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
- `question` — compared by *shape*: present-or-absent, and if present its
  `answer_style` (`menu` vs `text` — the phone sends a keystroke vs typed text, so this
  is behavior). The prompt body is prose, left to the judge.
- `rewind`, `tasks` — compared by *presence* only.

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
  "capture": "…the pane text…",       // what the model sees
  "expected": { "tool": "claude", "activity": "idle", "headline": "…" }
}
```

Coverage (13 samples, quality over quantity) — every state, every tool, and the
affordances this session actually hit:

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

Needs the three Vertex vars in the environment:

```
export GOOGLE_CLOUD_PROJECT=… VERTEX_AI_REGION_GEMINI=… GOOGLE_APPLICATION_CREDENTIALS=…
python -m research.eval
```

(`set -a; . ./.env; set +a` loads them from the repo `.env`; the unquoted OTEL-token
line errors under `source` — export the three vars directly if so.)
