---
title: "gemini-3.5-flash-lite vs 3.1-flash-lite"
---

# Benchmark: `gemini-3.5-flash-lite` vs deployed `gemini-3.1-flash-lite` (pane classifier)

**Question this answers:** should we switch the pane classifier from the currently
deployed `gemini-3.1-flash-lite` to `gemini-3.5-flash-lite`? Both sides are **Flash
*Lite***. This is NOT the non-Lite `gemini-3.5-flash`, a separate model that was only
smoke-tested — see the note at the end.

**TL;DR — DON'T switch yet. Needs more data / a prompt fix first.** `gemini-3.5-flash-lite`
is reachable, ~0.2–0.3s faster per parse, and cost-neutral (+0.8%) under current pricing. But on real
samples it is **less reliable at the one thing this classifier exists to get right: not
missing a pane that needs the human.** On 24 diverse real screens it disagreed with 3.1-lite
on `activity` 6 times, and in the ambiguous/hard cases it drifted toward *under*-reporting —
dropping a live user-question, missing an external-wait, and even emitting an
**out-of-schema `activity` value** (`"working"`). Those are exactly the failures the prompt
tells the model to bias *against*. The speed win is real but small; it doesn't buy back the
loss of an amber "tap me, I need you" badge.

## How this was run (apples-to-apples with production)

- **Model call path:** the daemon's real one. `research/probe.py`'s `_parse()` (which takes
  a `model` arg for this comparison) sends the pane text with the **production
  `daemon/parser_prompt.txt`** as `system_instruction`, `temperature=0.0`,
  `response_mime_type=application/json`, via the same Vertex client
  (`daemon.llm._client`). No hand-rolled prompt, no raw call that skips our prompting.
- **Input:** each sample's `pane_text` is the **already-assembled payload** captured from a
  single production call — it includes the `[tmux: foreground process is '…']` prefix and
  whatever prior-frame / already-reported-events context `classify()` had embedded *at that
  one capture*. So the model sees byte-for-byte what the daemon fed 3.1-lite on that call —
  but these are **single-frame replays**: they carry only the continuity baked into that one
  payload, not the live multi-frame trajectory the daemon accumulates across ticks (see the
  caveat below). Text-only, matching the shipped hot path (research showed text beats image;
  the image switch is off).
- **Auth/env:** Vertex on the inference project, region `global`, the daemon's
  service-account key. Both model ids returned valid JSON on every call —
  **`gemini-3.5-flash-lite` IS a valid, reachable model id on this setup** (this was the
  first thing checked; it is not a substitution).
- **Repeats:** 3 runs/model/sample; latency reported as the per-sample median.

## Sample source: real OTel telemetry (not the 3 `research/samples/` fixtures)

Pulled from production OTel telemetry — two days of archived `tmux-rc.classify` parse
records, which under `TMUXRC_QSDEBUG=1` carry `pane_text` + `output_json`.
**158,703** parse records → **33,092 distinct screens**. Production-observed activity mix
across those distinct screens: idle 15,896 / running 15,266 / waiting 1,710 / compacting 220;
tools claude 16,569 / shell 15,834 / codex 490 / gemini 199.

From that I curated **24 samples** spanning every state and tool plus the special
affordances — question, rewind, waiting-on-external, compacting — and a range of input sizes
(37 chars to 50k). The `research/samples/` fixtures were available as a fallback but the OTel
data is far richer and more varied, so it's the primary source. `output_json` on each record
is 3.1-lite's **live production answer**, used as a baseline reference (not infallible gold —
it was produced with real prior-frame continuity these single-frame replays don't have).

## Head-to-head numbers

| metric | `gemini-3.1-flash-lite` (deployed) | `gemini-3.5-flash-lite` (candidate) |
|---|---|---|
| JSON parses cleanly | 24/24 | 24/24 |
| **invalid `activity` enum** | **0** | **1** (`"working"`, sample 08) |
| median latency (per-sample medians) | 1.35s | **1.18s** |
| mean latency | 1.41s | **1.23s** |
| latency range | 0.80–2.45s | 0.76–2.03s |
| faster on … | — | **17 / 24 samples** |
| median paired Δlatency (3.5 − 3.1) | — | **−0.22s** |
| pane-text in-tokens (identical input/tokenizer) | 111,050 total | 111,050 total |
| out-tokens | 3,800 total (median 133) | 3,969 total (median 140) |
| cost / 1k calls (mean, $0.25/$1.50 per-M) | $1.394 | $1.405 |

