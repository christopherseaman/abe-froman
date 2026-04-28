"""Fixture-based unit tests for extracted node helpers in compile/nodes.py.

Known-good AND known-bad pairs for each helper, with @pytest.mark.parametrize
for routing tables where applicable.
"""

import asyncio

import pytest

from abe_froman.compile.nodes import (
    _get_retry_delay,
    assemble_success_update,
    build_context,
    build_evaluation_outcome_update,
    check_dep_failed,
    check_dry_run,
    classify_evaluation_outcome,
    execute_with_timeout,
    inject_retry_reason,
    make_failure_update,
)
from abe_froman.runtime.gates import EvaluationResult
from abe_froman.runtime.result import ExecutionResult
from abe_froman.schema.models import Node, Evaluation


def _phase(id="p1", depends_on=None, evaluation=None, **kw):
    return Node(id=id, name=id, depends_on=depends_on or [], evaluation=evaluation, **kw)


def _gate(threshold=0.8, blocking=True, max_retries=None):
    return Evaluation(validator="v.py", threshold=threshold, blocking=blocking, max_retries=max_retries)


class TestGetRetryDelay:
    def test_empty_backoff(self):
        assert _get_retry_delay(1, []) == 0.0

    def test_first_attempt(self):
        assert _get_retry_delay(1, [10, 30, 60]) == 10

    def test_second_attempt(self):
        assert _get_retry_delay(2, [10, 30, 60]) == 30

    def test_clamps_to_last(self):
        assert _get_retry_delay(5, [10, 30, 60]) == 60

    def test_single_value(self):
        assert _get_retry_delay(3, [5]) == 5


class TestCheckDepFailed:
    def test_dependency_failed(self):
        node = _phase(depends_on=["dep1"])
        state = {"failed_nodes": ["dep1"]}
        result = check_dep_failed(node, state)
        assert result["failed_nodes"] == ["p1"]
        assert "dependency 'dep1' failed" in result["errors"][0]["error"]

    def test_no_failed_deps(self):
        node = _phase(depends_on=["dep1"])
        state = {"failed_nodes": []}
        assert check_dep_failed(node, state) is None

    def test_no_deps(self):
        node = _phase(depends_on=[])
        state = {"failed_nodes": ["something"]}
        assert check_dep_failed(node, state) is None

    def test_unrelated_failure(self):
        node = _phase(depends_on=["dep1"])
        state = {"failed_nodes": ["dep2"]}
        assert check_dep_failed(node, state) is None


class TestCheckDryRun:
    def test_dry_run_without_gate(self):
        state = {"dry_run": True}
        result = check_dry_run(_phase(), state)
        assert result["completed_nodes"] == ["p1"]
        assert "[dry-run]" in result["node_outputs"]["p1"]
        assert "gate_scores" not in result

    def test_dry_run_with_gate(self):
        """Gated nodes in dry-run: node node writes node_outputs only; the
        downstream Evaluation node synthesizes a pass EvaluationRecord."""
        node = _phase(evaluation=_gate())
        state = {"dry_run": True}
        result = check_dry_run(node, state)
        assert "[dry-run]" in result["node_outputs"]["p1"]
        assert "completed_nodes" not in result

    def test_not_dry_run(self):
        assert check_dry_run(_phase(), {"dry_run": False}) is None

    def test_missing_dry_run_key(self):
        assert check_dry_run(_phase(), {}) is None



