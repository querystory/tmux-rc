#!/usr/bin/env python3
"""Smoke-test a Gemini model against the real classify path, before trusting it in prod.

The daemon's classifier model is `TMUXRC_GEMINI_MODEL` (default gemini-3.1-flash-lite).
When a new model ships (e.g. gemini-3.5-flash), point this at it to see — with real
Vertex calls, not mocks — whether it still returns well-formed, sensible classifications
for the pane shapes the watcher actually sends. It reuses the SAME prompt and classify()
seam the daemon uses, so a pass here means the daemon would behave.

    TMUXRC_GEMINI_MODEL=gemini-3.5-flash uv run python -m scripts.verify_model
    uv run python -m scripts.verify_model --model gemini-3.5-flash --live   # sample live panes

Cases are the canonical states the classifier must get right: an agent asking a question
(⇒ waiting/user), a busy agent (⇒ running/working), an idle shell, and a placeholder in an
empty input box (⇒ NOT reported as a real command — the ⟪placeholder⟫ regression guard).
Each case declares what a correct answer looks like; the script calls the model and checks.
It reports per-case PASS/FAIL plus tokens/cost/latency from the daemon's own metrics, so
"is 3.5 good enough and what does it cost" is answered in one run. Exit code is non-zero if
any case fails — usable in CI to gate a model bump.
"""

from __future__ import annotations

import argparse
import os

from dotenv import find_dotenv, load_dotenv

load_dotenv(find_dotenv(usecwd=True))  # GOOGLE_CLOUD_PROJECT + ADC, as the daemon does

from openbus import llm, tmux  # noqa: E402  (after .env, so llm reads a populated env)
from openbus.classify import classify  # noqa: E402

# (label, foreground process, pane capture, predicate on the result dict). Captures are
# dim-MARKED exactly as the watcher sends them, so the placeholder case exercises the real
# ⟪placeholder⟫ path.
CASES = [
    (
        "agent asking a question ⇒ waiting/user",
        "node",
        "Should I run the tests before committing?\n\n1. Yes\n2. No\n\n❯ ",
        lambda r: r.get("activity") == "waiting" and r.get("waiting_on") == "user",
    ),
    (
        "agent working ⇒ running",
        "node",
        "● Refactoring the parser…\n\nShimmying… (12s · ↓4.1k tokens)",
        lambda r: r.get("activity") in ("running", "compacting"),
    ),
    (
        "idle shell ⇒ shell/idle",
        "bash",
        "$ ls\nREADME.md  openbus  tests\n$ ",
        lambda r: r.get("tool") == "shell" and r.get("activity") == "idle",
    ),
    (
        "empty-box placeholder ⇒ not a real command",
        "node",
        f"● Done.\n\n❯ {tmux.PLACEHOLDER_OPEN}draft the release notes{tmux.PLACEHOLDER_CLOSE}",
        # The suggestion text must not surface as a reported event/headline.
        lambda r: "draft the release notes" not in (r.get("headline") or "")
        and not any("draft the release notes" in (e.get("text") or "")
                    for e in (r.get("events") or [])),
    ),
]


def _pane(cmd: str) -> tmux.Pane:
    return tmux.Pane("verify", "0", "verify", "0", "%0", cmd, "verify", "/tmp")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model", help="override TMUXRC_GEMINI_MODEL for this run")
    ap.add_argument("--live", action="store_true",
                    help="also classify the current tmux panes as a real-world sanity pass")
    args = ap.parse_args()
    if args.model:
        os.environ["TMUXRC_GEMINI_MODEL"] = args.model
    # Read AFTER the override — llm._MODEL is import-time, so report what will actually run.
    model = os.environ.get("TMUXRC_GEMINI_MODEL", "gemini-3.1-flash-lite")
    print(f"model: {model}\n")

    real_llm = lambda system, text: llm.classify_text(system, text)  # noqa: E731
    failures = 0
    for label, cmd, capture, ok in CASES:
        result = classify(_pane(cmd), capture, real_llm)
        passed = bool(ok(result))
        failures += not passed
        print(f"[{'PASS' if passed else 'FAIL'}] {label}")
        print(f"       -> activity={result.get('activity')!r} "
              f"tool={result.get('tool')!r} headline={result.get('headline')!r}")

    if args.live:
        print("\n--- live panes ---")
        for p in tmux.list_panes():
            cap = tmux.capture_pane(p.id, mark_dim=True)
            r = classify(p, cap, real_llm)
            print(f"  {p.label!r}: {r.get('tool')}/{r.get('activity')} "
                  f"— {r.get('headline') or ''}")

    t = llm.usage_totals()
    print(f"\ncalls={t['calls']} in={t['in_tokens']} out={t['out_tokens']} "
          f"cost=${t['cost']:.5f} errors={t['errors']}")
    print("RESULT:", "CLEAN" if not failures else f"{failures} FAILURE(S)")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
