"""Workflow execution with streaming state snapshots.

Persistence is handled by the compiled graph's checkpointer (if any),
configured at compile time via ``build_workflow_graph(checkpointer=...)``.
When a ``thread_id`` is supplied, the checkpointer associates each
state snapshot with that thread so it can be resumed later.
"""

from __future__ import annotations

from typing import Any

from abe_froman.schema.models import Graph


async def run_workflow(
    compiled_graph: Any,
    initial_state: dict[str, Any],
    config: Graph,
    thread_id: str | None = None,
    log_file: str | None = None,
    logger: Any | None = None,
) -> dict[str, Any]:
    """Execute a compiled workflow graph, streaming state snapshots.

    Two logger paths:
      - ``log_file`` (convenience): the runner constructs a JsonlLogger,
        emits workflow_start / workflow_end, and closes it. Subgraph-
        internal events are NOT prefixed back into this log because the
        compile layer was never handed the logger handle.
      - ``logger`` (injection): caller owns lifecycle (workflow_start /
        workflow_end / close). The compile layer can be given the same
        handle so subgraph-internal events surface here, prefixed by
        their parent node id (`paper::reconcile`).

    CLI uses the injection path so subgraph events surface end-to-end;
    direct test/standalone callers prefer the convenience path.
    """
    from abe_froman.runtime.logging import JsonlLogger

    owns_logger = False
    if logger is None and log_file is not None:
        logger = JsonlLogger(log_file)
        logger.emit({
            "event": "workflow_start",
            "workflow": config.name,
            "version": config.version,
        })
        owns_logger = True

    last_state = initial_state
    prev_state = initial_state
    run_config = (
        {"configurable": {"thread_id": thread_id}} if thread_id else {}
    )

    async for snapshot in compiled_graph.astream(
        initial_state, config=run_config, stream_mode="values"
    ):
        last_state = snapshot
        if logger is not None:
            logger.log_snapshot(prev_state, snapshot)
            prev_state = snapshot

    if owns_logger:
        logger.emit({
            "event": "workflow_end",
            "completed": len(last_state.get("completed_nodes", [])),
            "failed": len(last_state.get("failed_nodes", [])),
        })
        logger.close()

    return last_state