class TestBuildContext:
    def test_with_matching_deps(self):
        node = _phase(depends_on=["a"])
        state = {
            "node_outputs": {"a": "out-a"},
            "node_structured_outputs": {"a": {"k": "v"}},
        }
        ctx = build_context(node, state)
        assert ctx == {"a": "out-a", "a_structured": {"k": "v"}}

    def test_no_matching_deps(self):
        node = _phase(depends_on=["b"])
        state = {"node_outputs": {"a": "out-a"}}
        assert build_context(node, state) == {}

    def test_empty_deps(self):
        assert build_context(_phase(), {"node_outputs": {"a": "x"}}) == {}

    def test_output_only_no_structured(self):
        node = _phase(depends_on=["a"])
        state = {"node_outputs": {"a": "text"}}
        assert build_context(node, state) == {"a": "text"}

    def test_projects_dep_worktree_path(self):
        """`{{dep_worktree}}` is populated from state.node_worktrees."""
        node = _phase(depends_on=["a"])
        state = {
            "node_outputs": {"a": "out"},
            "node_worktrees": {"a": "/tmp/wt-a"},
        }
        ctx = build_context(node, state)
        assert ctx["a_worktree"] == "/tmp/wt-a"

    def test_no_worktree_means_no_worktree_key(self):
        node = _phase(depends_on=["a"])
        state = {"node_outputs": {"a": "out"}, "node_worktrees": {}}
        ctx = build_context(node, state)
        assert "a_worktree" not in ctx

    def test_projects_subphase_aggregations(self):
        """Downstream context synthesizes `{dep}_subphases` + worktrees from state.

        Stage 2b moved aggregation out of the final-node wrapper; any
        node depending on a dynamic parent now sees the same aggregate
        via build_context's state projection.
        """
        node = _phase(depends_on=["parent"])
        state = {
            "node_outputs": {"parent": "parent-out"},
            "child_outputs": {"parent::a": "x", "parent::b": "y"},
            "node_worktrees": {
                "parent::a": "/tmp/wt-a",
                "parent::b": "/tmp/wt-b",
            },
        }
        ctx = build_context(node, state)
        import json as _json
        assert _json.loads(ctx["parent_subphases"]) == {
            "parent::a": "x",
            "parent::b": "y",
        }
        assert sorted(_json.loads(ctx["parent_subphase_worktrees"])) == [
            "/tmp/wt-a",
            "/tmp/wt-b",
        ]

    def test_no_subphase_aggregations_when_absent(self):
        """Phases with no dynamic parent in deps get no aggregate keys."""
        node = _phase(depends_on=["normal"])
        state = {
            "node_outputs": {"normal": "out"},
            "child_outputs": {},
            "node_worktrees": {},
        }
        ctx = build_context(node, state)
        assert "normal_subphases" not in ctx
        assert "normal_subphase_worktrees" not in ctx


class TestInjectRetryReason:
    def test_first_attempt_no_injection(self):
        ctx = inject_retry_reason({}, _phase(evaluation=_gate()), {"retries": {}}, 3)
        assert "_retry_reason" not in ctx

    def test_retry_injects_reason(self):
        node = _phase(evaluation=_gate(threshold=0.8))
        state = {
            "retries": {"p1": 1},
            "evaluations": {
                "p1": [{
                    "invocation": 0,
                    "result": {"score": 0.5, "scores": {}, "feedback": None,
                               "pass_criteria_met": [], "pass_criteria_unmet": []},
                    "timestamp": "t",
                }],
            },
        }
        ctx = inject_retry_reason({}, node, state, 3)
        assert "score=0.50" in ctx["_retry_reason"]
        assert "threshold=0.8" in ctx["_retry_reason"]
        assert "retry 1 of 3" in ctx["_retry_reason"]

    def test_no_gate_no_injection(self):
        state = {"retries": {"p1": 1}}
        ctx = inject_retry_reason({}, _phase(), state, 3)
        assert "_retry_reason" not in ctx

    def test_includes_feedback_narrative(self):
        """Structured gate feedback appears in the retry reason."""
        node = _phase(evaluation=_gate())
        state = {
            "retries": {"p1": 1},
            "evaluations": {
                "p1": [{
                    "invocation": 0,
                    "result": {
                        "score": 0.4,
                        "scores": {},
                        "feedback": "missing docstring",
                        "pass_criteria_met": [],
                        "pass_criteria_unmet": ["docs", "tests"],
                    },
                    "timestamp": "t",
                }],
            },
        }
        ctx = inject_retry_reason({}, node, state, 3)
        reason = ctx["_retry_reason"]
        assert "Feedback: missing docstring" in reason
        assert "- docs" in reason
        assert "- tests" in reason

    def test_partial_feedback_produces_minimal_retry_reason(self):
        """Gate that returns only `{"score": ...}` produces feedback with
        feedback=None and empty pass_criteria lists (the shape
        build_evaluation_outcome_update writes). The retry reason must still surface
        the score/threshold/attempt lines without "Feedback:" or
        "Unmet criteria:" sections.
        """
        node = _phase(evaluation=_gate(threshold=0.8))
        state = {
            "retries": {"p1": 1},
            "evaluations": {
                "p1": [{
                    "invocation": 0,
                    "result": {
                        "score": 0.4,
                        "scores": {},
                        "feedback": None,
                        "pass_criteria_met": [],
                        "pass_criteria_unmet": [],
                    },
                    "timestamp": "t",
                }],
            },
        }
        ctx = inject_retry_reason({}, node, state, 3)
        reason = ctx["_retry_reason"]
        assert "score=0.40" in reason
        assert "threshold=0.8" in reason
        assert "retry 1 of 3" in reason
        assert "Feedback:" not in reason
        assert "Unmet criteria:" not in reason


