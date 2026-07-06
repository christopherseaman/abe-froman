"""Workflow execution with streaming state snapshots.

Persistence is handled by the compiled graph's checkpointer (if any),
configured at compile time via ``build_workflow_graph(checkpointer=...)``.
When a ``thread_id`` is supplied, the checkpointer associates each
state snapshot with that thread so it can be resumed later.
"""

from __future__ import annotations

from typing import Any

from sqrlly.schema.models import Graph


async def run_workflow(
    compiled_graph: Any,
    initial_state: dict[str, Any],
    config: Graph,
    thread_id: str | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Execute a compiled workflow graph, streaming state snapshots.

    ``logger`` is caller-owned (workflow_start / workflow_end / close
    lifecycle). The compile layer can be given the same handle so
    subgraph-internal events surface here, prefixed by their parent
    node id (`paper::reconcile`).
    """
    last_state = initial_state
    run_config = (
        {"configurable": {"thread_id": thread_id}} if thread_id else {}
    )

    # Tuple stream_mode: each chunk is ``(mode, payload)``.
    #   - ``("updates", {node_name: partial_update})`` per super-step;
    #     events derive from the partial update directly (no diffing).
    #   - ``("values", cumulative_state_dict)`` snapshots; tracked here
    #     only to capture the final state for the run summary.
    async for chunk_type, payload in compiled_graph.astream(
        initial_state, config=run_config,
        stream_mode=["updates", "values"],
    ):
        if chunk_type == "values":
            last_state = payload
        elif chunk_type == "updates" and logger is not None:
            for _node_name, update in payload.items():
                logger.log_update(update)
    return last_state
