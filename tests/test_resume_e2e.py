"""End-to-end resume tests using real command phases.

Every test runs actual subprocesses through the full LangGraph pipeline,
verifying that resume/start-from correctly skips completed phases.
"""

import json

import pytest

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.workflow.persistence import (
    STATE_FILENAME,
    load_state,
    save_state,
)
from abe_froman.workflow.resume import prepare_resume_state, prepare_start_state
from abe_froman.workflow.runner import run_workflow
from abe_froman.runtime.state import make_initial_state
from abe_froman.runtime.executor.dispatch import DispatchExecutor

from helpers import cmd_phase, fail_phase, make_config


def counter_phase(id, counter_path, depends_on=None):
    """Phase that appends its ID to a counter file, proving it executed."""
    return {
        "id": id,
        "name": id,
        "execution": {
            "type": "command",
            "command": "sh",
            "args": ["-c", f"echo -n {id} >> {counter_path} && echo -n {id}-out"],
        },
        "depends_on": depends_on or [],
    }


class TestResumeFromFailure:
    @pytest.mark.asyncio
    async def test_resume_skips_completed_phases(self, tmp_path):
        """Run a->b(fail), then resume. Phase a should not re-execute."""
        counter = tmp_path / "exec_log.txt"
        counter.write_text("")

        # First run: a succeeds, b fails
        config = make_config([
            counter_phase("a", str(counter)),
            fail_phase("b", depends_on=["a"]),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        result = await run_workflow(
            build_workflow_graph(config, executor),
            make_initial_state(workdir=str(tmp_path)),
            config,
        )
        assert "a" in result["completed_phases"]
        assert "b" in result["failed_phases"]
        assert counter.read_text() == "a"

        # State file should be preserved (workflow failed)
        assert (tmp_path / STATE_FILENAME).exists()

        # Second run: replace b with a succeeding phase, resume
        config2 = make_config([
            counter_phase("a", str(counter)),
            counter_phase("b", str(counter), depends_on=["a"]),
        ])
        saved = load_state(str(tmp_path))
        state = prepare_resume_state(saved, config2, str(tmp_path))
        executor2 = DispatchExecutor(workdir=str(tmp_path))
        result2 = await run_workflow(
            build_workflow_graph(config2, executor2),
            state,
            config2,
        )

        assert "a" in result2["completed_phases"]
        assert "b" in result2["completed_phases"]
        # Phase a should NOT have re-executed (counter still has one 'a')
        assert counter.read_text() == "ab"

    @pytest.mark.asyncio
    async def test_resume_preserves_outputs_for_dependents(self, tmp_path):
        """Resumed phase b should receive phase a's output from saved state."""
        config = make_config([
            cmd_phase("a", output="hello-from-a"),
            fail_phase("b", depends_on=["a"]),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        result = await run_workflow(
            build_workflow_graph(config, executor),
            make_initial_state(workdir=str(tmp_path)),
            config,
        )
        assert result["phase_outputs"]["a"] == "hello-from-a"

        # Resume: b now uses 'cat' to echo what it gets via context
        # (command phases don't get context, but the output from a is in state)
        config2 = make_config([
            cmd_phase("a", output="hello-from-a"),
            cmd_phase("b", output="b-done", depends_on=["a"]),
        ])
        saved = load_state(str(tmp_path))
        state = prepare_resume_state(saved, config2, str(tmp_path))
        assert state["phase_outputs"]["a"] == "hello-from-a"

        executor2 = DispatchExecutor(workdir=str(tmp_path))
        result2 = await run_workflow(
            build_workflow_graph(config2, executor2),
            state,
            config2,
        )
        assert "b" in result2["completed_phases"]


class TestStartFromPhase:
    @pytest.mark.asyncio
    async def test_start_from_middle(self, tmp_path):
        """Start from b in a->b->c chain. Only b and c should execute."""
        counter = tmp_path / "exec_log.txt"
        counter.write_text("")

        # First: run all three to completion
        config = make_config([
            counter_phase("a", str(counter)),
            counter_phase("b", str(counter), depends_on=["a"]),
            counter_phase("c", str(counter), depends_on=["b"]),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        await run_workflow(
            build_workflow_graph(config, executor),
            make_initial_state(workdir=str(tmp_path)),
            config,
            persist=True,
        )
        # Force state file to exist (successful runs clear it)
        save_state(
            {"completed_phases": ["a", "b", "c"],
             "failed_phases": [], "errors": [],
             "phase_outputs": {"a": "a-out", "b": "b-out", "c": "c-out"},
             "phase_structured_outputs": {}, "gate_scores": {},
             "retries": {}, "subphase_outputs": {},
             "workdir": str(tmp_path), "dry_run": False},
            str(tmp_path), "Test", "1.0.0",
        )
        counter.write_text("")  # reset counter

        saved = load_state(str(tmp_path))
        state = prepare_start_state(saved, config, "b", str(tmp_path))
        executor2 = DispatchExecutor(workdir=str(tmp_path))
        result = await run_workflow(
            build_workflow_graph(config, executor2),
            state,
            config,
        )

        assert set(result["completed_phases"]) == {"a", "b", "c"}
        # Only b and c should have executed (a was cached)
        assert counter.read_text() == "bc"


class TestStateFileLifecycle:
    @pytest.mark.asyncio
    async def test_state_file_cleared_on_success(self, tmp_path):
        config = make_config([cmd_phase("a", output="ok")])
        executor = DispatchExecutor(workdir=str(tmp_path))
        await run_workflow(
            build_workflow_graph(config, executor),
            make_initial_state(workdir=str(tmp_path)),
            config,
        )
        assert not (tmp_path / STATE_FILENAME).exists()

    @pytest.mark.asyncio
    async def test_state_file_preserved_on_failure(self, tmp_path):
        config = make_config([fail_phase("a")])
        executor = DispatchExecutor(workdir=str(tmp_path))
        await run_workflow(
            build_workflow_graph(config, executor),
            make_initial_state(workdir=str(tmp_path)),
            config,
        )
        assert (tmp_path / STATE_FILENAME).exists()
        envelope = load_state(str(tmp_path))
        assert "a" in envelope["state"]["failed_phases"]

    @pytest.mark.asyncio
    async def test_no_state_file_during_dry_run(self, tmp_path):
        config = make_config([cmd_phase("a")])
        graph = build_workflow_graph(config)
        await run_workflow(
            graph,
            make_initial_state(workdir=str(tmp_path), dry_run=True),
            config,
            persist=False,
        )
        assert not (tmp_path / STATE_FILENAME).exists()
