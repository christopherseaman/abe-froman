"""Unit tests for _make_execution_node from compile/nodes.py.

These call the returned closure directly with a MockExecutor and fake
state dict. Gated nodes only exercise the execution half here — the
Evaluation node body lives in test_evaluation_node.py and the
Decision node body in test_decision_node.py.
"""

import asyncio
import time

import pytest

from sqrlly.compile.nodes import _make_execution_node
from sqrlly.runtime.result import ExecutionResult
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import (
    Execute,
    OutputContract,
    Node,
    Evaluation,
    Settings,
    Graph,
)
from mock_executor import MockExecutor


# ---------------------------------------------------------------------------
# Closure-level unit tests for _make_execution_node
# ---------------------------------------------------------------------------


class _SlowExecutor:
    """NodeExecutor double that sleeps longer than any reasonable timeout."""

    async def execute(self, node, context, **_):
        await asyncio.sleep(10.0)
        return ExecutionResult(output="never")


def _config_with(node: Node, **settings_kwargs) -> Graph:
    return Graph(
        name="T", version="1.0",
        nodes=[node],
        settings=Settings(**settings_kwargs),
    )


class TestExecutionNodeClosure:
    @pytest.mark.asyncio
    async def test_re_entry_executes_body_again(self):
        """Re-entering a node (e.g. via inline-route `Command(goto=...)`)
        executes the body again. The framework's job is to dispatch the
        procedure on every reach; idempotence is a property of the
        procedure, not the framework. (Compare: a QA step still runs
        when a reopened bug fix routes back through it.)
        """
        node = Node(id="p1", name="P1", execute=Execute(url="t.md"))
        node = _make_execution_node(node, _config_with(node), MockExecutor())
        state = make_initial_state(completed_nodes={"p1"})
        update = await node(state)
        # Body ran: emits a new node_outputs entry and re-marks complete.
        assert "node_outputs" in update
        assert update["completed_nodes"] == {"p1"}

    @pytest.mark.asyncio
    async def test_none_executor_returns_no_executor_update(self):
        """CLI fallback when no git repo available: executor=None → graceful completion."""
        node = Node(id="p1", name="P1", execute=Execute(url="t.md"))
        node = _make_execution_node(node, _config_with(node), executor=None)
        update = await node(make_initial_state())
        assert update["completed_nodes"] == {"p1"}
        assert "[no-executor]" in update["node_outputs"]["p1"]

    @pytest.mark.asyncio
    async def test_none_executor_with_gate_emits_output_only(self):
        """Gated node without executor: node node emits node_outputs but does
        NOT write completed_nodes — the downstream Evaluation node handles that."""
        node = Node(
            id="p1", name="P1", execute=Execute(url="t.md"),
            evaluation=Evaluation(validator="v.py", threshold=0.8),
        )
        node = _make_execution_node(node, _config_with(node), executor=None)
        update = await node(make_initial_state())
        assert "completed_nodes" not in update
        assert "[no-executor]" in update["node_outputs"]["p1"]

    @pytest.mark.asyncio
    async def test_retry_delay_is_awaited(self):
        """retry_count > 0 with nonzero backoff → closure sleeps before executing."""
        node = Node(id="p1", name="P1", execute=Execute(url="t.md"))
        node = _make_execution_node(
            node, _config_with(node, retry_backoff=[0.05]), MockExecutor(),
        )
        state = make_initial_state(retries={"p1": 1})
        t0 = time.monotonic()
        await node(state)
        elapsed = time.monotonic() - t0
        assert elapsed >= 0.05, f"expected ≥0.05s sleep, got {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_no_retry_no_delay(self):
        """retry_count == 0 → no sleep, even if backoff configured."""
        node = Node(id="p1", name="P1", execute=Execute(url="t.md"))
        node = _make_execution_node(
            node, _config_with(node, retry_backoff=[5.0]), MockExecutor(),
        )
        t0 = time.monotonic()
        await node(make_initial_state())
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0, f"first attempt should not sleep, got {elapsed:.3f}s"

    @pytest.mark.asyncio
    async def test_execution_failure_returns_failure_update(self):
        """Executor returns success=False → failed_nodes + error in update."""
        node = Node(id="p1", name="P1", execute=Execute(url="t.md"))
        executor = MockExecutor(
            results={"p1": ExecutionResult(success=False, error="boom")},
        )
        node = _make_execution_node(node, _config_with(node), executor)
        update = await node(make_initial_state())
        assert update["failed_nodes"] == {"p1"}
        assert update["errors"][0]["error"] == "boom"

    @pytest.mark.asyncio
    async def test_execution_timeout_returns_failure(self):
        """Slow executor + tight timeout → failed_nodes with timeout message."""
        node = Node(id="p1", name="P1", execute=Execute(url="t.md"), timeout=0.01)
        node = _make_execution_node(node, _config_with(node), _SlowExecutor())
        update = await node(make_initial_state())
        assert update["failed_nodes"] == {"p1"}
        assert "timed out" in update["errors"][0]["error"]

    @pytest.mark.asyncio
    async def test_output_contract_violation_hard_fails(self, tmp_path):
        """Successful execution + missing required file → failed_nodes, no retry."""
        node = Node(
            id="p1", name="P1", execute=Execute(url="t.md"),
            output_contract=OutputContract(
                base_directory="out", required_files=["expected.md"],
            ),
        )
        node = _make_execution_node(node, _config_with(node), MockExecutor())
        state = make_initial_state(workdir=str(tmp_path))
        update = await node(state)
        assert update["failed_nodes"] == {"p1"}
        assert "missing files" in update["errors"][0]["error"]
        assert "expected.md" in update["errors"][0]["error"]

    @pytest.mark.asyncio
    async def test_output_contract_satisfied_allows_success(self, tmp_path):
        """Required file present post-execution → completion proceeds."""
        node = Node(
            id="p1", name="P1", execute=Execute(url="t.md"),
            output_contract=OutputContract(
                base_directory="out", required_files=["expected.md"],
            ),
        )
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "expected.md").write_text("present")
        node = _make_execution_node(node, _config_with(node), MockExecutor())
        state = make_initial_state(workdir=str(tmp_path))
        update = await node(state)
        assert update["completed_nodes"] == {"p1"}

    @pytest.mark.asyncio
    async def test_success_no_gate_writes_completed(self):
        """Happy path without gate → completed_nodes + node_outputs."""
        node = Node(id="p1", name="P1", execute=Execute(url="t.md"))
        node = _make_execution_node(node, _config_with(node), MockExecutor())
        update = await node(make_initial_state())
        assert update["completed_nodes"] == {"p1"}
        assert update["node_outputs"]["p1"] == "[mock] p1 completed"

    @pytest.mark.asyncio
    async def test_gated_phase_emits_output_without_completing(self, tmp_path):
        """Gated node: execution writes node_outputs; Evaluation node writes
        completed_nodes / retries / failed_nodes separately."""
        node = Node(
            id="p1", name="P1", execute=Execute(url="t.md"),
            evaluation=Evaluation(validator="v.py", threshold=0.8),
        )
        node = _make_execution_node(node, _config_with(node), MockExecutor())
        state = make_initial_state(workdir=str(tmp_path))
        update = await node(state)
        assert "completed_nodes" not in update
        assert "retries" not in update
        assert "failed_nodes" not in update
        assert update["node_outputs"]["p1"] == "[mock] p1 completed"
