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


def _merge_sets(left: set[str], right: set[str]) -> set[str]:
    """Set-union reducer for `completed_nodes` / `failed_nodes`.

    O(1) membership for the `in completed_nodes` guards scattered through
    `compile/nodes.py` and `compile/dynamic.py`, and structurally prevents
    duplicate accumulation when a goto-driven re-fire (or `--resume`-driven
    replay) writes the same node id more than once.
    """
    return left | right


# Reducer table — single source of truth for how state fields combine.
# Mirrors WorkflowState's Annotated metadata; consumed by both LangGraph
# (via the TypedDict annotations) and `dynamic._merge_updates` (when the
# fan-out node accumulates state inline across its retry loop).
REDUCERS: dict[str, Callable[[Any, Any], Any]] = {
    "completed_nodes": _merge_sets,
    "failed_nodes": _merge_sets,
    "errors": operator.add,
    "node_outputs": _merge_dicts,
    "node_structured_outputs": _merge_dicts,
    "retries": _merge_dicts,
    "child_outputs": _merge_dicts,
    "node_worktrees": _merge_dicts,
    "node_models": _merge_dicts,
    "evaluations": _merge_evaluations,
}


class WorkflowState(TypedDict):
    workflow_name: str
    completed_nodes: Annotated[set[str], REDUCERS["completed_nodes"]]
    failed_nodes: Annotated[set[str], REDUCERS["failed_nodes"]]
    node_outputs: Annotated[dict[str, Any], REDUCERS["node_outputs"]]
    node_structured_outputs: Annotated[dict[str, Any], REDUCERS["node_structured_outputs"]]
    evaluations: Annotated[dict[str, list[dict[str, Any]]], REDUCERS["evaluations"]]
    retries: Annotated[dict[str, int], REDUCERS["retries"]]
    child_outputs: Annotated[dict[str, Any], REDUCERS["child_outputs"]]
    node_worktrees: Annotated[dict[str, str], REDUCERS["node_worktrees"]]
    node_models: Annotated[dict[str, Any], REDUCERS["node_models"]]
    errors: Annotated[list[dict], REDUCERS["errors"]]
    workdir: str
    dry_run: bool
    _fan_out_item: NotRequired[dict[str, Any]]
    # Subgraph context: rendered inputs visible as template vars to subgraph
    # nodes. Set by the subgraph wrapper before subgraph invocation; not
    # populated at the top level. Merged into build_context output.
    node_inputs: NotRequired[dict[str, str]]
    # Inline-route sender threading (Stage 5c). Set by an inline-route
    # node's `_route_X` synthetic node when it emits Command(goto=...);
    # read by the goto target's `build_context` to bind `{{sender}}` /
    # `{{sender_id}}` / `{{sender_*}}` and (when include_eval=True) to
    # auto-prepend the neutral eval preamble. Last-write-wins via
    # `_merge_updates`'s default overwrite path — no REDUCER entry.
    _route_sender: NotRequired[str]
    # Pre-built eval preamble string. The synthetic `_route_<id>`
    # builds this at dispatch time when `include_eval: true` is set on
    # the matched case AND the source node has an evaluation that
    # produced a result. ``_dispatch_prompt`` reads it from context
    # (via build_context) and concatenates before the rendered prompt
    # body — auto-prepend, no template syntax required. Empty string
    # or absent = no preamble.
    _route_eval_preamble: NotRequired[str]
    # Resume skip-set (skip-completed --resume). A FROZEN snapshot of node
    # ids that completed in the prior run and are safe to skip this run.
    # Seeded ONCE at resume entry from the prior checkpoint; never written by
    # a node body, so it persists unchanged across super-steps. Last-write-wins
    # (NO REDUCER — must not set-union-accumulate). Guards read it; nothing
    # mutates it. Absent on a fresh run => skip nothing.
    _resume_skip: NotRequired[set[str]]
    # Prior fan-out branch ids per parent, for the `--resume` manifest-drift
    # guard. `{parent_id: {"<parent>::<item>", ...}}` — the DIRECT branch ids
    # (completed | failed) from the prior checkpoint. Seeded ONCE at resume
    # entry (`cli/main.py::_seed_state`), only for parents that had children,
    # and NOT on fresh / --entry / --rerun-all. Read by the `_fan_<id>`
    # dispatcher to detect a re-fan that DROPS a prior branch. Frozen,
    # last-write-wins (NO REDUCER). Absent => no drift check.
    _fan_prior_children: NotRequired[dict[str, set[str]]]


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
        "completed_nodes": set(),
        "failed_nodes": set(),
        "node_outputs": {},
        "node_structured_outputs": {},
        "evaluations": {},
        "retries": {},
        "child_outputs": {},
        "node_worktrees": {},
        "node_models": {},
        "errors": [],
        "workdir": workdir,
        "dry_run": dry_run,
    }
    state.update(overrides)
    return state
