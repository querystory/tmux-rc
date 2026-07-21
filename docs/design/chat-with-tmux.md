# Design: chat with your tmux

Status: **draft / thinking** — no code. Explores a natural-language (typed or voice)
command layer over tmux-rc: "open codex in a new pane and do X", "in the pane running the
build, retry it", with context awareness (route a command to the right pane even when
it's not selected) and a user-approval step before anything touches a live agent. Also
records how — and whether — this relates to the `/api/live` feed and the state long-poll
(#47), and to the unmerged qs-app Gemini Live Mode work.

## The idea, and why it fits tmux-rc

tmux-rc's founding principle is **observe the terminal, don't drive the agent** — the
daemon reads `capture-pane` and injects `send-keys`, vendor-agnostic, treating every pane
(claude/codex/gemini/shell) the same. "Chat with your tmux" is the natural next layer:
instead of the phone being a *manual* remote (tap a pane, type keys), it becomes an
*intent* remote — you say what you want and a router figures out which pane and what keys.

Crucially this doesn't violate the principle: the router still only ever does two things
the daemon already does — read panes (for context) and send keys (to act). It's a
smarter *composer*, not a new control channel into the agents. That keeps it vendor-
agnostic: "open codex and do X" is send-keys to a new shell; the router never needs a
codex/claude API.

## What it has to do

Four capabilities, roughly in increasing difficulty:

1. **Create panes.** "Open codex in a new pane" → spawn a pane (tmux `split-window` /
   `new-window`), optionally launch a program in it, then send the task. This is a
   genuinely new daemon primitive — today the daemon can `send`, `select`, `image`, but
   NOT create (see the `/api` surface; there is no pane-creation endpoint). It's the
   smallest, most self-contained piece and a useful feature on its own, independent of
   any NLP. **Ship this first, standalone.**

2. **Intent → keystrokes.** "Retry the build" → the literal keys to send. For a shell
   that might be `↑ Enter` or re-typing a command; for an agent pane it's natural-language
   the agent already understands, so the router mostly passes the intent through verbatim.
   The hard part isn't the agent panes (they parse language themselves) — it's the shell
   panes, where the router must produce exact commands.

3. **Pane routing (context awareness).** "Make the change to the thing failing in the
   other pane" → figure out WHICH pane. The daemon already has everything needed here:
   per-pane activity, headline, recent events, and scrollback. The router picks the
   target by matching the utterance against that existing per-pane state — this is a
   retrieval problem over data we already compute for the cards, not new instrumentation.

4. **Approval before acting.** Routing to a non-selected pane, or creating/killing panes,
   is higher-stakes than typing into the pane you're looking at — a misroute could dump a
   command into the wrong agent's prompt. So a mutating, routed command should surface a
   **confirmation** ("Send `npm test` to pane 3 (build)? ✓ / ✗") before it fires. This is
   also the honest UX answer to LLM routing being imperfect: keep a human in the loop for
   the irreversible bit, exactly as the top-level agent guidance says (confirm outward-
   facing / hard-to-reverse actions).

## Architecture sketch (intentionally thin)

The router is one more consumer of the daemon's existing read/write primitives, plus a
new create primitive:

- **Input**: text now; voice later (see Gemini Live section). Voice is an input *modality*,
  not an architectural change — speech→text→same router.
- **Context assembly**: gather the current per-pane state (labels, activities, headlines,
  recent events, maybe a scrollback slice for the candidate panes) — all already in the
  watcher.
- **Interpretation**: an LLM call that, given the utterance + pane context, emits a
  STRUCTURED plan: `{target_pane, action: send|create|select, keys|program, needs_approval}`.
  Structured output, not free text, so the daemon executes it deterministically.
- **Approval gate**: if `needs_approval`, push a confirmation to the phone (a
  notification-style prompt) and wait; otherwise fire.
- **Execution**: the daemon's existing `send`/`select` + the new `create`. Nothing here
  bypasses the observe-and-inject model.

The interpretation LLM is a *new* call distinct from the classify parser — it's
user-initiated and infrequent (one per command), so its cost/latency profile is unlike
the per-tick parser and it shouldn't share that path.

## How this relates to the /live feed and the state long-poll (#47)

First-blush intuition (to validate): **not directly related, and that's fine.** Reasoning:

- `/api/live` and the #47 state long-poll are the *observe* half — pushing terminal state
  TO the phone in real time (frames, pane switches). "Chat with tmux" is the *act* half —
  taking intent FROM the user and injecting keys. Different direction of flow.
- The one real touch-point: the command router **consumes** the same per-pane state the
  live/state feeds surface, for routing context (capability 3). So #47 and the activity
  data are an *input* to routing, not a shared mechanism. The router doesn't need the
  live frame stream; it needs the structured per-pane summary, which already exists.
- A second, softer link: after a routed command fires, the user wants to SEE it land —
  and #47 already makes that instant (the target pane's activity change returns the state
  hold immediately; the live view streams the frames). So #47 makes the *feedback* on a
  chat command feel immediate, but chat doesn't require anything new from it.

