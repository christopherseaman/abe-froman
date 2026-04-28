"""Unit tests for _make_evaluation_node from compile/nodes.py.

The Evaluation node is the second half of a gated phase pair: it reads
the upstream execution's output from state.phase_outputs, runs the gate,
walks routes (first-match + catch-all fallback), appends an
EvaluationRecord to state.evaluations[node_id], and writes the outcome
transitions (completed_phases / retries / failed_phases / errors).
"""

from __future__ import annotations

import pytest

from abe_froman.compile.nodes import _make_evaluation_node
from abe_froman.runtime.state import make_initial_state
from abe_froman.schema.models import (
    DimensionCheck,
    Phase,
    Evaluation,
    Settings,
    WorkflowConfig,
)


def _config_with(phase: Phase, **settings_kwargs) -> WorkflowConfig:
    return WorkflowConfig(
        name="T", version="1.0",
        phases=[phase],
        settings=Settings(**settings_kwargs),
    )


def _validator(tmp_path, name: str, score: str) -> str:
    path = tmp_path / name
    path.write_text(f"import sys\nsys.stdin.read()\nprint({score!r})\n")
    return str(path)


class TestEvaluationNodeBasics:
    @pytest.mark.asyncio
    async def test_writes_evaluation_record_on_pass(self, tmp_path):
        phase = Phase(
            id="p1", name="P1", prompt_file="t.md",
            quality_gate=Evaluation(validator=_validator(tmp_path, "v.py", "0.9"), threshold=0.8),
        )
        node = _make_evaluation_node(phase, _config_with(phase))
        state = make_initial_state(workdir=str(tmp_path))
        state["phase_outputs"] = {"p1": "output-for-eval"}
        update = await node(state)
        assert update["completed_phases"] == ["p1"]
        records = update["evaluations"]["p1"]
        assert len(records) == 1
        assert records[0]["invocation"] == 0
        assert records[0]["result"]["score"] == 0.9

    @pytest.mark.asyncio
    async def test_invocation_increments_with_retries_state(self, tmp_path):
        """State pre-populated with retries=2 yields invocation=2 on the record."""
        phase = Phase(
            id="p1", name="P1", prompt_file="t.md",
            quality_gate=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.9"),
                threshold=0.8,
            ),
        )
        node = _make_evaluation_node(phase, _config_with(phase, max_retries=5))
        state = make_initial_state(workdir=str(tmp_path))
        state["phase_outputs"] = {"p1": "out"}
        state["retries"] = {"p1": 2}
        update = await node(state)
        record = update["evaluations"]["p1"][0]
        assert record["invocation"] == 2

    @pytest.mark.asyncio
    async def test_retry_when_below_threshold_and_budget_left(self, tmp_path):
        phase = Phase(
            id="p1", name="P1", prompt_file="t.md",
            quality_gate=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.3"), threshold=0.8,
            ),
        )
        node = _make_evaluation_node(phase, _config_with(phase, max_retries=3))
        state = make_initial_state(workdir=str(tmp_path))
        state["phase_outputs"] = {"p1": "bad output"}
        update = await node(state)
        assert "completed_phases" not in update
        assert update["retries"] == {"p1": 1}

    @pytest.mark.asyncio
    async def test_fail_blocking_after_max_retries(self, tmp_path):
        phase = Phase(
            id="p1", name="P1", prompt_file="t.md",
            quality_gate=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.3"),
                threshold=0.8,
                blocking=True,
            ),
        )
        node = _make_evaluation_node(phase, _config_with(phase, max_retries=1))
        state = make_initial_state(workdir=str(tmp_path))
        state["phase_outputs"] = {"p1": "bad"}
        state["retries"] = {"p1": 1}
        update = await node(state)
        assert update["failed_phases"] == ["p1"]
        assert any("Evaluation failed" in e["error"] for e in update["errors"])

    @pytest.mark.asyncio
    async def test_warn_continue_after_max_retries_nonblocking(self, tmp_path):
        phase = Phase(
            id="p1", name="P1", prompt_file="t.md",
            quality_gate=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.3"),
                threshold=0.8,
                blocking=False,
            ),
        )
        node = _make_evaluation_node(phase, _config_with(phase, max_retries=1))
        state = make_initial_state(workdir=str(tmp_path))
        state["phase_outputs"] = {"p1": "bad"}
        state["retries"] = {"p1": 1}
        update = await node(state)
        assert update["completed_phases"] == ["p1"]
        assert any("non-blocking" in e["error"] for e in update["errors"])


