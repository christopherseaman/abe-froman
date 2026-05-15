"""Shared `_read_manifest` helper for fan-out manifest resolution.

Lives in its own module so both `compile/graph.py` (where the fan-out
parent's conditional-edge router reads the manifest) and
`compile/dynamic.py` (where the final-node aggregator reads it) can
import it without crossing the private-import boundary in the other
direction. Pure function — no langgraph imports.
"""
from __future__ import annotations

import json
from pathlib import Path

from sqrlly.runtime.state import WorkflowState
from sqrlly.schema.models import Node


def _read_manifest(state: WorkflowState, node: Node) -> list[dict]:
    """Resolve the manifest a fan-out parent dispatches over.

    Two-step resolution: first try parsing the parent's own
    ``node_outputs`` entry as JSON (canonical for runtime-generated
    manifests); on parse failure or missing key, fall back to the
    on-disk path declared in ``fan_out.manifest_path`` (canonical for
    static manifests checked into the repo). Returns ``[]`` on miss
    so the conditional-edge router can route to ``no_items`` cleanly.
    """
    output = state.get("node_outputs", {}).get(node.id, "")
    try:
        data = json.loads(output)
        if isinstance(data, dict) and "items" in data:
            return data["items"]
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, TypeError):
        pass

    if node.fan_out and node.fan_out.manifest_path:
        manifest_file = (
            Path(state.get("workdir", ".")) / node.fan_out.manifest_path
        )
        try:
            data = json.loads(manifest_file.read_text())
            if isinstance(data, dict) and "items" in data:
                return data["items"]
            if isinstance(data, list):
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    return []
