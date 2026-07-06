"""Unit tests for _make_gate_node from compile/nodes.py.

The gate node is ONE Command-returning node that replaces the former
Eval/Decision pair (and the combined eval+decide factory). It runs the
validator, writes the EvaluationRecord, classifies the outcome, and
returns a ``Command(update=..., goto=...)`` in a single body — no
separate record-then-route super-steps.

Gate-body guard order (each guard reproduces a pinned behavior):

  1. node_id in failed_nodes            -> Command(goto=END)
  2. _resume_skip carrying node_id      -> Command(goto=pass_goto), no update
  3. dry_run                            -> Command(update={record + completed},
                                                    goto=pass_goto)
  4. node_outputs[node_id] absent       -> plain {} (defer, NOT a Command)
  5. run validator; infra failure       -> Command(update=failure, goto=END)
  6. node_id in completed_nodes         -> Command(update=record_only,
                                                    goto=pass_goto)
  7. classify:
       pass          -> Command(update=record+completed, goto=pass_goto)
       retry         -> Command(update=record+retries+1, goto=exec_id)
       fail_blocking -> Command(update=record+failed+errors, goto=END)
       warn_continue -> Command(update=record+completed+errors, goto=pass_goto)

These pins are ported verbatim from the deleted test_evaluation_node.py
(13) and test_decision_node.py (12) — same values, one node.
"""

from __future__ import annotations

import pytest
from langgraph.graph import END
from langgraph.types import Command

from sqrlly.compile.nodes import _make_gate_node
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import (
    DimensionCheck,
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


def _validator(tmp_path, name: str, score: str) -> str:
    path = tmp_path / name
    path.write_text(f"import sys\nsys.stdin.read()\nprint({score!r})\n")
    return str(path)


def _exec(url: str = "t.md") -> Execute:
    return Execute(url=url)


def _gate(node, config, *, exec_id="p1", pass_targets=("b",), executor=None):
    return _make_gate_node(
        node, config, executor,
        exec_id=exec_id, pass_targets=list(pass_targets),
    )


def _gated_node(retry_threshold: float = 0.8, **eval_kwargs) -> Node:
    return Node(
        id="p1", name="P1",
        execute=Execute(url="t.md"),
        evaluation=Evaluation(
            validator="v.py", threshold=retry_threshold, **eval_kwargs,
        ),
    )


def _state_with_record(score: float, retries: int = 0) -> dict:
    """State pre-seeded with a completed node + one record, so the gate's
    step-6 already-completed branch emits record_only + pass goto."""
    state = make_initial_state()
    state["completed_nodes"] = {"p1"}
    state["node_outputs"] = {"p1": "output-for-gate"}
    state["evaluations"] = {
        "p1": [{
            "invocation": retries,
            "result": {
                "score": score, "scores": {}, "reasons": {},
                "feedback": None,
                "pass_criteria_met": [], "pass_criteria_unmet": [],
            },
            "timestamp": "t",
        }]
    }
    if retries:
        state["retries"] = {"p1": retries}
    return state


# ---------------------------------------------------------------------------
# Record-production content (ported from test_evaluation_node.py)
# ---------------------------------------------------------------------------


class TestGateWritesRecord:
    @pytest.mark.asyncio
    async def test_pass_writes_record_and_completed(self, tmp_path):
        """A passing script gate writes the record AND completed_nodes in
        one Command — the collapse's core inversion vs the split eval body."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.9"), threshold=0.8,
            ),
        )
        gate = _gate(node, _config_with(node), pass_targets=["b"])
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "output-for-eval"}
        cmd = await gate(state)

        assert isinstance(cmd, Command)
        assert cmd.goto == "b"
        records = cmd.update["evaluations"]["p1"]
        assert len(records) == 1
        assert records[0]["invocation"] == 0
        assert records[0]["result"]["score"] == 0.9
        assert cmd.update["completed_nodes"] == {"p1"}

    @pytest.mark.asyncio
    async def test_invocation_increments_with_retries_state(self, tmp_path):
        """State pre-populated with retries=2 yields invocation=2 on the record."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.9"), threshold=0.8,
            ),
        )
        gate = _gate(node, _config_with(node, max_retries=5), pass_targets=["b"])
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "out"}
        state["retries"] = {"p1": 2}
        cmd = await gate(state)
        record = cmd.update["evaluations"]["p1"][0]
        assert record["invocation"] == 2

    @pytest.mark.asyncio
    async def test_history_flows_into_record_via_state(self, tmp_path):
        """A prior record in state.evaluations + this run's new record; the
        Command update carries only the NEW record (reducer appends it)."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.9"), threshold=0.8,
            ),
        )
        gate = _gate(node, _config_with(node), pass_targets=["b"])
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "out"}
        state["evaluations"] = {
            "p1": [{"invocation": 0, "result": {"score": 0.1}, "timestamp": "t0"}],
        }
        cmd = await gate(state)
        # Update carries the new record only (not the prior history).
        assert len(cmd.update["evaluations"]["p1"]) == 1
        assert cmd.update["evaluations"]["p1"][0]["invocation"] == 0

    @pytest.mark.asyncio
    async def test_multidim_scores_populated(self, tmp_path):
        # Script gates emit dim scores as top-level numeric fields.
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
        gate = _gate(node, _config_with(node), pass_targets=["b"])
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "out"}
        cmd = await gate(state)
        scores = cmd.update["evaluations"]["p1"][0]["result"]["scores"]
        assert scores["rigor"] == 0.9
        assert scores["humor"] == 0.7


# ---------------------------------------------------------------------------
# Classification / routing (ported from test_decision_node.py)
# ---------------------------------------------------------------------------


class TestGatePass:
    @pytest.mark.asyncio
    async def test_pass_single_target_returns_command_goto_target(self, tmp_path):
        """Score above threshold → Command(goto=target) with record+completed."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.9"), threshold=0.8,
            ),
        )
        gate = _gate(
            node, _config_with(node, max_retries=2),
            exec_id="p1", pass_targets=["b"],
        )
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "out"}
        cmd = await gate(state)
        assert isinstance(cmd, Command)
        assert cmd.goto == "b"
        assert cmd.update["completed_nodes"] == {"p1"}

    @pytest.mark.asyncio
    async def test_pass_multiple_targets_fans_out_via_list(self, tmp_path):
        """Multi-pass targets → goto receives a list (LangGraph fans out)."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.9"), threshold=0.8,
            ),
        )
        gate = _gate(
            node, _config_with(node, max_retries=2),
            exec_id="p1", pass_targets=["b", "c"],
        )
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "out"}
        cmd = await gate(state)
        assert cmd.goto == ["b", "c"]

    @pytest.mark.asyncio
    async def test_pass_terminal_gated_node_routes_to_END(self, tmp_path):
        """Terminal gated node has pass_targets=[END]."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.9"), threshold=0.8,
            ),
        )
        gate = _gate(
            node, _config_with(node, max_retries=2),
            exec_id="p1", pass_targets=[END],
        )
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "out"}
        cmd = await gate(state)
        assert cmd.goto == END


