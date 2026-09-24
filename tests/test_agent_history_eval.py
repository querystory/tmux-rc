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
