---
title: "Live Mode (voice)"
---

# Design: Live Mode — talk to your tmux

Status: **v0.1 implemented** (this doc tracks the shipped MVP). Voice-only surface: a
Live button in the header opens a session where you talk in natural language about — and
into — every pane at once. The model hears you, sees the live state of all panes, answers
out loud, and can type into a pane, as the one and only action it can take.

## Why voice, why now

tmux-rc already solved *observation* (cards, summaries, the live frame stream) and
*targeted input* (the composer types into the selected pane). What it can't do is span
panes conversationally: "what's the codex pane stuck on?", "tell claude yes", "run the
tests again in the build pane" — each of those is trivial to *say* and clumsy to do by
tapping a card, reading, switching, typing. A voice loop over the whole session is the
natural next interface, and everything it needs already exists in the daemon: per-pane
state that is *always current* (the watcher's whole job), and key injection
(`send_keys`). Live Mode is a thin conversational layer over those two primitives — it
adds **no new observation machinery and no new control channel**, staying inside the
founding "observe the terminal, don't drive the agent" invariant.

## Architecture (two hops, mirrored from a proven implementation)

`browser → daemon WebSocket → Gemini Live (Vertex bidirectional stream)`

The browser only captures and plays audio; the daemon owns the model session. This is
deliberately modeled line-for-line on a battle-tested internal implementation of the
same shape (its session wrapper, reconnect loop, tool dispatch, and audio pipeline),
because that work already burned down the non-obvious failure modes — see "Traps
inherited" below.

- **Client**: `getUserMedia` mic → `AudioContext` at 16 kHz → 4096-sample
  Float32→Int16 PCM frames → base64 over the WebSocket. A gain-0 node keeps the graph
  alive without echoing the mic to the speakers. Playback of the model's voice is the
  reverse: base64 24 kHz PCM chunks → queued `AudioBufferSourceNode`s.
- **Daemon** (`daemon/live.py`): accepts the WS, builds the system prompt from live
  watcher state, opens `client.aio.live.connect(...)` on Vertex, then runs two
  concurrent coroutines — forward-audio-up and receive-events-down — plus a
  context-updater (below). Session drops reconnect with exponential backoff.
- **Model**: `gemini-live-2.5-flash-native-audio` (Vertex, `us-central1`), AUDIO
  response modality, input+output transcription on (so the UI can show a text
  transcript of both sides). Configurable via `TMUXRC_LIVE_MODEL`.
- The Live client is built separately from the classifier's cached client: Live needs
  `us-central1` (not the classifier's `global`) and must NOT inherit the classifier's
  20s per-request timeout — a live session is one long-lived stream.

## How the model sees pane state (the part that makes it "talking to tmux")

The watcher already keeps every pane's state current — that is the daemon's core loop.
Live Mode simply reads it; there is no separate polling and no LLM call until you speak.

- **At connect**: the system prompt embeds a full snapshot — every pane's id, label,
  tool, activity, headline, idle summary, pending question, and the tail of its current
  screen text. This is the same data the phone's cards render, sourced from
  `watcher.digest()` + the latest snapshot per pane.
- **While the session runs**: a context-updater coroutine waits on the watcher's
  `state_version` (the same change signal the `/api/state` long-poll uses — one source
  of truth for "something changed") and, throttled to at most one update every few
  seconds, injects a compact `[tmux update]` message into the session with
  `turn_complete=False`. That flag is the key mechanism: the content lands in the
  model's context **without prompting a response turn**, so the model never chatters at
  a state change — but when you next speak, its view is already current. This is
  exactly the "state is just always up to date; a turn happens when I talk" model.
- **After the model types**: the tool handler forces a daemon reparse of the target
  pane and, after a short settle, sends that pane's fresh screen tail as another
  ambient update — so the model sees what its own keystrokes did without us echoing
  anything through the tool response (see traps).

Updates are digest-sized (labels, activities, headlines, one-line summaries, pending
questions), not full screens — full screens ride only in the connect snapshot and in
the post-type refresh of the acted-on pane. That keeps a long-running session's token
drip small while keeping answers accurate; if it proves too coarse, the escalation path
is a screen-tail in updates for the active pane only.

## The one tool: `type_in_pane`

```
type_in_pane(pane_id, text, press_enter=true)
```

That is the entire action surface of v0.1, on purpose. It types exactly `text` into the
given pane via the daemon's existing `send_keys` (literal mode), optionally submitting
with Enter — the same primitive every other tmux-rc input path uses. The handler:

- rejects calls with a missing/unknown `pane_id`, empty `text`, or unexpected extra
  args (the model occasionally echoes a prior FunctionResponse back as a new call —
  inherited guard);
- executes, then returns a **terse** FunctionResponse (`typed into <label>`) carrying
  the original call id — never the resulting screen;
- notifies the browser (`{type:"typed", ...}`) so every action the voice takes is
  visibly logged in the overlay — you always see what it typed and where;
- forces a reparse and schedules the post-type context refresh described above.

Deliberately **absent** from v0.1: pane switching (the model doesn't need focus to
type — `send_keys` targets any pane), pane creation ("open claude in a new pane in
~/src/x" is the obvious v0.2, pending the create-pane endpoint), kill/destroy
(destructive, needs the approval design from chat-with-tmux), and any read tool (state
is pushed, not pulled).

## Prompting strategy (where the quality lives)

The reference implementation's conversational quality came from prompt iteration, not
architecture, and its lessons transfer even though its use case was the polar opposite
(a *passive* meeting listener that errs toward silence vs. our *active* command
executor). What carried over, adapted:

1. **Terse, imperative, behavior-first.** No persona preamble. Every sentence is an
   operating rule. The reference prompt is ~15 lines; ours stays in that class.
2. **Voice discipline.** The model speaks; so: one-or-two spoken sentences, no
   markdown, no lists, no code blocks read aloud. Without this, audio models recite
   formatting.
3. **Noise rules survive verbatim.** A live mic picks up keyboard clatter, other
   people, fragments. The reference's "ignore background, don't respond to fragments,
   don't invent questions, never act twice on the same request" rules apply unchanged —
   they're about the *microphone*, not the product.
4. **The tool-contract lesson, inverted.** The reference had to teach "the query system
   CANNOT see the screen — you must translate visual references into explicit terms."
   Our version of that hard-won line: **"type_in_pane types EXACTLY your text into a
   real terminal, as if the user typed it — you are typing, not chatting; say what
   you're typing and into which pane as you do it."** The model must understand it is
   operating a physical-ish effector with a precise contract, not emitting
   conversation.
5. **Where the reference says "err toward silence," we split the rule in two.**
   *Answering* questions is low-stakes — be helpful, answer from state, admit when the
   state doesn't show something (never invent terminal output). *Typing* is
   high-stakes — only on a clear user request, exact text only, and a hard rule against
   destructive commands (`rm -rf`, force-push, `kill`) unless the user spelled them
   out. The passive/active split lives inside the prompt rather than in the
   architecture.
6. **Ambient context is labeled and fenced.** `[tmux update]` messages are declared to
   be state refreshes, not the user speaking: "never respond to them; treat them as
   current truth." Belt (the `turn_complete=False` mechanism) and suspenders (the
   prompt rule).
7. **Per-pane audience awareness.** Agent panes (claude/codex/gemini) receive natural
   language — the agent parses it. Shell panes receive exact commands. Panes showing a
   numbered menu can be answered with the option the prompt expects. This routing
   knowledge lives in the prompt because the pane state already carries each pane's
   `tool`.

### Traps inherited (paid for elsewhere, honored here)

- **Never feed tool *results* back as conversation.** Returning rich results in the
  FunctionResponse induced feedback/echo loops in the reference (the model re-emitting
  responses as new tool calls). We return a terse status and deliver the actual outcome
  as ambient state instead.
- **Carry `fc.id` on every FunctionResponse** — omitting it wedges the session.
- **Reject malformed/echoed tool calls** (extra args, empty payload) instead of
  executing them.
- **Model/region/modality must match exactly** — a live model id on the wrong region
  fails opaquely. Pinned: `gemini-live-2.5-flash-native-audio` @ `us-central1`, AUDIO.
- **Proactive audio** (`proactivity.proactive_audio=true`) lets the model decide not to
  respond — required for the noise rules to actually work rather than the model
  answering every sound.

## Alternatives considered

- **Text chat first, voice later.** Rejected for this feature (though it remains the
  chat-with-tmux route): the point here is specifically the hands-free, cross-pane
  conversation; a text box duplicates the composer we already have.
- **Browser talks to Gemini directly** (no daemon hop). Rejected: the tool must execute
  `send_keys` on the daemon anyway, credentials live server-side, and the daemon is
  where pane state lives. Two hops is the only shape that doesn't scatter authority.
- **A `read_pane` tool instead of pushed context.** Pull keeps tokens minimal, but adds
  a round-trip before every answer and a second tool to prompt around. Push matches how
  the rest of tmux-rc works (the watcher maintains truth; consumers read it) and makes
  answers instant. Revisit only if ambient updates prove too expensive.
- **Reusing the classifier's Vertex client.** Rejected — see architecture: wrong region
  default, and its per-request timeout (a deliberate anti-wedge guard for one-shot
  parse calls) would sever a long-lived live stream.
- **Full screens in every ambient update.** Simplest mental model, unbounded token
  drip. Digest-level updates + targeted screen refreshes cover the same questions at a
  fraction of the cost.
- **An approval gate before typing.** The chat-with-tmux design calls for
  approve-before-inject on *routed text commands*. For v0.1 voice we deliberately run
  without one: the session is explicitly started by the user, every action is spoken
  aloud and logged in the overlay, and the prompt constrains typing to explicit
  requests. If real use shows misfires, the gate design is already written.

## v0.2 candidates (explicitly out of scope now)

Pane creation ("open claude code in a new pane in ~/src/x") once the create-pane
endpoint exists; the approval gate; barge-in/interruption tuning; a wake word; cost
metering per session (the reference tracks per-modality token usage — worth porting
when sessions get long).
