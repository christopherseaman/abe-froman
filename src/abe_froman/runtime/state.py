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
    completed_nodes: Annotated[list[str], operator.add]
    failed_nodes: Annotated[list[str], operator.add]
    node_outputs: Annotated[dict[str, Any], _merge_dicts]
    phase_structured_outputs: Annotated[dict[str, Any], _merge_dicts]
    evaluations: Annotated[dict[str, list[dict[str, Any]]], _merge_evaluations]
    retries: Annotated[dict[str, int], _merge_dicts]
    child_outputs: Annotated[dict[str, Any], _merge_dicts]
    token_usage: Annotated[dict[str, dict[str, int]], _merge_dicts]
    node_worktrees: Annotated[dict[str, str], _merge_dicts]
    errors: Annotated[list[dict], operator.add]
    workdir: str
    dry_run: bool
    _fan_out_item: NotRequired[dict[str, Any]]
    # Subgraph context: rendered inputs visible as template vars to subgraph
    # nodes. Set by the subgraph wrapper before subgraph invocation; not
    # populated at the top level. Merged into build_context output.
    node_inputs: NotRequired[dict[str, str]]


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
        "completed_nodes": [],
        "failed_nodes": [],
        "node_outputs": {},
        "phase_structured_outputs": {},
        "evaluations": {},
        "retries": {},
        "child_outputs": {},
        "token_usage": {},
        "node_worktrees": {},
        "errors": [],
        "workdir": workdir,
        "dry_run": dry_run,
    }
    state.update(overrides)
    return state
