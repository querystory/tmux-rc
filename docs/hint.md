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
  - `unknown` — the watcher couldn't classify the pane (e.g. a parse-failure stub);
    rare, but consumers should tolerate it.
  This is a property of the **screen being classified**, not of the API call. "Running
  state handles the most requests" means "most parses happened while a pane was in the
  running state," i.e. agents are most often actively working — NOT that running is a
  request type or batch mode.
- **`kind`** — `parse` (a live screen classification, the overwhelming majority) or
  `bootstrap` (a one-time deep read of a pane's scrollback that seeds the card's
  session summary and reconstructed history — one per pane per daemon run, much larger
  `in_tokens`). **Exclude `kind = 'bootstrap'` from parse-latency/cost benchmarks** or
  they'll skew; analyze bootstraps separately.
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

### Live-view records (`scope_name = 'tmux-rc.live'`, `body = 'tmux-rc live'`)

A SEPARATE scope from the parser benchmarks — **filter `WHERE scope_name = 'tmux-rc.live'`**
to keep these out of parse aggregates. This scope holds **two different record shapes** —
tell them apart by the `kind` attribute (or by `cost_usd IS NULL`):

- **watch-time rounds** (`kind` absent) — the phone's live-view long-poll, one record per
  ~25s hold. These carry NO model/token/cost fields. Detailed just below.
- **voice-turn records** (`kind = 'live_turn'`) — Live Mode's spoken-agent metering, one
  record per voice turn plus a `final` session summary. These DO carry model/token/cost/
  transcript. See "Live Mode voice turns" further down.

A `tmux-rc.live` query that wants only watch-time must add `WHERE kind IS NULL`; a cost
query must add `WHERE kind = 'live_turn'` — otherwise the two shapes mix and half the
columns are null.

The **watch-time rounds** measure the **live view**: a phone surface that streams a pane's
raw colored screen in real time over a long-poll endpoint (separate from the LLM parser
entirely). Use them to answer bandwidth cost, how much wall-clock time users spend watching
live, and (future) per-user live-hour billing.

**One record = one completed long-poll "round."** A round holds ~25s, checking the screen
every 250ms, and ends either the instant the screen changes (a *change round* — a frame
was sent) or on the idle hold timeout (an *idle round* — nothing sent). The client
re-holds immediately, so **a continuous viewer is a chain of back-to-back rounds**;
that's what makes watch-time summable (below). Attributes:

- **`hold_s`** — wall-clock seconds this round was open. **Watch-time / "live-time" =
  `SUM(hold_s)`**, grouped by `session` for per-viewing-session time or by `actor` for
  per-user time (the future billing signal, "live-hours"). Because rounds are contiguous,
  the sum is an undercount-only floor (only the sub-hold tail of the final round before a
  viewer leaves is missed) — safe to treat as a lower bound, never an over-count.
- **`session`** — anonymous per-page-load id the client generates, the spine that groups
  one viewing session's round chain. **NULLABLE: absent when the round arrived with no
  session (un-attributable).** Two different viewers never share a session id, so summing
  `hold_s` per non-null `session` is valid; **EXCLUDE rows where `session IS NULL`** from
  per-session watch-time rather than bucketing them together (they'd mis-merge unrelated
  viewers). It is NOT identity and NOT the pane — use `pane_uid` to group by pane.
- **`actor`** — the loopback-trusted tunnel owner's email (same trust model as the action
  audit's `actor`: honored only from the tunnel/loopback). The account key for
  live-time-per-USER / billing. **NULLABLE: absent for direct/LAN use** (no verified
  identity) — those rounds still carry `session`, just no account attribution.
- **`pane_uid`**, **`pane_label`** — same pane identity as parse/lifecycle records; join
  on `pane_uid` to see which pane / tool was being watched live. Always present.
- **`tool`** — `claude` | `codex` | `gemini` | `shell` for the watched pane. NULLABLE
  (absent if the pane's tool wasn't known yet). Tells you what kinds of panes get watched
  live.
- **`changed`** — `true` = the round ended because the screen changed (a frame was sent);
  `false` = idle hold timeout (no frame). The share of change rounds is the pane's live
  activity rate; only change rounds carry bytes.
- **`raw_bytes`** — uncompressed byte size of just the colored FRAME payload (the screen
  text) on a CHANGE round — not the full HTTP response envelope (headers/JSON wrapper are
  excluded). **NULLABLE / absent on idle rounds** (nothing was sent), so byte sums stay
  honest: `SUM(raw_bytes)` is real frame bytes generated. NOTE the wire is gzipped by
  middleware (~4-5x smaller), so gzipped-sent bytes are LOWER than `raw_bytes`; the
  compressed ground-truth is in the daemon's own log line, not this record (see
  docs/design/live-telemetry.md open questions).

### Live Mode voice turns (`scope_name = 'tmux-rc.live'`, `kind = 'live_turn'`)

Live Mode is the spoken-agent surface: a Gemini Live (native-audio) session that watches
the whole tmux state, talks back by voice, and acts via two tools (type into a pane, press
a key). Its cost is dominated by AUDIO tokens the flash-lite parser never sees, so it meters
itself here. **One record = one voice turn**; a `final = true` record closes each session
with cumulative totals. Filter `WHERE kind = 'live_turn'`. Attributes:

