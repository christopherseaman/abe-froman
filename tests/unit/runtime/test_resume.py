"""Tests for resume/start-from state preparation logic."""

import pytest

from abe_froman.runtime.resume import (
    _upstream_phases,
    prepare_resume_state,
    prepare_start_state,
)

from helpers import make_config


def _saved_envelope(state, config_name="Test"):
    """Wrap a state dict in the persistence envelope format."""
    return {
        "version": 1,
        "config_name": config_name,
        "config_version": "1.0.0",
        "saved_at": "2026-01-01T00:00:00Z",
        "state": state,
    }


class TestPrepareResumeState:
    def test_completed_phases_preserved(self):
        config = make_config([
            {"id": "a", "name": "A", "prompt_file": "t.md"},
            {"id": "b", "name": "B", "prompt_file": "t.md", "depends_on": ["a"]},
        ])
        saved = _saved_envelope({
            "completed_phases": ["a"],
            "failed_phases": ["b"],
            "phase_outputs": {"a": "a-out"},
            "phase_structured_outputs": {},
            "gate_scores": {},
            "retries": {"b": 2},
            "subphase_outputs": {},
            "errors": [{"phase": "b", "error": "boom"}],
            "workdir": ".",
            "dry_run": False,
        })
        state = prepare_resume_state(saved, config, "/work")
        assert state["completed_phases"] == ["a"]
        assert state["failed_phases"] == []
        assert state["errors"] == []
        assert state["retries"] == {}
        assert state["phase_outputs"] == {"a": "a-out"}
        assert state["workdir"] == "/work"

    def test_failed_phases_cleared(self):
        config = make_config([
            {"id": "a", "name": "A", "prompt_file": "t.md"},
        ])
        saved = _saved_envelope({
            "completed_phases": [],
            "failed_phases": ["a"],
            "phase_outputs": {},
            "phase_structured_outputs": {},
            "gate_scores": {},
            "retries": {},
            "subphase_outputs": {},
            "errors": [{"phase": "a", "error": "fail"}],
            "workdir": ".",
            "dry_run": False,
        })
        state = prepare_resume_state(saved, config, ".")
        assert state["failed_phases"] == []
        assert state["errors"] == []
        assert "a" not in state["completed_phases"]

    def test_gate_scores_preserved_for_completed(self):
        config = make_config([
            {"id": "a", "name": "A", "prompt_file": "t.md"},
            {"id": "b", "name": "B", "prompt_file": "t.md", "depends_on": ["a"]},
        ])
        saved = _saved_envelope({
            "completed_phases": ["a"],
            "failed_phases": ["b"],
            "phase_outputs": {"a": "out"},
            "phase_structured_outputs": {},
            "gate_scores": {"a": 0.95},
            "retries": {},
            "subphase_outputs": {},
            "errors": [],
            "workdir": ".",
            "dry_run": False,
        })
        state = prepare_resume_state(saved, config, ".")
        assert state["gate_scores"] == {"a": 0.95}

    def test_config_name_mismatch_raises(self):
        config = make_config([{"id": "a", "name": "A", "prompt_file": "t.md"}])
        saved = _saved_envelope({
            "completed_phases": [],
            "failed_phases": [],
            "phase_outputs": {},
            "phase_structured_outputs": {},
            "gate_scores": {},
            "retries": {},
            "subphase_outputs": {},
            "errors": [],
            "workdir": ".",
            "dry_run": False,
        }, config_name="OtherWorkflow")
        with pytest.raises(ValueError, match="OtherWorkflow"):
            prepare_resume_state(saved, config, ".")


