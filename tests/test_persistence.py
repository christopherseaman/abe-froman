"""Tests for state persistence — save/load/clear to disk."""

import json

import pytest

from abe_froman.workflow.persistence import (
    STATE_FILENAME,
    STATE_VERSION,
    clear_state,
    load_state,
    save_state,
    state_file_path,
)


class TestSaveAndLoad:
    def test_roundtrip(self, tmp_path):
        state = {"completed_phases": ["a"], "phase_outputs": {"a": "out"}}
        save_state(state, str(tmp_path), "MyFlow", "1.0")
        envelope = load_state(str(tmp_path))
        assert envelope["state"] == state

    def test_load_missing_returns_none(self, tmp_path):
        assert load_state(str(tmp_path)) is None

    def test_load_corrupt_raises(self, tmp_path):
        (tmp_path / STATE_FILENAME).write_text("not json{{{")
        with pytest.raises(ValueError, match="Corrupt"):
            load_state(str(tmp_path))

    def test_version_mismatch_raises(self, tmp_path):
        (tmp_path / STATE_FILENAME).write_text(
            json.dumps({"version": 999, "state": {}})
        )
        with pytest.raises(ValueError, match="version"):
            load_state(str(tmp_path))

    def test_envelope_contains_metadata(self, tmp_path):
        save_state({}, str(tmp_path), "Flow", "2.0")
        envelope = load_state(str(tmp_path))
        assert envelope["version"] == STATE_VERSION
        assert envelope["config_name"] == "Flow"
        assert envelope["config_version"] == "2.0"
        assert "saved_at" in envelope

    def test_atomic_write_no_tmp_leftover(self, tmp_path):
        save_state({}, str(tmp_path), "X", "1.0")
        assert not (tmp_path / f"{STATE_FILENAME}.tmp").exists()
        assert (tmp_path / STATE_FILENAME).exists()


class TestClearState:
    def test_clear_removes_file(self, tmp_path):
        save_state({}, str(tmp_path), "X", "1.0")
        clear_state(str(tmp_path))
        assert not (tmp_path / STATE_FILENAME).exists()

    def test_clear_missing_noop(self, tmp_path):
        clear_state(str(tmp_path))  # should not raise


class TestStateFilePath:
    def test_returns_expected_path(self, tmp_path):
        assert state_file_path(str(tmp_path)) == tmp_path / STATE_FILENAME
