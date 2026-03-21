"""Unit tests for _merge_dicts and make_initial_state in engine/state.py."""

from abe_froman.engine.state import _merge_dicts, make_initial_state


class TestMergeDicts:
    def test_merge_does_not_mutate_left(self):
        left = {"a": 1}
        _merge_dicts(left, {"a": 2})
        assert left == {"a": 1}

    def test_merge_overlapping_right_wins(self):
        assert _merge_dicts({"a": 1}, {"a": 2}) == {"a": 2}


class TestMakeInitialState:
    def test_default_state_has_all_keys(self):
        state = make_initial_state()
        expected_keys = {
            "workflow_name", "completed_phases",
            "failed_phases", "phase_outputs", "phase_structured_outputs",
            "gate_scores", "retries", "subphase_outputs", "token_usage",
            "errors", "workdir", "dry_run",
        }
        assert set(state.keys()) == expected_keys
        assert state["workflow_name"] == "Workflow"
        assert state["completed_phases"] == []
        assert state["failed_phases"] == []
        assert state["phase_outputs"] == {}
        assert state["token_usage"] == {}
        assert state["dry_run"] is False

    def test_token_usage_merges_across_phases(self):
        left = {"p1": {"input": 100, "output": 50}}
        right = {"p2": {"input": 200, "output": 75}}
        merged = _merge_dicts(left, right)
        assert merged == {
            "p1": {"input": 100, "output": 50},
            "p2": {"input": 200, "output": 75},
        }

    def test_mutable_default_isolation(self):
        """Mutating a returned list must not affect subsequent calls.

        Guards against shared mutable defaults — a real Python footgun
        that would silently corrupt LangGraph state across invocations.
        """
        first = make_initial_state()
        first["errors"].append({"phase": "p1", "error": "boom"})
        first["completed_phases"].append("p1")

        second = make_initial_state()
        assert second["errors"] == []
        assert second["completed_phases"] == []
