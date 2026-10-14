"""Worker evals must catch inflated active counts and lost waiting/idle workers."""
from research.eval.harness import score_structured


def test_worker_expectations_are_opt_in_and_keep_multiplicity():
    candidate = {"agents": 1, "subagents": [{"state": "running"}, {"state": "waiting"}]}
    assert score_structured(candidate, {})[0]
    assert score_structured(candidate, {"agents": 1,
                                       "subagent_states": ["waiting", "running"]})[0]
    assert not score_structured(candidate, {"agents": 2})[0]
    assert not score_structured(candidate, {"subagent_states": ["running", "running"]})[0]
    assert not score_structured({}, {"subagent_states": ["waiting"]})[0]


def test_shared_worker_model_accepts_every_observed_state():
    from openbus.models import SubAgent

    for state in ("running", "waiting", "idle", "compacting", "unknown", "done"):
        assert SubAgent(label="Worker", state=state).state == state
    assert SubAgent(label="Worker").state == "unknown"


def test_subagents_include_pins_a_workflow_row():
    workflow = {"label": "docs-sweep", "state": "running", "progress": "1/2", "elapsed": "3m37s"}
    candidate = {"subagents": [{"label": "Fix chart", "state": "running"}, workflow, "junk"]}
    def include(**want):
        return score_structured(candidate, {"subagents_include": [want]})[0]
    assert include(label="docs-sweep", progress="1/2")
    # The bar as a label, or a lost done/total, is the mangling this guards against.
    assert not include(label="docs-sweep", progress="2/2")
    assert not score_structured({}, {"subagents_include": [{"label": "docs-sweep"}]})[0]
