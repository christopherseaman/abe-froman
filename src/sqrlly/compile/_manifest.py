"""Shared langgraph-free compile helpers.

Lives in its own module so `compile/graph.py`, `compile/dynamic.py`,
and `compile/subgraph.py` can all import these without crossing a
private-import boundary in the other direction. Pure functions — no
langgraph imports.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from sqrlly.runtime.state import WorkflowState
from sqrlly.schema.models import Node

if TYPE_CHECKING:
    from sqrlly.schema.models import Graph


def find_terminal_nodes(config: "Graph") -> list[str]:
    """Node ids not depended on by any other node, in declaration order.

    A subgraph's terminal output is the *last* such node; the top-level
    builder wants the set. Returning an ordered list serves both —
    callers ``set(...)`` it or take ``[-1]`` as needed.
    """
    depended_on: set[str] = set()
    for node in config.nodes:
        depended_on.update(node.depends_on)
    return [n.id for n in config.nodes if n.id not in depended_on]


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
