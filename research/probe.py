"""Research harness: how well does Flash Lite "see" a terminal pane?

Captures the current (or a target) tmux pane three ways — plain text, rendered PNG,
and text+image — sends each to Flash Lite with the same parser prompt, and prints the
JSON each returns plus token counts. Lets us compare input modes empirically before
committing the architecture.

Usage (from repo root, venv active, GOOGLE_CLOUD_PROJECT set):
    python -m research.probe [pane_target]     # e.g. %0  or  session:win
    python -m research.probe --save NAME        # also save the capture+png to research/samples/

Model head-to-head (text-only, matching the daemon hot path) over a saved sample's
already-assembled pane text — the same `_parse` call, just parameterized on model id and
repeated for a median. Used by research/bench-*.md. `SAMPLE` is either a research/samples
`.txt` or a JSON with a top-level "pane_text" (an OTel-captured payload):
    python -m research.probe --sample SAMPLE --models gemini-3.1-flash-lite,gemini-3.5-flash-lite --repeat 3
"""

from __future__ import annotations

import json
import statistics
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


def _parse(parts, model: str = _MODEL) -> dict:
    """Run one parse and return quality + tokens + latency + cost estimate.

    `model` defaults to the daemon's configured model; the head-to-head mode passes each
    id under test. Everything else (prompt, temp 0, JSON mime, out-token cost) matches
    the daemon's classify_text call so results are apples-to-apples with production."""
    import time

    from google.genai import types

    client = _client()
    in_tokens = client.models.count_tokens(model=model, contents=parts).total_tokens
    t0 = time.time()
    resp = client.models.generate_content(
        model=model, contents=parts,
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


def _load_sample_text(path: Path) -> str:
    """Pull the pane text from a saved sample: a plain `.txt` capture, or a JSON with a
    top-level "pane_text" (an OTel-captured, already-assembled payload)."""
    if path.suffix == ".json":
        return json.loads(path.read_text())["pane_text"]
    return path.read_text()


def _bench_models(sample: Path, models: list[str], repeat: int) -> None:
    """Text-only head-to-head: run one sample through each model `repeat` times, print the
    JSON once per model plus median latency / tokens / cost. Matches the daemon hot path
    (text-only, production prompt) so it's directly comparable to what ships."""
    text = _load_sample_text(sample)
    print(f"sample {sample.name}  —  {len(text)} chars text-only, {repeat}x each\n")
    summary = []
    for model in models:
        runs = [_parse([text], model=model) for _ in range(repeat)]
        # Median every metric over the repeats — the point of --repeat is to smooth
        # run-to-run variance, so tokens/cost are aggregated like latency (not the last
        # run, which would skew the summary if responses vary even at temp=0).
        med = lambda k: statistics.median(r[k] for r in runs)
        med_lat, cost = med("latency"), med("cost")
        itok, otok = round(med("in")), round(med("out"))
        summary.append((model, itok, otok, med_lat, cost))
        print(f"===== {model}  ({itok} in / {otok} out tok · "
              f"median {med_lat:.2f}s over {repeat} · ${cost * 1000:.3f}/1k) =====")
        print(json.dumps(runs[-1]["json"], indent=2, ensure_ascii=False))
        print()
    print("===== SUMMARY =====")
    print(f"{'model':28} {'in':>6} {'out':>5} {'med lat':>8} {'$/1k':>8}")
    for model, itok, otok, lat, cost in summary:
        print(f"{model:28} {itok:>6} {otok:>5} {lat:>7.2f}s {cost * 1000:>7.3f}")


def main() -> None:
    from google.genai import types

    argv = sys.argv[1:]

    def _take(flag):  # pull "--flag VALUE" out of argv, returning VALUE or None
        nonlocal argv
        if flag in argv:
            i = argv.index(flag)
            if i + 1 >= len(argv):
                sys.exit(f"{flag} needs a value")
            val = argv[i + 1]
            argv = argv[:i] + argv[i + 2:]
            return val
        return None

    sample = _take("--sample")
    models = _take("--models")
    raw_repeat = _take("--repeat")
    try:
        repeat = int(raw_repeat) if raw_repeat else 3
    except ValueError:
        sys.exit(f"--repeat must be an integer, got {raw_repeat!r}")
    if sample:  # model head-to-head over a saved sample — no tmux/live capture
        if repeat < 1:
            sys.exit("--repeat must be >= 1")
        model_ids = (models or _MODEL).split(",")
        _bench_models(Path(sample), model_ids, repeat)
        return

    save_name = _take("--save")
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
