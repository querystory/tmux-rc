"""Closing-question reply buttons: measure where they should come from.

    python -m research.eval.closing_replies run --tag B --out /tmp/x.json [--runs 5]
    python -m research.eval.closing_replies report /tmp/a.json /tmp/b.json /tmp/c.json

`run` pushes every screen of `closing_questions/` (plus, with --existing, the regular
corpus) through the production `classify()` N times and records, per run, the post-override
result, tokens and latency of every model call, a rubric verdict on the buttons, and
whether the parse proposed buttons for a screen that has no closing question. What it
measures depends on the tree it runs in: a tree whose `classify` still takes `replies_fn`
is the separate-call design (pass --no-replies for the no-buttons baseline); one without
is the inline design. See README "Inline replies experiment" for the results.
"""

from __future__ import annotations

import argparse
import copy
import functools
import inspect
import json
import re
import statistics as st
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import openbus.classify as C
from openbus import llm
from openbus.tmux import Pane

from .harness import SAMPLES_DIR, Sample, score_structured

CLOSING_DIR = Path(__file__).parent / "closing_questions"
_ACCEPT = re.compile(
    r"(yes|yeah|yep|sure|ok|okay|go ahead|do it|proceed|please|push|merge)\b", re.IGNORECASE)
_DECLINE = re.compile(
    r"(no|not|don't|do not|hold|wait|skip|stop|cancel|leave|never|later)\b", re.IGNORECASE)
_JUDGE = (
    "You grade reply buttons that a phone shows under a coding agent's closing question. "
    "Given the QUESTION and the OPTIONS, rule PASS only if ALL hold: the first option accepts "
    "and a decline comes second where the question is yes/no; any 'Yes, but check with me "
    "before X' variant names only a step the question itself names; no option invents a step "
    "or topic the question does not mention; every option is a short, natural, complete reply "
    "a user could type. If the question is open-ended (what/which/how/where, a choice between "
    "alternatives, a request for content) the correct OPTIONS list is empty, so any option "
    'FAILS. Reply JSON only: {"verdict":"PASS"|"FAIL","reason":"<short>"}.'
)
_tl = threading.local()


@functools.cache
def _client():
    """One live client for the whole run: a client dropped after each call is closed by GC."""
    return llm._client()


def _call(model: str, system: str, text: str, kind: str) -> dict | None:
    """Production Vertex call settings (temperature 0, JSON mime), recording usage."""
    from google.genai import types

    t0 = time.time()
    resp = _client().models.generate_content(
        model=model, contents=[text],
        config=types.GenerateContentConfig(
            system_instruction=system, response_mime_type="application/json", temperature=0.0),
    )
    u = resp.usage_metadata
    try:
        out = llm._parse_json(resp.text)
    except (json.JSONDecodeError, ValueError):
        out = None
    if kind != "judge":
        _tl.calls.append({
            "kind": kind, "s": time.time() - t0, "in": u.prompt_token_count or 0,
            "out": u.candidates_token_count or 0, "think": u.thoughts_token_count or 0,
            "json": copy.deepcopy(out),  # what the model said, before classify() overrides it
        })
    return out


def _grade(kind: str, steps: list[str], result: dict) -> dict:
    """Deterministic rubric: what the buttons must look like for this kind of screen."""
    q = result.get("question")
    opts = q.get("options", []) if isinstance(q, dict) else []
    waiting = bool(q)
    if kind == "idle":
        ok = result.get("activity") == "idle" and not q
    elif kind == "none":
        ok = waiting and not opts
    elif kind == "error":
        ok = waiting and opts == ["try again"]
    else:
        checks = [o for o in opts if re.search(r"\bcheck\b", o, re.IGNORECASE)]
        ok = (waiting and 2 <= len(opts) <= 4 and bool(_ACCEPT.match(opts[0]))
              and bool(_DECLINE.match(opts[1])))
        if kind == "gated":
            ok = ok and bool(checks) and all(
                any(s in o.lower() for s in steps) for o in checks)
        else:
            ok = ok and not checks
    return {"ok": ok, "options": opts}


def _run_one(s: Sample, model: str, replies: bool, takes_replies_fn: bool) -> dict:
    _tl.calls = []
    pane = Pane(session="eval", window_index="0", window_name="eval", pane_index="0",
                id="%0", current_command=s.current_command, title=s.name)
    kw = {"replies_fn": (lambda sy, t: _call(model, sy, t, "replies")) if replies else None}
    result = classify_fn(pane, s.capture, llm_fn=lambda sy, t: _call(model, sy, t, "parse"),
                         repository=s.repository, **(kw if takes_replies_fn else {}))
    parses = [c["json"] for c in _tl.calls if c["kind"] == "parse" and isinstance(c["json"], dict)]
    return {"result": result, "calls": _tl.calls,
            "raw_closing_replies": next((p["closing_replies"] for p in parses
                                         if p.get("closing_replies")), None),
            "raw_question": next((p["question"] for p in parses if p.get("question")), None)}


classify_fn = C.classify


