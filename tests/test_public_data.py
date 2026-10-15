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

# A login, an address outside the example domains, or a credential shape.
IDENTITY = (r"shapor|/Users/"
            r"|[\w.+-]+@(?!(?:[\w-]+\.)*example(?:-org)?\.(?:com|net|org)(?![\w-]|\.\w))[\w-]+(?:\.[\w-]+)*\.[a-z]{2,}\b|sk-[A-Za-z0-9]|ghp_|gho_|github_pat_"
            r"|AKIA[0-9A-Z]{8}|AIza[0-9A-Za-z_-]{10}|xox[bp]-|BEGIN [A-Z ]*PRIVATE KEY")
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


@pytest.mark.parametrize(("text", "private"), [
    ("user@example.com.", False), ("dev@ci.example-org.com", False), ("x@4.149.0", False),
    ("x@example.com.other.invalid", True), ("x@example.com-mail.invalid", True),
    ("x@examplebusiness.invalid", True), ("x@1mail.invalid", True)])
def test_only_example_addresses_pass(text, private):
    assert bool(PRIVATE.search(text)) is private


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.name)
def test_fixture_names_no_one(path):
    assert not PRIVATE.findall(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("path", SAMPLES, ids=lambda p: p.stem)
def test_sample_is_invented_and_short(path):
    sample = json.loads(path.read_text(encoding="utf-8"))
    assert not PROVENANCE.search(f"{sample.get('title', '')} {sample['description']}")
    frames = [sample["capture"], *sample.get("prior", ())]  # every frame the classifier reads
    assert max(map(len, frames)) <= CAPTURE_CAP or path.stem in OVERSIZED
