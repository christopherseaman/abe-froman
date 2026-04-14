"""Workflow state persistence — save/load/clear state to disk."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_FILENAME = ".abe-froman-state.json"
STATE_VERSION = 1


def state_file_path(workdir: str) -> Path:
    """Return the state file path for a workdir."""
    return Path(workdir) / STATE_FILENAME


def save_state(
    state: dict[str, Any],
    workdir: str,
    config_name: str,
    config_version: str,
) -> Path:
    """Persist workflow state to disk atomically.

    Writes to a temp file first, then renames to avoid partial writes.
    """
    envelope = {
        "version": STATE_VERSION,
        "config_name": config_name,
        "config_version": config_version,
        "saved_at": datetime.now(timezone.utc).isoformat(),
        "state": state,
    }

    target = state_file_path(workdir)
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(envelope, indent=2, default=str))
    os.replace(str(tmp), str(target))
    return target


def load_state(workdir: str) -> dict[str, Any] | None:
    """Load saved state envelope from disk.

    Returns None if no state file exists.
    Raises ValueError on corrupt JSON or version mismatch.
    """
    path = state_file_path(workdir)
    if not path.exists():
        return None

    try:
        envelope = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise ValueError(f"Corrupt state file {path}: {e}") from e

    if envelope.get("version") != STATE_VERSION:
        raise ValueError(
            f"State file version {envelope.get('version')} "
            f"!= expected {STATE_VERSION}"
        )

    return envelope


def clear_state(workdir: str) -> None:
    """Remove the state file if it exists."""
    path = state_file_path(workdir)
    path.unlink(missing_ok=True)
