import pytest

from abe_froman.engine.builder import build_workflow_graph
from abe_froman.engine.state import make_initial_state
from abe_froman.executor.base import PhaseExecutor, PhaseResult
from abe_froman.executor.mock import MockExecutor
from abe_froman.schema.models import Phase, WorkflowConfig

from helpers import make_config


class TestMockExecutorProtocol:
    def test_mock_implements_protocol(self):
        assert isinstance(MockExecutor(), PhaseExecutor)

    @pytest.mark.asyncio
    async def test_mock_default_success(self):
        executor = MockExecutor()
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        result = await executor.execute(phase, {})
        assert result.success is True
        assert "p1" in result.output

    @pytest.mark.asyncio
    async def test_mock_canned_result(self):
        executor = MockExecutor(
            results={"p1": PhaseResult(success=True, output="custom output")}
        )
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        result = await executor.execute(phase, {})
        assert result.output == "custom output"

    @pytest.mark.asyncio
    async def test_mock_tracks_execution_order(self):
        executor = MockExecutor()
        for pid in ["p1", "p2"]:
            await executor.execute(Phase(id=pid, name=pid, prompt_file="t.md"), {})
        assert executor.execution_order == ["p1", "p2"]

    @pytest.mark.asyncio
    async def test_mock_failure(self):
        executor = MockExecutor(
            results={"p1": PhaseResult(success=False, error="something broke")}
        )
        result = await executor.execute(
            Phase(id="p1", name="P1", prompt_file="t.md"), {}
        )
        assert result.success is False
        assert result.error == "something broke"


class TestSinglePhaseExecution:
    @pytest.mark.asyncio
    async def test_single_phase_completes(self):
        config = make_config([{"id": "p1", "name": "P1", "prompt_file": "t.md"}])
        executor = MockExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert "p1" in result["completed_phases"]
        assert "p1" in result["phase_outputs"]

    @pytest.mark.asyncio
    async def test_dry_run_skips_executor(self):
        config = make_config([{"id": "p1", "name": "P1", "prompt_file": "t.md"}])
        executor = MockExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(dry_run=True))
        assert "p1" in result["completed_phases"]
        assert executor.execution_order == []


class TestLinearExecution:
    @pytest.mark.asyncio
    async def test_two_phases_execute_in_order(self):
        config = make_config(
            [
                {"id": "a", "name": "A", "prompt_file": "a.md"},
                {"id": "b", "name": "B", "prompt_file": "b.md", "depends_on": ["a"]},
            ]
        )
        executor = MockExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert executor.execution_order == ["a", "b"]
        assert set(result["completed_phases"]) == {"a", "b"}

    @pytest.mark.asyncio
    async def test_context_passed_from_dependency(self):
        config = make_config(
            [
                {"id": "a", "name": "A", "prompt_file": "a.md"},
                {"id": "b", "name": "B", "prompt_file": "b.md", "depends_on": ["a"]},
            ]
        )
        executor = MockExecutor(
            results={"a": PhaseResult(success=True, output="phase-a-output")}
        )
        graph = build_workflow_graph(config, executor)
        await graph.ainvoke(make_initial_state())
        assert executor.received_contexts["b"]["a"] == "phase-a-output"


class TestParallelExecution:
    @pytest.mark.asyncio
    async def test_diamond_all_complete(self):
        """Diamond: A -> (B, C) -> D — all complete, ordering respected."""
        config = make_config(
            [
                {"id": "a", "name": "A", "prompt_file": "a.md"},
                {"id": "b", "name": "B", "prompt_file": "b.md", "depends_on": ["a"]},
                {"id": "c", "name": "C", "prompt_file": "c.md", "depends_on": ["a"]},
                {"id": "d", "name": "D", "prompt_file": "d.md", "depends_on": ["b", "c"]},
            ]
        )
        executor = MockExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert set(result["completed_phases"]) == {"a", "b", "c", "d"}
        order = executor.execution_order
        assert order.index("a") < order.index("b")
        assert order.index("a") < order.index("c")
        assert order.index("b") < order.index("d")
        assert order.index("c") < order.index("d")


