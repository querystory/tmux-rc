from pathlib import Path

from openbus.classify import compose_prompt, parser_prompt


def test_composed_prompt_is_byte_identical_to_main_before_split():
    # Captured verbatim from main cd9aaf9; includes order, blank lines, and examples.
    baseline = Path(__file__).parents[1] / "research/eval/prompts/parser_main_cd9aaf9.txt"
    assert parser_prompt().encode("utf-8") == baseline.read_bytes()


def test_composition_preserves_plain_candidate_bytes():
    assert compose_prompt(lambda _: "\nplain candidate\n\n") == "\nplain candidate\n\n"


def test_composition_reads_fragments_from_selected_source():
    files = {"parser_prompt.txt": "before\n{{codex}}\nafter\n",
             "parser_codex.txt": "candidate-specific\n\n"}
    assert compose_prompt(files.__getitem__) == "before\ncandidate-specific\n\nafter\n"
