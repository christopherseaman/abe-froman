"""Phase node factory and decomposed helpers.

_make_phase_node returns an async callable for StateGraph.add_node.
Pure helpers (check_*, build_context, classify_gate_outcome, etc.)
operate on WorkflowState/Phase dicts with no langgraph dependency.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from abe_froman.runtime.contracts import (
    scaffold_output_directory,
    validate_output_contract,
)
from abe_froman.runtime.gates import evaluate_gate
from abe_froman.runtime.result import ExecutionResult
from abe_froman.runtime.state import WorkflowState
from abe_froman.schema.models import Phase, Settings, WorkflowConfig

if TYPE_CHECKING:
    from abe_froman.runtime.executor.base import PhaseExecutor


def _get_retry_delay(retry_count: int, backoff: list[float]) -> float:
    """Return delay in seconds for the given retry attempt (1-indexed).

    Uses the backoff list, clamping to the last value for attempts
    beyond the list length. Returns 0.0 if backoff is empty.
    """
    if not backoff:
        return 0.0
    idx = min(retry_count - 1, len(backoff) - 1)
    return backoff[idx]


def check_already_completed(phase: Phase, state: WorkflowState) -> dict | None:
    if phase.id in state.get("completed_phases", []):
        return {}
    return None


def check_dep_failed(phase: Phase, state: WorkflowState) -> dict | None:
    failed = state.get("failed_phases", [])
    for dep in phase.depends_on:
        if dep in failed:
            return {
                "failed_phases": [phase.id],
                "errors": [
                    {
                        "phase": phase.id,
                        "error": f"Skipped: dependency '{dep}' failed",
                    }
                ],
            }
    return None


def check_dry_run(phase: Phase, state: WorkflowState) -> dict | None:
    if not state.get("dry_run", False):
        return None
    update: dict[str, Any] = {
        "completed_phases": [phase.id],
        "phase_outputs": {phase.id: f"[dry-run] {phase.name}"},
    }
    if phase.quality_gate:
        update["gate_scores"] = {phase.id: 1.0}
    return update


def check_no_executor(phase: Phase, state: WorkflowState, executor) -> dict | None:
    if executor is not None:
        return None
    update: dict[str, Any] = {
        "completed_phases": [phase.id],
        "phase_outputs": {phase.id: f"[no-executor] {phase.name}"},
    }
    if phase.quality_gate:
        update["gate_scores"] = {phase.id: 1.0}
    return update


def build_context(phase: Phase, state: WorkflowState) -> dict[str, Any]:
    context: dict[str, Any] = {}
    for dep in phase.depends_on:
        if dep in state.get("phase_outputs", {}):
            context[dep] = state["phase_outputs"][dep]
        if dep in state.get("phase_structured_outputs", {}):
            context[f"{dep}_structured"] = state["phase_structured_outputs"][dep]
    return context


def inject_retry_reason(
    context: dict[str, Any], phase: Phase, state: WorkflowState, max_retries: int
) -> dict[str, Any]:
    retry_count = state.get("retries", {}).get(phase.id, 0)
    if retry_count > 0 and phase.quality_gate:
        prev_score = state.get("gate_scores", {}).get(phase.id, 0.0)
        context["_retry_reason"] = (
            f"Attempt {retry_count} failed quality gate "
            f"(score={prev_score:.2f}, threshold={phase.quality_gate.threshold}). "
            f"This is retry {retry_count} of {max_retries}."
        )
    return context


async def apply_backoff(
    phase: Phase, state: WorkflowState, settings: Settings
) -> None:
    retry_count = state.get("retries", {}).get(phase.id, 0)
    if retry_count > 0:
        delay = _get_retry_delay(retry_count, settings.retry_backoff)
        if delay > 0:
            await asyncio.sleep(delay)


async def execute_with_timeout(
    executor, phase: Phase, context: dict[str, Any], timeout: float | None
) -> ExecutionResult | str:
    try:
        if timeout is not None:
            result = await asyncio.wait_for(
                executor.execute(phase, context), timeout=timeout
            )
        else:
            result = await executor.execute(phase, context)
        return result
    except asyncio.TimeoutError:
        return "timeout"


def make_failure_update(phase_id: str, error_message: str) -> dict[str, Any]:
    return {
        "failed_phases": [phase_id],
        "errors": [{"phase": phase_id, "error": error_message}],
    }


def assemble_success_update(phase: Phase, result: ExecutionResult) -> dict[str, Any]:
    update: dict[str, Any] = {
        "phase_outputs": {phase.id: result.output},
    }
    if result.structured_output is not None:
        update["phase_structured_outputs"] = {phase.id: result.structured_output}
    if result.tokens_used is not None:
        update["token_usage"] = {phase.id: result.tokens_used}
    return update


def classify_gate_outcome(
    phase: Phase, score: float, retries: int, max_retries: int
) -> str:
    threshold = phase.quality_gate.threshold
    if score >= threshold:
        return "pass"
    if retries < max_retries:
        return "retry"
    if phase.quality_gate.blocking:
        return "fail_blocking"
    return "warn_continue"


def build_gate_outcome_update(
    phase: Phase, score: float, outcome: str, retries: int, max_retries: int
) -> dict[str, Any]:
    update: dict[str, Any] = {"gate_scores": {phase.id: score}}

    if outcome == "pass":
        update["completed_phases"] = [phase.id]
    elif outcome == "retry":
        update["retries"] = {phase.id: retries + 1}
    elif outcome == "fail_blocking":
        update["failed_phases"] = [phase.id]
        update["errors"] = [
            {
                "phase": phase.id,
                "error": (
                    f"Quality gate failed after {max_retries} retries "
                    f"(score={score:.2f}, threshold={phase.quality_gate.threshold})"
                ),
            }
        ]
    elif outcome == "warn_continue":
        update["completed_phases"] = [phase.id]
        update["errors"] = [
            {
                "phase": phase.id,
                "error": (
                    f"Quality gate below threshold after {max_retries} retries "
                    f"(score={score:.2f}, threshold={phase.quality_gate.threshold}), "
                    f"continuing (non-blocking)"
                ),
            }
        ]

    return update


async def evaluate_gate_and_outcome(
    phase: Phase,
    config: WorkflowConfig,
    state: WorkflowState,
    result: ExecutionResult,
    timeout: float | None,
) -> dict[str, Any]:
    max_retries = phase.effective_max_retries(config.settings)
    retries = state.get("retries", {}).get(phase.id, 0)

    try:
        if timeout is not None:
            score = await asyncio.wait_for(
                evaluate_gate(
                    phase.quality_gate,
                    phase.id,
                    workdir=state.get("workdir", "."),
                    phase_output=result.output,
                    workflow_name=config.name,
                    attempt_number=retries + 1,
                ),
                timeout=timeout,
            )
        else:
            score = await evaluate_gate(
                phase.quality_gate,
                phase.id,
                workdir=state.get("workdir", "."),
                phase_output=result.output,
                workflow_name=config.name,
                attempt_number=retries + 1,
            )
    except asyncio.TimeoutError:
        return make_failure_update(
            phase.id, f"Quality gate timed out after {timeout}s"
        )

    outcome = classify_gate_outcome(phase, score, retries, max_retries)
    return build_gate_outcome_update(phase, score, outcome, retries, max_retries)


def _make_phase_node(
    phase: Phase,
    config: WorkflowConfig,
    executor: PhaseExecutor | None = None,
):
    max_retries = phase.effective_max_retries(config.settings)
    timeout = phase.effective_timeout(config.settings)

    async def node_fn(state: WorkflowState) -> dict[str, Any]:
        for check in (check_already_completed, check_dep_failed, check_dry_run):
            if (r := check(phase, state)) is not None:
                return r
        if (r := check_no_executor(phase, state, executor)) is not None:
            return r

        context = build_context(phase, state)
        await apply_backoff(phase, state, config.settings)
        context = inject_retry_reason(context, phase, state, max_retries)

        if phase.output_contract:
            scaffold_output_directory(
                phase.output_contract, state.get("workdir", ".")
            )

        exec_result = await execute_with_timeout(executor, phase, context, timeout)
        if exec_result == "timeout":
            return make_failure_update(
                phase.id, f"Phase timed out after {timeout}s"
            )
        if not exec_result.success:
            return make_failure_update(phase.id, exec_result.error)

        if phase.output_contract:
            missing = validate_output_contract(
                phase.output_contract, state.get("workdir", ".")
            )
            if missing:
                return {
                    "failed_phases": [phase.id],
                    "errors": [
                        {
                            "phase": phase.id,
                            "error": (
                                f"Output contract violated: missing files: "
                                f"{', '.join(missing)}"
                            ),
                        }
                    ],
                    "phase_outputs": {phase.id: exec_result.output},
                }

        update = assemble_success_update(phase, exec_result)
        if phase.quality_gate:
            update |= await evaluate_gate_and_outcome(
                phase, config, state, exec_result, timeout
            )
        else:
            update["completed_phases"] = [phase.id]

        return update

    node_fn.__name__ = f"node_{phase.id}"
    return node_fn