def run(args: argparse.Namespace) -> None:
    takes = "replies_fn" in inspect.signature(C.classify).parameters
    samples = [Sample.load(p) for p in sorted(CLOSING_DIR.glob("*.json"))]
    meta = {s.name: json.loads((CLOSING_DIR / f"{s.name}.json").read_text()).get(
        "closing_replies", {}) for s in samples}
    existing = [Sample.load(p) for p in sorted(SAMPLES_DIR.glob("*.json"))] if args.existing else []
    rows = []
    _client()  # build before the threads race to
    with ThreadPoolExecutor(args.workers) as pool:
        for i in range(args.runs):
            if hasattr(C, "_replies"):
                C._replies.clear()  # one call per question per RUN, not per process
            jobs = [(s, "closing") for s in samples] + [(s, "existing") for s in existing]
            outs = pool.map(lambda j: _run_one(j[0], args.model, not args.no_replies, takes), jobs)
            for (s, corpus), o in zip(jobs, outs, strict=True):
                row = {"run": i, "sample": s.name, "corpus": corpus, **o}
                if corpus == "closing":
                    row["grade"] = _grade(meta[s.name]["kind"], meta[s.name]["steps"], o["result"])
                    row["kind"] = meta[s.name]["kind"]
                else:
                    row["struct_ok"] = score_structured(o["result"], s.expected)[0]
                row["asked"] = bool(o["result"].get("question")) and corpus == "closing"
                rows.append(row)
            print(f"run {i + 1}/{args.runs} done", flush=True)
        # Quality rubric by the judge model, once per non-idle closing result.
        def judge(row):
            if row["corpus"] != "closing" or row["kind"] == "idle":
                return None
            q = row["result"].get("question") or {}
            payload = json.dumps({"question": q.get("prompt"), "options": q.get("options", [])})
            v = _call(args.model, _JUDGE, payload, "judge")
            return v if isinstance(v, dict) else {"verdict": "FAIL", "reason": "no verdict"}
        for row, v in zip(rows, pool.map(judge, rows), strict=True):
            row["judge"] = v
    Path(args.out).write_text(json.dumps({"tag": args.tag, "rows": rows}, ensure_ascii=False))


def report(args: argparse.Namespace) -> None:
    for path in args.files:
        d = json.loads(Path(path).read_text())
        rows = d["rows"]
        closing = [r for r in rows if r["corpus"] == "closing"]
        print(f"\n== {d['tag']} ({len(closing)} closing runs) ==")
        for kind in ("yn", "gated", "none", "idle", "error"):
            k = [r for r in closing if r["kind"] == kind]
            judged = [r for r in k if r.get("judge")]
            ok = sum(r["grade"]["ok"] for r in k)
            jp = sum(r["judge"]["verdict"] == "PASS" for r in judged)
            print(f"  {kind:6} rubric {ok:3}/{len(k):3}   judge PASS {jp:3}/{len(judged):3}")
        idle = [r for r in closing if r["kind"] == "idle"]
        fp = [r for r in rows if r["raw_closing_replies"] and not r["asked"]]
        print(f"  idle stayed idle {sum(r['grade']['ok'] for r in idle)}/{len(idle)};"
              f" parses with closing_replies but no closing question: {len(fp)}/{len(rows)}")
        # First parse only: classify() re-reads a slice after a rejected session/question,
        # and that retry is the same cost under every design.
        parses = [next(c for c in r["calls"] if c["kind"] == "parse") for r in rows if r["calls"]]
        retries = sum(sum(c["kind"] == "parse" for c in r["calls"]) > 1 for r in rows)
        extra = [c for r in rows for c in r["calls"] if c["kind"] == "replies"]
        if parses:
            m = {k: st.mean(c[k] for c in parses) for k in ("in", "out", "think", "s")}
            print(f"  parse: in {m['in']:.0f} tok, out {m['out']:.1f}, think {m['think']:.1f}, "
                  f"lat {m['s']:.2f}s (median {st.median(c['s'] for c in parses):.2f}s) "
                  f"n={len(parses)}, retried {retries}")
        if extra:
            m = {k: st.mean(c[k] for c in extra) for k in ("in", "out", "s")}
            print(f"  replies call: {len(extra)} calls, in {m['in']:.0f}, out {m['out']:.1f}, "
                  f"lat {m['s']:.2f}s")
        ex = [r for r in rows if r["corpus"] == "existing"]
        if ex:
            print(f"  existing corpus structured ok: {sum(r['struct_ok'] for r in ex)}/{len(ex)};"
                  f" failing: {sorted({r['sample'] for r in ex if not r['struct_ok']})}")
        # Run-to-run variance: cases whose rubric verdict differed across runs.
        by = {}
        for r in closing:
            by.setdefault(r["sample"], []).append(r["grade"]["ok"])
        flaky = sorted(n for n, v in by.items() if len(set(v)) > 1)
        print(f"  unstable cases (verdict flips across runs): {len(flaky)} {flaky}")
        print(f"  always failing: {sorted(n for n, v in by.items() if not any(v))}")


def main() -> None:
    ap = argparse.ArgumentParser(prog="python -m research.eval.closing_replies")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--tag", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--runs", type=int, default=5)
    r.add_argument("--workers", type=int, default=8)
    r.add_argument("--model", default=llm._MODEL)
    r.add_argument("--no-replies", action="store_true", help="separate-call tree: no buttons")
    r.add_argument("--existing", action="store_true", help="also run the regular corpus")
    r.set_defaults(fn=run)
    p = sub.add_parser("report")
    p.add_argument("files", nargs="+")
    p.set_defaults(fn=report)
    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
