# tmux-rc — agent notes

Phone dashboard for tmux panes running AI agents. A local daemon watches every pane,
classifies each screen with a small LLM, and serves the result on `localhost:8080`.

## Reading pane state (use the daemon — it has derived state tmux doesn't)

- `curl -s localhost:8080/api/digest` — **start here**: per-pane headline, activity
  (`running`/`waiting`/`idle`), idle time, pending question, LLM idle-summary, and the
  recent timestamped event history. One GET answers "what's been going on in each pane."
- `curl -s localhost:8080/api/state` — the phone's live view: current cards only (events
  field carries only what's NEW since the last parse; history lives in /api/digest).
- `curl -s localhost:8080/api/panes/<id>/snapshots` (+ `/<snap_id>`) — raw screen
  captures over time, for when you need the actual text.

## Acting on panes (use tmux directly — it's the better verb interface)

`tmux send-keys -t %3 ...`, `tmux capture-pane -p -t %3`, etc. The daemon's POST
endpoints exist for the phone (they add IAP-actor audit records); a local agent gains
nothing by proxying through them.

## Development

- `make test` / `make fmt` (scope ruff to files you touched — don't reformat the tree).
- The daemon usually runs from this checkout with uvicorn `--reload`: edits here restart
  it (in-memory state rebuilds from tmux within a couple of ticks).
- The parser prompt lives in `daemon/parser_prompt.txt` — load-bearing; edit as prose.
- Telemetry fields and record types are documented in `docs/hint.md`.
