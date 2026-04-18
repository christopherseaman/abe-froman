"""Orchestrator tests — full LangGraph pipeline execution.

Most tests use real subprocesses via DispatchExecutor + command phases.
MockExecutor is used only where the thing under test is wiring/context
propagation rather than executor semantics.
"""

import pytest

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.runtime.state import make_initial_state
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.schema.models import Phase

from helpers import cmd_phase, fail_phase, make_config


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
        from mock_executor import MockExecutor

        mock = MockExecutor()
        config = make_config([cmd_phase("p1")])
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state(dry_run=True))
        assert "p1" in result["completed_phases"]
        assert "dry-run" in result["phase_outputs"]["p1"]
        assert mock.execution_order == [], "executor should not be called in dry-run"


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
        assert result["phase_outputs"]["a"] == "a-out"
        assert result["phase_outputs"]["b"] == "b-out"

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
        assert result["phase_outputs"]["a"] == "1"
        assert result["phase_outputs"]["b"] == "2"
        assert result["phase_outputs"]["c"] == "3"


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
        assert result["phase_outputs"]["a"] == "root"
        assert result["phase_outputs"]["b"] == "left"
        assert result["phase_outputs"]["c"] == "right"
        assert result["phase_outputs"]["d"] == "join"

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
        assert result["phase_outputs"]["x"] == "x-out"
        assert result["phase_outputs"]["y"] == "y-out"


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


# ---------------------------------------------------------------------------
# Gate retry re-execution
# ---------------------------------------------------------------------------


class TestGateRetryExecution:
    @pytest.mark.asyncio
    async def test_retry_re_executes_phase(self, tmp_path):
        """Validator uses a counter file: returns 0.0 on first call, 1.0 on second."""
        counter = tmp_path / "counter.txt"
        counter.write_text("0")

        validator = tmp_path / "counting_validator.py"
        validator.write_text(
            "from pathlib import Path\n"
            f"counter = Path(r'{counter}')\n"
            "n = int(counter.read_text().strip())\n"
            "n += 1\n"
            "counter.write_text(str(n))\n"
            "print('1.0' if n >= 2 else '0.0')\n"
        )

        config = make_config([
            cmd_phase(
                "p1", output="data",
                quality_gate={
                    "validator": str(validator),
                    "threshold": 1.0,
                    "blocking": True,
                    "max_retries": 2,
                },
            ),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p1" in result["completed_phases"]
        assert int(counter.read_text().strip()) == 2


# ---------------------------------------------------------------------------
# Retry context injection
# ---------------------------------------------------------------------------


class TestRetryContextInjection:
    @pytest.mark.asyncio
    async def test_retry_injects_retry_reason(self, tmp_path):
        """On retry, executor receives _retry_reason in context."""
        counter = tmp_path / "counter.txt"
        counter.write_text("0")

        validator = tmp_path / "counting_validator.py"
        validator.write_text(
            "from pathlib import Path\n"
            f"counter = Path(r'{counter}')\n"
            "n = int(counter.read_text().strip())\n"
            "n += 1\n"
            "counter.write_text(str(n))\n"
            "print('1.0' if n >= 2 else '0.3')\n"
        )

        from mock_executor import MockExecutor
        from abe_froman.runtime.result import ExecutionResult

        mock = MockExecutor(results={
            "p1": ExecutionResult(success=True, output="data"),
        })
        config = make_config([
            {
                "id": "p1",
                "name": "P1",
                "prompt_file": "t.md",
                "quality_gate": {
                    "validator": str(validator),
                    "threshold": 1.0,
                    "blocking": True,
                    "max_retries": 2,
                },
            },
        ])
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p1" in result["completed_phases"]
        # Phase was executed twice (initial + retry)
        assert mock.execution_order.count("p1") == 2
        # MockExecutor overwrites context per phase ID, so last call wins —
        # the retry call should have _retry_reason
        assert "_retry_reason" in mock.received_contexts["p1"]

    @pytest.mark.asyncio
    async def test_retry_reason_contains_score_and_threshold(self, tmp_path):
        """_retry_reason includes the previous score and threshold."""
        counter = tmp_path / "counter.txt"
        counter.write_text("0")

        validator = tmp_path / "counting_validator.py"
        validator.write_text(
            "from pathlib import Path\n"
            f"counter = Path(r'{counter}')\n"
            "n = int(counter.read_text().strip())\n"
            "n += 1\n"
            "counter.write_text(str(n))\n"
            "print('1.0' if n >= 2 else '0.30')\n"
        )

        from mock_executor import MockExecutor
        from abe_froman.runtime.result import ExecutionResult

        mock = MockExecutor(results={
            "p1": ExecutionResult(success=True, output="data"),
        })
        config = make_config([
            {
                "id": "p1",
                "name": "P1",
                "prompt_file": "t.md",
                "quality_gate": {
                    "validator": str(validator),
                    "threshold": 1.0,
                    "blocking": True,
                    "max_retries": 2,
                },
            },
        ])
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p1" in result["completed_phases"]
        # MockExecutor overwrites context on each call, so last call has _retry_reason
        ctx = mock.received_contexts["p1"]
        assert "_retry_reason" in ctx
        assert "0.30" in ctx["_retry_reason"]
        assert "threshold=1.0" in ctx["_retry_reason"]
        assert "retry 1 of 2" in ctx["_retry_reason"].lower()

    @pytest.mark.asyncio
    async def test_no_retry_reason_on_first_attempt(self, tmp_path):
        """First execution should not have _retry_reason in context."""
        script = tmp_path / "pass.py"
        script.write_text("print(1.0)")

        from mock_executor import MockExecutor
        from abe_froman.runtime.result import ExecutionResult

        mock = MockExecutor(results={
            "p1": ExecutionResult(success=True, output="data"),
        })
        config = make_config([
            {
                "id": "p1",
                "name": "P1",
                "prompt_file": "t.md",
                "quality_gate": {
                    "validator": str(script),
                    "threshold": 0.8,
                    "blocking": True,
                },
            },
        ])
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p1" in result["completed_phases"]
        assert "_retry_reason" not in mock.received_contexts["p1"]


# ---------------------------------------------------------------------------
# Context propagation via MockExecutor
# ---------------------------------------------------------------------------


class TestContextPropagation:
    """Wiring tests — uses MockExecutor because the thing under test is
    context-flow between phases, not executor semantics."""

    @pytest.mark.asyncio
    async def test_dependency_output_in_executor_context(self):
        from mock_executor import MockExecutor
        from abe_froman.runtime.result import ExecutionResult

        mock = MockExecutor(results={
            "a": ExecutionResult(success=True, output="a-output"),
        })
        config = make_config([
            {"id": "a", "name": "A", "prompt_file": "t.md"},
            {"id": "b", "name": "B", "prompt_file": "t.md", "depends_on": ["a"]},
        ])
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state())

        assert "a" in result["completed_phases"]
        assert "b" in result["completed_phases"]
        assert mock.received_contexts["b"]["a"] == "a-output"

    @pytest.mark.asyncio
    async def test_structured_output_flows_to_dependent(self):
        from mock_executor import MockExecutor
        from abe_froman.runtime.result import ExecutionResult

        mock = MockExecutor(results={
            "a": ExecutionResult(
                success=True, output="text",
                structured_output={"key": "val"},
            ),
        })
        config = make_config([
            {"id": "a", "name": "A", "prompt_file": "t.md"},
            {"id": "b", "name": "B", "prompt_file": "t.md", "depends_on": ["a"]},
        ])
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state())

        assert "b" in result["completed_phases"]
        assert mock.received_contexts["b"]["a_structured"] == {"key": "val"}


