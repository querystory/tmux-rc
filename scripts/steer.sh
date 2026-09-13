#!/usr/bin/env bash
# steer <pane> <message> — type a prompt into an agent TUI running in another tmux pane
# and try to confirm it was actually submitted.
#
# Why each step is here (the long version is docs/agent-orchestration.md):
#   * `send-keys 'text' Enter` in ONE call frequently loses the Enter — the TUI has not
#     consumed the paste when the Return arrives — leaving the message composed but
#     unsent, with nothing raised anywhere. So: literal text, pause, Enter on its own.
#   * Confirm on the INPUT LINE, never the whole pane: a *sent* message is echoed into
#     the transcript, so a pane-wide grep matches either way and reports success whether
#     or not the send worked.
#   * Retrying the Enter is safe because a bare Return on an empty input box is a no-op.
#
# If the tmux-rc daemon is running, prefer POST /api/panes/<id>/send: it serialises sends,
# audits them with an actor, and re-parses the pane afterwards. Writing to a pane behind
# the daemon's back leaves its view of that pane stale until the next capture, and leaves
# no record of who typed. This helper is for when the daemon is not there — and as
# evidence for the argument in docs/design/agent-client.md.
#
# LIMITATIONS. This says UNCONFIRMED rather than "unsent", because polling a terminal for
# delivery is inference about a repaint, not a receipt. Three false negatives are known,
# all found by measuring live panes rather than by reading the screen carefully:
#   1. a transcript line rendered with the same glyph as the input line (handled: take
#      the last match, not the first);
#   2. the empty input box padded with U+00A0, which POSIX [[:space:]] does not strip in
#      most locales (handled: folded below);
#   3. greyed placeholder / hint text drawn after the glyph, which a plain-text capture
#      cannot distinguish from typed input (NOT handled, and not fixable here — the
#      daemon captures styled output and marks that run ⟪placeholder⟫; see
#      _mark_placeholder in openbus/tmux.py).
# Assume a fourth is waiting.
set -u

glyph=${STEER_GLYPH:-❯}  # the agent's input-line prompt; differs per harness
MAX_BYTES=4000           # tmux caps one send-keys near 16KB; the daemon chunks, we refuse

[ $# -ge 2 ] || { echo "usage: steer <pane> <message>" >&2; exit 2; }
pane=$1
shift
msg=$*
[ -n "$msg" ] || { echo "steer: empty message" >&2; exit 2; }
bytes=$(printf %s "$msg" | wc -c)
if [ "$bytes" -gt "$MAX_BYTES" ]; then
  echo "steer: message is $bytes bytes; this helper does not chunk. Send it through the" \
       "daemon (/api/panes/$pane/send), which does." >&2
  exit 2
fi

# The input line is the LAST line starting with the glyph — the transcript above may
# render sent messages with the same one. Matched and stripped LITERALLY: a glyph holding
# a regex or sed metacharacter would otherwise silently change what gets matched, and the
# result of that match is the only evidence this script has. Exit 1 means no input line
# was found at all, which is NOT the same as finding an empty one.
inputline() {
  local line
  line=$(tmux capture-pane -p -t "$pane" | awk -v g="$glyph" 'index($0, g) == 1' | tail -1) || return 1
  [ -n "$line" ] || return 1
  line=${line#"$glyph"}
  printf '%s' "$line" | sed 's/\xc2\xa0/ /g; s/[[:space:]]//g'  # fold the NBSP pad, drop spaces
}

# A draft left behind by an earlier dropped Enter would be silently concatenated with this
# message, and the Enter below would submit the pair while reporting it as ours. Refuse
# loudly instead, printing what was there — placeholder text (limitation 3) trips this too,
# and a visible refusal is the failure mode to prefer over a corrupted prompt.
draft=$(inputline) || { echo "steer($pane): no input line found — wrong pane, or wrong STEER_GLYPH?" >&2; exit 1; }
[ -z "$draft" ] || { echo "steer($pane): composer is not empty, refusing to append to: $draft" >&2; exit 1; }

tmux send-keys -t "$pane" -l "$msg" || exit 1
for i in 1 2 3; do
  sleep 1
  tmux send-keys -t "$pane" Enter || exit 1
  sleep 2
  if line=$(inputline) && [ -z "$line" ]; then
    echo "steer($pane): submitted (attempt $i)"
    exit 0
  fi
done

echo "steer($pane): UNCONFIRMED — could not prove delivery (see LIMITATIONS); input line reads: ${line-}" >&2
exit 1
