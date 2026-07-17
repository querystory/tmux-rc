"""Research harness: how well does Flash Lite "see" a terminal pane?

Captures the current (or a target) tmux pane three ways — plain text, rendered PNG,
and text+image — sends each to Flash Lite with the same parser prompt, and prints the
JSON each returns plus token counts. Lets us compare input modes empirically before
committing the architecture.

Usage (from repo root, venv active, GOOGLE_CLOUD_PROJECT set):
    python -m research.probe [pane_target]     # e.g. %0  or  session:win
    python -m research.probe --save NAME        # also save the capture+png to research/samples/
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from daemon import tmux
from daemon.llm import _MODEL, _client
from daemon.render import render_png

SAMPLES = Path(__file__).parent / "samples"

# The parser prompt under test IS the production one — import it (single source of
# truth) rather than keeping a copy here that silently drifts from what ships.
from daemon.classify import parser_prompt  # noqa: E402

PROMPT = parser_prompt()


def _capture_ansi(pane_id: str) -> str:
    return subprocess.run(
        ["tmux", "capture-pane", "-p", "-e", "-J", "-t", pane_id, "-S", "-50"],
        capture_output=True, text=True,
    ).stdout


# Gemini 3.x Flash Lite pricing (USD per 1M tokens). Update if it changes; used only
# for a rough per-call cost estimate in this research harness.
_IN_PER_M, _OUT_PER_M = 0.10, 0.40


def _parse(parts) -> dict:
    """Run one parse and return quality + tokens + latency + cost estimate."""
    import time

    from google.genai import types

    client = _client()
    in_tokens = client.models.count_tokens(model=_MODEL, contents=parts).total_tokens
    t0 = time.time()
    resp = client.models.generate_content(
        model=_MODEL, contents=parts,
        config=types.GenerateContentConfig(
            system_instruction=PROMPT, response_mime_type="application/json", temperature=0.0
        ),
    )
    latency = time.time() - t0
    out_tokens = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
    cost = in_tokens / 1e6 * _IN_PER_M + out_tokens / 1e6 * _OUT_PER_M
    try:
        result = json.loads(resp.text)
    except Exception:
        result = {"_raw": resp.text}
    return {"json": result, "in": in_tokens, "out": out_tokens, "latency": latency, "cost": cost}


def main() -> None:
    from google.genai import types

    save_name = None
    argv = sys.argv[1:]
    if "--save" in argv:
        i = argv.index("--save")
        save_name = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]  # drop --save and its value
    target = argv[0] if argv else None

    pane = tmux.find_pane(target)
    if pane is None:
        print("no pane found")
        return
    plain = tmux.capture_pane(pane.id)
    ansi = _capture_ansi(pane.id)
    png = render_png(ansi)

    if save_name:
        SAMPLES.mkdir(parents=True, exist_ok=True)
        (SAMPLES / f"{save_name}.txt").write_text(plain)
        (SAMPLES / f"{save_name}.png").write_bytes(png)
        print(f"saved sample '{save_name}' to {SAMPLES}\n")

    img = types.Part.from_bytes(data=png, mime_type="image/png")
    modes = {
        "TEXT      ": [plain],
        "IMAGE     ": [img],
        "TEXT+IMAGE": [plain, img],
    }
    print(f"pane {pane.id} ({pane.label})  —  PNG {len(png) // 1024}KB\n")
    rows = []
    for name, parts in modes.items():
        m = _parse(parts)
        rows.append((name, m))
        print(f"===== {name}  ({m['in']} in / {m['out']} out tok · "
              f"{m['latency']:.1f}s · ${m['cost'] * 1000:.3f}/1k calls) =====")
        print(json.dumps(m["json"], indent=2, ensure_ascii=False))
        print()
    print("===== SUMMARY =====")
    print(f"{'mode':12} {'in':>6} {'out':>5} {'latency':>8} {'$/1k calls':>11}")
    for name, m in rows:
        print(f"{name:12} {m['in']:>6} {m['out']:>5} {m['latency']:>7.1f}s {m['cost'] * 1000:>10.3f}")


if __name__ == "__main__":
    main()
