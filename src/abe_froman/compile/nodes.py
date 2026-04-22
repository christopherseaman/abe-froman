"""Phase node factory and decomposed helpers.

_make_phase_node returns an async callable for StateGraph.add_node.
Pure helpers (check_*, build_context, classify_gate_outcome, etc.)
operate on WorkflowState/Phase dicts with no langgraph dependency.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from abe_froman.runtime.gates import (
    GateResult,
    scaffold_output_directory,
    validate_output_contract,
)
from abe_froman.runtime.gates import evaluate_gate
from abe_froman.runtime.result import ExecutionResult
from abe_froman.runtime.state import WorkflowState
from abe_froman.schema.models import Phase, Settings, WorkflowConfig

if TYPE_CHECKING:
    from abe_froman.runtime.result import PhaseExecutor


def _get_retry_delay(retry_count: int, backoff: list[float]) -> float:
    """Return delay in seconds for the given retry attempt (1-indexed).

    Uses the backoff list, clamping to the last value for attempts
    beyond the list length. Returns 0.0 if backoff is empty.
    """
    if not backoff:
        return 0.0
    idx = min(retry_count - 1, len(backoff) - 1)
    return backoff[idx]


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


def all_deps_completed(phase: Phase, state: WorkflowState) -> bool:
    """True iff every dep is in completed_phases.

    Multi-predecessor phases whose preds are gated get triggered by each
    pred's router independently (conditional edges). Returning {} from the
    node body until all preds are done causes LangGraph to re-fire the
    node on each subsequent pred-trigger — a natural join barrier.
    """
    completed = set(state.get("completed_phases", []))
    return all(dep in completed for dep in phase.depends_on)


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


def build_context(phase: Phase, state: WorkflowState) -> dict[str, Any]:
    context: dict[str, Any] = {}
    outputs = state.get("phase_outputs", {})
    structured = state.get("phase_structured_outputs", {})
    worktrees = state.get("phase_worktrees", {})
    for dep in phase.depends_on:
        if dep in outputs:
            context[dep] = outputs[dep]
        if dep in structured:
            context[f"{dep}_structured"] = structured[dep]
        if dep in worktrees:
            context[f"{dep}_worktree"] = worktrees[dep]
        # Dynamic parents expose fan-out aggregations via synthetic keys;
        # forward them to the final-phase template as `{{dep_subphases}}`
        # and `{{dep_subphase_worktrees}}`.
        for suffix in ("_subphases", "_subphase_worktrees"):
            key = f"{dep}{suffix}"
            if key in outputs:
                context[key] = outputs[key]
    return context


def inject_retry_reason(
    context: dict[str, Any], phase: Phase, state: WorkflowState, max_retries: int
) -> dict[str, Any]:
    retry_count = state.get("retries", {}).get(phase.id, 0)
    if retry_count == 0 or not phase.quality_gate:
        return context

    gate = phase.quality_gate
    feedback = state.get("gate_feedback", {}).get(phase.id, {})

    if gate.dimensions:
        dim_scores = feedback.get("scores", {})
        score_parts = [
            f"{d.field}={dim_scores.get(d.field, 0.0):.2f} (min={d.min})"
            for d in gate.dimensions
        ]
        score_summary = "; ".join(score_parts)
    else:
        prev_score = state.get("gate_scores", {}).get(phase.id, 0.0)
        score_summary = f"score={prev_score:.2f}, threshold={gate.threshold}"

    lines = [
        f"Attempt {retry_count} failed quality gate ({score_summary}). "
        f"This is retry {retry_count} of {max_retries}."
    ]
    if feedback.get("feedback"):
        lines.append(f"Feedback: {feedback['feedback']}")
    unmet = feedback.get("pass_criteria_unmet") or []
    if unmet:
        lines.append(
            "Unmet criteria:\n" + "\n".join(f"- {c}" for c in unmet)
        )

    context["_retry_reason"] = "\n\n".join(lines)
    return context


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
    phase: Phase, gate_result: GateResult, retries: int, max_retries: int
) -> str:
    gate = phase.quality_gate
    if gate.dimensions:
        passed = all(
            gate_result.scores.get(dim.field, 0.0) >= dim.min
            for dim in gate.dimensions
        )
    else:
        passed = gate_result.score >= gate.threshold
    if passed:
        return "pass"
    if retries < max_retries:
        return "retry"
    if gate.blocking:
        return "fail_blocking"
    return "warn_continue"


def _gate_summary(phase: Phase, result: GateResult) -> str:
    gate = phase.quality_gate
    if gate.dimensions:
        parts = [
            f"{d.field}={result.scores.get(d.field, 0.0):.2f}>={d.min}"
            for d in gate.dimensions
        ]
        return ", ".join(parts)
    return f"score={result.score:.2f}, threshold={gate.threshold}"


def build_gate_outcome_update(
    phase: Phase, result: GateResult, outcome: str, retries: int, max_retries: int
) -> dict[str, Any]:
    update: dict[str, Any] = {
        "gate_scores": {phase.id: result.score},
        "gate_feedback": {
            phase.id: {
                "feedback": result.feedback,
                "pass_criteria_met": list(result.pass_criteria_met),
                "pass_criteria_unmet": list(result.pass_criteria_unmet),
                "scores": dict(result.scores),
            }
        },
    }
    summary = _gate_summary(phase, result)

    if outcome == "pass":
        update["completed_phases"] = [phase.id]
    elif outcome == "retry":
        update["retries"] = {phase.id: retries + 1}
    elif outcome == "fail_blocking":
        update["failed_phases"] = [phase.id]
        update["errors"] = [
            {
                "phase": phase.id,
                "error": f"Quality gate failed after {max_retries} retries ({summary})",
            }
        ]
    elif outcome == "warn_continue":
        update["completed_phases"] = [phase.id]
        update["errors"] = [
            {
                "phase": phase.id,
                "error": (
                    f"Quality gate below threshold after {max_retries} retries "
                    f"({summary}), continuing (non-blocking)"
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
    backend: Any = None,
) -> dict[str, Any]:
    max_retries = phase.effective_max_retries(config.settings)
    retries = state.get("retries", {}).get(phase.id, 0)

    gate_call = evaluate_gate(
        phase.quality_gate,
        phase.id,
        workdir=state.get("workdir", "."),
        phase_output=result.output,
        workflow_name=config.name,
        attempt_number=retries + 1,
        backend=backend,
        default_model=config.settings.default_model,
    )
    try:
        if timeout is not None:
            gate_result = await asyncio.wait_for(gate_call, timeout=timeout)
        else:
            gate_result = await gate_call
    except asyncio.TimeoutError:
        return make_failure_update(
            phase.id, f"Quality gate timed out after {timeout}s"
        )

    outcome = classify_gate_outcome(phase, gate_result, retries, max_retries)
    return build_gate_outcome_update(phase, gate_result, outcome, retries, max_retries)


def _make_phase_node(
    phase: Phase,
    config: WorkflowConfig,
    executor: PhaseExecutor | None = None,
):
    max_retries = phase.effective_max_retries(config.settings)
    timeout = phase.effective_timeout(config.settings)

    async def node_fn(state: WorkflowState) -> dict[str, Any]:
        if phase.id in state.get("completed_phases", []):
            return {}
        for check in (check_dep_failed, check_dry_run):
            if (r := check(phase, state)) is not None:
                return r
        if phase.depends_on and not all_deps_completed(phase, state):
            # A gated predecessor routed here before its siblings finished.
            # Return no-op — subsequent pred completions re-fire this node.
            return {}
        if executor is None:
            update: dict[str, Any] = {
                "completed_phases": [phase.id],
                "phase_outputs": {phase.id: f"[no-executor] {phase.name}"},
            }
            if phase.quality_gate:
                update["gate_scores"] = {phase.id: 1.0}
            return update

        context = build_context(phase, state)
        retry_count = state.get("retries", {}).get(phase.id, 0)
        if retry_count > 0:
            delay = _get_retry_delay(retry_count, config.settings.retry_backoff)
            if delay > 0:
                await asyncio.sleep(delay)
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
        if hasattr(executor, "get_worktree"):
            wt = executor.get_worktree(phase.id)
            if wt:
                update["phase_worktrees"] = {phase.id: wt}
        if phase.quality_gate:
            backend = (
                executor.get_backend() if hasattr(executor, "get_backend") else None
            )
            update |= await evaluate_gate_and_outcome(
                phase, config, state, exec_result, timeout, backend=backend,
            )
        else:
            update["completed_phases"] = [phase.id]

        return update

    node_fn.__name__ = f"node_{phase.id}"
    return node_fn