class TestGateRetry:
    @pytest.mark.asyncio
    async def test_retry_returns_command_goto_exec(self, tmp_path):
        """Score below threshold + budget left → Command(goto=exec_id)."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.3"), threshold=0.8,
            ),
        )
        gate = _gate(
            node, _config_with(node, max_retries=3),
            exec_id="p1", pass_targets=["b"],
        )
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "bad"}
        cmd = await gate(state)
        assert isinstance(cmd, Command)
        assert cmd.goto == "p1"
        assert cmd.update["retries"] == {"p1": 1}
        # The record is written on the retry path too (needed for
        # inject_retry_reason on the re-fired executor), and it carries the
        # actual below-threshold score, not an empty/placeholder payload.
        assert cmd.update["evaluations"]["p1"][0]["result"]["score"] == 0.3

    @pytest.mark.asyncio
    async def test_retry_goto_matches_parameterized_exec_id(self, tmp_path):
        """A gated fan-out final's retry goes to its synthetic exec_id, not
        node.id — the gate factory keeps the retry target parameterized."""
        node = Node(
            id="_final_p_f0", name="F0", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.3"), threshold=0.8,
            ),
        )
        gate = _gate(
            node, _config_with(node, max_retries=3),
            exec_id="_final_p_f0", pass_targets=["nxt"],
        )
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"_final_p_f0": "bad"}
        cmd = await gate(state)
        assert cmd.goto == "_final_p_f0"
        assert cmd.update["retries"] == {"_final_p_f0": 1}

    @pytest.mark.asyncio
    async def test_retry_increments_existing_counter(self, tmp_path):
        """retries=2 in state → bumped to 3 in update."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.3"), threshold=0.8,
            ),
        )
        gate = _gate(
            node, _config_with(node, max_retries=5),
            exec_id="p1", pass_targets=["b"],
        )
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "bad"}
        state["retries"] = {"p1": 2}
        cmd = await gate(state)
        assert cmd.update["retries"] == {"p1": 3}


