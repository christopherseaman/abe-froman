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
    *,
    compile_fn: Any = None,
    base_dir: Any = None,
    depth: int = 0,
):
    """Create a template node function for dynamic children.

    Each invocation receives a different ``_fan_out_item`` via LangGraph's
    Send. Gated templates run their retry loop **inline** inside this
    node body — LangGraph merges Send-dispatched branches at any
    conditional-edge boundary, which would strip per-branch
    ``_fan_out_item`` state and break graph-level retry self-loops.
    Inline retry preserves per-item state trivially.

    Two template kinds are supported:
      - ``template.prompt_file``: each Send branch executes a synthetic
        Node carrying that prompt against parent context + manifest item.
      - ``template.config``: each Send branch invokes a recursive
        subgraph in isolation, with ``template.inputs`` rendered against
        parent context + manifest item and projected into the subgraph's
        ``node_inputs`` channel. The subgraph's terminal output becomes
        the child's output. ``compile_fn``, ``base_dir``, and ``depth``
        are required for this path so the subgraph can be pre-compiled
        once at factory time.
    """
    template = parent_node.fan_out.template
    timeout = parent_node.effective_timeout(config.settings)
    max_retries = parent_node.effective_max_retries(config.settings)
    retry_backoff = config.settings.retry_backoff

    # Pre-compile the per-child subgraph if the template uses config:
    sub_compiled = None
    sub_config = None
    if template.config:
        if compile_fn is None or base_dir is None:
            raise ValueError(
                f"Fan-out template on '{parent_node.id}' uses config: "
                "but factory wasn't given compile_fn/base_dir"
            )
        from abe_froman.compile.subgraph import load_graph
        sub_config = load_graph(template.config, base_dir=base_dir)
        sub_compiled = compile_fn(
            sub_config, executor=executor, _depth=depth + 1,
        )
    inputs_decl = dict(template.inputs)

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

        if executor is None and sub_compiled is None:
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

            if sub_compiled is not None:
                exec_result = await _invoke_fan_out_subgraph(
                    sub_compiled, sub_config, inputs_decl, context, state,
                    timeout=timeout,
                )
            else:
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


async def _invoke_fan_out_subgraph(
    sub_compiled: Any,
    sub_config: Graph,
    inputs_decl: dict[str, str],
    context: dict[str, Any],
    parent_state: WorkflowState,
    *,
    timeout: float | None,
) -> ExecutionResult | str:
    """Invoke a per-child subgraph and project its terminal output as an
    ExecutionResult. Mirrors the project's existing subgraph wrapper
    semantics (see compile/subgraph.py::make_subgraph_node) but adapted
    to the fan-out inline-retry flow.

    Inputs are rendered against the per-Send context (parent deps +
    parent output + manifest item fields) so each branch projects its
    own item into the subgraph. Returns an ExecutionResult with the
    subgraph's terminal-node output, or "timeout" / a failure result.
    """
    from abe_froman.compile.subgraph import _terminal_node_output
    from abe_froman.runtime.executor.prompt import render_template
    from abe_froman.runtime.state import make_initial_state

    rendered_inputs = {
        k: render_template(v, context) for k, v in inputs_decl.items()
    }
    sub_state = make_initial_state(
        workflow_name=sub_config.name,
        workdir=parent_state.get("workdir", "."),
        dry_run=parent_state.get("dry_run", False),
    )
    sub_state["node_inputs"] = rendered_inputs

    try:
        if timeout is not None:
            sub_result = await asyncio.wait_for(
                sub_compiled.ainvoke(sub_state), timeout=timeout,
            )
        else:
            sub_result = await sub_compiled.ainvoke(sub_state)
    except asyncio.TimeoutError:
        return "timeout"

    if sub_result.get("failed_nodes"):
        errors = sub_result.get("errors", [])
        msg = (
            f"subgraph '{sub_config.name}' failed: "
            f"{sub_result['failed_nodes']}"
        )
        if errors:
            msg += f" ({errors[0].get('error', '')})"
        return ExecutionResult(success=False, output="", error=msg)

    terminal_output = _terminal_node_output(sub_result, sub_config)
    return ExecutionResult(success=True, output=terminal_output)


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
        # Defer (return {}) in any of three cases:
        #   1. already done — LangGraph re-fires the node every super-step
        #      until termination; without this skip, inner runs twice.
        #   2. items known but not all settled — fan-out children still
        #      in flight; wait for them.
        #   3. items empty AND parent hasn't run yet — LangGraph fires
        #      conditional edges pre-emptively (when an upstream-of-parent
        #      completes), routing 'no_items' here before the parent's
        #      prompt produces a manifest. Empty items here means "unknown",
        #      not "zero children".
        completed = state.get("completed_nodes", [])
        failed = state.get("failed_nodes", [])
        items = _read_manifest(state, parent_node)
        settled = set(completed) | set(failed)
        children_pending = items and any(
            f"{parent_id}::{it.get('id', 'unknown')}" not in settled for it in items
        )
        parent_unsettled = parent_id not in completed and parent_id not in failed
        should_defer = (
            node_id in completed
            or children_pending
            or (not items and parent_unsettled)
        )
        if should_defer:
            return {}
        return await inner(state)

    barrier.__name__ = f"final_{parent_node.id}_{final_node.id}"
    return barrier
