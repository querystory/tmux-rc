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
- **`pane_uid`** — STABLE identity of the tmux pane a record belongs to, format
  `<boot_id>:<server_pid>:<pane_id>` (e.g. `a557…:28816:%3`). Present on every parse record
  AND on the lifecycle records (below). Unlike the screen-scraped `output_json.session`
  (only in debug mode, may be blank, may collide), this is machine-stable: it survives
  reordering/resizing panes, restarting the program inside a pane, and restarting the
  tmux-rc daemon itself. It changes only when the pane is closed (tmux may recycle its
  `%N`) or the machine reboots. **Use `pane_uid` — not `session` — to group records by
  pane, count distinct panes, or track one pane over time.**
- **`pane_label`** — human name for the pane (window/session name, or the agent's scraped
  session title), the same string shown on the phone card. Free-text, may repeat across
  panes; for display/grouping-by-name, not identity (use `pane_uid` for identity).

### Pane lifecycle records (`body = 'tmux-rc pane'`)

Separate records, one per pane appearing or disappearing, so you can reconstruct which
panes existed when. Distinguished from parse records by **`body = 'tmux-rc pane'`** and an
**`event`** attribute; they carry `pane_uid`, `pane_label`, and (for created) `tool`, but
NONE of the parse metrics (model/latency/tokens/etc.).

- **`event`** — `pane_created` (a pane first observed, or a recycled `%N` reborn) or
  `pane_removed` (a pane vanished, or the old occupant of a recycled `%N`).
- **Currently-active panes** = pane_uids with a `pane_created` and no later `pane_removed`.
  This is the RELIABLE way to answer "which panes are active right now" — the old approach
  of "a parse exists for this session" never expires when a pane closes, so it overcounts.
  A pane with only parse records but no `pane_removed` is still open; one with a later
  `pane_removed` is gone. (Lifecycle records need the OTLP endpoint configured, same as
  parses; they are emitted regardless of QSDEBUG since they carry no screen content.)

### Action audit records (`body = 'tmux-rc action'`)

One record per state-CHANGING request a user made through the phone UI — the audit trail
for "what is making changes to my terminals, and who?". These are USER actions, not
parser activity — don't mix them into parse metrics.

- **`event`** — `send_keys` | `select_pane` | `paste_image`.
- **`pane_uid`** — joins to parse and lifecycle records.
- **`actor`** — the IAP-authenticated email forwarded by the tunnel (trusted only when
  the request arrived from loopback, i.e. via the tunnel client), `local:<ip>` for
  direct requests, or `local:<ip> claiming '<email>'` when a non-loopback client sent
  the identity header — a visible spoof attempt, treat with suspicion.
- **`outcome`** — `ok` for completed actions; `rejected: ...` / `error: ...` for refused
  or failed attempts (probing for nonexistent panes shows up here).
- optional **`detail`**, and **`keys`** (the injected text — only in debug mode AND when
  key logging isn't disabled via TMUXRC_AUDIT_KEYS=0).

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

## Inside `output_json` — the parsed card structure

`output_json` is a **JSON string** (parse it before reading sub-fields; e.g. DuckDB
`json_extract`/`json_extract_string` over the `output_json` column). It is the parser's
description of one pane screen — the card the phone renders. Only present in debug mode.

**Critical framing:** every value here describes the **agent/screen being watched**, NOT
the parser call. In particular `output_json.model` and `output_json.cost` are the ON-SCREEN
agent's own status-line readouts (e.g. the Claude Code session's model + running cost),
which is a *different* thing from the top-level `model`/`cost_usd` (the parser that
classified the screen). Never sum `output_json.cost` as if it were spend — it's a string
like `"$9.99"` scraped off someone's status bar.

Fields (include a field only when determinable, so most are frequently absent — treat
missing as "not shown on screen / not applicable," not zero):

- **`tool`** — which program occupies the pane: `claude` | `codex` | `gemini` | `shell` |
  `unknown`. The kind of thing being watched. (Observed mostly `claude`, some `shell`.)
- **`activity`** — `running` | `waiting` | `idle`. Same meaning as the top-level `activity`
  column (that column is derived from this).
- **`headline`** — one human sentence summarizing what's happening / being worked on. The
  main line of the card. Good for "what was the agent doing" theme analysis.
- **`model`** — the model NAME shown on the watched agent's status line (e.g. `Opus 4.8`).
  The on-screen agent's model, NOT the parser. Free-text scraped from the screen.
- **`context_pct`** — the watched agent's context-window usage percent, from its status
  line (int).
- **`cost`** — the watched agent's session cost as a DISPLAY STRING (e.g. `"$9.99"`),
  scraped from its status line. Not the parser's cost; not summable as-is.
- **`session`** — the watched agent's own session name/title if it prints one (e.g.
  `bug-fixes`); used as the card label. Absent if none shown.
- **`mode`** — the agent's permission mode: `plan` | `accept-edits` | `bypass` | `normal`.
- **`working`** — present while `activity=running`: `{verb, elapsed, tokens}` — the
  whimsical gerund (`"Brewed"`), elapsed time string (`"7m 31s"`), and tokens-streamed
  string. All display strings off the status line.
- **`events`** — array of NEW activity items since the last parse: each
  `{text, file?: {path, added, removed}, meta?}`. `text` is what happened in plain
  language; `file` present when it was a file edit (path + lines +/-); `meta` a short
  side-note (e.g. `"ran 3 commands"`, `"exit 1"`). This is the activity feed — good for
  "what kinds of actions happen most" analysis. Newest-relevant first.
- **`tasks`** — array of `{text, done}` — a TODO/checklist visible on screen.
- **`question`** — present only when the agent is BLOCKED awaiting input:
  `{prompt, options[], answer_style}`. `prompt` is the (context-rich) question; `options`
  are candidate answers; `answer_style` is `text` (parser will TYPE the chosen option +
  Enter — normal conversational questions) or `menu` (an on-screen picker; options map to
  keystrokes). When `question` is present the pane is `waiting`.
- **`tables`** — array of `{title?, headers[], rows[][]}` — tabular data extracted from the
  screen (ASCII/box-drawn tables), so it renders as a real table rather than prose.
- **`rewind`** — present only when Claude Code's Esc-Esc restore picker is shown:
  `{entries: [{text, note?, selected}], more_above, more_below}`.

Querying tips:
- To analyze what agents work on: `json_extract_string(output_json, '$.headline')`, or
  unnest `$.events[*].text`.
- To find blocked/actionable moments: rows where `output_json` has a non-null `$.question`
  (equivalently the top-level `activity = 'waiting'`).
- `$.tool` distribution tells you claude-vs-shell mix among watched panes.
- Do NOT aggregate `$.cost`, `$.model`, `$.context_pct` as parser metrics — they are the
  watched agent's self-reported values, heterogeneous and screen-scraped.
