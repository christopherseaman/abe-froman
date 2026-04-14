"""Prepare workflow state for resume/start-from scenarios."""

from __future__ import annotations

from typing import Any

from abe_froman.schema.models import WorkflowConfig


def _upstream_phases(config: WorkflowConfig, phase_id: str) -> set[str]:
    """Return all transitive dependencies of phase_id (not including itself)."""
    adj = {p.id: list(p.depends_on) for p in config.phases}
    result: set[str] = set()
    stack = list(adj.get(phase_id, []))
    while stack:
        dep = stack.pop()
        if dep not in result:
            result.add(dep)
            stack.extend(adj.get(dep, []))
    return result


def prepare_resume_state(
    saved: dict[str, Any],
    config: WorkflowConfig,
    workdir: str,
) -> dict[str, Any]:
    """Build a state dict that resumes from a saved checkpoint.

    Keeps completed phases and their outputs. Clears failures so failed
    phases get re-executed with a fresh retry budget.
    """
    saved_name = saved.get("config_name", "")
    if saved_name != config.name:
        raise ValueError(
            f"State file is from workflow '{saved_name}', "
            f"but config is '{config.name}'"
        )

    old = saved["state"]
    completed = list(old.get("completed_phases", []))
    failed = set(old.get("failed_phases", []))

    completed = [p for p in completed if p not in failed]

    valid_ids = {p.id for p in config.phases}
    completed = [p for p in completed if p in valid_ids or "::" in p]

    phase_outputs = {
        k: v for k, v in old.get("phase_outputs", {}).items()
        if k in completed
    }
    phase_structured_outputs = {
        k: v for k, v in old.get("phase_structured_outputs", {}).items()
        if k in completed
    }
    gate_scores = {
        k: v for k, v in old.get("gate_scores", {}).items()
        if k in completed
    }
    subphase_outputs = {
        k: v for k, v in old.get("subphase_outputs", {}).items()
        if k in completed
    }
    token_usage = {
        k: v for k, v in old.get("token_usage", {}).items()
        if k in completed
    }

    return {
        "workflow_name": config.name,
        "completed_phases": completed,
        "failed_phases": [],
        "phase_outputs": phase_outputs,
        "phase_structured_outputs": phase_structured_outputs,
        "gate_scores": gate_scores,
        "retries": {},
        "subphase_outputs": subphase_outputs,
        "token_usage": token_usage,
        "errors": [],
        "workdir": workdir,
        "dry_run": False,
    }


def prepare_start_state(
    saved: dict[str, Any],
    config: WorkflowConfig,
    start_phase_id: str,
    workdir: str,
) -> dict[str, Any]:
    """Build a state dict that starts from a specific phase.

    Marks upstream phases as completed (using saved outputs) so the
    start phase and everything after it gets re-executed.
    """
    valid_ids = {p.id for p in config.phases}
    if start_phase_id not in valid_ids:
        raise ValueError(
            f"Phase '{start_phase_id}' not found in config. "
            f"Available: {', '.join(sorted(valid_ids))}"
        )

    upstream = _upstream_phases(config, start_phase_id)
    old = saved["state"]
    old_completed = set(old.get("completed_phases", []))

    missing = upstream - old_completed
    if missing:
        raise ValueError(
            f"Cannot start from '{start_phase_id}': upstream phases "
            f"not completed in saved state: {', '.join(sorted(missing))}"
        )

    completed = [p for p in old.get("completed_phases", []) if p in upstream]

    phase_outputs = {
        k: v for k, v in old.get("phase_outputs", {}).items()
        if k in upstream
    }
    phase_structured_outputs = {
        k: v for k, v in old.get("phase_structured_outputs", {}).items()
        if k in upstream
    }
    gate_scores = {
        k: v for k, v in old.get("gate_scores", {}).items()
        if k in upstream
    }
    subphase_outputs = {
        k: v for k, v in old.get("subphase_outputs", {}).items()
        if k in upstream
    }
    token_usage = {
        k: v for k, v in old.get("token_usage", {}).items()
        if k in upstream
    }

    return {
        "workflow_name": config.name,
        "completed_phases": completed,
        "failed_phases": [],
        "phase_outputs": phase_outputs,
        "phase_structured_outputs": phase_structured_outputs,
        "gate_scores": gate_scores,
        "retries": {},
        "subphase_outputs": subphase_outputs,
        "token_usage": token_usage,
        "errors": [],
        "workdir": workdir,
        "dry_run": False,
    }
