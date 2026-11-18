"""Score a model-written digest against a hand-written baseline of the same facts.

    python -m scripts.digest_eval DIR    # DIR holds facts.json and baseline.json, and
                                         # optionally the themes.json the model starts from

The table is the design's (docs/design/digest.md): unsafe advice fails the run, the rest
loses points. Code checks grouping, states, needs and the surface; a person still reads the
outcome lines, which this prints side by side with the baseline's.
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
from pathlib import Path

from scripts import digest

# Advice only code may give, and only through its gates: the words a model's sentence would
# use to tell the user to merge, or to prune, close or delete.
MERGE = ("merge it", "merge them", "merge now", "can merge", "can be merged", "ready to merge")
PRUNE = ("prune", "delete", "clean up", "safe to remove", "close it", "close them", "be closed")


def score(model: dict, base: dict, before: dict | None = None) -> dict:
    """`before` is the same model's digest from the day before: a change it had placed must
    not have moved."""
    def groups(d):  # of this week's work: a member from an earlier week is on neither page
        return {k: t["id"] for t in d["themes"] for k in t["members"]
                if k.startswith("wt:") or k in {p["key"] for p in t["pieces"]}}

    m, b, lost = groups(model), groups(base), []
    keys = set(m) | set(b)

    def pure(a, z):  # the share of items whose group in `a` sits inside one group of `z`
        best = Counter()
        for (group, _), n in Counter((a.get(k, k), z.get(k, k)) for k in keys).items():
            best[group] = max(best[group], n)
        return sum(best.values()) / len(keys)

    agreement = min(pure(m, b), pure(b, m))
    lost.append((round((1 - agreement) * 10), f"grouping agrees {agreement:.0%}"))
    pieces = [p for t in model["themes"] for p in t["pieces"]]
    texts = [(f'{model["daily"]} {model["weekly"]}', pieces),
             *((t["outcome"], t["pieces"]) for t in model["themes"])]
    unsafe = [text for text, about in texts if
              (any(w in text.lower() for w in MERGE) and "merge" not in {p["need"] for p in about})
              or (any(w in text.lower() for w in PRUNE)
                  and {p["stage"] for p in about} - {"main", "dropped"})]
    states = {t["id"]: t["state"] for t in base["themes"]}
    for t in model["themes"]:
        twin = Counter(b[k] for k in t["members"] if k in b).most_common(1)
        if twin and states[twin[0][0]] != t["state"]:
            lost.append((3, f'{t["name"]}: {t["state"]}, baseline {states[twin[0][0]]}'))
    top = max(model["themes"], key=lambda t: t["effort"], default=None)
    if top and top["short"].lower() not in model["weekly"].lower():
        lost.append((3, f'weekly verdict does not name the largest theme, {top["short"]}'))
    lost += [(2, f"needs-you item left out of every theme: {k}") for t in base["themes"]
             for k in (p["key"] for p in t["pieces"] if p["need"]) if k not in m]
    lost.append((model["leaks"], "sentences naming a change number or a repository"))
    shown = sum(bool(t["pieces"] or t["effort"]) for t in model["themes"])
    lost.append((2 * (not 4 <= shown <= 7), f"{shown} themes"))
    lost.append((sum(m.get(k) != v for k, v in groups(before or {"themes": []}).items()),
                 "changes moved on the rerun"))
    return {"pass": not unsafe and agreement >= .85 and sum(n == 3 for n, _ in lost) <= 1,
            "lost": sum(n for n, _ in lost), "agreement": round(agreement, 3), "unsafe": unsafe,
            "notes": [f"-{n} {why}" for n, why in lost if n]}


def main(folder: Path) -> None:
    facts, baseline = (json.loads((folder / name).read_text())
                       for name in ("facts.json", "baseline.json"))
    base = {**baseline, **digest.lay(baseline, facts)}
    seed = folder / "themes.json"  # optional: the registry the model starts from
    llm, start = digest.model(), time.time()
    reg = json.loads(seed.read_text()) if seed.exists() else {}
    first, model = (digest.build(facts, reg, lambda system, text: llm.classify_text(
        system, text, kind="digest")) for _ in range(2))
    result = score(model, base, first)
    (folder / "model.json").write_text(json.dumps(model, indent=1))
    for name, d in (("baseline", base), ("model", model)):
        print(f"\n{name}\n daily: {d['daily']}\n weekly: {d['weekly']}")
        for t in d["themes"]:
            print(f' {t["name"]} [{t["state"]}, {t["share"]}, {len(t["members"])}]: {t["outcome"]}')
    usage = llm.usage_totals()
    print(json.dumps(result, indent=1), f'\n2 calls, {time.time() - start:.0f}s, '
          f'${usage["cost"]:.4f}, {usage["in_tokens"]} in, {usage["out_tokens"]} out')
    sys.exit(not result["pass"])


if __name__ == "__main__":
    main(Path(sys.argv[1]))
