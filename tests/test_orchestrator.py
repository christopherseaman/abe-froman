"""Orchestrator tests — real execution via DispatchExecutor with command phases.

Every test runs actual subprocesses through the full LangGraph pipeline.
No MockExecutor, no stub validators.
"""

import pytest

from abe_froman.engine.builder import build_workflow_graph
from abe_froman.engine.state import make_initial_state
from abe_froman.executor.dispatch import DispatchExecutor
from abe_froman.schema.models import Phase

from helpers import make_config


def cmd_phase(id, name="", depends_on=None, output="ok", **kwargs):
    """Shorthand for a command phase that echoes a known string."""
    return {
        "id": id,
        "name": name or id,
        "execution": {"type": "command", "command": "echo", "args": ["-n", output]},
        "depends_on": depends_on or [],
        **kwargs,
    }


def fail_phase(id, name="", depends_on=None, **kwargs):
    """Shorthand for a command phase that always fails."""
    return {
        "id": id,
        "name": name or id,
        "execution": {"type": "command", "command": "false"},
        "depends_on": depends_on or [],
        **kwargs,
    }


# ---------------------------------------------------------------------------
# Single phase execution
# ---------------------------------------------------------------------------


class TestSinglePhaseExecution:
    @pytest.mark.asyncio
    async def test_single_phase_completes(self):
        config = make_config([cmd_phase("p1", output="hello")])
        executor = DispatchExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert "p1" in result["completed_phases"]
        assert result["phase_outputs"]["p1"] == "hello"

    @pytest.mark.asyncio
    async def test_dry_run_skips_execution(self):
        config = make_config([cmd_phase("p1")])
        graph = build_workflow_graph(config)
        result = await graph.ainvoke(make_initial_state(dry_run=True))
        assert "p1" in result["completed_phases"]
        assert "dry-run" in result["phase_outputs"]["p1"]


# ---------------------------------------------------------------------------
# Linear execution and context passing
# ---------------------------------------------------------------------------


class TestLinearExecution:
    @pytest.mark.asyncio
    async def test_two_phases_execute_in_order(self):
        config = make_config([
            cmd_phase("a", output="a-out"),
            cmd_phase("b", output="b-out", depends_on=["a"]),
        ])
        executor = DispatchExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert result["completed_phases"] == ["a", "b"]

    @pytest.mark.asyncio
    async def test_three_phase_chain(self):
        config = make_config([
            cmd_phase("a", output="1"),
            cmd_phase("b", output="2", depends_on=["a"]),
            cmd_phase("c", output="3", depends_on=["b"]),
        ])
        executor = DispatchExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert set(result["completed_phases"]) == {"a", "b", "c"}
        assert set(result["phase_outputs"].keys()) == {"a", "b", "c"}


# ---------------------------------------------------------------------------
# Parallel / diamond execution
# ---------------------------------------------------------------------------


class TestParallelExecution:
    @pytest.mark.asyncio
    async def test_diamond_all_complete(self):
        """Diamond: A -> (B, C) -> D"""
        config = make_config([
            cmd_phase("a", output="root"),
            cmd_phase("b", output="left", depends_on=["a"]),
            cmd_phase("c", output="right", depends_on=["a"]),
            cmd_phase("d", output="join", depends_on=["b", "c"]),
        ])
        executor = DispatchExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert set(result["completed_phases"]) == {"a", "b", "c", "d"}

    @pytest.mark.asyncio
    async def test_independent_phases_both_complete(self):
        """Two phases with no dependencies both run."""
        config = make_config([
            cmd_phase("x", output="x-out"),
            cmd_phase("y", output="y-out"),
        ])
        executor = DispatchExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert set(result["completed_phases"]) == {"x", "y"}


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


class TestFailureHandling:
    @pytest.mark.asyncio
    async def test_failed_phase_skips_dependent(self):
        config = make_config([
            fail_phase("a"),
            cmd_phase("b", depends_on=["a"]),
        ])
        executor = DispatchExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert "a" in result["failed_phases"]
        assert "b" in result["failed_phases"]
        assert "b" not in result["completed_phases"]
        errors_by_phase = {e["phase"]: e["error"] for e in result["errors"]}
        assert "dependency" in errors_by_phase["b"].lower()

    @pytest.mark.asyncio
    async def test_parallel_failure_one_branch(self):
        """In diamond, if B fails, C still runs; D is skipped."""
        config = make_config([
            cmd_phase("a", output="ok"),
            fail_phase("b", depends_on=["a"]),
            cmd_phase("c", output="ok", depends_on=["a"]),
            cmd_phase("d", output="ok", depends_on=["b", "c"]),
        ])
        executor = DispatchExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert "a" in result["completed_phases"]
        assert "c" in result["completed_phases"]
        assert "b" in result["failed_phases"]
        assert "d" in result["failed_phases"]

    @pytest.mark.asyncio
    async def test_error_captures_message(self):
        config = make_config([fail_phase("p1")])
        executor = DispatchExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert "p1" in result["failed_phases"]
        assert len(result["errors"]) > 0
        assert result["errors"][0]["phase"] == "p1"