class TestGateFail:
    @pytest.mark.asyncio
    async def test_fail_blocking_after_max_retries(self, tmp_path):
        """Below threshold + budget exhausted + blocking=True → goto END,
        failed_nodes set, errors populated with 'Evaluation failed'."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.3"),
                threshold=0.8, blocking=True,
            ),
        )
        gate = _gate(
            node, _config_with(node, max_retries=1),
            exec_id="p1", pass_targets=["b"],
        )
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "bad"}
        state["retries"] = {"p1": 1}
        cmd = await gate(state)
        assert cmd.goto == END
        assert cmd.update["failed_nodes"] == {"p1"}
        assert any(
            "Evaluation failed" in e["error"] for e in cmd.update["errors"]
        )
        # Record still written on the fail path.
        assert "p1" in cmd.update["evaluations"]

    @pytest.mark.asyncio
    async def test_warn_continue_routes_pass_records_error(self, tmp_path):
        """Below threshold + budget exhausted + blocking=False → routes as
        pass (completed_nodes set) but records a non-blocking error."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.3"),
                threshold=0.8, blocking=False,
            ),
        )
        gate = _gate(
            node, _config_with(node, max_retries=1),
            exec_id="p1", pass_targets=["b"],
        )
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "bad"}
        state["retries"] = {"p1": 1}
        cmd = await gate(state)
        assert cmd.goto == "b"
        assert cmd.update["completed_nodes"] == {"p1"}
        assert any(
            "non-blocking" in e["error"] for e in cmd.update["errors"]
        )


