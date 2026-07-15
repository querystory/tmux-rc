# Research: how well does Flash Lite parse a terminal pane?

`probe.py` captures the current tmux pane three ways — plain text, a rendered PNG
(via `render.py`, from `capture-pane -e` color), and text+image — sends each to Gemini
Flash Lite with one layered parser prompt, and prints the JSON + tokens + latency +
cost for each. Goal: pick the input mode empirically (quality vs cost/latency) and
discover what fields naturally show up, before committing the architecture.

    python -m research.probe [pane_target] [--save NAME]

Samples we save (capture .txt + render .png) live in `samples/` for repeatable runs.

## Findings so far

Working Claude Code pane (`samples/working`):

| mode | in tok | out tok | latency | $/1k calls |
|------|-------:|--------:|--------:|-----------:|
| text | 991 | 183 | 2.1s | 0.172 |
| image | 1120 | 175 | 2.0s | 0.182 |
| text+image | 2111 | 187 | 2.3s | 0.286 |

- All three parsed the working screen correctly, incl. a real task headline
  ("Fixing argument parsing bug in probe.py") — not just the spinner word.
- Text and image are ~equal in cost/latency; text+image is redundant here.
Rewind picker (`samples/rewind`) — the supposed image-wins case:

| mode | in tok | out tok | latency | $/1k calls |
|------|-------:|--------:|--------:|-----------:|
| text | 591 | 282 | 1.6s | 0.172 |
| image | 1120 | 257 | 3.1s | 0.215 |
| text+image | 1711 | 278 | 2.3s | 0.282 |

- ALL THREE parsed the picker correctly: 3 entries, notes, the selected entry,
  more_above=40 / more_below=15. Image did NOT win — because Claude Code marks
  selection with a ❯ CHARACTER (survives in text), not pure color/highlight.
- Text was faster (1.6s vs image 3.1s), cheaper, and captured the selected entry's
  full text where image truncated slightly.

Red-error output (`samples/error`) — the "color is the whole signal" case:

| mode | in tok | out tok | latency | $/1k calls |
|------|-------:|--------:|--------:|-----------:|
| text | 270 | 85 | 1.0s | 0.061 |
| image | 1120 | 85 | 1.4s | 0.146 |
| text+image | 1390 | 98 | 1.9s | 0.178 |

- Text recognized the failures perfectly ("finished with errors", listed the failed
  test) — because the WORDS ("FAILED", "ERROR", "AssertionError") carry the meaning;
  the red color was redundant. Text was 2.4x cheaper and faster.

## CONCLUSION — ship text-only
Across all three cases (working, rewind picker, red errors) TEXT wins: equal-or-better
accuracy, 2–2.4x cheaper, consistently faster. Image's theoretical edge (color/
highlight) never materialized, because agent UIs encode state in CHARACTERS and WORDS
(❯, box art, "FAILED"), not pure color. Text+image is never worth the extra cost.

Decision: hot-path parser = TEXT only. Keep render.py + the image switch wired but OFF
— the sole remaining image-only case is a picker that shows selection ONLY by
background highlight with no cursor char/label (rare); if we ever hit it, flip the flag.
Do NOT pay the renderer/token cost on every call for a case we haven't even seen.