class TestExecuteWithTimeout:
    @pytest.mark.asyncio
    async def test_successful_execution(self):
        class FakeExec:
            async def execute(self, node, context):
                return ExecutionResult(output="done")

        result = await execute_with_timeout(FakeExec(), _phase(), {}, None)
        assert isinstance(result, ExecutionResult)
        assert result.output == "done"

    @pytest.mark.asyncio
    async def test_with_timeout_succeeds(self):
        class FakeExec:
            async def execute(self, node, context):
                return ExecutionResult(output="fast")

        result = await execute_with_timeout(FakeExec(), _phase(), {}, 5.0)
        assert result.output == "fast"

    @pytest.mark.asyncio
    async def test_timeout_returns_sentinel(self):
        class SlowExec:
            async def execute(self, node, context):
                await asyncio.sleep(10)
                return ExecutionResult(output="never")

        result = await execute_with_timeout(SlowExec(), _phase(), {}, 0.01)
        assert result == "timeout"


class TestMakeFailureUpdate:
    def test_structure(self):
        result = make_failure_update("p1", "something broke")
        assert result == {
            "failed_nodes": ["p1"],
            "errors": [{"node": "p1", "error": "something broke"}],
        }


class TestAssembleSuccessUpdate:
    def test_basic_output(self):
        result = ExecutionResult(output="hello")
        update = assemble_success_update(_phase(), result)
        assert update == {"node_outputs": {"p1": "hello"}}

    def test_with_tokens(self):
        result = ExecutionResult(output="x", tokens_used={"input": 10, "output": 20})
        update = assemble_success_update(_phase(), result)
        assert update["token_usage"] == {"p1": {"input": 10, "output": 20}}

    def test_with_structured_output(self):
        result = ExecutionResult(output="x", structured_output={"key": "val"})
        update = assemble_success_update(_phase(), result)
        assert update["node_structured_outputs"] == {"p1": {"key": "val"}}

    def test_none_tokens_excluded(self):
        result = ExecutionResult(output="x", tokens_used=None)
        update = assemble_success_update(_phase(), result)
        assert "token_usage" not in update

    def test_none_structured_excluded(self):
        result = ExecutionResult(output="x", structured_output=None)
        update = assemble_success_update(_phase(), result)
        assert "node_structured_outputs" not in update


class TestClassifyGateOutcome:
    @pytest.mark.parametrize(
        "score, threshold, retries, max_retries, blocking, expected",
        [
            (1.0, 0.8, 0, 3, True, "pass"),
            (0.5, 0.8, 0, 3, True, "retry"),
            (0.5, 0.8, 3, 3, True, "fail_blocking"),
            (0.5, 0.8, 3, 3, False, "warn_continue"),
            (0.8, 0.8, 0, 3, True, "pass"),  # exactly at threshold
            (0.79, 0.8, 0, 0, True, "fail_blocking"),  # no retries allowed
            (0.79, 0.8, 0, 0, False, "warn_continue"),
            (1.0, 0.8, 3, 3, True, "pass"),  # retries don't matter on pass
        ],
    )
    def test_gate_outcomes(self, score, threshold, retries, max_retries, blocking, expected):
        node = _phase(evaluation=_gate(threshold=threshold, blocking=blocking))
        result = EvaluationResult(score=score)
        assert classify_evaluation_outcome(node, result, retries, max_retries) == expected