class TestGateReadsLatestRecord:
    @pytest.mark.asyncio
    async def test_gate_classifies_on_latest_record(self, tmp_path):
        """With prior failing records in history, the gate runs a fresh
        validator and classifies on THIS run's score (a passing one)."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.9"), threshold=0.8,
            ),
        )
        gate = _gate(
            node, _config_with(node, max_retries=3),
            exec_id="p1", pass_targets=["b"],
        )
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "out"}
        state["evaluations"] = {
            "p1": [{
                "invocation": 0,
                "result": {
                    "score": 0.3, "scores": {}, "reasons": {},
                    "feedback": None,
                    "pass_criteria_met": [], "pass_criteria_unmet": [],
                },
                "timestamp": "t0",
            }],
        }
        cmd = await gate(state)
        assert cmd.goto == "b"
        assert cmd.update["completed_nodes"] == {"p1"}


# ---------------------------------------------------------------------------
# Guards (steps 1-6)
# ---------------------------------------------------------------------------


class TestGateGuards:
    @pytest.mark.asyncio
    async def test_already_failed_routes_to_END(self, tmp_path):
        """Step 1: failed_nodes carrying the id → Command(goto=END)."""
        node = _gated_node()
        gate = _gate(node, _config_with(node), pass_targets=["b"])
        state = make_initial_state(workdir=str(tmp_path), failed_nodes={"p1"})
        cmd = await gate(state)
        assert cmd.goto == END

    @pytest.mark.asyncio
    async def test_resume_skip_routes_pass_no_update_no_validator(self):
        """Step 2: _resume_skip carrying the id → Command(goto=pass_goto)
        with NO update and NO validator call (executor=None with an LLM
        validator would raise if the validator ran — proves it did not)."""
        # An LLM (.md) validator so a validator call would raise on backend=None.
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(validator="check.md", threshold=0.8),
        )
        gate = _gate(node, _config_with(node), pass_targets=["b"], executor=None)
        state = make_initial_state(
            node_outputs={"p1": "prior output"}, _resume_skip={"p1"},
        )
        cmd = await gate(state)
        assert isinstance(cmd, Command)
        assert cmd.goto == "b"
        assert cmd.update is None or cmd.update == {}

    @pytest.mark.asyncio
    async def test_resume_skip_terminal_routes_END(self):
        """Step 2 with a terminal gated node (pass_targets=[END])."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(validator="check.md", threshold=0.8),
        )
        gate = _gate(node, _config_with(node), pass_targets=[END], executor=None)
        state = make_initial_state(
            node_outputs={"p1": "prior output"}, _resume_skip={"p1"},
        )
        cmd = await gate(state)
        assert cmd.goto == END

    @pytest.mark.asyncio
    async def test_dry_run_synthesizes_pass_with_record(self):
        """Step 3: dry_run → Command(goto=pass_goto) writing BOTH the
        synthetic [dry-run] record (score 1.0) AND completed_nodes."""
        node = _gated_node()
        gate = _gate(node, _config_with(node), pass_targets=["b"])
        state = make_initial_state(dry_run=True)
        cmd = await gate(state)
        assert cmd.goto == "b"
        assert cmd.update["completed_nodes"] == {"p1"}
        record = cmd.update["evaluations"]["p1"][0]
        assert record["invocation"] == 0
        assert record["result"]["score"] == 1.0
        assert record["result"]["feedback"] == "[dry-run]"

    @pytest.mark.asyncio
    async def test_defers_when_upstream_output_absent(self, tmp_path):
        """Step 4: node_outputs[node_id] absent → plain {} (NOT a Command),
        so LangGraph re-fires the gate when upstream writes."""
        node = _gated_node()
        gate = _gate(node, _config_with(node), pass_targets=["b"])
        state = make_initial_state(workdir=str(tmp_path))
        result = await gate(state)
        assert result == {}, f"gate should defer on absent output; got {result}"

    @pytest.mark.asyncio
    async def test_evaluates_when_upstream_writes_empty_string(self, tmp_path):
        """Step 4 companion: an empty-string output ("" from a join node)
        counts as PRESENT — the gate proceeds and writes a record."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.9"), threshold=0.8,
            ),
        )
        gate = _gate(node, _config_with(node), pass_targets=["b"])
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": ""}
        cmd = await gate(state)
        assert "p1" in cmd.update.get("evaluations", {})

    @pytest.mark.asyncio
    async def test_already_completed_routes_pass_after_validator(self, tmp_path):
        """Step 6: node_id in completed_nodes → the validator STILL runs
        (record written), verdict deliberately not enforced, goto=pass."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(
                validator=_validator(tmp_path, "v.py", "0.3"),  # would FAIL
                threshold=0.8,
            ),
        )
        gate = _gate(node, _config_with(node), pass_targets=["b"])
        state = _state_with_record(score=0.3)  # completed + prior record
        # Overwrite the record's location with real output so the validator
        # actually runs in tmp_path.
        state["workdir"] = str(tmp_path)
        cmd = await gate(state)
        # Routes pass despite the low fresh score (verdict not enforced when
        # already completed).
        assert cmd.goto == "b"
        # Record from THIS run is written (validator ran).
        assert "p1" in cmd.update["evaluations"]
        # No completed_nodes write in the record-only update (already there).
        assert "completed_nodes" not in cmd.update

    @pytest.mark.asyncio
    async def test_not_skipped_when_resume_skip_absent(self):
        """No _resume_skip in state: the step-2 guard must NOT fire, so the
        gate proceeds into validation. With no backend the LLM validator
        raises ValueError — proving the guard didn't short-circuit."""
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(validator="check.md", threshold=0.8),
        )
        gate = _gate(node, _config_with(node), pass_targets=["b"], executor=None)
        state = make_initial_state(node_outputs={"p1": "prior output"})
        with pytest.raises(ValueError, match="PromptBackend"):
            await gate(state)


class TestGateInfraFailure:
    @pytest.mark.asyncio
    async def test_eval_timeout_routes_END_no_record(self, tmp_path):
        """Step 5: an eval timeout is an infra failure → Command(update=
        failure, goto=END) with NO evaluations record (failed_nodes+errors
        only)."""
        # A validator that sleeps longer than the node timeout.
        validator = tmp_path / "slow.py"
        validator.write_text("import time, sys\nsys.stdin.read()\ntime.sleep(5)\nprint('0.9')\n")
        node = Node(
            id="p1", name="P1", execute=_exec(),
            evaluation=Evaluation(validator=str(validator), threshold=0.8),
            timeout=1,
        )
        gate = _gate(node, _config_with(node), pass_targets=["b"])
        state = make_initial_state(workdir=str(tmp_path))
        state["node_outputs"] = {"p1": "out"}
        cmd = await gate(state)
        assert isinstance(cmd, Command)
        assert cmd.goto == END
        assert cmd.update["failed_nodes"] == {"p1"}
        assert "evaluations" not in cmd.update
