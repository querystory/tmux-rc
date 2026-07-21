# Design: chat with your tmux

Status: **draft / thinking** — no code. Explores a natural-language (typed or voice)
command layer over tmux-rc: "open codex in a new pane and do X", "in the pane running the
build, retry it", with context awareness (route a command to the right pane even when
it's not selected) and a user-approval step before anything touches a live agent. Also
records how — and whether — this relates to the `/api/live` feed and the state long-poll
(#47), and what a voice (streaming-LLM) input path would take.

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

- **Input**: text now; voice later (see the voice-input section). Voice is a *modality*,
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

## Voice input: what a real-time streaming-LLM integration takes

Voice is the third milestone (below), and it's worth writing down what a bidirectional
speech→intent pipeline actually costs to build, because the mechanics are non-obvious and
easy to underestimate. These notes distil experience wiring a browser mic to a real-time
streaming LLM with tool/function-calling — the reusable shape, and the traps.

**The transport is the hard part, and it generalizes.** The workable architecture is two
hops: `browser → our daemon (its own WebSocket) → the streaming-LLM provider`. The daemon
owns the provider session; the browser only captures and plays. Concretely:

- **Mic → PCM → WebSocket.** Capture with `getUserMedia`, run through an `AudioContext`
  fixed at 16 kHz, convert Float32→Int16 PCM in fixed-size frames, base64-frame each chunk
  over the WebSocket. Prefer an `AudioWorkletNode` for the capture/processing stage —
  `ScriptProcessorNode` also works and is simpler, but it is deprecated in the Web Audio
  API, so treat it as a fallback rather than the default. A silent (gain 0) node keeps the
  audio graph alive without echoing to the speakers. This "stream mic to a server"
  pipeline is entirely product-neutral.
- **Provider session wrapper + reconnect.** A connect → two concurrent coroutines
  (forward-audio-up, receive-events-down) → exponential-backoff reconnect loop. Streaming
  sessions drop; the reconnect is not optional.
- **Tool-dispatch loop.** Receive a tool/function call → run a handler → return a function
  response, with **deduplication** and **rejection of malformed or echoed calls**. This is
  exactly the shape the command router needs: the tools are `list_panes()`,
  `run_in_pane(pane, command)`, `create_pane(...)` — the same interpret→dispatch structure
  as the typed router, just fed by speech instead of text.

**Traps worth pre-empting** (each cost real debugging time elsewhere):
- Get the model/region/modality triple right up front — a mismatch fails silently or
  degrades quality.
- A function *response* usually must carry the originating call's id; omit it and the
  session wedges.
- **Do NOT feed tool *results* back into the model** as conversation — it induces feedback
  and echo loops. Relay results to the UI, return only a terse "done/triggered" to the
  model.

**What does NOT carry over from a passive-listening design.** If any prior streaming-LLM
work was tuned to *passively listen and stay silent* (e.g. transcribe/observe without
interrupting), its system prompt is the **opposite** of what we need — an active command
interpreter that confirms and executes. Rewrite the prompt from scratch. And an
autonomous, read-only assistant has **no approval gate**; our capability 4 (confirm before
injecting keys) is net-new and must be designed here, not inherited.

**Bottom line for sequencing.** The streaming/audio/tool-loop transport is a real chunk of
work but a well-understood one, and it only accelerates the **voice** milestone (step 3).
It does nothing for create-pane or the typed router, so voice must not gate or reshape
those — build the routing + approval UX on cheap typed input first, add the voice
transport on top once that UX is proven.

## Recommended sequencing

1. **Pane creation endpoint** — standalone, useful now, no NLP. `/api/panes` POST →
   `split-window`/`new-window`, optional program, returns the new pane. (This is the
   concrete "create a pane from the UI" ask; it lands independent of the rest.)
2. **Typed command router** — utterance + pane context → structured plan → approval →
   execute. Text-only. Proves the routing + approval UX where it's cheapest to iterate.
3. **Voice input** — speech→intent feeding the same router; build the streaming-LLM
   transport here (see the voice-input section for what that takes).

Each step is independently shippable and independently valuable, which is the point: we
learn the routing/approval UX on cheap typed input before committing to a voice transport.
