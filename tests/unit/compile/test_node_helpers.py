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
    build_gate_outcome_update,
    check_dep_failed,
    check_dry_run,
    classify_gate_outcome,
    execute_with_timeout,
    inject_retry_reason,
    make_failure_update,
)
from abe_froman.runtime.result import ExecutionResult
from abe_froman.schema.models import Phase, QualityGate


def _phase(id="p1", depends_on=None, quality_gate=None, **kw):
    return Phase(id=id, name=id, depends_on=depends_on or [], quality_gate=quality_gate, **kw)


def _gate(threshold=0.8, blocking=True, max_retries=None):
    return QualityGate(validator="v.py", threshold=threshold, blocking=blocking, max_retries=max_retries)


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
        phase = _phase(depends_on=["dep1"])
        state = {"failed_phases": ["dep1"]}
        result = check_dep_failed(phase, state)
        assert result["failed_phases"] == ["p1"]
        assert "dependency 'dep1' failed" in result["errors"][0]["error"]

    def test_no_failed_deps(self):
        phase = _phase(depends_on=["dep1"])
        state = {"failed_phases": []}
        assert check_dep_failed(phase, state) is None

    def test_no_deps(self):
        phase = _phase(depends_on=[])
        state = {"failed_phases": ["something"]}
        assert check_dep_failed(phase, state) is None

    def test_unrelated_failure(self):
        phase = _phase(depends_on=["dep1"])
        state = {"failed_phases": ["dep2"]}
        assert check_dep_failed(phase, state) is None


class TestCheckDryRun:
    def test_dry_run_without_gate(self):
        state = {"dry_run": True}
        result = check_dry_run(_phase(), state)
        assert result["completed_phases"] == ["p1"]
        assert "[dry-run]" in result["phase_outputs"]["p1"]
        assert "gate_scores" not in result

    def test_dry_run_with_gate(self):
        phase = _phase(quality_gate=_gate())
        state = {"dry_run": True}
        result = check_dry_run(phase, state)
        assert result["gate_scores"] == {"p1": 1.0}

    def test_not_dry_run(self):
        assert check_dry_run(_phase(), {"dry_run": False}) is None

    def test_missing_dry_run_key(self):
        assert check_dry_run(_phase(), {}) is None



class TestBuildContext:
    def test_with_matching_deps(self):
        phase = _phase(depends_on=["a"])
        state = {
            "phase_outputs": {"a": "out-a"},
            "phase_structured_outputs": {"a": {"k": "v"}},
        }
        ctx = build_context(phase, state)
        assert ctx == {"a": "out-a", "a_structured": {"k": "v"}}

    def test_no_matching_deps(self):
        phase = _phase(depends_on=["b"])
        state = {"phase_outputs": {"a": "out-a"}}
        assert build_context(phase, state) == {}

    def test_empty_deps(self):
        assert build_context(_phase(), {"phase_outputs": {"a": "x"}}) == {}

    def test_output_only_no_structured(self):
        phase = _phase(depends_on=["a"])
        state = {"phase_outputs": {"a": "text"}}
        assert build_context(phase, state) == {"a": "text"}


class TestInjectRetryReason:
    def test_first_attempt_no_injection(self):
        ctx = inject_retry_reason({}, _phase(quality_gate=_gate()), {"retries": {}}, 3)
        assert "_retry_reason" not in ctx

    def test_retry_injects_reason(self):
        phase = _phase(quality_gate=_gate(threshold=0.8))
        state = {"retries": {"p1": 1}, "gate_scores": {"p1": 0.5}}
        ctx = inject_retry_reason({}, phase, state, 3)
        assert "score=0.50" in ctx["_retry_reason"]
        assert "threshold=0.8" in ctx["_retry_reason"]
        assert "retry 1 of 3" in ctx["_retry_reason"]

    def test_no_gate_no_injection(self):
        state = {"retries": {"p1": 1}}
        ctx = inject_retry_reason({}, _phase(), state, 3)
        assert "_retry_reason" not in ctx


