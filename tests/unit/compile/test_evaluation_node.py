"""Unit tests for _make_evaluation_node from compile/nodes.py.

After the Stage-5d Eval/Decision split, the Evaluation node's only
job is to produce an EvaluationRecord (writing
state.evaluations[node_id]). Outcome classification (pass/retry/
fail) and routing live in the Decision node — see
test_decision_node.py.

Tests here assert the eval-only contract:
  - On success: returns {"evaluations": {key: [record]}} only.
  - Does NOT write completed_nodes / failed_nodes / retries / errors
    (those moved to the Decision node).
  - Skip-when-already-failed guard.
  - Defer-when-upstream-output-absent guard.
  - Dry-run synthesizes a passing record (Decision node handles the
    completed_nodes write on its dry-run path).
"""

from __future__ import annotations

import pytest

from sqrlly.compile.nodes import _make_evaluation_node
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import (
    DimensionCheck,
    Execute,
    Node,
    Evaluation,
    Settings,
    Graph,
)


def _config_with(node: Node, **settings_kwargs) -> Graph:
    return Graph(
        name="T", version="1.0",
        nodes=[node],
        settings=Settings(**settings_kwargs),
    )


def _validator(tmp_path, name: str, score: str) -> str:
    path = tmp_path / name
    path.write_text(f"import sys\nsys.stdin.read()\nprint({score!r})\n")
    return str(path)


def _exec(url: str = "t.md") -> Execute:
    """Stage-5b prompt-style execute block (URL points to a .md file)."""
    return Execute(url=url)


class TestEvaluationNodeBasics:
    @pytest.mark.asyncio
    async def test_writes_evaluation_record_only(self, tmp_path):
        """Eval body returns {"evaluations": {...}} and nothing else.
        Outcome state (completed/failed/retries) writes moved to Decision."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(validator=_validator(tmp_path, "v.py", "0.9"), threshold=0.8),
        )
        node = _make_evaluation_node(node, _config_with(node))
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "output-for-eval"}
        update = await node(state)

        assert "completed_nodes" not in update
        assert "failed_nodes" not in update
        assert "retries" not in update
        assert "errors" not in update

        records = update["evaluations"]["p1"]
        assert len(records) == 1
        assert records[0]["invocation"] == 0
        assert records[0]["result"]["score"] == 0.9

    @pytest.mark.asyncio
    async def test_invocation_increments_with_retries_state(self, tmp_path):
        """State pre-populated with retries=2 yields invocation=2 on the record."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.9"),
                threshold=0.8,
            ),
        )
        node = _make_evaluation_node(node, _config_with(node, max_retries=5))
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "out"}
        state["retries"] = {"p1": 2}
        update = await node(state)
        record = update["evaluations"]["p1"][0]
        assert record["invocation"] == 2

    @pytest.mark.asyncio
    async def test_low_score_still_writes_record_only(self, tmp_path):
        """Below-threshold result writes a record (with the low score);
        the retry decision is made later by the Decision node."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.3"), threshold=0.8,
            ),
        )
        node = _make_evaluation_node(node, _config_with(node, max_retries=3))
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "bad output"}
        update = await node(state)

        # No outcome-state writes — those are the Decision node's job.
        assert "retries" not in update
        assert "completed_nodes" not in update
        assert "failed_nodes" not in update

        record = update["evaluations"]["p1"][0]
        assert record["result"]["score"] == 0.3


class TestEvaluationNodeHistoryAndDims:
    @pytest.mark.asyncio
    async def test_history_flows_into_record_via_state(self, tmp_path):
        """Prior records in state.evaluations reach the route walker via
        build_eval_context (the 'history' field). Criterion semantics today
        don't reference history beyond `invocation`, but the record written
        this call appends to the pre-existing list via the reducer."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.9"), threshold=0.8,
            ),
        )
        node = _make_evaluation_node(node, _config_with(node))
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "out"}
        state["evaluations"] = {
            "p1": [{"invocation": 0, "result": {"score": 0.1}, "timestamp": "t0"}],
        }
        update = await node(state)
        # New record appended (the reducer handles the merge; the update
        # only carries the NEW record, not the history).
        assert len(update["evaluations"]["p1"]) == 1
        assert update["evaluations"]["p1"][0]["invocation"] == 0

    @pytest.mark.asyncio
    async def test_multidim_scores_populated(self, tmp_path):
        # Script gates emit dim scores as top-level numeric fields (see
        # runtime/gates.py::_parse_evaluation_output), not nested in "scores".
        validator = tmp_path / "dim.py"
        validator.write_text(
            'import json, sys\nsys.stdin.read()\n'
            'print(json.dumps({'
            '"score": 0.0, "rigor": 0.9, "humor": 0.7'
            '}))\n'
        )
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=str(validator),
                dimensions=[
                    DimensionCheck(field="rigor", min=0.7),
                    DimensionCheck(field="humor", min=0.5),
                ],
            ),
        )
        node = _make_evaluation_node(node, _config_with(node))
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "out"}
        update = await node(state)
        scores = update["evaluations"]["p1"][0]["result"]["scores"]
        assert scores["rigor"] == 0.9
        assert scores["humor"] == 0.7


class TestEvaluationNodeDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_synthesizes_passing_record(self, tmp_path):
        """Dry-run path emits a synthetic [dry-run] record. The Decision
        node picks up this record and routes pass (writing
        completed_nodes itself) — see test_decision_node.py::
        test_dry_run_synthesizes_pass."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(validator="v.py", threshold=0.8),
        )
        node = _make_evaluation_node(node, _config_with(node))
        state = make_initial_state(workdir=str(tmp_path), dry_run=True)
        update = await node(state)

        # No outcome-state writes from eval — the Decision node handles
        # routing and the completed_nodes write under dry_run.
        assert "completed_nodes" not in update

        record = update["evaluations"]["p1"][0]
        assert record["result"]["score"] == 1.0
        assert record["result"]["feedback"] == "[dry-run]"


class TestEvaluationNodeSkips:
    @pytest.mark.asyncio
    async def test_evaluates_even_when_completed_in_state(self, tmp_path):
        """Eval node body has no completed_nodes guard — that check
        moved to the Decision node. The eval body re-runs the
        validator if reached again, which is correct for the goto-re-fire
        case (a node hit again via Command(goto=...) deliberately re-
        evaluates its output)."""
        node = Node(
            id="p1", name="P1",
            execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.9"),
                threshold=0.8,
            ),
        )
        node = _make_evaluation_node(node, _config_with(node))
        state = make_initial_state(workdir=str(tmp_path))
        state["completed_nodes"] = ["p1"]
        state["node_outputs"] = {"p1": "out"}
        update = await node(state)
        # Eval body still ran and wrote a record.
        assert "p1" in update.get("evaluations", {})

    @pytest.mark.asyncio
    async def test_skips_when_already_failed(self, tmp_path):
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(validator="v.py", threshold=0.8),
        )
        node = _make_evaluation_node(node, _config_with(node))
        state = make_initial_state(workdir=str(tmp_path))
        state["failed_nodes"] = ["p1"]
        assert await node(state) == {}

    @pytest.mark.asyncio
    async def test_defers_when_upstream_output_absent(self, tmp_path):
        """The eval node defers (returns {}) when its upstream Execution
        node hasn't written node_outputs[node_id] yet.

        LangGraph evaluates fan-out conditional edges pre-emptively: the
        router from a fan-out parent fires when an UPSTREAM-of-parent
        completes (not when the parent itself runs), routes to _final_*
        via 'no_items', and the static edge _final_* → _eval__final_*
        triggers this eval against an empty state. Without this guard,
        the gate scores empty content as 0.0/0.0 and burns the retry
        budget before the real fan-out children settle.

        Key-absence (not empty-value) is the signal — a join-mode
        Execute legitimately writes "" and must still be evaluated.
        """
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(validator="v.py", threshold=0.8),
        )
        node_fn = _make_evaluation_node(node, _config_with(node))
        state = make_initial_state(workdir=str(tmp_path))
        # node_outputs is empty — upstream hasn't run.
        result = await node_fn(state)
        assert result == {}, (
            f"eval should defer when upstream output absent; got {result}"
        )

    @pytest.mark.asyncio
    async def test_evaluates_when_upstream_writes_empty_string(self, tmp_path):
        """Companion to defers_when_upstream_output_absent: when the
        upstream wrote "" (e.g. join-mode Execute's no-op output), eval
        MUST proceed — empty value is distinct from absent key.
        """
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.9"), threshold=0.8,
            ),
        )
        node_fn = _make_evaluation_node(node, _config_with(node))
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": ""}  # legitimate empty output (e.g. join)
        result = await node_fn(state)
        # eval ran: a record was written. Decision node will route on
        # the next super-step.
        assert "p1" in result.get("evaluations", {})


class TestEvaluationNodeSubphaseResolver:
    @pytest.mark.asyncio
    async def test_resolver_keys_off_subphase_item(self, tmp_path):
        """Subphase-style resolver derives node_id from _fan_out_item,
        so per-branch evaluation writes to distinct keys."""
        node = Node(
            id="_eval_sub_p", name="sub gate", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.9"), threshold=0.8,
            ),
        )
        def resolve(state):
            item = state.get("_fan_out_item", {})
            return f"p::{item.get('id', '?')}"
        node = _make_evaluation_node(node, _config_with(node), node_id_resolver=resolve)
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p::x": "out-x"}
        state["_fan_out_item"] = {"id": "x"}
        update = await node(state)
        # Eval-only: no completed_nodes write here; the Decision node
        # (test_decision_node.py) handles that side via the same
        # resolver shape.
        assert "p::x" in update["evaluations"]
        assert update["evaluations"]["p::x"][0]["result"]["score"] == 0.9