class TestPrepareStartState:
    def test_upstream_phases_cached(self):
        config = make_config([
            {"id": "a", "name": "A", "prompt_file": "t.md"},
            {"id": "b", "name": "B", "prompt_file": "t.md", "depends_on": ["a"]},
            {"id": "c", "name": "C", "prompt_file": "t.md", "depends_on": ["b"]},
        ])
        saved = _saved_envelope({
            "completed_phases": ["a", "b", "c"],
            "failed_phases": [],
            "phase_outputs": {"a": "a-out", "b": "b-out", "c": "c-out"},
            "phase_structured_outputs": {},
            "gate_scores": {},
            "retries": {},
            "subphase_outputs": {},
            "errors": [],
            "workdir": ".",
            "dry_run": False,
        })
        state = prepare_start_state(saved, config, "b", ".")
        assert state["completed_phases"] == ["a"]
        assert state["phase_outputs"] == {"a": "a-out"}
        assert "b" not in state["completed_phases"]

    def test_missing_phase_id_raises(self):
        config = make_config([{"id": "a", "name": "A", "prompt_file": "t.md"}])
        saved = _saved_envelope({
            "completed_phases": ["a"],
            "failed_phases": [],
            "phase_outputs": {},
            "phase_structured_outputs": {},
            "gate_scores": {},
            "retries": {},
            "subphase_outputs": {},
            "errors": [],
            "workdir": ".",
            "dry_run": False,
        })
        with pytest.raises(ValueError, match="not found"):
            prepare_start_state(saved, config, "nonexistent", ".")

    def test_missing_upstream_output_raises(self):
        config = make_config([
            {"id": "a", "name": "A", "prompt_file": "t.md"},
            {"id": "b", "name": "B", "prompt_file": "t.md", "depends_on": ["a"]},
        ])
        saved = _saved_envelope({
            "completed_phases": [],
            "failed_phases": ["a"],
            "phase_outputs": {},
            "phase_structured_outputs": {},
            "gate_scores": {},
            "retries": {},
            "subphase_outputs": {},
            "errors": [],
            "workdir": ".",
            "dry_run": False,
        })
        with pytest.raises(ValueError, match="not completed"):
            prepare_start_state(saved, config, "b", ".")

    def test_diamond_upstream_correct(self):
        config = make_config([
            {"id": "a", "name": "A", "prompt_file": "t.md"},
            {"id": "b", "name": "B", "prompt_file": "t.md", "depends_on": ["a"]},
            {"id": "c", "name": "C", "prompt_file": "t.md", "depends_on": ["a"]},
            {"id": "d", "name": "D", "prompt_file": "t.md", "depends_on": ["b", "c"]},
        ])
        saved = _saved_envelope({
            "completed_phases": ["a", "b", "c", "d"],
            "failed_phases": [],
            "phase_outputs": {"a": "1", "b": "2", "c": "3", "d": "4"},
            "phase_structured_outputs": {},
            "gate_scores": {},
            "retries": {},
            "subphase_outputs": {},
            "errors": [],
            "workdir": ".",
            "dry_run": False,
        })
        state = prepare_start_state(saved, config, "d", ".")
        assert set(state["completed_phases"]) == {"a", "b", "c"}
        assert set(state["phase_outputs"].keys()) == {"a", "b", "c"}


class TestUpstreamPhases:
    def test_linear_chain(self):
        config = make_config([
            {"id": "a", "name": "A", "prompt_file": "t.md"},
            {"id": "b", "name": "B", "prompt_file": "t.md", "depends_on": ["a"]},
            {"id": "c", "name": "C", "prompt_file": "t.md", "depends_on": ["b"]},
        ])
        assert _upstream_phases(config, "c") == {"a", "b"}

    def test_root_has_no_upstream(self):
        config = make_config([
            {"id": "a", "name": "A", "prompt_file": "t.md"},
            {"id": "b", "name": "B", "prompt_file": "t.md", "depends_on": ["a"]},
        ])
        assert _upstream_phases(config, "a") == set()

    def test_diamond(self):
        config = make_config([
            {"id": "a", "name": "A", "prompt_file": "t.md"},
            {"id": "b", "name": "B", "prompt_file": "t.md", "depends_on": ["a"]},
            {"id": "c", "name": "C", "prompt_file": "t.md", "depends_on": ["a"]},
            {"id": "d", "name": "D", "prompt_file": "t.md", "depends_on": ["b", "c"]},
        ])
        assert _upstream_phases(config, "d") == {"a", "b", "c"}