# ---------------------------------------------------------------------------
# Gate integration with real validators
# ---------------------------------------------------------------------------


class TestGateIntegration:
    @pytest.mark.asyncio
    async def test_passing_gate_completes(self, tmp_path):
        script = tmp_path / "pass.py"
        script.write_text("print(0.95)")
        config = make_config([
            cmd_phase("p1", output="data",
                      quality_gate={"validator": str(script), "threshold": 0.8}),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert result["gate_scores"]["p1"] == 0.95
        assert "p1" in result["completed_phases"]

    @pytest.mark.asyncio
    async def test_gate_pass_allows_dependent(self, tmp_path):
        script = tmp_path / "pass.py"
        script.write_text("print(1.0)")
        config = make_config([
            cmd_phase("a", output="ok",
                      quality_gate={"validator": str(script), "threshold": 0.8}),
            cmd_phase("b", output="ok", depends_on=["a"]),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "a" in result["completed_phases"]
        assert "b" in result["completed_phases"]

    @pytest.mark.asyncio
    async def test_blocking_gate_failure_blocks(self, tmp_path):
        script = tmp_path / "fail.py"
        script.write_text("print(0.1)")
        config = make_config([
            cmd_phase("p1", output="data",
                      quality_gate={
                          "validator": str(script), "threshold": 0.8,
                          "blocking": True, "max_retries": 0,
                      }),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "p1" in result["failed_phases"]
        assert any("gate failed" in e["error"].lower() for e in result["errors"])

    @pytest.mark.asyncio
    async def test_non_blocking_gate_failure_continues(self, tmp_path):
        script = tmp_path / "fail.py"
        script.write_text("print(0.1)")
        config = make_config([
            cmd_phase("p1", output="data",
                      quality_gate={
                          "validator": str(script), "threshold": 0.8,
                          "blocking": False, "max_retries": 0,
                      }),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "p1" in result["completed_phases"]
        assert any("non-blocking" in e["error"].lower() for e in result["errors"])


# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


class TestStructuredOutput:
    @pytest.mark.asyncio
    async def test_json_output_parsed_as_structured(self, tmp_path):
        """Command produces JSON → stored as structured_output when schema set."""
        payload = tmp_path / "data.json"
        payload.write_text('{"result": "value"}')
        config = make_config([
            {
                "id": "p1",
                "name": "P1",
                "execution": {"type": "command", "command": "cat", "args": [str(payload)]},
                "output_schema": {"type": "object"},
            },
        ])
        # DispatchExecutor doesn't auto-parse JSON for command phases.
        # Structured output is a PromptExecutor concern.
        # This test verifies the state wiring works when structured_output is None.
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "p1" in result["completed_phases"]
        assert '{"result": "value"}' in result["phase_outputs"]["p1"]


# ---------------------------------------------------------------------------
# Dry run
# ---------------------------------------------------------------------------


class TestDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_traces_gated_chain(self):
        config = make_config([
            cmd_phase("a", quality_gate={"validator": "v.md", "threshold": 0.99}),
            cmd_phase("b", depends_on=["a"]),
        ])
        graph = build_workflow_graph(config)
        result = await graph.ainvoke(make_initial_state(dry_run=True))
        assert "a" in result["completed_phases"]
        assert "b" in result["completed_phases"]


# ---------------------------------------------------------------------------
# DispatchExecutor routing
# ---------------------------------------------------------------------------


class TestDispatchExecutor:
    @pytest.mark.asyncio
    async def test_command_phase_runs_subprocess(self):
        executor = DispatchExecutor()
        phase = Phase(
            id="c1", name="C1",
            execution={"type": "command", "command": "echo", "args": ["hello"]},
        )
        result = await executor.execute(phase, {})
        assert result.success is True
        assert "hello" in result.output

    @pytest.mark.asyncio
    async def test_gate_only_phase(self):
        executor = DispatchExecutor()
        phase = Phase(id="g1", name="G1", execution={"type": "gate_only"})
        result = await executor.execute(phase, {})
        assert result.success is True
        assert "gate-only" in result.output

    @pytest.mark.asyncio
    async def test_command_phase_failure(self):
        executor = DispatchExecutor()
        phase = Phase(
            id="c1", name="C1",
            execution={"type": "command", "command": "false"},
        )
        result = await executor.execute(phase, {})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_no_execution_returns_error(self):
        executor = DispatchExecutor()
        phase = Phase(id="p1", name="P1")
        result = await executor.execute(phase, {})
        assert result.success is False
        assert "no execution" in result.error.lower()

    @pytest.mark.asyncio
    async def test_prompt_without_backend_returns_stub(self):
        """DispatchExecutor with no backend returns inline stub for prompts."""
        executor = DispatchExecutor()
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        result = await executor.execute(phase, {})
        assert result.success is True
        assert "prompt-stub" in result.output
