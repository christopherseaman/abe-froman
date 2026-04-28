"""Phase node factory and decomposed helpers.

_make_phase_node returns an async callable for StateGraph.add_node.
Pure helpers (check_*, build_context, classify_evaluation_outcome, etc.)
operate on WorkflowState/Phase dicts with no langgraph dependency.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Callable

from abe_froman.compile.evaluation import (
    EvaluationRecord,
    build_eval_context,
    evaluation_fallback,
    evaluation_to_routes,
    walk_routes,
)
from abe_froman.runtime.gates import (
    EvaluationResult,
    scaffold_output_directory,
    validate_output_contract,
)
from abe_froman.runtime.gates import run_evaluation
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
    # Dry-run writes phase_outputs + completed_phases. Gated phases still
    # route through their Evaluation node, which handles dry-run itself by
    # synthesizing a pass EvaluationRecord — so we don't pre-complete here
    # for gated phases.
    update: dict[str, Any] = {
        "phase_outputs": {phase.id: f"[dry-run] {phase.name}"},
    }
    if not phase.evaluation:
        update["completed_phases"] = [phase.id]
    return update


def build_context(phase: Phase, state: WorkflowState) -> dict[str, Any]:
    import json as _json

    context: dict[str, Any] = {}
    outputs = state.get("phase_outputs", {})
    structured = state.get("phase_structured_outputs", {})
    worktrees = state.get("phase_worktrees", {})
    sub_outputs = state.get("subphase_outputs", {})
    for dep in phase.depends_on:
        if dep in outputs:
            context[dep] = outputs[dep]
        if dep in structured:
            context[f"{dep}_structured"] = structured[dep]
        if dep in worktrees:
            context[f"{dep}_worktree"] = worktrees[dep]
        # Synthesize fan-out aggregates from state. Any phase depending on
        # a dynamic parent sees `{{dep_subphases}}` (JSON id→output map) and
        # `{{dep_subphase_worktrees}}` (JSON list of worktree paths) — not
        # just the final-phase wrapper.
        prefix = f"{dep}::"
        dep_subs = {k: v for k, v in sub_outputs.items() if k.startswith(prefix)}
        if dep_subs:
            context[f"{dep}_subphases"] = _json.dumps(dep_subs)
            dep_wts = [v for k, v in worktrees.items() if k.startswith(prefix)]
            context[f"{dep}_subphase_worktrees"] = _json.dumps(dep_wts)
    return context


def inject_retry_reason(
    context: dict[str, Any],
    phase: Phase,
    state: WorkflowState,
    max_retries: int,
    *,
    node_id: str | None = None,
) -> dict[str, Any]:
    key = node_id or phase.id
    retry_count = state.get("retries", {}).get(key, 0)
    if retry_count == 0 or not phase.evaluation:
        return context

    records = state.get("evaluations", {}).get(key, [])
    if not records:
        return context
    last_result = records[-1].get("result", {}) or {}

    evaluation = phase.evaluation
    if evaluation.dimensions:
        dim_scores = last_result.get("scores", {}) or {}
        score_parts = [
            f"{d.field}={dim_scores.get(d.field, 0.0):.2f} (min={d.min})"
            for d in evaluation.dimensions
        ]
        score_summary = "; ".join(score_parts)
    else:
        prev_score = last_result.get("score", 0.0) or 0.0
        score_summary = f"score={prev_score:.2f}, threshold={evaluation.threshold}"

    lines = [
        f"Attempt {retry_count} failed evaluation ({score_summary}). "
        f"This is retry {retry_count} of {max_retries}."
    ]
    if last_result.get("feedback"):
        lines.append(f"Feedback: {last_result['feedback']}")
    unmet = last_result.get("pass_criteria_unmet") or []
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


def _evaluation_result_payload(
    eval_result: EvaluationResult, evaluation: Any | None = None
) -> dict[str, Any]:
    """Flatten EvaluationResult into the `result` dict the route walker sees.

    When the evaluation declares dimensions, backfill any missing dims with
    0.0 so numeric comparisons don't silently evaluate against None and
    escape both pass and retry routes.
    """
    scores = dict(eval_result.scores)
    if evaluation is not None and getattr(evaluation, "dimensions", None):
        for d in evaluation.dimensions:
            scores.setdefault(d.field, 0.0)
    return {
        "score": eval_result.score,
        "scores": scores,
        "feedback": eval_result.feedback,
        "pass_criteria_met": list(eval_result.pass_criteria_met),
        "pass_criteria_unmet": list(eval_result.pass_criteria_unmet),
    }


def classify_evaluation_outcome(
    phase: Phase,
    eval_result: EvaluationResult,
    retries: int,
    max_retries: int,
    *,
    history: list[dict[str, Any]] | None = None,
) -> str:
    """Walk the routes generated from the Evaluation sugar.

    Kept as public API (tests + external callers). Internally this is just
    a thin adapter over `walk_routes` from compile/evaluation.py — the
    string return value ("pass", "retry", "fail_blocking", "warn_continue")
    is the matched route's destination label.
    """
    evaluation = phase.evaluation
    routes = evaluation_to_routes(evaluation, max_retries)
    context = build_eval_context(
        _evaluation_result_payload(eval_result, evaluation),
        invocation=retries,
        history=list(history or []),
    )
    matched = walk_routes(routes, context)
    if matched is not None:
        return matched.to
    return evaluation_fallback(evaluation)


def _evaluation_summary(phase: Phase, result: EvaluationResult) -> str:
    evaluation = phase.evaluation
    if evaluation.dimensions:
        parts = [
            f"{d.field}={result.scores.get(d.field, 0.0):.2f}>={d.min}"
            for d in evaluation.dimensions
        ]
        return ", ".join(parts)
    return f"score={result.score:.2f}, threshold={evaluation.threshold}"


def build_evaluation_outcome_update(
    phase: Phase,
    result: EvaluationResult,
    outcome: str,
    retries: int,
    max_retries: int,
    *,
    node_id: str | None = None,
) -> dict[str, Any]:
    key = node_id or phase.id
    record = EvaluationRecord.now(
        invocation=retries,
        result=_evaluation_result_payload(result, phase.evaluation),
    )
    update: dict[str, Any] = {
        "evaluations": {key: [record.to_dict()]},
    }
    summary = _evaluation_summary(phase, result)

    if outcome == "pass":
        update["completed_phases"] = [key]
    elif outcome == "retry":
        update["retries"] = {key: retries + 1}
    elif outcome == "fail_blocking":
        update["failed_phases"] = [key]
        update["errors"] = [
            {
                "phase": key,
                "error": f"Evaluation failed after {max_retries} retries ({summary})",
            }
        ]
    elif outcome == "warn_continue":
        update["completed_phases"] = [key]
        update["errors"] = [
            {
                "phase": key,
                "error": (
                    f"Evaluation below threshold after {max_retries} retries "
                    f"({summary}), continuing (non-blocking)"
                ),
            }
        ]

    return update


async def run_evaluation_and_outcome(
    phase: Phase,
    config: WorkflowConfig,
    state: WorkflowState,
    result: ExecutionResult,
    timeout: float | None,
    backend: Any = None,
    *,
    node_id: str | None = None,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    key = node_id or phase.id
    max_retries = phase.effective_max_retries(config.settings)
    retries = state.get("retries", {}).get(key, 0)

    eval_call = run_evaluation(
        phase.evaluation,
        key,
        workdir=state.get("workdir", "."),
        phase_output=result.output,
        workflow_name=config.name,
        attempt_number=retries + 1,
        backend=backend,
        default_model=config.settings.default_model,
    )
    try:
        if timeout is not None:
            eval_result = await asyncio.wait_for(eval_call, timeout=timeout)
        else:
            eval_result = await eval_call
    except asyncio.TimeoutError:
        return make_failure_update(
            key, f"Evaluation timed out after {timeout}s"
        )

    outcome = classify_evaluation_outcome(
        phase, eval_result, retries, max_retries, history=history
    )
    return build_evaluation_outcome_update(
        phase, eval_result, outcome, retries, max_retries, node_id=key
    )




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
                "phase_outputs": {phase.id: f"[no-executor] {phase.name}"},
            }
            if not phase.evaluation:
                update["completed_phases"] = [phase.id]
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
        if not phase.evaluation:
            update["completed_phases"] = [phase.id]
        # Gated phases hand off to _eval_{phase.id} via plain edge; the
        # Evaluation node writes completed_phases / retries / failed_phases.

        return update

    node_fn.__name__ = f"node_{phase.id}"
    return node_fn


def _make_evaluation_node(
    phase: Phase,
    config: WorkflowConfig,
    executor: "PhaseExecutor | None" = None,
    *,
    node_id_resolver: Callable[[WorkflowState], str] | None = None,
):
    """Create the Evaluation node — second half of a gated phase pair.

    Reads `phase_outputs[node_id]` (produced by the upstream Execution
    node), runs the gate, walks routes (first-match + catch-all fallback),
    and writes an `EvaluationRecord` plus the outcome's state transitions.

    `node_id_resolver` lets subphase eval nodes derive the per-branch id
    from `state._subphase_item`. Defaults to `phase.id` for top-level use.
    """
    timeout = phase.effective_timeout(config.settings)
    resolve = node_id_resolver or (lambda _s: phase.id)

    async def node_fn(state: WorkflowState) -> dict[str, Any]:
        node_id = resolve(state)

        if node_id in state.get("completed_phases", []):
            return {}
        if node_id in state.get("failed_phases", []):
            return {}

        if state.get("dry_run", False):
            record = EvaluationRecord.now(
                invocation=0,
                result={
                    "score": 1.0,
                    "scores": {},
                    "feedback": "[dry-run]",
                    "pass_criteria_met": [],
                    "pass_criteria_unmet": [],
                },
            )
            return {
                "evaluations": {node_id: [record.to_dict()]},
                "completed_phases": [node_id],
            }

        history = list(state.get("evaluations", {}).get(node_id, []))
        output = state.get("phase_outputs", {}).get(node_id, "")
        structured = state.get("phase_structured_outputs", {}).get(node_id)
        synthetic_result = ExecutionResult(
            success=True, output=output, structured_output=structured
        )

        backend = (
            executor.get_backend()
            if (executor is not None and hasattr(executor, "get_backend"))
            else None
        )
        return await run_evaluation_and_outcome(
            phase,
            config,
            state,
            synthetic_result,
            timeout,
            backend=backend,
            node_id=node_id,
            history=history,
        )

    node_fn.__name__ = f"eval_{phase.id}"
    return node_fn
