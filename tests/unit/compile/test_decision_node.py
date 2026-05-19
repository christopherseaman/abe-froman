"""Unit tests for _make_decision_node from compile/nodes.py.

The Decision node is the second half of a gated execution pair. It
reads the latest EvaluationRecord that the Eval node wrote to
`state.evaluations[node_id]`, classifies the outcome via
`classify_evaluation_outcome`, and returns a
`Command(update=..., goto=...)`:

  - pass / warn_continue → goto pass_targets, update completed_nodes
  - retry → goto exec_id, update retries
  - fail_blocking → goto END, update failed_nodes + errors

These tests mirror what the deleted `_make_evaluation_router` used
to assert (the router read written state and returned a string
target). Now the Decision node returns a Command directly — same
goto targets, same outcome state writes.
"""
from __future__ import annotations

import pytest
from langgraph.graph import END
from langgraph.types import Command

from sqrlly.compile.nodes import _make_decision_node
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import (
    Evaluation,
    Execute,
    Graph,
    Node,
    Settings,
)


def _config_with(node: Node, **settings_kwargs) -> Graph:
    return Graph(
        name="T", version="1.0",
        nodes=[node],
        settings=Settings(**settings_kwargs),
    )


def _gated_node(retry_threshold: float = 0.8) -> Node:
    return Node(
        id="p1", name="P1",
        execute=Execute(url="t.md"),
        evaluation=Evaluation(validator="v.py", threshold=retry_threshold),
    )


def _state_with_record(score: float, retries: int = 0) -> dict:
    state = make_initial_state()
    state["evaluations"] = {
        "p1": [{
            "invocation": retries,
            "result": {
                "score": score,
                "scores": {},
                "reasons": {},
                "feedback": None,
                "pass_criteria_met": [],
                "pass_criteria_unmet": [],
            },
            "timestamp": "t",
        }]
    }
    if retries:
        state["retries"] = {"p1": retries}
    return state


class TestDecisionNodePass:
    @pytest.mark.asyncio
    async def test_pass_single_target_returns_command_goto_target(self):
        """Score above threshold → Command(update=completed, goto=target)."""
        node = _gated_node(retry_threshold=0.8)
        decide = _make_decision_node(
            node, _config_with(node, max_retries=2),
            exec_id="p1", pass_targets=["b"],
        )
        cmd = await decide(_state_with_record(score=0.9))
        assert isinstance(cmd, Command)
        assert cmd.goto == "b"
        assert cmd.update == {"completed_nodes": {"p1"}}

    @pytest.mark.asyncio
    async def test_pass_multiple_targets_fans_out_via_list(self):
        """Multi-pass targets → goto receives a list (LangGraph fans out)."""
        node = _gated_node(0.8)
        decide = _make_decision_node(
            node, _config_with(node, max_retries=2),
            exec_id="p1", pass_targets=["b", "c"],
        )
        cmd = await decide(_state_with_record(score=0.9))
        assert cmd.goto == ["b", "c"]

    @pytest.mark.asyncio
    async def test_pass_terminal_gated_node_routes_to_END(self):
        """Terminal gated node has pass_targets=[END]."""
        node = _gated_node(0.8)
        decide = _make_decision_node(
            node, _config_with(node, max_retries=2),
            exec_id="p1", pass_targets=[END],
        )
        cmd = await decide(_state_with_record(score=0.9))
        assert cmd.goto == END


class TestDecisionNodeRetry:
    @pytest.mark.asyncio
    async def test_retry_returns_command_goto_exec(self):
        """Score below threshold + budget left → Command(goto=exec_id)."""
        node = _gated_node(0.8)
        decide = _make_decision_node(
            node, _config_with(node, max_retries=3),
            exec_id="p1", pass_targets=["b"],
        )
        cmd = await decide(_state_with_record(score=0.3, retries=0))
        assert isinstance(cmd, Command)
        assert cmd.goto == "p1"
        assert cmd.update == {"retries": {"p1": 1}}

    @pytest.mark.asyncio
    async def test_retry_increments_existing_counter(self):
        """retries=2 in state → bumped to 3 in update."""
        node = _gated_node(0.8)
        decide = _make_decision_node(
            node, _config_with(node, max_retries=5),
            exec_id="p1", pass_targets=["b"],
        )
        cmd = await decide(_state_with_record(score=0.3, retries=2))
        assert cmd.update == {"retries": {"p1": 3}}


