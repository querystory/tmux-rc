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

## Emerging conclusion
Across working + rewind, TEXT wins: equal-or-better accuracy, cheaper, faster. Image's
theoretical edge (color/highlight) hasn't materialized because Claude Code encodes
state in CHARACTERS (❯, box art, labels), not pure color. Text+image is never worth
the extra cost so far.

STILL TO TEST — the only cases where image should truly win: selection shown ONLY by
background highlight (no cursor char), and red=error where color is the whole signal.
If text handles those too, ship text-only and drop the renderer from the hot path.
