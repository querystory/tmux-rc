#!/usr/bin/env bash
# steer <pane> <message> — type a prompt into an agent TUI running in another tmux pane
# and try to confirm it was actually submitted.
#
# Why each step is here (the long version is docs/agent-orchestration.md):
#   * `send-keys 'text' Enter` in ONE call frequently loses the Enter — the TUI has not
#     consumed the paste when the Return arrives — leaving the message composed but
#     unsent, with nothing raised anywhere. So: literal text, pause, Enter on its own.
#   * Confirm by reading the INPUT LINE, never the whole pane: a *sent* message is echoed
#     into the transcript and stays in scrollback, so a pane-wide grep matches either way.
#   * Retrying the Enter is safe because a bare Return on an empty input box is a no-op.
#
# LIMITATION — this returns false negatives, and that is the interesting part. Polling a
# terminal for delivery is inference about a repaint, not a receipt: the screen is stale
# until the TUI redraws, the input glyph is harness-specific, and both false-negative
# sources handled below (a transcript line sharing the input glyph; a U+00A0 pad that
# POSIX [[:space:]] will not strip) were found by measurement, not by reading carefully.
# Assume a third is waiting. A process that already watches every pane and re-parses it
# after each send can report delivery instead of anyone guessing at it — the argument in
# docs/design/agent-client.md.
set -u

glyph=${STEER_GLYPH:-❯}  # the agent's input-line prompt; differs per harness
pane=$1
shift
msg=$*

# LAST match, not the first: the transcript above may render sent messages with the same
# glyph, and the input line is the bottom one. Fold the empty box's non-breaking-space
# padding into an ordinary space so the emptiness test can actually see it.
inputline() {
  tmux capture-pane -p -t "$pane" | grep "^$glyph" | tail -1 |
    sed "s/^$glyph//; s/\xc2\xa0/ /g; s/[[:space:]]//g"
}

tmux send-keys -t "$pane" -l "$msg"
for i in 1 2 3; do
  sleep 1
  tmux send-keys -t "$pane" Enter
  sleep 2
  if [ -z "$(inputline)" ]; then
    echo "steer($pane): submitted (attempt $i)"
    exit 0
  fi
done

# Deliberately "unconfirmed", not "unsent": the check is a heuristic, so this exit status
# means we could not prove delivery — not that the message failed to arrive.
echo "steer($pane): UNCONFIRMED — input line still reads: $(inputline)" >&2
exit 1