# ---------------------------------------------------------------------------
# Output contract enforcement
# ---------------------------------------------------------------------------


class TestDimensionGateExecution:
    @pytest.mark.asyncio
    async def test_dimension_gate_passes_all_checks(self, tmp_path):
        """Script returns multi-dimension JSON; all dimensions pass thresholds."""
        validator = tmp_path / "dim_validator.py"
        validator.write_text(
            'import json\n'
            'print(json.dumps({"correctness": 0.9, "style": 0.7}))\n'
        )
        config = make_config([
            cmd_phase(
                "p1", output="data",
                quality_gate={
                    "validator": str(validator),
                    "dimensions": [
                        {"field": "correctness", "min": 0.8},
                        {"field": "style", "min": 0.5},
                    ],
                },
            ),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p1" in result["completed_phases"]
        fb = result["gate_feedback"]["p1"]
        assert fb["scores"]["correctness"] == 0.9
        assert fb["scores"]["style"] == 0.7

    @pytest.mark.asyncio
    async def test_dimension_gate_fails_one_dimension(self, tmp_path):
        """One dimension below min → retry, then pass on second attempt."""
        counter = tmp_path / "counter.txt"
        counter.write_text("0")

        validator = tmp_path / "dim_validator.py"
        validator.write_text(
            'import json\n'
            'from pathlib import Path\n'
            f'counter = Path(r"{counter}")\n'
            'n = int(counter.read_text().strip())\n'
            'n += 1\n'
            'counter.write_text(str(n))\n'
            'style = 0.9 if n >= 2 else 0.3\n'
            'print(json.dumps({"correctness": 0.9, "style": style}))\n'
        )
        config = make_config([
            cmd_phase(
                "p1", output="data",
                quality_gate={
                    "validator": str(validator),
                    "max_retries": 2,
                    "blocking": True,
                    "dimensions": [
                        {"field": "correctness", "min": 0.8},
                        {"field": "style", "min": 0.5},
                    ],
                },
            ),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p1" in result["completed_phases"]
        assert int(counter.read_text().strip()) == 2


class TestTokenUsageAccumulation:
    @pytest.mark.asyncio
    async def test_token_usage_accumulated_in_state(self):
        from mock_executor import MockExecutor
        from abe_froman.runtime.result import ExecutionResult

        mock = MockExecutor(results={
            "a": ExecutionResult(
                success=True, output="a-out",
                tokens_used={"input": 100, "output": 50},
            ),
            "b": ExecutionResult(
                success=True, output="b-out",
                tokens_used={"input": 200, "output": 75},
            ),
        })
        config = make_config([
            {"id": "a", "name": "A", "prompt_file": "t.md"},
            {"id": "b", "name": "B", "prompt_file": "t.md", "depends_on": ["a"]},
        ])
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state())

        assert result["token_usage"] == {
            "a": {"input": 100, "output": 50},
            "b": {"input": 200, "output": 75},
        }

    @pytest.mark.asyncio
    async def test_token_usage_empty_for_command_phases(self):
        config = make_config([cmd_phase("p1", output="ok")])
        executor = DispatchExecutor()
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state())

        assert "p1" in result["completed_phases"]
        assert result.get("token_usage", {}) == {}

    @pytest.mark.asyncio
    async def test_token_usage_none_not_stored(self):
        from mock_executor import MockExecutor
        from abe_froman.runtime.result import ExecutionResult

        mock = MockExecutor(results={
            "a": ExecutionResult(success=True, output="out", tokens_used=None),
        })
        config = make_config([
            {"id": "a", "name": "A", "prompt_file": "t.md"},
        ])
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state())

        assert result.get("token_usage", {}) == {}


class TestOutputContract:
    @pytest.mark.asyncio
    async def test_contract_pass_continues_to_gate(self, tmp_path):
        """Files exist → gate runs → phase completes."""
        out = tmp_path / "out"
        out.mkdir()
        (out / "result.txt").write_text("done")

        script = tmp_path / "pass.py"
        script.write_text("print(1.0)")

        config = make_config([
            cmd_phase(
                "p1", output="data",
                output_contract={"base_directory": "out", "required_files": ["result.txt"]},
                quality_gate={"validator": str(script), "threshold": 0.8},
            ),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "p1" in result["completed_phases"]
        assert result["gate_scores"]["p1"] == 1.0

    @pytest.mark.asyncio
    async def test_contract_fail_blocks_phase(self, tmp_path):
        """Missing files → phase fails, gate never runs."""
        config = make_config([
            cmd_phase(
                "p1", output="data",
                output_contract={"base_directory": "out", "required_files": ["missing.txt"]},
                quality_gate={"validator": "never_called.py", "threshold": 0.8},
            ),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "p1" in result["failed_phases"]
        assert "p1" not in result.get("gate_scores", {})
        assert any("contract violated" in e["error"].lower() for e in result["errors"])

    @pytest.mark.asyncio
    async def test_contract_fail_skips_dependent(self, tmp_path):
        """Contract failure cascades — downstream phase also fails."""
        config = make_config([
            cmd_phase(
                "a", output="data",
                output_contract={"base_directory": "out", "required_files": ["missing.txt"]},
            ),
            cmd_phase("b", output="ok", depends_on=["a"]),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "a" in result["failed_phases"]
        assert "b" in result["failed_phases"]

    @pytest.mark.asyncio
    async def test_contract_without_gate(self, tmp_path):
        """Contract checked even when no quality_gate is defined."""
        config = make_config([
            cmd_phase(
                "p1", output="data",
                output_contract={"base_directory": "out", "required_files": ["missing.txt"]},
            ),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "p1" in result["failed_phases"]

    @pytest.mark.asyncio
    async def test_contract_fail_no_retry(self, tmp_path):
        """Contract failure is a hard fail — does NOT trigger gate retry."""
        config = make_config([
            cmd_phase(
                "p1", output="data",
                output_contract={"base_directory": "out", "required_files": ["missing.txt"]},
                quality_gate={
                    "validator": "never_called.py", "threshold": 0.8,
                    "blocking": True, "max_retries": 3,
                },
            ),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "p1" in result["failed_phases"]
        assert result.get("retries", {}).get("p1", 0) == 0


