import tomllib
from pathlib import Path

from openbus.classify import compose_prompt


def test_composition_preserves_plain_candidate_bytes():
    assert compose_prompt(lambda _: "\nplain candidate\n\n") == "\nplain candidate\n\n"


def test_composition_reads_fragments_from_selected_source():
    files = {"parser_prompt.txt": "before\n{{codex}}\nafter\n",
             "parser_codex.txt": "candidate-specific\n\n"}
    assert compose_prompt(files.__getitem__) == "before\ncandidate-specific\n\nafter\n"


def test_every_prompt_ships_in_the_wheel():
    """Prompts are force-included by name, so a new fragment must be listed too."""
    root = Path(__file__).parents[1]
    wheel = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    shipped = wheel["tool"]["hatch"]["build"]["targets"]["wheel"]["force-include"]
    assert {f"openbus/{p.name}" for p in (root / "openbus").glob("*.txt")} <= set(shipped)
