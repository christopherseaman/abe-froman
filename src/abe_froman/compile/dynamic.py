"""Node factories for dynamic children (fan-out via Send)."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from abe_froman.compile.nodes import (
    _get_retry_delay,
    _make_execution_node,
    build_context,
    run_evaluation_and_outcome,
    execute_with_timeout,
    inject_retry_reason,
    make_failure_update,
)
from abe_froman.runtime.result import ExecutionResult
from abe_froman.runtime.state import REDUCERS, WorkflowState
from abe_froman.schema.models import Node, Graph

if TYPE_CHECKING:
    from abe_froman.runtime.result import NodeExecutor


def _merge_updates(base: dict[str, Any], extra: dict[str, Any]) -> dict[str, Any]:
    """Merge `extra` into `base` using the same reducers LangGraph uses.

    The fan-out node accumulates state inline across its retry loop; once
    it returns, LangGraph's super-step reducers fold the aggregate into
    prior state. Using `state.REDUCERS` here keeps the inline merge
    semantically identical to the boundary merge — single source of truth.
    Keys not in REDUCERS (e.g. `_fan_out_item`) overwrite by assignment.
    """
    out = dict(base)
    for key, value in extra.items():
        if key in out and key in REDUCERS:
            out[key] = REDUCERS[key](out[key], value)
        else:
            out[key] = value
    return out


def _make_fan_out_node(
    parent_node: Node,
    config: Graph,
    executor: NodeExecutor | None = None,
):
    """Create a template node function for dynamic children.

    Each invocation receives a different ``_fan_out_item`` via LangGraph's
    Send. Gated templates run their retry loop **inline** inside this
    node body — LangGraph merges Send-dispatched branches at any
    conditional-edge boundary, which would strip per-branch
    ``_fan_out_item`` state and break graph-level retry self-loops.
    Inline retry preserves per-item state trivially.
    """
    template = parent_node.fan_out.template
    timeout = parent_node.effective_timeout(config.settings)
    max_retries = parent_node.effective_max_retries(config.settings)
    retry_backoff = config.settings.retry_backoff

    async def node_fn(state: WorkflowState) -> dict[str, Any]:
        item = state.get("_fan_out_item", {})
        item_id = item.get("id", "unknown")
        child_id = f"{parent_node.id}::{item_id}"

        if child_id in state.get("completed_nodes", []):
            return {}
        if child_id in state.get("failed_nodes", []):
            return {}

        if state.get("dry_run", False):
            return {
                "node_outputs": {child_id: f"[dry-run] child {item_id}"},
                "child_outputs": {child_id: f"[dry-run] child {item_id}"},
                "completed_nodes": [child_id],
            }

        if executor is None:
            return {
                "node_outputs": {
                    child_id: f"[no-executor] child {item_id}"
                },
                "child_outputs": {
                    child_id: f"[no-executor] child {item_id}"
                },
                "completed_nodes": [child_id],
            }

        synthetic_node = Node(
            id=child_id,
            name=f"{parent_node.name} - {item.get('name', item_id)}",
            prompt_file=template.prompt_file,
            evaluation=template.evaluation,
            model=parent_node.model,
        )

        # Build context up front — dep outputs + per-item manifest fields
        # do not change across retries within this node invocation.
        base_context = build_context(parent_node, state)
        parent_output = state.get("node_outputs", {}).get(parent_node.id, "")
        base_context[parent_node.id] = parent_output
        for key, value in item.items():
            base_context[key] = str(value)

        # Simulated state that accumulates inline across retries within this
        # branch. Starts from the caller-provided state so reducers at the
        # super-step boundary see the whole history.
        update: dict[str, Any] = {}
        history: list[dict[str, Any]] = list(
            state.get("evaluations", {}).get(child_id, [])
        )
        retries_local = state.get("retries", {}).get(child_id, 0)

        while True:
            context = dict(base_context)
            if retries_local > 0:
                delay = _get_retry_delay(retries_local, retry_backoff)
                if delay > 0:
                    await asyncio.sleep(delay)
                synthetic_state: WorkflowState = {
                    **state,
                    "evaluations": _synth_evaluations(
                        state.get("evaluations", {}), child_id, history
                    ),
                    "retries": {
                        **state.get("retries", {}),
                        child_id: retries_local,
                    },
                }
                context = inject_retry_reason(
                    context, synthetic_node, synthetic_state,
                    max_retries, node_id=child_id,
                )

            exec_result = await execute_with_timeout(
                executor, synthetic_node, context, timeout
            )
            if exec_result == "timeout":
                return _merge_updates(update, make_failure_update(
                    child_id, f"Node timed out after {timeout}s"
                ))
            if not exec_result.success:
                return _merge_updates(update, make_failure_update(
                    child_id, exec_result.error
                ))

            exec_update: dict[str, Any] = {
                "node_outputs": {child_id: exec_result.output},
                "child_outputs": {child_id: exec_result.output},
            }
            if exec_result.tokens_used is not None:
                exec_update["token_usage"] = {child_id: exec_result.tokens_used}
            update = _merge_updates(update, exec_update)

            if not template.evaluation:
                update.setdefault("completed_nodes", []).append(child_id)
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
                    child_id: retries_local,
                },
            }
            eval_update = await run_evaluation_and_outcome(
                synthetic_node, config, eval_state, exec_result, timeout,
                backend=backend, node_id=child_id, history=history,
            )
            update = _merge_updates(update, eval_update)

            new_record = eval_update.get("evaluations", {}).get(child_id, [])
            history = history + list(new_record)

            if child_id in eval_update.get("completed_nodes", []):
                return update
            if child_id in eval_update.get("failed_nodes", []):
                return update

            # Retry outcome — increment and loop.
            bumped = eval_update.get("retries", {}).get(child_id)
            if bumped is None:
                # Defensive: no retry signal → treat as terminal to avoid infinite loop.
                return update
            retries_local = bumped

    node_fn.__name__ = f"subphase_{parent_node.id}"
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


def _make_final_fan_out_node(
    parent_node: Node,
    final_node,
    config: Graph,
    executor: NodeExecutor | None = None,
    *,
    is_first: bool = False,
):
    """Create a node function for a final node in a dynamic child group.

    Subphase aggregates reach the final node through `build_context`'s
    suffix synthesis (same mechanism any non-final downstream uses).

    The FIRST final node in the chain has an incoming static edge from
    `_sub_<parent>`. With LangGraph's Send-dispatch semantics, that edge
    fires once per Send branch — i.e. once per fan-out child. Without a
    barrier, the first final would dispatch on the FIRST child's
    completion, before sibling children land in `child_outputs`, so its
    `{{<parent>_subphases}}` template var would render against an
    incomplete state. The wrapper below short-circuits with `{}` until
    all expected manifest items have completed.

    Subsequent finals chain through the prior final and don't need the
    barrier (their predecessor already ran with full state).
    """
    node_id = f"_final_{parent_node.id}_{final_node.id}"

    synthetic = Node(
        id=node_id,
        name=final_node.name,
        description=final_node.description,
        prompt_file=final_node.prompt_file,
        execution=final_node.execution,
        evaluation=final_node.evaluation,
        model=parent_node.model,
        depends_on=[parent_node.id],
    )

    inner = _make_execution_node(synthetic, config, executor)
    inner.__name__ = f"final_{parent_node.id}_{final_node.id}"

    if not is_first:
        return inner

    # First-final barrier: wait until every manifest item's child has
    # landed in completed_nodes (or failed_nodes — failures count as
    # "settled" so we don't wait forever on a hung child).
    from abe_froman.compile.graph import _read_manifest

    parent_id = parent_node.id

    async def barrier(state: WorkflowState) -> dict[str, Any]:
        if node_id in state.get("completed_nodes", []):
            return {}
        completed = state.get("completed_nodes", [])
        failed = state.get("failed_nodes", [])
        parent_settled = parent_id in completed or parent_id in failed
        items = _read_manifest(state, parent_node)
        if items:
            settled = set(completed) | set(failed)
            done = sum(1 for it in items if f"{parent_id}::{it.get('id', 'unknown')}" in settled)
            if done < len(items):
                # Wait for more children to settle. LangGraph re-fires this
                # node on every super-step until all sibling Send branches
                # have completed.
                return {}
        elif not parent_settled:
            # Parent hasn't even produced its manifest yet — defer. Without
            # this guard, an isolated `_sub_<parent>` (Send-dispatched) that
            # transitions via static edge can fire `_final_*` before the
            # parent's prompt has run, so manifest reads empty and `inner`
            # runs against undefined `{{<parent>_subphases}}`.
            return {}
        return await inner(state)

    barrier.__name__ = f"final_{parent_node.id}_{final_node.id}"
    return barrier
