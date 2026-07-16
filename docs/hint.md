# tmux-rc telemetry — QueryStory hints

Guidance for the AI when querying and analyzing tmux-rc telemetry. Load as project-level
hints in the QueryStory "tmux-rc" project.

## What tmux-rc is

tmux-rc is a phone dashboard for watching and controlling terminal AI coding agents
(Claude Code, Codex, etc.) running in tmux. A local daemon polls each tmux **pane**,
captures the **visible screen text**, and sends it to a small LLM (the "parser") that
returns structured JSON describing what's on screen — is an agent working, waiting for
input, or idle; what it's doing; any menu/question/table/rewind-picker visible. The phone
renders that JSON as live cards.

**Each telemetry record = one parse of one pane's screen at one moment.** It is NOT an
end-user API request, NOT a chat message, and NOT parsing "tmux configuration" — the LLM
is reading a **terminal screen capture** (a fixed-width character grid of whatever the
agent/shell is currently showing) and classifying it. Reason about the data as
"screen-classification calls," not "user traffic."

Why the telemetry exists: to benchmark the parser across models/providers in real use —
compare latency, cost, tokens, and (with content) accuracy — so we can pick the best model
for this classification task. Records accumulate as the daemon runs; there is no user-
facing request behind them.

## How to filter to tmux-rc data

These records share a GCS bucket / table with other OTEL sources (e.g. Claude Code).
**Always filter `WHERE scope_name = 'tmux-rc.classify'`** to get only tmux-rc parses.
`service.name = 'tmux-rc'` is equivalent. Without this filter you'll mix in unrelated
telemetry.

## Fields

### The benchmark payload (what we actually analyze)

- **`model`** — the parser model that did this classification, e.g. `gemini-3.1-flash-lite`.
  The dimension to compare across when benchmarking.
- **`provider`** — inference backend serving the model, e.g. `vertex`, `cerebras`, `groq`.
  Same model can run on different providers with different latency/cost.
- **`latency_s`** — wall-clock seconds for the whole parse call (request→full response).
  The primary speed metric. Driven mostly by INPUT size + time-to-first-token, not output.
- **`ttft_s`** — time-to-first-token in seconds, when measured (streaming path). Often
  `null` on the non-streaming path — treat null as "not measured," not zero.
- **`tps`** — output tokens per second (`out_tokens / generation_time`). A throughput
  metric for the generation phase only. HIGH tps does not imply LOW latency: a model can
  stream fast yet still be slow overall if it emits many tokens or has high TTFT. Do NOT
  describe high-tps states as "faster" without checking `latency_s`.
- **`in_tokens`** — input/prompt tokens: the big static parser prompt + a couple of prior
  screen frames (for continuity) + the current screen. Typically thousands (≈3k–9k). This
  is the dominant driver of `latency_s` and `cost_usd`. When latency varies across
  records, `in_tokens` is usually why.
- **`out_tokens`** — output tokens: the JSON the parser returned. Small (≈150–300) for
  non-reasoning models; much larger for reasoning models (which spend tokens on hidden
  reasoning before the JSON).
- **`cost_usd`** — estimated USD cost of this one call (in/out tokens × per-token price).
  Per-parse, so it's tiny; SUM it for totals. Estimated from a static price table, not a
  billed amount.
- **`activity`** — the parser's verdict on the pane's state, one of:
  - `running` — an agent is actively working (spinner, elapsed timer, streaming output).
  - `waiting` — the agent is blocked awaiting user input (a question, menu, or rewind
    picker is on screen). These are the "actionable" panes.
  - `idle` — nothing happening; a bare shell prompt or a finished agent.
  This is a property of the **screen being classified**, not of the API call. "Running
  state handles the most requests" means "most parses happened while a pane was in the
  running state," i.e. agents are most often actively working — NOT that running is a
  request type or batch mode.
- **`changed`** — `true` if this parse was triggered by a real content change on screen;
  `false` if it was a periodic "heartbeat" re-parse of an unchanged screen. Volatile churn
  (spinners, ticking timers/token counts) is stripped before deciding "changed," so a
  merely-animating screen does NOT count as changed. Useful to separate "new activity"
  parses from steady-state re-checks. A high `changed` rate in a state means the screen is
  actively mutating there.
- **`input_sha256`** — hash of the exact input text sent to the model. Lets you group
  repeated parses of the identical screen without storing the text. Same hash across rows =
  same screen classified more than once.

### Content (only present when TMUXRC_QSDEBUG is enabled)

- **`pane_text`** — the raw terminal screen capture sent to the model (includes the labeled
  prior frames). Large (thousands of chars). Present only in debug/accuracy-diff mode;
  otherwise absent. Use for "what was on screen," and to eyeball whether a model classified
  it correctly.
- **`output_json`** — the parser's full JSON response (tool, activity, headline, model,
  cost, mode, question, tables, rewind, events, etc. — the card the phone renders). Present
  only in debug mode. Use to compare what different models produced for the same
  `input_sha256`, i.e. accuracy diffing. It's a JSON string; parse it to read sub-fields.
  Note the `model`/`cost` INSIDE output_json describe the agent ON SCREEN (e.g. the Claude
  Code session's own model/cost readout), NOT the parser — don't confuse them with the
  top-level `model`/`cost_usd`, which describe the parser call.

### Structural / infra fields (rarely the subject of analysis)

- **`scope_name`** (`tmux-rc.classify`), **`service.name`** (`tmux-rc`) — source
  identifiers; use for filtering (above).
- **`timestamp`, `observed_timestamp`** — Unix nanoseconds when the parse happened. Divide
  to seconds for time-series; both are usually equal.
- **`otel_opt_in`** — always `true` for tmux-rc (required so the receiver doesn't redact
  content). Not analytically interesting.
- **`host.name`, `user.username`, `service.instance.id`** — which machine/user/daemon-
  instance emitted it. Single-developer tool, so usually one value each.
- **`body`** — constant `"tmux-rc parse"`. A label, not data.
- **`severity_number`/`severity_text`, `trace_id`/`span_id`, `telemetry.sdk.*`** — OTLP
  boilerplate. Ignore for analysis.

## Reasoning guidance

- Frame findings as "the parser/classifier," "parses," and "screen states" — never
  "users," "user traffic," "chat," or "tmux configuration parsing."
- When comparing models/providers: `latency_s` for speed, `cost_usd` (summed) for cost,
  `tps`/`out_tokens` for throughput/verbosity. Remember reasoning models inflate
  `out_tokens` and can be slower despite high `tps`.
- Latency is dominated by `in_tokens` (prompt + prior frames), not output — if latency
  differs, check input size before inventing behavioral explanations.
- `activity` and `changed` describe the SCREEN, not the API call. Don't attribute latency
  or "batch processing" to an activity state; correlations there reflect what agents were
  doing, not how requests were served.
- A negative/failed parse still emits a record with an `error` attribute and null token
  fields — exclude `error IS NOT NULL` when measuring success-path latency/cost.
