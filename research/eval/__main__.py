"""CLI for the prompt-eval harness. Runs the corpus through the production classifier
prompt + model, scores each sample, prints a PASS/FAIL table, exits non-zero if any
sample fails (so it's CI-usable and A/B-usable).

    python -m research.eval                         # whole corpus, production prompt+model
    python -m research.eval --sample <name>         # one sample
    python -m research.eval --model gemini-3.5-flash-lite   # A/B a different model
    python -m research.eval --prompt path/to/candidate_prompt.txt  # A/B a prompt edit

Needs the three Vertex vars in the environment (GOOGLE_CLOUD_PROJECT,
VERTEX_AI_REGION_GEMINI, GOOGLE_APPLICATION_CREDENTIALS) — see research/eval/README.md.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from daemon import llm
from daemon.classify import parser_prompt

from .harness import evaluate, format_table, load_corpus


def _vertex_caller(model: str):
    """A `llm_fn(system, text) -> dict|None` bound to `model`, using the daemon's real
    Vertex client with production call settings (temperature 0, JSON mime) — the SAME
    path daemon.llm.classify_text uses, so results match production. Kept here (not in
    harness.py) so the harness core stays import-light and stub-testable."""
    from google.genai import types

    def call(system: str, text: str) -> dict | None:
        resp = llm._client().models.generate_content(
            model=model,
            contents=[text],
            config=types.GenerateContentConfig(
                system_instruction=system,
                response_mime_type="application/json",
                temperature=0.0,
            ),
        )
        try:
            return json.loads(resp.text)
        except json.JSONDecodeError:
            return None

    return call


def main() -> int:
    ap = argparse.ArgumentParser(prog="python -m research.eval")
    ap.add_argument("--model", default=llm._MODEL, help="model id to classify with (A/B)")
    ap.add_argument(
        "--prompt",
        type=Path,
        help="candidate parser prompt file (default: the production parser_prompt.txt)",
    )
    ap.add_argument("--sample", help="run only this sample (by name, no extension)")
    ap.add_argument(
        "--judge-model",
        default=llm._MODEL,
        help="model id for the free-text judge (default: same as --model's family)",
    )
    args = ap.parse_args()

    samples = load_corpus(args.sample)

    # The classifier call uses the candidate prompt if given, else the production one.
    # We monkeypatch the loader the classify() path reads, so the whole production
    # pipeline (payload assembly, overrides) runs against the candidate prompt with no
    # duplicated call logic — this is HOW a prompt edit gets regression-tested.
    if args.prompt:
        prompt_text = args.prompt.read_text(encoding="utf-8").strip()
        import daemon.classify as C

        C.parser_prompt = lambda: prompt_text  # type: ignore[assignment]
        print(f"prompt:  {args.prompt} (candidate)")
    else:
        print(f"prompt:  daemon/parser_prompt.txt (production, {len(parser_prompt())} chars)")
    print(f"model:   {args.model}")
    print(f"judge:   {args.judge_model}")
    print(f"samples: {len(samples)}\n")

    classify_llm = _vertex_caller(args.model)
    judge_llm = _vertex_caller(args.judge_model)
    results = [evaluate(s, classify_llm, judge_llm) for s in samples]

    print(format_table(results))
    return 0 if all(r.passed for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
