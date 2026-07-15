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
- STILL TO TEST — the discriminating cases where text loses info: a menu/Rewind
  picker where selection is shown by highlight/color, and red error output. That's
  where image should beat text.
