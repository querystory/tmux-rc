# Design: Viewing the real pane & the activity timeline

Status: **draft / thinking** — no code yet. Captures the design space for two related
features and the storage decision underneath them.

## The problem

termiphone's card is a *point-in-time summary* — it abstracts the terminal into
glanceable structured fields (headline, tasks, notable, question, rewind). That is
the right default. But two needs recur that the summary card cannot serve:

1. **"Let me see the actual thing."** A diffstat table, a traceback, a git log, a
   block of output the agent produced. The card paraphrases it in `notable[]`, but you
   sometimes need the real content, verbatim — you can't act on a paraphrase of a diff.
   (Concretely: this doc's own diffstat table rendered fine in the terminal but the
   phone card only had the one-line summary — you couldn't see the table.)

2. **"What has this agent been doing?"** In a real session you read scrollback
   constantly to reconstruct the story: "spent 5 min building the UI, then ran tests,
   then hit an error." The card only shows *now*. You want to scroll back through a
   *history* — ideally not raw scrollback, but summarized cards over time ("Building
   the UI · 5m · +200 LOC · 3 tasks done") that you can dive into.

These are two distinct features — a **point-in-time drill-down** and a **timeline of
summaries** — but they share one substrate: what we capture and store, and when we
summarize. Decide the substrate once; both features fall out of it.

## Feature A — drill down to the real pane

When the card isn't enough, let the user see the actual terminal content. Options,
roughly increasing in effort and richness:

### A1. Verbatim pane text, readable
Serve the raw `capture-pane` text (we already store it) as scrollable monospace on
the phone. Zero new capture cost; it's the true content. Weakness: loses color/bold,
and wide output (tables, diffs) wraps badly on a narrow screen. Cheapest, always works.

### A2. Pixel-perfect snapshot on demand
Render the pane to an image (we have `render.py` already, or screenshot the real
window) and serve that when the user drills in. Preserves color/layout exactly — a
diff's red/green, a table's columns. Heavier per view, but only rendered on demand
(not every tick), so cost is bounded. This is the "see exactly what's on screen" answer.

### A3. LLM-generated mobile-friendly rendering
Ask the model to *re-render* the content the user cares about as clean mobile HTML —
e.g. turn a diffstat into a proper mobile table, a traceback into a collapsible stack,
a task list into checkboxes. The user could even say what they want to "slice into"
("show me just the failed tests"). Most powerful and most phone-native, but: costs an
LLM call per view, can hallucinate/omit, and is non-deterministic. Best as an *option*
on top of A1/A2 (show the real thing, plus a "make this readable" button), never the
only way to see ground truth.

**Likely answer:** A1 as the always-available floor (verbatim = trustworthy), A2 for
"see it exactly" (we have the renderer), A3 as an optional enhancement for messy
content. Ground truth must always be reachable without the LLM.

## Feature B — timeline of summarized activity

A scrollable history of what the pane has done, as summary cards over time, each
divable. "Building the login UI · 4m52s · +180/-20 · tasks: 3✓ 1○" → tap → see the
notable bullets, the tasks as they were, maybe the pane snapshot at that moment.

Design questions:
- **What is a timeline entry?** A "turn" / unit of work — bounded by the agent going
  idle→working→idle, or a task completing, or a fixed time bucket. Turn boundaries are
  the natural unit (matches how you think: "the thing it did"), but detecting them is
  fuzzy; time buckets are crude but robust. Probably: segment on
  working→waiting/idle transitions, fall back to time.
- **What does an entry hold?** A headline, duration, LOC delta, tasks done, key
  `notable` bullets — and a pointer to the raw capture(s) so you can drill into the
  real thing (Feature A) from any point in history.
- **This is timeline-summarization** — and note a point-in-time card is just the
  degenerate case (a timeline of length 1). So B generalizes A's summary; build the
  substrate for B and the live card is one slice of it.

## The substrate decision — store vs. generate

Both features need *some* history retained. Three strategies:

### S1. Store raw captures, summarize on the fly
Keep a ring buffer of raw pane captures (we already keep 50). Generate timeline
summaries and drill-downs on demand from the raw text. Simplest storage, always able
to show ground truth, no wasted LLM calls for history nobody looks at. Weakness: raw
captures are bulky and overlap heavily (each is a near-copy of the last); a long
session is a lot of redundant text; summarizing a long span on demand is a big LLM call.

### S2. Summarize as we go, append to a log
Each time we parse (already happening on change/heartbeat), also fold the delta into a
running, append-only summary log — short timeline entries built incrementally. Cheap
per step (we're already calling the LLM), history is compact, timeline is instant to
show. Weakness: we commit to a summary shape at capture time; if we later want a
different cut ("just show errors"), we may have discarded the raw detail. And we'd be
summarizing even when nobody's watching that pane.

### S3. Hybrid (likely)
Store raw captures with **dedup** (only keep a capture when the content fingerprint
changed meaningfully — we already compute this), giving a compact ground-truth trail.
**Incrementally maintain a lightweight timeline** (turn boundaries + headline + deltas)
as we parse, but keep it thin and regenerate richer detail on demand from the retained
raw captures. Best of both: cheap steady-state, ground truth preserved, rich views
generated only when viewed. Bound both buffers (age/size) so a marathon session
doesn't grow unbounded.

## Open questions to resolve before building
- Turn-boundary detection: is working→idle reliable enough, or do we need the LLM to
  mark "a unit of work finished"?
- Do we summarize panes nobody is currently viewing? (Cost vs. having history ready
  when you open the app after being away — the whole point is catching up on what
  happened while you weren't looking, which argues for summarizing in the background,
  at least at a low rate.)
- Retention bounds: how long/large is the raw trail and the timeline before we age out?
- Where the pixel-perfect snapshot fits: render-on-demand from retained text (cheap,
  we have render.py) vs. capturing real screenshots continuously (heavy). On-demand
  render from stored text is almost certainly right.

## Persist state/history to Postgres (introspect with QueryStory)

Instead of (or alongside) the in-memory ring buffer, log every parse — the full
state dict plus the raw capture and token/cost/latency — to Postgres, one row per
parse (pane_id, ts, activity, headline, model, cost, tokens, raw_text, json). Why:

- **Real-time introspection with QueryStory** (dogfooding): query the session's whole
  history live — cost over time, idle vs working minutes, headline timeline, token
  burn, how often it hit `waiting`, per-pane breakdowns. The timeline feature becomes
  "SELECT … ORDER BY ts" instead of bespoke in-memory logic.
- **Durable substrate for Feature B**: the timeline is just a query/rollup over the
  rows; drill-down (Feature A) reads `raw_text` for a row. Survives restarts (which,
  given the reload/resize stalls above, matters).
- Ties directly into the substrate decision: this is essentially **S1 (store raw) +
  S2 (store the as-we-go summary)** both landing in durable rows, with rich views as
  SQL/rollups on top — arguably making S3's "thin in-memory timeline" unnecessary.

Tradeoffs to think through: a Postgres dependency for a PoC (vs. SQLite/JSONL for
zero-infra); write volume (dedupّd parses only, not every 1.5s tick); retention/rollup
of old rows; and whether the raw_text column bloats (compress, or keep only changed
frames). Likely start with SQLite or append-only JSONL to stay zero-infra, with a
schema shaped so moving to Postgres later (for the QueryStory angle) is trivial.

## Recommendation (for when we build)
Build the **S3 substrate**: dedup'd raw-capture trail + a thin incremental timeline.
Then Feature A (drill-down: verbatim text floor, on-demand image, optional LLM
re-render) and Feature B (timeline of summary cards) are both views over that
substrate — and the live card is just the newest timeline entry.