Latency edge is consistent across input sizes (small `<1500` in-tok: −0.29s; mid: −0.25s;
large: −0.38s). 3.5-lite writes ~5% more output tokens on average, so cost is a hair higher
but **effectively identical** — **+$0.011 / 1k calls, +0.8%** — under the flash-lite pricing
both models share in `daemon/llm.py`. *(Assumption: 3.5-lite bills at the same Vertex list
price as 3.1-lite. Confirm real list price before switching — if 3.5-lite costs more, the
case gets weaker still, since it's not winning on quality.)*

### Two corrections to the originally published figures

This report first ran on 2026-07-22 and its cost section has been regenerated twice over.
Both fixes move the absolute numbers and neither moves the conclusion — stated here rather
than silently restated, since the original figures circulated.

1. **Stale prices.** The original quoted `$0.526` / `$0.529` per 1k calls using
   **$0.10/$0.40** per M tokens. Those were `2.5-flash-lite`'s prices, still hardcoded long
   after the move to 3.1. `daemon/llm.py` now carries the current
   **$0.25 in / $1.50 out** (`_IN_PER_M` / `_OUT_PER_M`, overridable via
   `TMUXRC_IN_PER_M` / `TMUXRC_OUT_PER_M`) — the source of the prices quoted above, and the
   reason `research/probe.py` imports them rather than keeping a copy. Output being 3.75x
   dearer is what widens the paired gap from `+$0.003` to `+$0.011`.
2. **Input was undercounted ~2x.** The 111,050 figure came from a `count_tokens` call over
   the pane text **only**. Every request also sends the ~20KB production parser prompt as
   `system_instruction`, which billing counts and that helper did not — roughly **+5,000
   tokens per call**, against a mean of 4,627 pane-text tokens. Full production accounting
   (what `usage_metadata.prompt_token_count` reports, now what `probe.py` records) puts real
   input near **9,700 tokens/call**, and the true per-1k cost near **$2.66 for both models**.

The cost *comparison* survives both corrections intact: input tokens are identical between
the two models, so they cancel out of the difference entirely. The gap is set solely by
3.5-lite's 169 extra output tokens across 24 calls — **+$0.011 / 1k calls whichever input
baseline you use.** Cost was never the deciding variable here, and still isn't; the quality
findings below are.

## Quality — where they diverge (the part that decides it)

Agreement was high: identical key fields on 18/24. Divergences: **activity 6/24, `waiting_on`
4/24, tool 1/24, question-presence 1/24.** Adjudicated against the pane text and the prompt's
own rules (esp. "**bias to `user`… an under-fired user-wait hides something that actually
needs the human, which is worse than an over-shown busy pane**"):

| # | screen | 3.1-lite | 3.5-lite | verdict |
|---|---|---|---|---|
| 06 | Claude blocked on a preview build, spinner "1 shell still running" | `waiting`/**external** ✓ | **`idle`, no wait** | **3.5 WORSE** — silently drops an active external-wait |
| 08 | Claude "Waiting for 1 background agent to finish (9m40s)" | `waiting`/external ✓ | **`activity:"working"`** (invalid enum) | **3.5 WORSE + broken** — out-of-schema value; `classify()` would treat it as non-waiting and strip `waiting_on` |
| 02 | scrolled-back frame, question still pending | `waiting`/user + `question` ✓ | **`idle`, question dropped** | **3.5 WORSE** — loses a live user-question (the amber badge) |
| 20 | Gemini CLI auto-updating itself | `running` + update event ✓ | **`idle`, event dropped** | **3.5 WORSE** — reports a busy pane as idle |
| 12 | 50k server-log tail, no shell prompt | `idle` (matches prod) | `running` | 3.5 arguably worse — a log tail with no prompt is idle; borderline |
| 00 | 109-char "Should I run tests? 1.Yes 2.No ❯" | tool=**codex** ✗ | tool=**claude** ✓ | **3.5 BETTER** — the `❯` box is Claude's; prod agrees it's claude |
| 03 | Claude session/worktree resume picker | `idle` + question(menu) | `waiting`/user + question(menu) | 3.5 arguably BETTER — a picker awaiting a selection is a genuine user-wait |
| 04 | rewind picker over live "converting Parquet" | `waiting`/user | `waiting`/user | tie (both differ from prod's `running`; a visible picker justifies a user-wait) |

**Pattern:** the two models are equally good on the easy, unambiguous screens (all four
`running_claude`, both `compacting`, plain idles, the device-auth link case — identical or
trivially different). They diverge on the **hard** screens — long scrolled-back frames,
external-waits, self-housekeeping — and there **3.5-lite's errors cluster on the dangerous
side: under-reporting.** It dropped a pending question (02), missed an external-wait (06),
called a self-updating agent idle (20), and produced an invalid `activity` (08). 3.5-lite's
only clear *win* (00, the tool call) is on a 109-char screen genuinely ambiguous between
claude and codex. It never once caught a wait that 3.1-lite missed.

The invalid-enum result (08) is the most concrete red flag: `"working"` is not in the schema
(`running|waiting|idle|compacting`). The daemon doesn't validate the enum, so it would pass
straight through — the `question`/`rewind` override wouldn't fire, `waiting_on` would be
stripped by `classify()`'s cleanup, and the card would render as neither waiting nor a known
busy state. A model that invents enum values on a temp-0 structured call is a reliability
regression regardless of the average-quality wash.

## Recommendation: DON'T switch (needs-more-data + a prompt pass first)

The speed win (~0.2s median, ~15%) is real but small on a parse that isn't latency-critical
(it runs on a heartbeat, not in a user's tight loop), and cost is a wash. Against that,
3.5-lite is measurably **less trustworthy on the classifier's core job** — surfacing panes
that need the human — and it emitted an out-of-schema value. On this evidence the trade is
speed-for-reliability in the wrong direction for a "tap me, I need you" dashboard.

Concretely:

1. **Do not flip `TMUXRC_GEMINI_MODEL` to `gemini-3.5-flash-lite` now.** No quality
   justification; a reliability regression on the cases that matter most.
2. **If the speed matters enough to pursue it**, first (a) confirm 3.5-lite's real Vertex
   list price, and (b) re-run at larger N with the **full prior-frame context** the live
   daemon feeds (these were single-frame replays; several 3.5-lite misses were on
   scrolled-back / continuity-dependent frames where the missing prior frames plausibly hurt
   it). If 3.5-lite closes the under-reporting gap *with* real context, revisit. A cheap
   in-daemon A/B — shadow-parse a fraction of live ticks with 3.5-lite and diff `activity` /
   `waiting_on` against 3.1-lite in QueryStory — would settle it on live traffic without a
   risky global switch.
3. **Independently**, the invalid-enum case argues for a tiny guard in `classify()`: coerce
   an unrecognized `activity` to a safe default rather than passing it through — cheap
   insurance against any model (3.1 included, on some unseen screen) inventing a value.

## Context: the non-Lite finding, separately

The **non-Lite** `gemini-3.5-flash` was smoke-tested separately and passes a
basic smoke test at ~identical cost to 3.1-lite. That's a *different* model (bigger, not the
Lite tier) and only a smoke test, not a real-sample head-to-head — noted here so the two
results aren't conflated. This report is the Lite-vs-Lite, real-sample comparison, and its
conclusion (don't switch on current evidence) is specific to `gemini-3.5-flash-lite`.

---
*Harness: `research/probe.py --sample <SAMPLE.json> --models a,b --repeat N` (text-only,
production prompt, matches the daemon call). 24 real OTel samples, 3 repeats each, run
2026-07-22 on Vertex, region `global`. Latency and token counts are from that run; the cost
figures were recomputed from those token totals at current prices (see the corrections
above) rather than re-billed against the API.*
