#!/usr/bin/env bash
# steer <pane> <message> — type a prompt into an agent TUI running in another tmux pane
# and try to confirm it was actually submitted.
#
# Why each step is here (the long version is docs/agent-orchestration.md):
#   * `send-keys 'text' Enter` in ONE call frequently loses the Enter — the TUI has not
#     consumed the paste when the Return arrives — leaving the message composed but
#     unsent, with nothing raised anywhere. So: literal text, pause, Enter on its own —
#     and the PAUSE is the load-bearing part; two calls only make it expressible.
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
# delivery is inference about a repaint, not a receipt. Four false negatives are known,
# three of them found by measuring live panes rather than by reading the screen carefully:
#   1. a transcript line rendered with the same glyph as the input line (handled: take
#      the last match, not the first);
#   2. the empty input box padded with U+00A0, which POSIX [[:space:]] does not strip in
#      most locales (handled: folded below);
#   3. greyed placeholder / hint text drawn after the glyph, which a plain-text capture
#      cannot distinguish from typed input (NOT handled, and not fixable here — the
#      daemon captures styled output and marks that run ⟪placeholder⟫; see
#      _mark_placeholder in openbus/tmux.py);
#   4. the capture racing the repaint. There is no redraw event to wait for, only the
#      fixed sleep below, so a TUI that takes longer than it reports UNCONFIRMED on a
#      message that did land (NOT handled — a longer sleep trades one wrong answer for a
#      slower one, and there is no value that is right for every harness and load).
# Assume a fifth is waiting.
set -u

glyph=${STEER_GLYPH:-❯}  # the agent's input-line prompt; differs per harness
# U+00A0 as literal bytes. `\xNN` in a sed expression is a GNU extension: BSD/macOS sed
# matches a literal "x c 2" instead and the NBSP fold below silently stops working, which
# turns limitation 2 back on and makes the script report UNCONFIRMED on messages that
# landed. printf's octal escapes are POSIX and mean the same thing everywhere.
NBSP=$(printf '\302\240')
MAX_BYTES=4000           # tmux caps one send-keys near 16KB; the daemon chunks, we refuse

[ $# -ge 2 ] || { echo "usage: steer <pane> <message>" >&2; exit 2; }
# A tmux target is not an identity. "%3" and "session:win.0" name the same pane but are
# different strings, so a lock keyed on the string does not serialise two callers who
# spell it differently; and tmux REUSES "%3" once that pane dies, so a pane that exits
# between the composer check and a retrying Enter hands the rest of the message to a
# stranger. Resolve the target once to the canonical id plus the pane's PID, key
# everything on that, and re-check it before every keystroke — the daemon's send path
# makes the same PID check, for the same reason.
# Check the ANSWER, not the exit status: tmux display-message exits 0 on a target it
# cannot find and hands back the format with the fields empty. Taking that as success
# leaves the target empty, and an empty -t is the CURRENT pane — this script would then
# type into whoever ran it.
ident() { tmux display-message -p -t "$1" '#{pane_id} #{pane_pid}' 2>/dev/null; }
who=$(ident "$1")
pane=${who%% *}
case $pane in %[0-9]*) ;; *) echo "steer: no such pane: $1" >&2; exit 1 ;; esac
same() { [ "$(ident "$pane")" = "$who" ] || {
  echo "steer($pane): pane is gone or was recycled — aborting" >&2; exit 1; }; }
# The draft check and the send below are not one atomic step: two runs against the same
# pane would each see an empty composer, then interleave their text and Enters into one
# prompt while both retry loops reported success. An orchestrator steering a fleet in
# parallel is exactly the caller that hits this, so serialise per pane. It covers other
# runs of THIS script only — a human at the keyboard, or the daemon's own send path, is
# outside it. One keystroke path is the real fix, and that is what the daemon provides.
# The lock lives in a per-user runtime dir, not $TMPDIR: the name is derived from the pane
# id and so is guessable, and on a shared /tmp another user can park a symlink there and
# have this redirection truncate whatever it points at. Missing flock still skips the lock —
# nothing is lost that was not already unlocked — but a flock that is present and then
# fails to take the lock is a different thing, and we exit rather than send unserialised.
if command -v flock >/dev/null 2>&1; then
  lockdir=${XDG_RUNTIME_DIR:-$HOME/.cache}/tmux-rc
  mkdir -p "$lockdir" || { echo "steer: cannot create $lockdir" >&2; exit 1; }
  exec 9>"$lockdir/steer-${pane//[^A-Za-z0-9]/_}.lock" || exit 1
  flock 9 || { echo "steer: could not lock $pane" >&2; exit 1; }
fi
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
  printf '%s' "$line" | sed "s/$NBSP/ /g; s/[[:space:]]//g"  # fold the NBSP pad, drop spaces
}

# A draft left behind by an earlier dropped Enter would be silently concatenated with this
# message, and the Enter below would submit the pair while reporting it as ours. Refuse
# loudly instead, printing what was there — placeholder text (limitation 3) trips this too,
# and a visible refusal is the failure mode to prefer over a corrupted prompt.
draft=$(inputline) || { echo "steer($pane): no input line found — wrong pane, or wrong STEER_GLYPH?" >&2; exit 1; }
[ -z "$draft" ] || { echo "steer($pane): composer is not empty, refusing to append to: $draft" >&2; exit 1; }

same
tmux send-keys -t "$pane" -l "$msg" || exit 1
for i in 1 2 3; do
  sleep 1
  same
  tmux send-keys -t "$pane" Enter || exit 1
  sleep 2
  # Identity again AFTER the capture, not just before the keystroke: if the pane exited
  # and tmux handed its id to a replacement, the empty composer we just read is the
  # replacement's and reporting "submitted" off it would be a false positive about a
  # message that went nowhere.
  if line=$(inputline) && [ -z "$line" ]; then
    same
    echo "steer($pane): submitted (attempt $i)"
    exit 0
  fi
done

echo "steer($pane): UNCONFIRMED — could not prove delivery (see LIMITATIONS); input line reads: ${line-}" >&2
exit 1