class TestBuildGateOutcomeUpdate:
    def test_pass(self):
        node = _phase(evaluation=_gate())
        update = build_evaluation_outcome_update(node, EvaluationResult(score=0.9), "pass", 0, 3)
        record = update["evaluations"]["p1"][0]
        assert record["result"]["score"] == 0.9
        assert record["result"]["feedback"] is None
        assert record["result"]["scores"] == {}
        assert update["completed_nodes"] == ["p1"]
        assert "failed_nodes" not in update

    def test_retry(self):
        node = _phase(evaluation=_gate())
        update = build_evaluation_outcome_update(node, EvaluationResult(score=0.5), "retry", 1, 3)
        assert update["retries"] == {"p1": 2}
        assert "completed_nodes" not in update

    def test_fail_blocking(self):
        node = _phase(evaluation=_gate(threshold=0.8))
        update = build_evaluation_outcome_update(
            node, EvaluationResult(score=0.3), "fail_blocking", 3, 3
        )
        assert update["failed_nodes"] == ["p1"]
        assert "score=0.30" in update["errors"][0]["error"]
        assert "threshold=0.8" in update["errors"][0]["error"]

    def test_warn_continue(self):
        node = _phase(evaluation=_gate(threshold=0.8, blocking=False))
        update = build_evaluation_outcome_update(
            node, EvaluationResult(score=0.3), "warn_continue", 3, 3
        )
        assert update["completed_nodes"] == ["p1"]
        assert "non-blocking" in update["errors"][0]["error"]

    def test_feedback_populated_from_gate_result(self):
        node = _phase(evaluation=_gate())
        gr = EvaluationResult(
            score=0.5,
            feedback="missing docstring",
            pass_criteria_met=["tests pass"],
            pass_criteria_unmet=["docs"],
        )
        update = build_evaluation_outcome_update(node, gr, "retry", 0, 3)
        record = update["evaluations"]["p1"][0]
        assert record["result"]["feedback"] == "missing docstring"
        assert record["result"]["pass_criteria_met"] == ["tests pass"]
        assert record["result"]["pass_criteria_unmet"] == ["docs"]
        assert record["result"]["scores"] == {}

    def test_evaluation_record_written_to_state(self):
        """Each gate call appends an EvaluationRecord to state.evaluations."""
        node = _phase(evaluation=_gate(threshold=0.8))
        gr = EvaluationResult(
            score=0.6,
            feedback="too thin",
            pass_criteria_unmet=["detail"],
        )
        update = build_evaluation_outcome_update(node, gr, "retry", 1, 3)
        records = update["evaluations"]["p1"]
        assert len(records) == 1
        rec = records[0]
        assert rec["invocation"] == 1
        assert rec["result"]["score"] == 0.6
        assert rec["result"]["feedback"] == "too thin"
        assert rec["result"]["pass_criteria_unmet"] == ["detail"]
        assert "T" in rec["timestamp"]  # ISO-8601


# ---------------------------------------------------------------------------
# Multi-dimension gate classification
# ---------------------------------------------------------------------------

from abe_froman.schema.models import DimensionCheck


class TestDimensionGateClassification:
    def _dim_gate(self, dims, blocking=True):
        return Evaluation(
            validator="v.py",
            blocking=blocking,
            dimensions=[DimensionCheck(**d) for d in dims],
        )

    def test_all_dimensions_pass(self):
        gate = self._dim_gate([{"field": "correctness", "min": 0.7}, {"field": "style", "min": 0.5}])
        node = _phase(evaluation=gate)
        result = EvaluationResult(score=0.0, scores={"correctness": 0.8, "style": 0.6})
        assert classify_evaluation_outcome(node, result, 0, 3) == "pass"

    def test_one_dimension_fails(self):
        gate = self._dim_gate([{"field": "correctness", "min": 0.7}, {"field": "style", "min": 0.5}])
        node = _phase(evaluation=gate)
        result = EvaluationResult(score=0.0, scores={"correctness": 0.8, "style": 0.3})
        assert classify_evaluation_outcome(node, result, 0, 3) == "retry"

    def test_missing_dimension_treated_as_zero(self):
        gate = self._dim_gate([{"field": "correctness", "min": 0.7}])
        node = _phase(evaluation=gate)
        result = EvaluationResult(score=0.0, scores={})
        assert classify_evaluation_outcome(node, result, 0, 3) == "retry"

    def test_dimension_gate_exhausted_blocking(self):
        gate = self._dim_gate([{"field": "x", "min": 0.8}], blocking=True)
        node = _phase(evaluation=gate)
        result = EvaluationResult(score=0.0, scores={"x": 0.5})
        assert classify_evaluation_outcome(node, result, 3, 3) == "fail_blocking"

    def test_dimension_gate_exhausted_non_blocking(self):
        gate = self._dim_gate([{"field": "x", "min": 0.8}], blocking=False)
        node = _phase(evaluation=gate)
        result = EvaluationResult(score=0.0, scores={"x": 0.5})
        assert classify_evaluation_outcome(node, result, 3, 3) == "warn_continue"

    def test_dimension_scores_stored_in_feedback(self):
        gate = self._dim_gate([{"field": "correctness", "min": 0.7}])
        node = _phase(evaluation=gate)
        gr = EvaluationResult(score=0.0, scores={"correctness": 0.9})
        update = build_evaluation_outcome_update(node, gr, "pass", 0, 3)
        assert update["evaluations"]["p1"][0]["result"]["scores"] == {"correctness": 0.9}

    def test_dimension_gate_ignore_threshold(self):
        """When dimensions are set, the single-score threshold is irrelevant."""
        gate = Evaluation(
            validator="v.py", threshold=0.99,
            dimensions=[DimensionCheck(field="x", min=0.5)],
        )
        node = _phase(evaluation=gate)
        result = EvaluationResult(score=0.0, scores={"x": 0.6})
        assert classify_evaluation_outcome(node, result, 0, 3) == "pass"


