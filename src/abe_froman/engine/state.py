from __future__ import annotations

import operator
from typing import Annotated, Any

from typing_extensions import TypedDict


def _merge_dicts(left: dict, right: dict) -> dict:
    merged = left.copy()
    merged.update(right)
    return merged


class WorkflowState(TypedDict):
    workflow_name: str
    current_phase_id: str | None
    completed_phases: Annotated[list[str], operator.add]
    failed_phases: Annotated[list[str], operator.add]
    phase_outputs: Annotated[dict[str, Any], _merge_dicts]
    phase_structured_outputs: Annotated[dict[str, Any], _merge_dicts]
    gate_scores: Annotated[dict[str, float], _merge_dicts]
    retries: Annotated[dict[str, int], _merge_dicts]
    subphase_outputs: Annotated[dict[str, Any], _merge_dicts]
    errors: Annotated[list[dict], operator.add]
    workdir: str
    dry_run: bool


def make_initial_state(
    workflow_name: str = "Workflow",
    workdir: str = ".",
    dry_run: bool = False,
    **overrides: Any,
) -> dict[str, Any]:
    """Create initial state dict for graph invocation.

    Single source of truth for all state fields — used by CLI and tests.
    """
    state: dict[str, Any] = {
        "workflow_name": workflow_name,
        "current_phase_id": None,
        "completed_phases": [],
        "failed_phases": [],
        "phase_outputs": {},
        "phase_structured_outputs": {},
        "gate_scores": {},
        "retries": {},
        "subphase_outputs": {},
        "errors": [],
        "workdir": workdir,
        "dry_run": dry_run,
    }
    state.update(overrides)
    return state
