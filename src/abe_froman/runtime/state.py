from __future__ import annotations

import operator
from typing import Annotated, Any, Callable, NotRequired

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


# Reducer table — single source of truth for how state fields combine.
# Mirrors WorkflowState's Annotated metadata; consumed by both LangGraph
# (via the TypedDict annotations) and `dynamic._merge_updates` (when the
# fan-out node accumulates state inline across its retry loop).
REDUCERS: dict[str, Callable[[Any, Any], Any]] = {
    "completed_nodes": operator.add,
    "failed_nodes": operator.add,
    "errors": operator.add,
    "node_outputs": _merge_dicts,
    "node_structured_outputs": _merge_dicts,
    "retries": _merge_dicts,
    "child_outputs": _merge_dicts,
    "node_worktrees": _merge_dicts,
    "evaluations": _merge_evaluations,
}


class WorkflowState(TypedDict):
    workflow_name: str
    completed_nodes: Annotated[list[str], REDUCERS["completed_nodes"]]
    failed_nodes: Annotated[list[str], REDUCERS["failed_nodes"]]
    node_outputs: Annotated[dict[str, Any], REDUCERS["node_outputs"]]
    node_structured_outputs: Annotated[dict[str, Any], REDUCERS["node_structured_outputs"]]
    evaluations: Annotated[dict[str, list[dict[str, Any]]], REDUCERS["evaluations"]]
    retries: Annotated[dict[str, int], REDUCERS["retries"]]
    child_outputs: Annotated[dict[str, Any], REDUCERS["child_outputs"]]
    node_worktrees: Annotated[dict[str, str], REDUCERS["node_worktrees"]]
    errors: Annotated[list[dict], REDUCERS["errors"]]
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
        "node_structured_outputs": {},
        "evaluations": {},
        "retries": {},
        "child_outputs": {},
        "node_worktrees": {},
        "errors": [],
        "workdir": workdir,
        "dry_run": dry_run,
    }
    state.update(overrides)
    return state
