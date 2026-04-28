"""Node factories for dynamic subphases (fan-out via Send)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from abe_froman.compile.nodes import (
    _get_retry_delay,
    _make_phase_node,
    build_context,
    run_evaluation_and_outcome,
    execute_with_timeout,
    inject_retry_reason,
    make_failure_update,
)
from abe_froman.runtime.result import ExecutionResult
from abe_froman.runtime.state import WorkflowState
from abe_froman.schema.models import Phase, WorkflowConfig

if TYPE_CHECKING:
    from abe_froman.runtime.result import PhaseExecutor


def _merge_updates(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Merge `extra` into `base`, mirroring the top-level state reducers.

    The subphase closure builds up its own update dict across execute +
    evaluate calls within a single node body. Once we return, the graph's
    reducers will reduce this aggregate against the prior state. Within
    the node body we replicate their semantics:
      - lists (completed_phases, failed_phases, errors) → concat
      - dict-of-list (evaluations) → per-key append
      - plain dicts (retries, phase_outputs, …) → update-overwrite
    """
    out = dict(base)
    for key, value in extra.items():
        if key == "evaluations" and isinstance(value, dict):
            existing = dict(out.get(key, {}))
            for sub_key, new_records in value.items():
                existing[sub_key] = list(existing.get(sub_key, [])) + list(new_records)
            out[key] = existing
        elif key in out and isinstance(out[key], list) and isinstance(value, list):
            out[key] = out[key] + value
        elif key in out and isinstance(out[key], dict) and isinstance(value, dict):
            merged = dict(out[key])
            merged.update(value)
            out[key] = merged
        else:
            out[key] = value
    return out