Verdict: build chat-with-tmux on the daemon's send/select/create + per-pane state. It
benefits from #47's instant feedback but has no dependency on the live transport. Keep
them decoupled.

## Gemini Live Mode reuse (qs-app #2358 / #2356 / #2357)

The user asked whether the unmerged March Gemini Live Mode work in qs-app (meeting
intelligence) offers reusable primitives for the voice side of this feature — a Gemini
Live session wrapper, an audio pipeline, a tool-dispatch loop — or whether the overlap is
just "both use Gemini."

The finding (from a code-level deep-dive of all three PRs): the **transport and plumbing
are genuinely reusable; the product logic is not** — and the overlap is more than "both
use Gemini." Details:

**What they are.** #2356 is the design doc; #2357 the first implementation; **#2358 the
authoritative superset** (it contains #2357 plus everything through v5). The architecture
is `browser → QS backend (its own WebSocket) → Gemini Live (Vertex bidi stream)` — two
hops, the Google SDK owns the actual Gemini socket. Model
`gemini-live-2.5-flash-native-audio` on Vertex, `us-central1`. All three are stalled and
unmerged (~4 months, `CONFLICTING`/`DIRTY`, only automated reviews + one unresolved human
thread, #2357's CI failing) — a shelved rapid-prototype, not a killed one. Copilot flagged
two real string-vs-UUID bugs in the DB code (`live.py:542`, `:361`) — note if copying.

**Reusable, product-agnostic building blocks** (worth lifting, `ui/src/hooks/useLiveMode.ts`
+ `backend/routes/live.py`):
- **Mic → 16 kHz PCM → WebSocket recipe**: AudioContext + ScriptProcessorNode (4096-frame),
  Float32→Int16, base64 framing, and the silent gain=0 node to keep the processor alive.
  A clean "stream mic to a server" pipeline — copy it for voice.
- **Gemini Live session wrapper + reconnect loop** (`_run_gemini_session`): connect →
  concurrent forward-audio / receive coroutines → exponential-backoff reconnect (≤10).
- **Voice tool-dispatch loop** (`_gemini_receiver` → `_handle_tool_call`): receive
  `tool_call` → run handler → return `FunctionResponse`, WITH dedup and malformed/echoed-
  call rejection. This is exactly the shape our router needs — swap their single
  `query_data` tool for `run_in_pane(pane, command)` / `list_panes()` / `create_pane(...)`.
- **Hard-won Gemini-Live operational lessons**, directly transferable: the right model id/
  region/AUDIO modality, needing `fc.id` on the `FunctionResponse`, and crucially **do NOT
  echo tool results back to Gemini** (caused feedback/echo loops) — weeks of trial-and-
  error already burned down.

**What does NOT transfer / actively diverges:**
- **The system prompt is the wrong shape.** Live Mode's prompt is tuned to *passively
  eavesdrop and stay silent* (err toward not interrupting a meeting) — the OPPOSITE of an
  active command interpreter that should confirm and execute. Full rewrite.
- **No approval/confirmation flow exists.** Live Mode fires queries autonomously because
  they're read-only and low-stakes; our capability 4 (approve-before-injecting-keys) is
  net-new — nothing here models it.
- All the meeting product surface (Sessions UI, summaries, project/data-source context,
  cost dashboards, tab-audio, `handle_ask_request` wiring) is irrelevant.

**Verdict:** treat #2358 as a **reference implementation for the voice→tool-dispatch
transport layer** — clone the audio pipeline, the session/reconnect wrapper, and the
tool-dispatch loop; discard everything meeting-specific; write our own prompt + approval
gate. It's a real head start on the hard streaming/audio/tool-loop mechanics, not an
incidental Gemini overlap. But it only accelerates the **voice** milestone (step 3 below);
it does nothing for create-pane or the typed router, so it must not gate or reshape those.

## Recommended sequencing

1. **Pane creation endpoint** — standalone, useful now, no NLP. `/api/panes` POST →
   `split-window`/`new-window`, optional program, returns the new pane. (This is the
   concrete "create a pane from the UI" ask; it lands independent of the rest.)
2. **Typed command router** — utterance + pane context → structured plan → approval →
   execute. Text-only. Proves the routing + approval UX where it's cheapest to iterate.
3. **Voice input** — speech→text feeding the same router; evaluate the qs-app Live
   primitives here (per the research section).

Each step is independently shippable and independently valuable, which is the point: we
learn the routing/approval UX on cheap typed input before committing to a voice transport.