class TestFailureHandling:
    @pytest.mark.asyncio
    async def test_failed_phase_skips_dependent(self):
        config = make_config(
            [
                {"id": "a", "name": "A", "prompt_file": "a.md"},
                {"id": "b", "name": "B", "prompt_file": "b.md", "depends_on": ["a"]},
            ]
        )
        executor = MockExecutor(
            results={"a": PhaseResult(success=False, error="crashed")}
        )
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert "a" in result["failed_phases"]
        assert "b" in result["failed_phases"]
        assert "b" not in result["completed_phases"]
        # Error for both: a's crash and b's skip
        errors_by_phase = {e["phase"]: e["error"] for e in result["errors"]}
        assert "crashed" in errors_by_phase["a"]
        assert "dependency" in errors_by_phase["b"].lower()

    @pytest.mark.asyncio
    async def test_parallel_failure_one_branch(self):
        """In diamond, if B fails, C still runs; D is skipped."""
        config = make_config(
            [
                {"id": "a", "name": "A", "prompt_file": "a.md"},
                {"id": "b", "name": "B", "prompt_file": "b.md", "depends_on": ["a"]},
                {"id": "c", "name": "C", "prompt_file": "c.md", "depends_on": ["a"]},
                {"id": "d", "name": "D", "prompt_file": "d.md", "depends_on": ["b", "c"]},
            ]
        )
        executor = MockExecutor(
            results={"b": PhaseResult(success=False, error="b failed")}
        )
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert "a" in result["completed_phases"]
        assert "c" in result["completed_phases"]
        assert "b" in result["failed_phases"]
        assert "d" in result["failed_phases"]

    @pytest.mark.asyncio
    async def test_error_captures_message(self):
        config = make_config([{"id": "p1", "name": "P1", "prompt_file": "t.md"}])
        error_msg = "detailed error: file not found"
        executor = MockExecutor(
            results={"p1": PhaseResult(success=False, error=error_msg)}
        )
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert any(error_msg in e["error"] for e in result["errors"])


class TestStructuredOutput:
    @pytest.mark.asyncio
    async def test_structured_output_stored(self):
        config = make_config(
            [
                {
                    "id": "p1",
                    "name": "P1",
                    "prompt_file": "t.md",
                    "output_schema": {"type": "object"},
                }
            ]
        )
        executor = MockExecutor(
            results={
                "p1": PhaseResult(
                    success=True,
                    output="text",
                    structured_output={"result": "data"},
                )
            }
        )
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert result["phase_structured_outputs"]["p1"] == {"result": "data"}

    @pytest.mark.asyncio
    async def test_structured_output_in_downstream_context(self):
        config = make_config(
            [
                {"id": "a", "name": "A", "prompt_file": "a.md"},
                {"id": "b", "name": "B", "prompt_file": "b.md", "depends_on": ["a"]},
            ]
        )
        executor = MockExecutor(
            results={
                "a": PhaseResult(
                    success=True,
                    output="text",
                    structured_output={"key": "value"},
                )
            }
        )
        graph = build_workflow_graph(config, executor)
        await graph.ainvoke(make_initial_state())
        assert executor.received_contexts["b"]["a_structured"] == {"key": "value"}


class TestGateIntegration:
    """Test that quality gates are evaluated and scores flow through state."""

    @pytest.mark.asyncio
    async def test_gate_score_populated(self):
        """After execution, gate_scores contains the evaluated score."""
        config = make_config(
            [
                {
                    "id": "p1",
                    "name": "P1",
                    "prompt_file": "t.md",
                    "quality_gate": {"validator": "v.md", "threshold": 0.8},
                }
            ]
        )
        executor = MockExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        # .md validators stub to 1.0
        assert result["gate_scores"]["p1"] == 1.0
        assert "p1" in result["completed_phases"]

    @pytest.mark.asyncio
    async def test_non_terminal_gate_passes_to_dependent(self):
        """Gate on non-terminal phase: if score passes, dependent runs."""
        config = make_config(
            [
                {
                    "id": "a",
                    "name": "A",
                    "prompt_file": "a.md",
                    "quality_gate": {"validator": "v.md", "threshold": 0.8},
                },
                {"id": "b", "name": "B", "prompt_file": "b.md", "depends_on": ["a"]},
            ]
        )
        executor = MockExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert "a" in result["completed_phases"]
        assert "b" in result["completed_phases"]

    @pytest.mark.asyncio
    async def test_gate_with_py_validator(self, tmp_path):
        """Gate with a real Python validator script."""
        script = tmp_path / "validator.py"
        script.write_text("print(0.95)")

        config = make_config(
            [
                {
                    "id": "p1",
                    "name": "P1",
                    "prompt_file": "t.md",
                    "quality_gate": {
                        "validator": str(script),
                        "threshold": 0.8,
                    },
                }
            ]
        )
        executor = MockExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert result["gate_scores"]["p1"] == 0.95
        assert "p1" in result["completed_phases"]

    @pytest.mark.asyncio
    async def test_gate_failure_below_threshold(self, tmp_path):
        """Gate that fails (score < threshold) with blocking=True marks phase failed."""
        script = tmp_path / "validator.py"
        script.write_text("print(0.1)")

        config = make_config(
            [
                {
                    "id": "p1",
                    "name": "P1",
                    "prompt_file": "t.md",
                    "quality_gate": {
                        "validator": str(script),
                        "threshold": 0.8,
                        "blocking": True,
                        "max_retries": 0,
                    },
                }
            ]
        )
        executor = MockExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "p1" in result["failed_phases"]
        assert any("gate failed" in e["error"].lower() for e in result["errors"])

    @pytest.mark.asyncio
    async def test_non_blocking_gate_failure_continues(self, tmp_path):
        """Non-blocking gate failure still completes (with warning)."""
        script = tmp_path / "validator.py"
        script.write_text("print(0.1)")

        config = make_config(
            [
                {
                    "id": "p1",
                    "name": "P1",
                    "prompt_file": "t.md",
                    "quality_gate": {
                        "validator": str(script),
                        "threshold": 0.8,
                        "blocking": False,
                        "max_retries": 0,
                    },
                }
            ]
        )
        executor = MockExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "p1" in result["completed_phases"]
        assert any("non-blocking" in e["error"].lower() for e in result["errors"])


