from __future__ import annotations

import operator
from typing import Annotated, Any, NotRequired

from typing_extensions import TypedDict


def _merge_dicts(left: dict, right: dict) -> dict:
    merged = left.copy()
    merged.update(right)
    return merged


def _merge_evaluations(
    left: dict[str, list[dict[str, Any]]],
    right: dict[str, list[dict[str, Any]]],
) -> dict[str, list[dict[str, Any]]]:
    """Append-per-key reducer for `evaluations` — history grows, never replaces."""
    merged: dict[str, list[dict[str, Any]]] = {k: list(v) for k, v in left.items()}
    for key, new_records in right.items():
        merged.setdefault(key, []).extend(new_records)
    return merged


class WorkflowState(TypedDict):
    workflow_name: str
    completed_phases: Annotated[list[str], operator.add]
    failed_phases: Annotated[list[str], operator.add]
    phase_outputs: Annotated[dict[str, Any], _merge_dicts]
    phase_structured_outputs: Annotated[dict[str, Any], _merge_dicts]
    gate_scores: Annotated[dict[str, float], _merge_dicts]
    gate_feedback: Annotated[dict[str, dict[str, Any]], _merge_dicts]
    evaluations: Annotated[dict[str, list[dict[str, Any]]], _merge_evaluations]
    retries: Annotated[dict[str, int], _merge_dicts]
    subphase_outputs: Annotated[dict[str, Any], _merge_dicts]
    token_usage: Annotated[dict[str, dict[str, int]], _merge_dicts]
    phase_worktrees: Annotated[dict[str, str], _merge_dicts]
    errors: Annotated[list[dict], operator.add]
    workdir: str
    dry_run: bool
    _subphase_item: NotRequired[dict[str, Any]]


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
        "completed_phases": [],
        "failed_phases": [],
        "phase_outputs": {},
        "phase_structured_outputs": {},
        "gate_scores": {},
        "gate_feedback": {},
        "evaluations": {},
        "retries": {},
        "subphase_outputs": {},
        "token_usage": {},
        "phase_worktrees": {},
        "errors": [],
        "workdir": workdir,
        "dry_run": dry_run,
    }
    state.update(overrides)
    return state