class TestExecuteWithTimeout:
    @pytest.mark.asyncio
    async def test_successful_execution(self):
        class FakeExec:
            async def execute(self, phase, context):
                return ExecutionResult(output="done")

        result = await execute_with_timeout(FakeExec(), _phase(), {}, None)
        assert isinstance(result, ExecutionResult)
        assert result.output == "done"

    @pytest.mark.asyncio
    async def test_with_timeout_succeeds(self):
        class FakeExec:
            async def execute(self, phase, context):
                return ExecutionResult(output="fast")

        result = await execute_with_timeout(FakeExec(), _phase(), {}, 5.0)
        assert result.output == "fast"

    @pytest.mark.asyncio
    async def test_timeout_returns_sentinel(self):
        class SlowExec:
            async def execute(self, phase, context):
                await asyncio.sleep(10)
                return ExecutionResult(output="never")

        result = await execute_with_timeout(SlowExec(), _phase(), {}, 0.01)
        assert result == "timeout"


class TestMakeFailureUpdate:
    def test_structure(self):
        result = make_failure_update("p1", "something broke")
        assert result == {
            "failed_phases": ["p1"],
            "errors": [{"phase": "p1", "error": "something broke"}],
        }


class TestAssembleSuccessUpdate:
    def test_basic_output(self):
        result = ExecutionResult(output="hello")
        update = assemble_success_update(_phase(), result)
        assert update == {"phase_outputs": {"p1": "hello"}}

    def test_with_tokens(self):
        result = ExecutionResult(output="x", tokens_used={"input": 10, "output": 20})
        update = assemble_success_update(_phase(), result)
        assert update["token_usage"] == {"p1": {"input": 10, "output": 20}}

    def test_with_structured_output(self):
        result = ExecutionResult(output="x", structured_output={"key": "val"})
        update = assemble_success_update(_phase(), result)
        assert update["phase_structured_outputs"] == {"p1": {"key": "val"}}

    def test_none_tokens_excluded(self):
        result = ExecutionResult(output="x", tokens_used=None)
        update = assemble_success_update(_phase(), result)
        assert "token_usage" not in update

    def test_none_structured_excluded(self):
        result = ExecutionResult(output="x", structured_output=None)
        update = assemble_success_update(_phase(), result)
        assert "phase_structured_outputs" not in update


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
        phase = _phase(quality_gate=_gate(threshold=threshold, blocking=blocking))
        assert classify_gate_outcome(phase, score, retries, max_retries) == expected


class TestBuildGateOutcomeUpdate:
    def test_pass(self):
        phase = _phase(quality_gate=_gate())
        update = build_gate_outcome_update(phase, 0.9, "pass", 0, 3)
        assert update["gate_scores"] == {"p1": 0.9}
        assert update["completed_phases"] == ["p1"]
        assert "failed_phases" not in update

    def test_retry(self):
        phase = _phase(quality_gate=_gate())
        update = build_gate_outcome_update(phase, 0.5, "retry", 1, 3)
        assert update["retries"] == {"p1": 2}
        assert "completed_phases" not in update

    def test_fail_blocking(self):
        phase = _phase(quality_gate=_gate(threshold=0.8))
        update = build_gate_outcome_update(phase, 0.3, "fail_blocking", 3, 3)
        assert update["failed_phases"] == ["p1"]
        assert "score=0.30" in update["errors"][0]["error"]
        assert "threshold=0.8" in update["errors"][0]["error"]

    def test_warn_continue(self):
        phase = _phase(quality_gate=_gate(threshold=0.8, blocking=False))
        update = build_gate_outcome_update(phase, 0.3, "warn_continue", 3, 3)
        assert update["completed_phases"] == ["p1"]
        assert "non-blocking" in update["errors"][0]["error"]