# ---------------------------------------------------------------------------
# run_evaluation_and_outcome — end-to-end gate+classifier+update integration
# ---------------------------------------------------------------------------

from abe_froman.compile.nodes import run_evaluation_and_outcome
from abe_froman.schema.models import Settings, Graph


def _config_with_phase(node: Node, **settings_kwargs) -> Graph:
    return Graph(
        name="T", version="1.0", nodes=[node],
        settings=Settings(**settings_kwargs),
    )


def _write_validator(tmp_path, name: str, score: str) -> str:
    """Write a trivial stdin-reading script validator that prints `score`."""
    path = tmp_path / name
    path.write_text(f"import sys\nsys.stdin.read()\nprint({score!r})\n")
    return name


class TestEvaluateGateAndOutcome:
    @pytest.mark.asyncio
    async def test_pass_path(self, tmp_path):
        validator = _write_validator(tmp_path, "pass.py", "1.0")
        node = _phase(evaluation=_gate(threshold=0.8))
        node.evaluation.validator = validator
        config = _config_with_phase(node)
        state = {
            "workdir": str(tmp_path),
            "retries": {},
        }
        update = await run_evaluation_and_outcome(
            node, config, state, ExecutionResult(output="x"), timeout=None,
        )
        assert update["completed_nodes"] == ["p1"]
        assert update["evaluations"]["p1"][0]["result"]["score"] == 1.0

    @pytest.mark.asyncio
    async def test_retry_path_bumps_retries(self, tmp_path):
        validator = _write_validator(tmp_path, "fail.py", "0.0")
        node = _phase(evaluation=_gate(threshold=0.8))
        node.evaluation.validator = validator
        config = _config_with_phase(node, max_retries=3)
        state = {"workdir": str(tmp_path), "retries": {"p1": 1}}
        update = await run_evaluation_and_outcome(
            node, config, state, ExecutionResult(output="x"), timeout=None,
        )
        assert update["retries"] == {"p1": 2}
        assert "completed_nodes" not in update

    @pytest.mark.asyncio
    async def test_fail_blocking_path(self, tmp_path):
        validator = _write_validator(tmp_path, "fail.py", "0.0")
        node = _phase(evaluation=_gate(threshold=0.8, blocking=True))
        node.evaluation.validator = validator
        config = _config_with_phase(node, max_retries=0)
        state = {"workdir": str(tmp_path), "retries": {}}
        update = await run_evaluation_and_outcome(
            node, config, state, ExecutionResult(output="x"), timeout=None,
        )
        assert update["failed_nodes"] == ["p1"]

    @pytest.mark.asyncio
    async def test_warn_continue_path(self, tmp_path):
        validator = _write_validator(tmp_path, "fail.py", "0.0")
        node = _phase(evaluation=_gate(threshold=0.8, blocking=False))
        node.evaluation.validator = validator
        config = _config_with_phase(node, max_retries=0)
        state = {"workdir": str(tmp_path), "retries": {}}
        update = await run_evaluation_and_outcome(
            node, config, state, ExecutionResult(output="x"), timeout=None,
        )
        assert update["completed_nodes"] == ["p1"]
        assert "non-blocking" in update["errors"][0]["error"]

    @pytest.mark.asyncio
    async def test_timeout_returns_failure_update(self, tmp_path):
        """Slow validator + tight timeout → failure update with timeout message."""
        slow = tmp_path / "slow.py"
        slow.write_text("import time, sys\nsys.stdin.read()\ntime.sleep(10)\nprint('1.0')\n")
        node = _phase(evaluation=_gate(threshold=0.8))
        node.evaluation.validator = "slow.py"
        config = _config_with_phase(node)
        state = {"workdir": str(tmp_path), "retries": {}}
        update = await run_evaluation_and_outcome(
            node, config, state, ExecutionResult(output="x"), timeout=0.1,
        )
        assert update["failed_nodes"] == ["p1"]
        assert "timed out" in update["errors"][0]["error"]