class TestEvaluationNodeHistoryAndDims:
    @pytest.mark.asyncio
    async def test_history_flows_into_record_via_state(self, tmp_path):
        """Prior records in state.evaluations reach the route walker via
        build_eval_context (the 'history' field). Criterion semantics today
        don't reference history beyond `invocation`, but the record written
        this call appends to the pre-existing list via the reducer."""
        phase = Phase(
            id="p1", name="P1", prompt_file="t.md",
            quality_gate=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.9"), threshold=0.8,
            ),
        )
        node = _make_evaluation_node(phase, _config_with(phase))
        state = make_initial_state(workdir=str(tmp_path))
        state["phase_outputs"] = {"p1": "out"}
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
        phase = Phase(
            id="p1", name="P1", prompt_file="t.md",
            quality_gate=Evaluation(
                validator=str(validator),
                dimensions=[
                    DimensionCheck(field="rigor", min=0.7),
                    DimensionCheck(field="humor", min=0.5),
                ],
            ),
        )
        node = _make_evaluation_node(phase, _config_with(phase))
        state = make_initial_state(workdir=str(tmp_path))
        state["phase_outputs"] = {"p1": "out"}
        update = await node(state)
        scores = update["evaluations"]["p1"][0]["result"]["scores"]
        assert scores["rigor"] == 0.9
        assert scores["humor"] == 0.7


class TestEvaluationNodeDryRun:
    @pytest.mark.asyncio
    async def test_dry_run_synthesizes_pass(self, tmp_path):
        phase = Phase(
            id="p1", name="P1", prompt_file="t.md",
            quality_gate=Evaluation(validator="v.py", threshold=0.8),
        )
        node = _make_evaluation_node(phase, _config_with(phase))
        state = make_initial_state(workdir=str(tmp_path), dry_run=True)
        update = await node(state)
        assert update["completed_phases"] == ["p1"]
        record = update["evaluations"]["p1"][0]
        assert record["result"]["score"] == 1.0
        assert record["result"]["feedback"] == "[dry-run]"


class TestEvaluationNodeSkips:
    @pytest.mark.asyncio
    async def test_skips_when_already_completed(self, tmp_path):
        phase = Phase(
            id="p1", name="P1", prompt_file="t.md",
            quality_gate=Evaluation(validator="v.py", threshold=0.8),
        )
        node = _make_evaluation_node(phase, _config_with(phase))
        state = make_initial_state(workdir=str(tmp_path))
        state["completed_phases"] = ["p1"]
        assert await node(state) == {}

    @pytest.mark.asyncio
    async def test_skips_when_already_failed(self, tmp_path):
        phase = Phase(
            id="p1", name="P1", prompt_file="t.md",
            quality_gate=Evaluation(validator="v.py", threshold=0.8),
        )
        node = _make_evaluation_node(phase, _config_with(phase))
        state = make_initial_state(workdir=str(tmp_path))
        state["failed_phases"] = ["p1"]
        assert await node(state) == {}


class TestEvaluationNodeSubphaseResolver:
    @pytest.mark.asyncio
    async def test_resolver_keys_off_subphase_item(self, tmp_path):
        """Subphase-style resolver derives node_id from _subphase_item,
        so per-branch evaluation writes to distinct keys."""
        phase = Phase(
            id="_eval_sub_p", name="sub gate", prompt_file="t.md",
            quality_gate=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.9"), threshold=0.8,
            ),
        )
        def resolve(state):
            item = state.get("_subphase_item", {})
            return f"p::{item.get('id', '?')}"
        node = _make_evaluation_node(phase, _config_with(phase), node_id_resolver=resolve)
        state = make_initial_state(workdir=str(tmp_path))
        state["phase_outputs"] = {"p::x": "out-x"}
        state["_subphase_item"] = {"id": "x"}
        update = await node(state)
        assert update["completed_phases"] == ["p::x"]
        assert "p::x" in update["evaluations"]
        assert update["evaluations"]["p::x"][0]["result"]["score"] == 0.9