class TestStateAccumulation:
    @pytest.mark.asyncio
    async def test_outputs_accumulate_across_chain(self):
        config = make_config(
            [
                {"id": "a", "name": "A", "prompt_file": "a.md"},
                {"id": "b", "name": "B", "prompt_file": "b.md", "depends_on": ["a"]},
                {"id": "c", "name": "C", "prompt_file": "c.md", "depends_on": ["b"]},
            ]
        )
        executor = MockExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())
        assert set(result["completed_phases"]) == {"a", "b", "c"}
        assert set(result["phase_outputs"].keys()) == {"a", "b", "c"}


class TestDryRunWithGates:
    @pytest.mark.asyncio
    async def test_dry_run_bypasses_gate_evaluation(self):
        """Dry run should trace all phases even with gates."""
        config = make_config(
            [
                {
                    "id": "a",
                    "name": "A",
                    "prompt_file": "a.md",
                    "quality_gate": {"validator": "v.md", "threshold": 0.99},
                },
                {"id": "b", "name": "B", "prompt_file": "b.md", "depends_on": ["a"]},
            ]
        )
        executor = MockExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(dry_run=True))
        assert "a" in result["completed_phases"]
        assert "b" in result["completed_phases"]
        assert executor.execution_order == []


class TestDispatchExecutor:
    """Test the DispatchExecutor routes to correct executor by type."""

    @pytest.mark.asyncio
    async def test_prompt_phase_returns_stub(self):
        from abe_froman.executor.dispatch import DispatchExecutor

        executor = DispatchExecutor()
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        result = await executor.execute(phase, {})
        assert result.success is True
        assert "prompt-stub" in result.output

    @pytest.mark.asyncio
    async def test_gate_only_phase(self):
        from abe_froman.executor.dispatch import DispatchExecutor

        phase = Phase(
            id="g1",
            name="G1",
            execution={"type": "gate_only"},
        )
        executor = DispatchExecutor()
        result = await executor.execute(phase, {})
        assert result.success is True
        assert "gate-only" in result.output

    @pytest.mark.asyncio
    async def test_command_phase_runs_subprocess(self):
        from abe_froman.executor.dispatch import DispatchExecutor

        phase = Phase(
            id="c1",
            name="C1",
            execution={"type": "command", "command": "echo", "args": ["hello"]},
        )
        executor = DispatchExecutor()
        result = await executor.execute(phase, {})
        assert result.success is True
        assert "hello" in result.output

    @pytest.mark.asyncio
    async def test_command_phase_failure(self):
        from abe_froman.executor.dispatch import DispatchExecutor

        phase = Phase(
            id="c1",
            name="C1",
            execution={"type": "command", "command": "false"},
        )
        executor = DispatchExecutor()
        result = await executor.execute(phase, {})
        assert result.success is False

    @pytest.mark.asyncio
    async def test_no_execution_returns_error(self):
        from abe_froman.executor.dispatch import DispatchExecutor

        phase = Phase(id="p1", name="P1")
        executor = DispatchExecutor()
        result = await executor.execute(phase, {})
        assert result.success is False
        assert "no execution" in result.error.lower()