- **`session`** — per-voice-session UUID, the summable spine. The SAME key the watch-time
  rounds use, so a session's voice spend can be joined to its screen watch-time. Sum per
  `session` for per-session cost; use the `final = true` row for the authoritative session
  total (per-turn rows are cumulative snapshots, so **do not `SUM` the per-turn rows** — you'd
  multi-count; take the `final` row, or `MAX` per session).
- **`cost_usd`** — this turn's cumulative session cost in USD, priced with a four-way rate
  card (text-in / text-out / audio-in / audio-out) because audio-out bills ~24× text.
- **`in_tokens`** / **`out_tokens`** — total prompt / response tokens (text + audio). Big
  input (thousands): the system prompt carries every pane's live state each turn.
- **`audio_in_tokens`** / **`audio_out_tokens`** — the audio-modality slice of in/out. Text
  tokens = `in_tokens - audio_in_tokens` (same for out). Audio out is the cost driver.
- **`turns`** — voice turns so far this session (monotonic; equals the count on the `final` row).
- **`duration_s`** — wall-clock seconds since the session opened (cumulative).
- **`final`** — `true` on the one end-of-session summary row, `false` on per-turn rows. Use
  `WHERE final = true` for one authoritative row per session.
- **`model`** (`gemini-live-2.5-flash-native-audio`), **`provider`** (`vertex`) — the Live model.
- **`actor`** — the tunnel owner's email when known, else a `local:<ip>` marker. Same
  loopback-trust model as elsewhere.
- **`transcript`** — the turn's rolling voice transcript (`user:` / `model:` lines and
  `[typed]` actions). **Present ONLY under TMUXRC_QSDEBUG** (fail-closed, like `pane_text`);
  absent otherwise. Voice content is at least as sensitive as pane text.

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

These apply to **every** record type — parse, pane-lifecycle, action, AND live-view —
not just parses; don't assume a field described here implies a record is a parse.

- **`scope_name`** — `tmux-rc.classify` for parser/lifecycle/action records, or
  `tmux-rc.live` for BOTH live-view watch-time rounds AND Live Mode voice turns (own
  sections above; split those two by `kind`); **`service.name`** (`tmux-rc`) is common to
  all. Use `scope_name` to keep the parser and live worlds apart — a benchmark query stays
  on `tmux-rc.classify`, a live query on `tmux-rc.live`.
- **`kind`** — only on `tmux-rc.live` voice-turn records, value `'live_turn'`; absent on
  watch-time rounds. The discriminator between the two `.live` shapes.
- **`timestamp`, `observed_timestamp`** — Unix nanoseconds when the record was emitted
  (the parse, action, lifecycle event, or live round). Divide to seconds for time-series;
  both are usually equal.
- **`otel_opt_in`** — always `true` for tmux-rc (required so the receiver doesn't redact
  content). Not analytically interesting.
- **`host.name`, `user.username`, `service.instance.id`** — which machine/user/daemon-
  instance emitted it. Single-developer tool, so usually one value each.
- **`body`** — a per-record-type label, not data: `"tmux-rc parse"`, `"tmux-rc pane"`,
  `"tmux-rc action"`, or `"tmux-rc live"`. Use `scope_name`/`body` to tell types apart.
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
- **`activity`** — `running` | `waiting` | `idle` (| `unknown` in daemon-generated stub
  states). Same meaning as the top-level `activity` column (that column is derived from this).
- **`headline`** — one human sentence summarizing what's happening / being worked on. The
  main line of the card, written like a mobile push notification: it carries the SUBSTANCE
  (the actual question/decision/result), not a meta-description. Good for "what was the
  agent doing" theme analysis.
- **`model`** — the model NAME shown on the watched agent's status line (e.g. `Opus 4.8`).
  The on-screen agent's model, NOT the parser. Free-text scraped from the screen.
- **`context_pct`** — the watched agent's context-window usage percent, from its status
  line (int).
- **`cost`** — the watched agent's session cost as a DISPLAY STRING (e.g. `"$9.99"`),
  scraped from its status line. Not the parser's cost; not summable as-is.
- **`session`** — the watched agent's own session name/title if it prints one (e.g.
  `bug-fixes`); used as the card label. Absent if none shown.
- **`mode`** — the agent's permission mode: `plan` | `accept-edits` | `bypass` | `normal`.
- **`status_entries`** — array of short status-line readout strings with no dedicated
  field (e.g. a usage-limit readout `36%/30% 5h/7d`, telemetry/history summaries).
  Deliberately unstructured screen-scraped display strings — group/count them, don't
  parse numbers out of them for arithmetic.
- **`working`** — present while `activity=running`: `{verb, elapsed, tokens}` — the
  whimsical gerund (`"Brewed"`), elapsed time string (`"7m 31s"`), and tokens-streamed
  string. All display strings off the status line.
- **`session_summary`** — (state field, not from the per-tick parser) 2-3 sentence
  "story so far" produced by the bootstrap pass from scrollback; refreshed only when a
  pane re-bootstraps (daemon restart / new pane).
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
- **`links`** — array of `{href, text}`: URLs the screen was OFFERING the user to open
  (auth/approval links, PRs, previews), with the parser reassembling URLs the terminal
  wrapped across lines. `text` is a short label, not the URL. Rendered as tap-to-open
  buttons on the phone (which appends the destination host — labels are model output
  from untrusted screen content and could otherwise disguise a destination).
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
