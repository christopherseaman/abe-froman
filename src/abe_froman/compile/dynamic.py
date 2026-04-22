"""Node factories for dynamic subphases (fan-out via Send)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from abe_froman.compile.nodes import (
    _make_phase_node,
    build_context,
    execute_with_timeout,
    make_failure_update,
)
from abe_froman.runtime.gates import evaluate_gate
from abe_froman.runtime.state import WorkflowState
from abe_froman.schema.models import Phase, WorkflowConfig

if TYPE_CHECKING:
    from abe_froman.runtime.result import PhaseExecutor


def _make_subphase_node(
    parent_phase: Phase,
    config: WorkflowConfig,
    executor: PhaseExecutor | None = None,
):
    """Create a template node function for dynamic subphases.

    Each invocation receives a different ``_subphase_item`` via LangGraph's
    Send — the manifest item dict for this particular subphase instance.
    """
    template = parent_phase.dynamic_subphases.template
    timeout = parent_phase.effective_timeout(config.settings)

    async def node_fn(state: WorkflowState) -> dict[str, Any]:
        item = state.get("_subphase_item", {})
        item_id = item.get("id", "unknown")
        subphase_id = f"{parent_phase.id}::{item_id}"

        if subphase_id in state.get("completed_phases", []):
            return {}

        if state.get("dry_run", False):
            return {
                "completed_phases": [subphase_id],
                "phase_outputs": {subphase_id: f"[dry-run] subphase {item_id}"},
                "subphase_outputs": {subphase_id: f"[dry-run] subphase {item_id}"},
            }

        if executor is None:
            return {
                "completed_phases": [subphase_id],
                "phase_outputs": {
                    subphase_id: f"[no-executor] subphase {item_id}"
                },
                "subphase_outputs": {
                    subphase_id: f"[no-executor] subphase {item_id}"
                },
            }

        synthetic_phase = Phase(
            id=subphase_id,
            name=f"{parent_phase.name} - {item.get('name', item_id)}",
            prompt_file=template.prompt_file,
            model=parent_phase.model,
        )

        # Subphase templates inherit the parent phase's full upstream context
        # so `{{dep}}` works for any of the parent's dependencies, not just
        # the parent output itself. Per-item manifest fields layer on top.
        context = build_context(parent_phase, state)
        parent_output = state.get("phase_outputs", {}).get(parent_phase.id, "")
        context[parent_phase.id] = parent_output
        for key, value in item.items():
            context[key] = str(value)

        exec_result = await execute_with_timeout(
            executor, synthetic_phase, context, timeout
        )
        if exec_result == "timeout":
            return make_failure_update(
                subphase_id, f"Phase timed out after {timeout}s"
            )
        if not exec_result.success:
            return make_failure_update(subphase_id, exec_result.error)

        update: dict[str, Any] = {
            "completed_phases": [subphase_id],
            "phase_outputs": {subphase_id: exec_result.output},
            "subphase_outputs": {subphase_id: exec_result.output},
        }
        if exec_result.tokens_used is not None:
            update["token_usage"] = {subphase_id: exec_result.tokens_used}

        if template.quality_gate:
            import asyncio

            backend = (
                executor.get_backend() if hasattr(executor, "get_backend") else None
            )
            gate_call = evaluate_gate(
                template.quality_gate,
                subphase_id,
                workdir=state.get("workdir", "."),
                phase_output=exec_result.output,
                workflow_name=config.name,
                attempt_number=1,
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
                    subphase_id, f"Quality gate timed out after {timeout}s"
                )
            update["gate_scores"] = {subphase_id: gate_result.score}
            update["gate_feedback"] = {
                subphase_id: {
                    "feedback": gate_result.feedback,
                    "pass_criteria_met": list(gate_result.pass_criteria_met),
                    "pass_criteria_unmet": list(gate_result.pass_criteria_unmet),
                }
            }

        return update

    node_fn.__name__ = f"subphase_{parent_phase.id}"
    return node_fn


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
        quality_gate=final_phase.quality_gate,
        model=parent_phase.model,
        depends_on=[parent_phase.id],
    )

    inner = _make_phase_node(synthetic, config, executor)
    inner.__name__ = f"final_{parent_phase.id}_{final_phase.id}"
    return inner
