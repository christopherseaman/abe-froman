"""Workflow execution with streaming state persistence."""

from __future__ import annotations

from typing import Any

from abe_froman.schema.models import WorkflowConfig
from abe_froman.runtime.persistence import clear_state, save_state


async def run_workflow(
    compiled_graph: Any,
    initial_state: dict[str, Any],
    config: WorkflowConfig,
    persist: bool = True,
    log_file: str | None = None,
) -> dict[str, Any]:
    """Execute a compiled workflow graph with optional state persistence.

    Streams state snapshots from LangGraph. After each snapshot, persists
    the current state to disk so the workflow can be resumed on failure.
    Clears the state file on successful completion.

    When log_file is provided, structured JSONL events are written for
    each state transition (phase completions, failures, gates, retries).
    """
    from abe_froman.runtime.logging import JsonlLogger

    workdir = initial_state.get("workdir", ".")
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

    async for snapshot in compiled_graph.astream(
        initial_state, stream_mode="values"
    ):
        last_state = snapshot
        if persist:
            save_state(snapshot, workdir, config.name, config.version)
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

    if persist and not last_state.get("failed_phases"):
        clear_state(workdir)

    return last_state