class TestDecisionNodeFail:
    @pytest.mark.asyncio
    async def test_fail_blocking_after_max_retries(self):
        """Below threshold + budget exhausted + blocking=True → goto END,
        failed_nodes set, errors populated."""
        node = Node(
            id="p1", name="P1",
            execute=Execute(url="t.md"),
            evaluation=Evaluation(
                validator="v.py", threshold=0.8, blocking=True,
            ),
        )
        decide = _make_decision_node(
            node, _config_with(node, max_retries=1),
            exec_id="p1", pass_targets=["b"],
        )
        cmd = await decide(_state_with_record(score=0.3, retries=1))
        assert cmd.goto == END
        assert cmd.update["failed_nodes"] == {"p1"}
        assert any(
            "Evaluation failed" in e["error"] for e in cmd.update["errors"]
        )

    @pytest.mark.asyncio
    async def test_warn_continue_routes_pass_records_error(self):
        """Below threshold + budget exhausted + blocking=False → routes
        as pass (completed_nodes set) but records a non-blocking error."""
        node = Node(
            id="p1", name="P1",
            execute=Execute(url="t.md"),
            evaluation=Evaluation(
                validator="v.py", threshold=0.8, blocking=False,
            ),
        )
        decide = _make_decision_node(
            node, _config_with(node, max_retries=1),
            exec_id="p1", pass_targets=["b"],
        )
        cmd = await decide(_state_with_record(score=0.3, retries=1))
        assert cmd.goto == "b"
        assert cmd.update["completed_nodes"] == {"p1"}
        assert any(
            "non-blocking" in e["error"] for e in cmd.update["errors"]
        )


class TestDecisionNodeGuards:
    """Edge-case guards. The Decision node's pre-flight checks
    short-circuit when state already records a terminal outcome,
    matching the old router's behavior at the goto level."""

    @pytest.mark.asyncio
    async def test_already_failed_routes_to_END(self):
        """failed_nodes carrying the id → Command(goto=END), no record read."""
        node = _gated_node()
        decide = _make_decision_node(
            node, _config_with(node),
            exec_id="p1", pass_targets=["b"],
        )
        state = make_initial_state(failed_nodes={"p1"})
        cmd = await decide(state)
        assert cmd.goto == END

    @pytest.mark.asyncio
    async def test_already_completed_routes_pass(self):
        """completed_nodes carrying the id → Command(goto=pass_targets),
        no re-classification."""
        node = _gated_node()
        decide = _make_decision_node(
            node, _config_with(node),
            exec_id="p1", pass_targets=["b"],
        )
        state = make_initial_state(completed_nodes={"p1"})
        cmd = await decide(state)
        assert cmd.goto == "b"

    @pytest.mark.asyncio
    async def test_no_record_yet_returns_no_op(self):
        """Eval deferred (no record in state) → Decide also defers,
        returns {} so LangGraph re-fires when eval writes a record."""
        node = _gated_node()
        decide = _make_decision_node(
            node, _config_with(node),
            exec_id="p1", pass_targets=["b"],
        )
        state = make_initial_state()
        result = await decide(state)
        assert result == {}

    @pytest.mark.asyncio
    async def test_dry_run_synthesizes_pass(self):
        """Dry-run mode → Command(goto=pass_target, update=completed)."""
        node = _gated_node()
        decide = _make_decision_node(
            node, _config_with(node),
            exec_id="p1", pass_targets=["b"],
        )
        state = make_initial_state(dry_run=True)
        cmd = await decide(state)
        assert cmd.goto == "b"
        assert cmd.update == {"completed_nodes": {"p1"}}


class TestDecisionNodeSubphaseResolver:
    @pytest.mark.asyncio
    async def test_resolver_keys_decision_off_fan_out_item(self):
        """`node_id_resolver` lets per-branch decision nodes derive
        the child's id from `_fan_out_item`."""
        node = _gated_node()
        def resolve(state):
            item = state.get("_fan_out_item", {})
            return f"parent::{item.get('id', '?')}"

        decide = _make_decision_node(
            node, _config_with(node, max_retries=2),
            exec_id="_sub_parent",
            pass_targets=["_final_parent_f0"],
            node_id_resolver=resolve,
        )
        state = make_initial_state()
        state["_fan_out_item"] = {"id": "x"}
        state["evaluations"] = {
            "parent::x": [{
                "invocation": 0,
                "result": {
                    "score": 0.9, "scores": {}, "reasons": {},
                    "feedback": None,
                    "pass_criteria_met": [], "pass_criteria_unmet": [],
                },
                "timestamp": "t",
            }],
        }
        cmd = await decide(state)
        assert cmd.goto == "_final_parent_f0"
        assert cmd.update == {"completed_nodes": {"parent::x"}}


class TestDecisionReadsLatestRecord:
    @pytest.mark.asyncio
    async def test_decision_reads_latest_when_history_present(self):
        """If multiple records exist (retries accumulated), Decision
        classifies on the LATEST record only."""
        node = _gated_node(retry_threshold=0.8)
        decide = _make_decision_node(
            node, _config_with(node, max_retries=3),
            exec_id="p1", pass_targets=["b"],
        )
        state = make_initial_state()
        state["evaluations"] = {
            "p1": [
                {  # earlier attempt failed
                    "invocation": 0,
                    "result": {
                        "score": 0.3, "scores": {}, "reasons": {},
                        "feedback": None,
                        "pass_criteria_met": [], "pass_criteria_unmet": [],
                    },
                    "timestamp": "t0",
                },
                {  # latest attempt passed
                    "invocation": 1,
                    "result": {
                        "score": 0.9, "scores": {}, "reasons": {},
                        "feedback": None,
                        "pass_criteria_met": [], "pass_criteria_unmet": [],
                    },
                    "timestamp": "t1",
                },
            ],
        }
        cmd = await decide(state)
        assert cmd.goto == "b"
        assert cmd.update == {"completed_nodes": {"p1"}}
