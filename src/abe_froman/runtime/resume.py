"""Prepare workflow state for resume/start-from scenarios."""

from __future__ import annotations

from typing import Any

from abe_froman.schema.models import WorkflowConfig

_STATE_DICT_KEYS = [
    "phase_outputs", "phase_structured_outputs", "gate_scores",
    "subphase_outputs", "token_usage",
]


def _filter_state(old: dict[str, Any], keep: set | list) -> dict[str, Any]:
    keep_set = set(keep)
    return {
        key: {k: v for k, v in old.get(key, {}).items() if k in keep_set}
        for key in _STATE_DICT_KEYS
    }


def _build_state(
    config_name: str, completed: list[str], old: dict[str, Any], keep: set | list, workdir: str
) -> dict[str, Any]:
    return {
        "workflow_name": config_name,
        "completed_phases": completed,
        "failed_phases": [],
        **_filter_state(old, keep),
        "retries": {},
        "errors": [],
        "workdir": workdir,
        "dry_run": False,
    }


def _upstream_phases(config: WorkflowConfig, phase_id: str) -> set[str]:
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
    """Resume from a saved checkpoint. Clears failures for re-execution."""
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

    return _build_state(config.name, completed, old, completed, workdir)


def prepare_start_state(
    saved: dict[str, Any],
    config: WorkflowConfig,
    start_phase_id: str,
    workdir: str,
) -> dict[str, Any]:
    """Start from a specific phase, keeping upstream outputs."""
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
    return _build_state(config.name, completed, old, upstream, workdir)
