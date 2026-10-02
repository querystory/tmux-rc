from pathlib import Path

from openbus.classify import compose_prompt, parser_prompt


def test_composed_prompt_changes_only_the_shared_rules():
    # Latest main, including #238; #243 adds only these shared rules and keeps fragments.
    baseline = Path(__file__).parents[1] / "research/eval/prompts/parser_main_27d81e1.txt"
    candidate = parser_prompt()
    for addition in (
        ("Below the control-delimited [visible screen] marker, derive current state and questions\n"
         "from it; text above the marker is history.\n"),
        "  A past-tense duration with a clock time marks a finished turn, not a live spinner.\n",
        ("An opaque ID, or a title quoted in tool output or another pane's listing, is not\n"
         "this pane's conversation name.\n\n"),
    ):
        assert candidate.count(addition) == 1
        candidate = candidate.replace(addition, "")
    # omp: its own fragment, plus its name in the tool lists.
    fragment = Path(__file__).parents[1] / "openbus/parser_omp.txt"
    candidate = candidate.replace(fragment.read_text(encoding="utf-8"), "", 1)
    for omp, without in (
        ('"opencode", "omp",', '"opencode",'),
        ('"opencode"|"omp"|', '"opencode"|'),
        ("node/bun/claude/codex/gemini/opencode/omp", "node/claude/codex/gemini/opencode"),
    ):
        assert candidate.count(omp) == 1
        candidate = candidate.replace(omp, without)
    candidate = candidate.replace(
        '"question.prompt" quotes the current visible question verbatim. Put the supporting\n'
        'command, list, or decision context in "tables" so the question stands alone on a phone.\n',
        '"question.prompt" must carry ENOUGH CONTEXT to stand alone on a phone — don\'t just echo\n'
        'a terse line like "Want me to tackle any of these?". Include what "these" refers to\n'
        '(e.g. "Which doc-update task should I start? (from the audit above)"). If the question\n'
        'is about a table/list shown on screen, put that table in "tables" so it renders as\n'
        'context above the options.\n',
    )
    assert candidate.encode("utf-8") == baseline.read_bytes()


def test_composition_preserves_plain_candidate_bytes():
    assert compose_prompt(lambda _: "\nplain candidate\n\n") == "\nplain candidate\n\n"


def test_composition_reads_fragments_from_selected_source():
    files = {"parser_prompt.txt": "before\n{{codex}}\nafter\n",
             "parser_codex.txt": "candidate-specific\n\n"}
    assert compose_prompt(files.__getitem__) == "before\ncandidate-specific\n\nafter\n"
