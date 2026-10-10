// Desktop key passthrough: a keydown on the focused terminal becomes a /send body.
//
// Named keys go as tmux key names (literal: false), printable characters as literal text.
// Browser names that differ from tmux's are mapped; Escape, Tab, Enter, Home, End and the
// F-keys are the same in both. PPage/NPage are what tmux's list-keys prints.
const NAMED = { ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left", ArrowRight: "Right", PageUp: "PPage", PageDown: "NPage", Backspace: "BSpace", Delete: "DC", Insert: "IC", Escape: "Escape", Tab: "Tab", Enter: "Enter", Home: "Home", End: "End" };

// Returns { keys, literal } or null for a key the browser keeps. Cmd is always the
// browser's (copy, paste, tabs), as are Ctrl-Shift chords (devtools, reopen tab, and the
// terminal-emulator paste, which arrives as a paste event), Ctrl-Tab and Ctrl-PgUp/PgDn
// (tab switching), and Ctrl-Esc (the OS's). Ctrl-C copies instead while text is
// selected, as Windows Terminal does.
// Plain Ctrl-V is the pane's C-v (literal-next, Claude Code's image paste), like a terminal.
// IME composition never reaches here: the terminal is not editable, so an input method
// has nothing to compose into, and CJK text goes through the composer, which is.
export function tmuxKey(e, selected = false) {
  if (e.isComposing || e.metaKey) return null;
  const altGr = e.getModifierState?.("AltGraph"), ctrl = e.ctrlKey && !altGr, alt = e.altKey && !altGr;
  if (ctrl && e.shiftKey) return null;
  const name = NAMED[e.key] || (/^F([1-9]|1[0-2])$/.test(e.key) ? e.key : null);
  if (name) {
    if (ctrl && (e.key === "Tab" || e.key === "Escape" || name.endsWith("Page"))) return null;
    // tmux has no name for Ctrl-Backspace (an unknown name is typed out as text); a
    // terminal's means delete-word, which is C-w to a shell and to an agent's prompt.
    if (ctrl && name === "BSpace") return { keys: "C-w", literal: false };
    if (name === "Tab" && e.shiftKey && !ctrl && !alt) return { keys: "BTab", literal: false };
    return { keys: `${ctrl ? "C-" : ""}${alt ? "M-" : ""}${e.shiftKey ? "S-" : ""}${name}`, literal: false };
  }
  if ([...e.key].length !== 1) return null; // a bare modifier, a dead key, Unidentified
  if (ctrl) {
    // A non-Latin layout reports its own letter; the chord means the key's Latin one.
    const letter = /^[\x20-\x7e]$/.test(e.key) ? e.key.toLowerCase() : /^Key([A-Z])$/.exec(e.code)?.[1].toLowerCase();
    if (!letter || alt || (selected && letter === "c")) return null;
    return { keys: `C-${letter === " " ? "Space" : letter}`, literal: false };
  }
  // Option on a Mac types its own characters (å, ∫); only an ASCII one is a Meta chord.
  if (alt && /^[\x21-\x7e]$/.test(e.key)) return { keys: `M-${e.key}`, literal: false };
  return { keys: e.key, literal: true };
}

// Every input to a pane goes out through one queue, one request at a time, in order:
// keystrokes, and as jobs (`run`) the composer, the key row and terminal clicks, so a draft
// submitted mid-burst cannot land between two keys, nor a key typed after Submit before it.
// Text typed while a request is in flight joins the queued literal for the same pane, so
// fast typing costs a round trip per burst rather than per key. An auto-repeat
// (`op.repeat`, a held key) is dropped while anything is still queued, so holding
// Backspace or an arrow stops within a round trip of letting go instead of draining a
// backlog far past where the user meant to stop. A failure, of a key (`send(op)` resolves
// false) or of a job (it rejects), drops everything queued behind it, and dropped jobs
// reject: typing on, or submitting a draft, into a pane whose state is now unknown is
// worse than losing it.
export function inputQueue(send) {
  const queue = [];
  let pumping = false;
  const pump = async () => {
    if (pumping) return;
    pumping = true;
    try {
      while (queue.length) {
        const op = queue.shift();
        const ok = op.run
          ? await op.run().then((value) => { op.resolve(value); return true; }, (error) => { op.reject(error); return false; })
          : await send(op);
        if (!ok) for (const dropped of queue.splice(0)) dropped.reject?.(new Error("dropped after a failed pane input"));
      }
    } finally { pumping = false; }
  };
  const push = (op) => {
    const last = queue.at(-1);
    if (op.repeat && last) return;
    if (op.literal && last?.literal && last.pane === op.pane) last.keys += op.keys;
    else queue.push({ ...op });
    pump();
  };
  push.run = (job) => new Promise((resolve, reject) => { queue.push({ run: job, resolve, reject }); pump(); });
  return push;
}
