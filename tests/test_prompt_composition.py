from pathlib import Path

from openbus.classify import parser_prompt


def test_composed_prompt_is_byte_identical_to_main_before_split():
    # Captured verbatim from main cd9aaf9; includes order, blank lines, and examples.
    baseline = Path(__file__).parents[1] / "research/eval/prompts/parser_main_cd9aaf9.txt"
    assert parser_prompt().encode("utf-8") == baseline.read_bytes()