def _make_subphase_node(
    parent_phase: Phase,
    config: WorkflowConfig,
    executor: PhaseExecutor | None = None,
):
    """Create a template node function for dynamic subphases.

    Each invocation receives a different ``_subphase_item`` via LangGraph's
    Send. Gated templates run their retry loop **inline** inside this
    node body — LangGraph merges Send-dispatched branches at any
    conditional-edge boundary, which would strip per-branch
    ``_subphase_item`` state and break graph-level retry self-loops.
    Inline retry preserves per-item state trivially.
    """
    template = parent_phase.dynamic_subphases.template
    timeout = parent_phase.effective_timeout(config.settings)
    max_retries = parent_phase.effective_max_retries(config.settings)
    retry_backoff = config.settings.retry_backoff

    async def node_fn(state: WorkflowState) -> dict[str, Any]:
        item = state.get("_subphase_item", {})
        item_id = item.get("id", "unknown")
        subphase_id = f"{parent_phase.id}::{item_id}"

        if subphase_id in state.get("completed_phases", []):
            return {}
        if subphase_id in state.get("failed_phases", []):
            return {}

        if state.get("dry_run", False):
            return {
                "phase_outputs": {subphase_id: f"[dry-run] subphase {item_id}"},
                "subphase_outputs": {subphase_id: f"[dry-run] subphase {item_id}"},
                "completed_phases": [subphase_id],
            }

        if executor is None:
            return {
                "phase_outputs": {
                    subphase_id: f"[no-executor] subphase {item_id}"
                },
                "subphase_outputs": {
                    subphase_id: f"[no-executor] subphase {item_id}"
                },
                "completed_phases": [subphase_id],
            }

        synthetic_phase = Phase(
            id=subphase_id,
            name=f"{parent_phase.name} - {item.get('name', item_id)}",
            prompt_file=template.prompt_file,
            evaluation=template.evaluation,
            model=parent_phase.model,
        )

        # Build context up front — dep outputs + per-item manifest fields
        # do not change across retries within this node invocation.
        base_context = build_context(parent_phase, state)
        parent_output = state.get("phase_outputs", {}).get(parent_phase.id, "")
        base_context[parent_phase.id] = parent_output
        for key, value in item.items():
            base_context[key] = str(value)

        # Simulated state that accumulates inline across retries within this
        # branch. Starts from the caller-provided state so reducers at the
        # super-step boundary see the whole history.
        update: dict[str, Any] = {}
        history: list[dict[str, Any]] = list(
            state.get("evaluations", {}).get(subphase_id, [])
        )
        retries_local = state.get("retries", {}).get(subphase_id, 0)

        while True:
            context = dict(base_context)
            if retries_local > 0:
                delay = _get_retry_delay(retries_local, retry_backoff)
                if delay > 0:
                    await asyncio.sleep(delay)
                synthetic_state: WorkflowState = {
                    **state,
                    "evaluations": _synth_evaluations(
                        state.get("evaluations", {}), subphase_id, history
                    ),
                    "retries": {
                        **state.get("retries", {}),
                        subphase_id: retries_local,
                    },
                }
                context = inject_retry_reason(
                    context, synthetic_phase, synthetic_state,
                    max_retries, node_id=subphase_id,
                )

            exec_result = await execute_with_timeout(
                executor, synthetic_phase, context, timeout
            )
            if exec_result == "timeout":
                return _merge_updates(update, make_failure_update(
                    subphase_id, f"Phase timed out after {timeout}s"
                ))
            if not exec_result.success:
                return _merge_updates(update, make_failure_update(
                    subphase_id, exec_result.error
                ))

            exec_update: dict[str, Any] = {
                "phase_outputs": {subphase_id: exec_result.output},
                "subphase_outputs": {subphase_id: exec_result.output},
            }
            if exec_result.tokens_used is not None:
                exec_update["token_usage"] = {subphase_id: exec_result.tokens_used}
            update = _merge_updates(update, exec_update)

            if not template.evaluation:
                update.setdefault("completed_phases", []).append(subphase_id)
                return update

            backend = (
                executor.get_backend()
                if hasattr(executor, "get_backend") else None
            )
            # Call run_evaluation_and_outcome with a state reflecting the
            # inline retries counter, so its own read of retries matches.
            eval_state: WorkflowState = {
                **state,
                "retries": {
                    **state.get("retries", {}),
                    subphase_id: retries_local,
                },
            }
            eval_update = await run_evaluation_and_outcome(
                synthetic_phase, config, eval_state, exec_result, timeout,
                backend=backend, node_id=subphase_id, history=history,
            )
            update = _merge_updates(update, eval_update)

            new_record = eval_update.get("evaluations", {}).get(subphase_id, [])
            history = history + list(new_record)

            if subphase_id in eval_update.get("completed_phases", []):
                return update
            if subphase_id in eval_update.get("failed_phases", []):
                return update

            # Retry outcome — increment and loop.
            bumped = eval_update.get("retries", {}).get(subphase_id)
            if bumped is None:
                # Defensive: no retry signal → treat as terminal to avoid infinite loop.
                return update
            retries_local = bumped

    node_fn.__name__ = f"subphase_{parent_phase.id}"
    return node_fn


def _synth_evaluations(
    existing: dict[str, list[dict[str, Any]]],
    key: str,
    history: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Return a new evaluations dict where `key` reflects the inline
    history. Leaves other keys untouched.
    """
    out = {k: list(v) for k, v in existing.items()}
    out[key] = list(history)
    return out


def _make_final_phase_node(
    parent_phase: Phase,
    final_phase,
    config: WorkflowConfig,
    executor: PhaseExecutor | None = None,
):
    """Create a node function for a final phase in a dynamic subphase group.

    Subphase aggregates reach the final phase through `build_context`'s
    suffix synthesis (same mechanism any non-final downstream uses), so
    this factory is a thin wrapper that just renames the synthetic phase
    to stay out of the parent-id namespace.
    """
    node_id = f"_final_{parent_phase.id}_{final_phase.id}"

    synthetic = Phase(
        id=node_id,
        name=final_phase.name,
        description=final_phase.description,
        prompt_file=final_phase.prompt_file,
        execution=final_phase.execution,
        evaluation=final_phase.evaluation,
        model=parent_phase.model,
        depends_on=[parent_phase.id],
    )

    inner = _make_phase_node(synthetic, config, executor)
    inner.__name__ = f"final_{parent_phase.id}_{final_phase.id}"
    return inner
