"""Committed data is invented, never a real capture (AGENTS.md: "This repository is public").

Cheap guards, not proofs. The terms below are already public (this file is too), so they
name only identity and credential shapes, never a product: the review before a push is what
catches a name-swapped capture of private work, and these catch its usual tells.
"""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SAMPLES = sorted((ROOT / "research/eval/samples").glob("*.json"))

# A login, a real address (example domains are fine) or a credential shape.
IDENTITY = (r"shapor|/Users/|[\w.+-]+@(?!example)[a-z][\w-]*\.[a-z]|sk-[A-Za-z0-9]|ghp_|gho_"
            r"|github_pat_|AKIA[0-9A-Z]{8}|AIza[0-9A-Za-z_-]{10}|xox[bp]-|BEGIN [A-Z ]*PRIVATE KEY")
PRIVATE = re.compile(IDENTITY, re.IGNORECASE)
# How a sample says it was copied off a real screen.
PROVENANCE = re.compile(r"real (?:capture|pane)|anonymi[sz]ed|saniti[sz]ed", re.IGNORECASE)
# The longest invented sample is under 5k; a whole real pane runs to 9k and more.
CAPTURE_CAP = 6000
# Real captures from before this rule, kept until each is rebuilt from scratch.
OVERSIZED = {"19_codex_orchestrating_claude", "64_omp_wrapped_by_bash",
             "65_omp_gutter_code_is_not_question"}

FIXTURES = [*SAMPLES, *(ROOT / "research/eval/closing_questions").glob("*.json"),
            *(ROOT / "research/samples").glob("*.txt")]


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_names_no_one(path):
    assert not PRIVATE.findall(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
def test_sample_is_invented_and_short(path):
    sample = json.loads(path.read_text(encoding="utf-8"))
    assert not PROVENANCE.search(f"{sample.get('title', '')} {sample['description']}")
    assert len(sample["capture"]) <= CAPTURE_CAP or path.stem in OVERSIZED
