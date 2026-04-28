"""Unit tests for _make_evaluation_router from compile/graph.py.

The router is a pure state-reader: it inspects `completed_nodes` /
`failed_nodes` written by the Evaluation node and returns concrete
targets (END, the execution node id for retry, or dependent node ids
for pass). Classification logic (score vs threshold, blocking, retry
budget) lives in the Evaluation node body (`compile/nodes.py`), not here.
"""

import asyncio
import time

import pytest
from langgraph.graph import END

from abe_froman.compile.graph import _make_evaluation_router
from abe_froman.compile.nodes import _make_execution_node
from abe_froman.runtime.result import ExecutionResult
from abe_froman.runtime.state import make_initial_state
from abe_froman.schema.models import (
    OutputContract,
    Node,
    Evaluation,
    Settings,
    Graph,
)
from mock_executor import MockExecutor


class TestEvaluationRouter:
    def test_pass_single_target(self):
        """Completed with one pass target → return that target directly."""
        router = _make_evaluation_router("p1", pass_targets=["b"])
        state = make_initial_state(completed_nodes=["p1"])
        assert router(state) == "b"

    def test_pass_multiple_targets_fans_out(self):
        """Completed with multiple pass targets → return list for fan-out."""
        router = _make_evaluation_router("p1", pass_targets=["b", "c"])
        state = make_initial_state(completed_nodes=["p1"])
        assert router(state) == ["b", "c"]

    def test_pass_defaults_to_end(self):
        """Terminal gated node → pass routes to END."""
        router = _make_evaluation_router("p1", pass_targets=[END])
        state = make_initial_state(completed_nodes=["p1"])
        assert router(state) == END

    def test_fail_routes_to_end(self):
        """failed_nodes contains id → router returns END."""
        router = _make_evaluation_router("p1", pass_targets=["b"])
        state = make_initial_state(failed_nodes=["p1"])
        assert router(state) == END

    def test_retry_returns_execution_node_id(self):
        """Eval node wrote retries (not completed, not failed) → re-enter exec node."""
        router = _make_evaluation_router("p1", pass_targets=["b"])
        state = make_initial_state(retries={"p1": 1})
        assert router(state) == "p1"

    def test_failed_takes_precedence_over_completed(self):
        """Defensive: if both lists contain the id, fail wins."""
        router = _make_evaluation_router("p1", pass_targets=["b"])
        state = make_initial_state(
            completed_nodes=["p1"], failed_nodes=["p1"]
        )
        assert router(state) == END

    def test_retry_on_empty_state(self):
        """Fresh state with no markers → re-enter exec node (hasn't executed yet)."""
        router = _make_evaluation_router("p1", pass_targets=["b"])
        state = make_initial_state()
        assert router(state) == "p1"

    def test_resolver_switches_node_id(self):
        """node_id_resolver lets child routers key off _fan_out_item."""
        def resolve(state):
            return f"parent::{state.get('_fan_out_item', {}).get('id', '?')}"

        router = _make_evaluation_router(
            "_sub_parent", pass_targets=["_final_parent_f0"], node_id_resolver=resolve,
        )
        state = make_initial_state(completed_nodes=["parent::x"])
        state["_fan_out_item"] = {"id": "x"}
        assert router(state) == "_final_parent_f0"


# ---------------------------------------------------------------------------
# Closure-level unit tests for _make_execution_node
#
# These call the returned closure directly with a MockExecutor and fake
# state dict. Gated nodes only exercise the execution half here — the
# Evaluation node half is tested in test_evaluation_node.py.
# ---------------------------------------------------------------------------


class _SlowExecutor:
    """NodeExecutor double that sleeps longer than any reasonable timeout."""

    async def execute(self, node, context):
        await asyncio.sleep(10.0)
        return ExecutionResult(output="never")


def _config_with(node: Node, **settings_kwargs) -> Graph:
    return Graph(
        name="T", version="1.0",
        nodes=[node],
        settings=Settings(**settings_kwargs),
    )


