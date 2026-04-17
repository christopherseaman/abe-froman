"""Workflow execution with streaming state snapshots.

Persistence is handled by the compiled graph's checkpointer (if any),
configured at compile time via ``build_workflow_graph(checkpointer=...)``.
When a ``thread_id`` is supplied, the checkpointer associates each
state snapshot with that thread so it can be resumed later.
"""

from __future__ import annotations

from typing import Any

from abe_froman.schema.models import WorkflowConfig


async def run_workflow(
    compiled_graph: Any,
    initial_state: dict[str, Any],
    config: WorkflowConfig,
    thread_id: str | None = None,
    log_file: str | None = None,
) -> dict[str, Any]:
    """Execute a compiled workflow graph, streaming state snapshots.

    When ``log_file`` is provided, structured JSONL events are written for
    each state transition (phase completions, failures, gates, retries).
    """
    from abe_froman.runtime.logging import JsonlLogger

    last_state = initial_state
    logger: JsonlLogger | None = None

    if log_file is not None:
        logger = JsonlLogger(log_file)
        logger.emit({
            "event": "workflow_start",
            "workflow": config.name,
            "version": config.version,
        })

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

    if logger is not None:
        logger.emit({
            "event": "workflow_end",
            "completed": len(last_state.get("completed_phases", [])),
            "failed": len(last_state.get("failed_phases", [])),
        })
        logger.close()

    return last_state
