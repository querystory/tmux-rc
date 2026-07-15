# Design: the agentic control plane

Status: **draft / thinking** — no code yet.

## The shift

So far termiphone lets you *type into* whatever's in a pane. The bigger idea: let you
**direct the environment by intent**, and have termiphone figure out and perform the
concrete terminal actions. termiphone stops being a keyboard and becomes an
orchestrator that drives Claude Code *and* tmux itself.

Examples of intent (what you say) → actions (what it does):
- "open a shell and run the tests" → new tmux window/pane, type `make test`, Enter.
- "stop and roll back to when we did X" → Ctrl-C the current work, Esc-Esc to the
  Rewind picker, navigate to the entry matching "X", Enter to restore.
- "kill whatever's stuck in window 3" → target that pane, Ctrl-C / kill.
- "tell Claude to also update the tests" → compose and send a well-formed prompt.

This is "terminal use" as an agentic capability — the mobile control plane can operate
the whole terminal, not just relay keystrokes to one pane.

## Autonomy model: intent → confirm → execute

Decision: **intent → confirm → execute.** You state intent; termiphone *plans* the
concrete tmux/agent actions (using its parse of current state — which pane, what's on
screen, is the Rewind picker up, etc.) and shows the plan for **one-tap confirmation**
before executing. You stay in the loop; nothing destructive happens without an OK.

Why not full autonomy: it's an agent controlling agents with the power to kill
processes, send keystrokes, and roll back code — the blast radius demands a human
checkpoint. Why not suggestions-only: the value is that termiphone does the fiddly
multi-step sequencing (find pane → Esc-Esc → arrow to entry → Enter); staging raw keys
for you to send defeats the point.

### The confirmation should be smart, not uniform
Not every action needs the same friction. Tier the confirmation by risk:
- **Auto-advance known-safe steps** (no confirm). Trivial, reversible, obviously-
  intended next steps: submitting an AskUserQuestion after you picked options (the
  "Ready to submit? [Submit]" step — you already answered, termiphone can press it),
  "Press Enter to continue" prompts, dismissing a notice. These are one keystroke with
  no downside; making you take another turn is pure friction. *(This example came up
  live: after selecting multi-choice answers, the trailing Submit could auto-fire.)*
- **One-tap confirm** (default) for actions with real effect: running a command,
  sending a prompt, navigating a picker.
- **Explicit/typed confirm** for destructive/irreversible: killing a process, a
  rollback that discards work, anything with data loss. Show exactly what will happen.

The parse we already do tells termiphone which tier applies (it knows a Submit step vs
a rollback).

## How intent becomes actions

An LLM planner turns natural-language intent + current pane state into a concrete,
ordered action list over the primitives termiphone already has:
- `send_keys` (literal text / named keys — Enter, Esc, C-c, arrows) — have it.
- tmux window/pane ops (new-window, select, kill) — thin wrappers to add.
- higher-level moves composed from the above ("open Rewind and restore to X" = a
  scripted sequence the planner emits, informed by the live parse of the picker).

The planner outputs the action list + a human summary ("I'll: 1. Ctrl-C the current
run  2. Esc-Esc  3. select 'pivot to LLM passthrough'  4. Enter to restore"). UI shows
the summary → you confirm → termiphone executes step by step, re-parsing between steps
to stay grounded (e.g. confirm the Rewind picker actually opened before arrowing).

## Hard problems to think through
- **Grounding & verification.** Between planned steps the screen must be re-checked —
  did the picker open? is the cursor on the right entry? Blindly firing a scripted
  sequence is how you restore to the wrong point. Each step should verify its
  precondition from a fresh parse (this is why the parser matters here too).
- **Targeting the right entry** in a picker by fuzzy description ("when we did X") —
  match against the parsed Rewind entries, and if ambiguous, ask.
- **Failure/abort.** If a step's precondition isn't met, stop and report, don't plow on.
- **Destructive intent.** Rollback/kill must surface exactly what's lost and require
  the stronger confirm tier.
- **Multi-pane routing.** "run the tests" — which pane/session? Infer from context or ask.

## Relationship to other docs
- Leans hard on the **LLM parser** (the state grounding for planning + step
  verification) and the **primitives** (send_keys, tmux ops).
- The work-tree/timeline give intents like "roll back to when we did X" something
  concrete to resolve against (X = a node/Rewind entry).

## Open questions
- Is the planner a separate LLM call, or an extension of the parse prompt?
- How much scripting is hard-coded (known sequences like "open Rewind") vs.
  planner-generated per intent?
- Voice input on the phone for intent? (Natural fit for "tell it what to do" on mobile.)
- Where does this overlap with just prompting Claude Code itself to do the thing —
  when should termiphone drive tmux vs. hand the intent to the agent in the pane?
