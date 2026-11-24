# Review instructions

The LLM reads the pane (see AGENTS.md#parser). In `openbus/`, flag as a blocking issue:

- any added regex, string match or keyword check on terminal or UI text (spinner lines,
  banners, agent status messages, model names, prompts);
- any code that reclassifies the model's judgement, such as turning options into offers,
  dropping questions or overriding activity or waiting state.

The fix for a parser miss is a minimal, positive change to `openbus/parser_prompt.txt`
proven on the eval, not code. Raising a count in `tests/test_regex_ratchet.py` needs the
same scrutiny.
