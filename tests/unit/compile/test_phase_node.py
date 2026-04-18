"""Unit tests for _make_gate_router from compile/graph.py.

The router is a pure state-reader: it inspects `completed_phases` /
`failed_phases` written by the phase node and returns concrete node
targets (END, the phase id for retry, or dependent phase ids for pass).
Classification logic (score vs threshold, blocking, retry budget) lives
in the phase node (`compile/nodes.py::classify_gate_outcome`), not here.
"""

import asyncio
import time

import pytest
from langgraph.graph import END

from abe_froman.compile.graph import _make_gate_router
from abe_froman.compile.nodes import _make_phase_node
from abe_froman.runtime.result import ExecutionResult
from abe_froman.runtime.state import make_initial_state
from abe_froman.schema.models import (
    OutputContract,
    Phase,
    QualityGate,
    Settings,
    WorkflowConfig,
)
from mock_executor import MockExecutor


class TestGateRouter:
    def _make_phase_with_gate(self, threshold=0.8, blocking=True):
        return Phase(
            id="p1", name="P1", prompt_file="t.md",
            quality_gate=QualityGate(
                validator="v.py", threshold=threshold, blocking=blocking,
            ),
        )

    def test_pass_single_target(self):
        """Completed with one pass target → return that target directly."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase, pass_targets=["b"])
        state = make_initial_state(completed_phases=["p1"])
        assert router(state) == "b"

    def test_pass_multiple_targets_fans_out(self):
        """Completed with multiple pass targets → return list for fan-out."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase, pass_targets=["b", "c"])
        state = make_initial_state(completed_phases=["p1"])
        assert router(state) == ["b", "c"]

    def test_pass_defaults_to_end(self):
        """Terminal gated phase → pass routes to END."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase)
        state = make_initial_state(completed_phases=["p1"])
        assert router(state) == END

    def test_fail_routes_to_end(self):
        """failed_phases contains id → router returns END."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase, pass_targets=["b"])
        state = make_initial_state(failed_phases=["p1"])
        assert router(state) == END

    def test_retry_returns_phase_id(self):
        """Phase node bumped retries (not completed, not failed) → re-enter phase."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase, pass_targets=["b"])
        state = make_initial_state(retries={"p1": 1})
        assert router(state) == "p1"

    def test_failed_takes_precedence_over_completed(self):
        """Defensive: if both lists contain the id, fail wins."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase, pass_targets=["b"])
        state = make_initial_state(
            completed_phases=["p1"], failed_phases=["p1"]
        )
        assert router(state) == END

    def test_retry_on_empty_state(self):
        """Fresh state with no markers → re-enter phase (hasn't executed yet)."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase, pass_targets=["b"])
        state = make_initial_state()
        assert router(state) == "p1"


# ---------------------------------------------------------------------------
# Closure-level unit tests for _make_phase_node
#
# These call the returned closure directly with a MockExecutor and fake
# state dict. The helpers (build_context, classify_gate_outcome, …) are
# unit-tested separately in test_node_helpers.py — these tests pin the
# closure's sequencing: early-exit, executor=None fallback, retry delay,
# output contract validation, gate-outcome integration.
# ---------------------------------------------------------------------------


class _SlowExecutor:
    """PhaseExecutor double that sleeps longer than any reasonable timeout."""

    async def execute(self, phase, context):
        await asyncio.sleep(10.0)
        return ExecutionResult(output="never")


def _config_with(phase: Phase, **settings_kwargs) -> WorkflowConfig:
    return WorkflowConfig(
        name="T", version="1.0",
        phases=[phase],
        settings=Settings(**settings_kwargs),
    )


class TestPhaseNodeClosure:
    @pytest.mark.asyncio
    async def test_already_completed_returns_empty(self):
        """Re-entering a completed node is a no-op (idempotent)."""
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        node = _make_phase_node(phase, _config_with(phase), MockExecutor())
        state = make_initial_state(completed_phases=["p1"])
        assert await node(state) == {}

    @pytest.mark.asyncio
    async def test_none_executor_returns_no_executor_update(self):
        """CLI fallback when no git repo available: executor=None → graceful completion."""
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        node = _make_phase_node(phase, _config_with(phase), executor=None)
        update = await node(make_initial_state())
        assert update["completed_phases"] == ["p1"]
        assert "[no-executor]" in update["phase_outputs"]["p1"]

    @pytest.mark.asyncio
    async def test_none_executor_with_gate_seeds_perfect_score(self):
        """Without executor, gate can't evaluate; closure seeds score=1.0 so the
        router treats it as pass."""
        phase = Phase(
            id="p1", name="P1", prompt_file="t.md",
            quality_gate=QualityGate(validator="v.py", threshold=0.8),
        )
        node = _make_phase_node(phase, _config_with(phase), executor=None)
        update = await node(make_initial_state())
        assert update["gate_scores"] == {"p1": 1.0}

    @pytest.mark.asyncio
    async def test_retry_delay_is_awaited(self):
        """retry_count > 0 with nonzero backoff → closure sleeps before executing."""
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        node = _make_phase_node(
            phase, _config_with(phase, retry_backoff=[0.05]), MockExecutor(),
        )
        state = make_initial_state(retries={"p1": 1})
        t0 = time.monotonic()
        await node(state)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.05, f"expected ≥0.05s sleep, got {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_no_retry_no_delay(self):
        """retry_count == 0 → no sleep, even if backoff configured."""
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        node = _make_phase_node(
            phase, _config_with(phase, retry_backoff=[5.0]), MockExecutor(),
        )
        t0 = time.monotonic()
        await node(make_initial_state())
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"first attempt should not sleep, got {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_execution_failure_returns_failure_update(self):
        """Executor returns success=False → failed_phases + error in update."""
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        executor = MockExecutor(
            results={"p1": ExecutionResult(success=False, error="boom")},
        )
        node = _make_phase_node(phase, _config_with(phase), executor)
        update = await node(make_initial_state())
        assert update["failed_phases"] == ["p1"]
        assert update["errors"][0]["error"] == "boom"

    @pytest.mark.asyncio
    async def test_execution_timeout_returns_failure(self):
        """Slow executor + tight timeout → failed_phases with timeout message."""
        phase = Phase(id="p1", name="P1", prompt_file="t.md", timeout=0.01)
        node = _make_phase_node(phase, _config_with(phase), _SlowExecutor())
        update = await node(make_initial_state())
        assert update["failed_phases"] == ["p1"]
        assert "timed out" in update["errors"][0]["error"]

    @pytest.mark.asyncio
    async def test_output_contract_violation_hard_fails(self, tmp_path):
        """Successful execution + missing required file → failed_phases, no retry."""
        phase = Phase(
            id="p1", name="P1", prompt_file="t.md",
            output_contract=OutputContract(
                base_directory="out", required_files=["expected.md"],
            ),
        )
        node = _make_phase_node(phase, _config_with(phase), MockExecutor())
        state = make_initial_state(workdir=str(tmp_path))
        update = await node(state)
        assert update["failed_phases"] == ["p1"]
        assert "missing files" in update["errors"][0]["error"]
        assert "expected.md" in update["errors"][0]["error"]

    @pytest.mark.asyncio
    async def test_output_contract_satisfied_allows_success(self, tmp_path):
        """Required file present post-execution → completion proceeds."""
        phase = Phase(
            id="p1", name="P1", prompt_file="t.md",
            output_contract=OutputContract(
                base_directory="out", required_files=["expected.md"],
            ),
        )
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "expected.md").write_text("present")
        node = _make_phase_node(phase, _config_with(phase), MockExecutor())
        state = make_initial_state(workdir=str(tmp_path))
        update = await node(state)
        assert update["completed_phases"] == ["p1"]

    @pytest.mark.asyncio
    async def test_success_no_gate_writes_completed(self):
        """Happy path without gate → completed_phases + phase_outputs."""
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        node = _make_phase_node(phase, _config_with(phase), MockExecutor())
        update = await node(make_initial_state())
        assert update["completed_phases"] == ["p1"]
        assert update["phase_outputs"]["p1"] == "[mock] p1 completed"

    @pytest.mark.asyncio
    async def test_gate_pass_writes_completed(self, tmp_path):
        """Gate validator returns 1.0 → completed_phases."""
        validator = tmp_path / "pass.py"
        validator.write_text("import sys\nsys.stdin.read()\nprint('1.0')\n")
        phase = Phase(
            id="p1", name="P1", prompt_file="t.md",
            quality_gate=QualityGate(validator="pass.py", threshold=0.8),
        )
        node = _make_phase_node(phase, _config_with(phase), MockExecutor())
        state = make_initial_state(workdir=str(tmp_path))
        update = await node(state)
        assert update["completed_phases"] == ["p1"]
        assert update["gate_scores"] == {"p1": 1.0}

    @pytest.mark.asyncio
    async def test_gate_retry_bumps_retries(self, tmp_path):
        """Gate validator returns 0.0, retries left → retries dict incremented."""
        validator = tmp_path / "fail.py"
        validator.write_text("import sys\nsys.stdin.read()\nprint('0.0')\n")
        phase = Phase(
            id="p1", name="P1", prompt_file="t.md",
            quality_gate=QualityGate(validator="fail.py", threshold=0.8),
        )
        node = _make_phase_node(
            phase, _config_with(phase, max_retries=3), MockExecutor(),
        )
        state = make_initial_state(workdir=str(tmp_path))
        update = await node(state)
        assert update["retries"] == {"p1": 1}
        assert "completed_phases" not in update