class TestPhaseNodeClosure:
    @pytest.mark.asyncio
    async def test_already_completed_returns_empty(self):
        """Re-entering a completed node is a no-op (idempotent)."""
        node = Node(id="p1", name="P1", prompt_file="t.md")
        node = _make_execution_node(node, _config_with(node), MockExecutor())
        state = make_initial_state(completed_nodes=["p1"])
        assert await node(state) == {}

    @pytest.mark.asyncio
    async def test_none_executor_returns_no_executor_update(self):
        """CLI fallback when no git repo available: executor=None → graceful completion."""
        node = Node(id="p1", name="P1", prompt_file="t.md")
        node = _make_execution_node(node, _config_with(node), executor=None)
        update = await node(make_initial_state())
        assert update["completed_nodes"] == ["p1"]
        assert "[no-executor]" in update["node_outputs"]["p1"]

    @pytest.mark.asyncio
    async def test_none_executor_with_gate_emits_output_only(self):
        """Gated node without executor: node node emits node_outputs but does
        NOT write completed_nodes — the downstream Evaluation node handles that."""
        node = Node(
            id="p1", name="P1", prompt_file="t.md",
            evaluation=Evaluation(validator="v.py", threshold=0.8),
        )
        node = _make_execution_node(node, _config_with(node), executor=None)
        update = await node(make_initial_state())
        assert "completed_nodes" not in update
        assert "[no-executor]" in update["node_outputs"]["p1"]

    @pytest.mark.asyncio
    async def test_retry_delay_is_awaited(self):
        """retry_count > 0 with nonzero backoff → closure sleeps before executing."""
        node = Node(id="p1", name="P1", prompt_file="t.md")
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
        node = Node(id="p1", name="P1", prompt_file="t.md")
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
        node = Node(id="p1", name="P1", prompt_file="t.md")
        executor = MockExecutor(
            results={"p1": ExecutionResult(success=False, error="boom")},
        )
        node = _make_execution_node(node, _config_with(node), executor)
        update = await node(make_initial_state())
        assert update["failed_nodes"] == ["p1"]
        assert update["errors"][0]["error"] == "boom"

    @pytest.mark.asyncio
    async def test_execution_timeout_returns_failure(self):
        """Slow executor + tight timeout → failed_nodes with timeout message."""
        node = Node(id="p1", name="P1", prompt_file="t.md", timeout=0.01)
        node = _make_execution_node(node, _config_with(node), _SlowExecutor())
        update = await node(make_initial_state())
        assert update["failed_nodes"] == ["p1"]
        assert "timed out" in update["errors"][0]["error"]

    @pytest.mark.asyncio
    async def test_output_contract_violation_hard_fails(self, tmp_path):
        """Successful execution + missing required file → failed_nodes, no retry."""
        node = Node(
            id="p1", name="P1", prompt_file="t.md",
            output_contract=OutputContract(
                base_directory="out", required_files=["expected.md"],
            ),
        )
        node = _make_execution_node(node, _config_with(node), MockExecutor())
        state = make_initial_state(workdir=str(tmp_path))
        update = await node(state)
        assert update["failed_nodes"] == ["p1"]
        assert "missing files" in update["errors"][0]["error"]
        assert "expected.md" in update["errors"][0]["error"]

    @pytest.mark.asyncio
    async def test_output_contract_satisfied_allows_success(self, tmp_path):
        """Required file present post-execution → completion proceeds."""
        node = Node(
            id="p1", name="P1", prompt_file="t.md",
            output_contract=OutputContract(
                base_directory="out", required_files=["expected.md"],
            ),
        )
        (tmp_path / "out").mkdir()
        (tmp_path / "out" / "expected.md").write_text("present")
        node = _make_execution_node(node, _config_with(node), MockExecutor())
        state = make_initial_state(workdir=str(tmp_path))
        update = await node(state)
        assert update["completed_nodes"] == ["p1"]

    @pytest.mark.asyncio
    async def test_success_no_gate_writes_completed(self):
        """Happy path without gate → completed_nodes + node_outputs."""
        node = Node(id="p1", name="P1", prompt_file="t.md")
        node = _make_execution_node(node, _config_with(node), MockExecutor())
        update = await node(make_initial_state())
        assert update["completed_nodes"] == ["p1"]
        assert update["node_outputs"]["p1"] == "[mock] p1 completed"

    @pytest.mark.asyncio
    async def test_gated_phase_emits_output_without_completing(self, tmp_path):
        """Gated node: execution writes node_outputs; Evaluation node writes
        completed_nodes / retries / failed_nodes separately."""
        node = Node(
            id="p1", name="P1", prompt_file="t.md",
            evaluation=Evaluation(validator="v.py", threshold=0.8),
        )
        node = _make_execution_node(node, _config_with(node), MockExecutor())
        state = make_initial_state(workdir=str(tmp_path))
        update = await node(state)
        assert "completed_nodes" not in update
        assert "retries" not in update
        assert "failed_nodes" not in update
        assert update["node_outputs"]["p1"] == "[mock] p1 completed"
