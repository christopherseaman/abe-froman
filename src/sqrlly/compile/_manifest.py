"""Shared langgraph-free compile helpers.

Lives in its own module so `compile/graph.py`, `compile/dynamic.py`,
and `compile/subgraph.py` can all import these without crossing a
private-import boundary in the other direction. Pure functions — no
langgraph imports.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from sqrlly.runtime._json import extract_json
from sqrlly.runtime.result import ManifestError
from sqrlly.runtime.state import WorkflowState
from sqrlly.schema.models import Node

if TYPE_CHECKING:
    from collections.abc import Iterable

    from sqrlly.schema.models import Graph

logger = logging.getLogger(__name__)


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


def direct_child_ids(parent_id: str, node_ids: Iterable[str]) -> set[str]:
    """A fan-out parent's DIRECT branch ids (``<parent>::<item>``) in ``node_ids``.

    Excludes the bare parent, other parents, and any deeper
    ``<parent>::<item>::<inner>`` id. Today a subgraph-template branch records
    only its own ``<parent>::<item>`` id at the top level — inner subgraph node
    ids stay in the subgraph's own state (``compile/subgraph.py::make_subgraph_node``
    returns just the branch id), so they never reach ``completed_nodes`` — but
    this exclusion is DEFENSIVE: if an inner id ever surfaced at the top level
    (e.g. a future fan-out-in-subgraph resume), counting it as a branch child
    would make the drift guard false-fire on the (working) subgraph-template
    shape.
    """
    prefix = f"{parent_id}::"
    return {
        nid for nid in node_ids
        if nid.startswith(prefix) and "::" not in nid[len(prefix):]
    }


def manifest_drift(prior_children: set[str], new_child_ids: set[str]) -> set[str]:
    """Prior fan-out branch ids NOT covered by the freshly read manifest.

    ``prior - new``: empty for a stable-id resume OR a purely additive re-fan
    (``new`` is a superset of ``prior``), so those never trip the drift guard —
    it fires only when a resume DROPS a prior branch (silent data loss: a
    completed sibling vanishes, or the failed child is orphaned).
    """
    return set(prior_children) - set(new_child_ids)


def _normalize_items(items: object) -> list[dict]:
    """Coerce a manifest's items into a list of objects.

    A bare scalar item (string / number / bool) becomes
    ``{"id": str(item)}`` so a manifest like ``["alpha", "beta"]`` fans
    out with ``{{id}}`` bound per branch — rather than crashing on
    ``item.get("id")``. A non-list manifest, or an item that is neither
    object nor scalar (a nested list / null), is an author error and
    raises.
    """
    if not isinstance(items, list):
        raise ValueError(
            "fan-out manifest must be a JSON array (or {\"items\": [...]}), "
            f"got {type(items).__name__}"
        )
    out: list[dict] = []
    for i, item in enumerate(items):
        if isinstance(item, dict):
            if "id" not in item:
                # An id-less dict item collapses every branch onto one
                # `<parent>::unknown` child (the fan-out body keys child_id
                # off item.get("id", "unknown")). Warn loudly — this is a
                # silent N→1 fan-out collapse the author rarely intends.
                logger.warning(
                    "fan-out manifest item %d is missing 'id' (%r) — every "
                    "such item collapses onto a single '::unknown' branch; "
                    "give each manifest item a unique 'id'.",
                    i, item,
                )
            out.append(item)
        elif isinstance(item, (str, int, float, bool)):
            out.append({"id": str(item)})
        else:
            raise ValueError(
                f"fan-out manifest item {i} must be an object or scalar, "
                f"got {type(item).__name__}: {item!r}"
            )
    return out


def _read_manifest(state: WorkflowState, node: Node) -> list[dict]:
    """Resolve the manifest a fan-out parent dispatches over.

    Two-step resolution: first try parsing the parent's own
    ``node_outputs`` entry as JSON (canonical for runtime-generated
    manifests); on parse failure or missing key, fall back to the
    on-disk path declared in ``fan_out.manifest_path`` (canonical for
    static manifests checked into the repo). Returns ``[]`` on miss
    so the conditional-edge router can route to ``no_items`` cleanly.
    Items are normalized via :func:`_normalize_items` (scalars coerced
    to ``{"id": ...}``).
    """
    output = state.get("node_outputs", {}).get(node.id, "")
    try:
        data = json.loads(extract_json(output))
        if isinstance(data, dict) and "items" in data:
            return _normalize_items(data["items"])
        if isinstance(data, list):
            return _normalize_items(data)
    except (json.JSONDecodeError, TypeError):
        pass

    if node.fan_out and node.fan_out.manifest_path:
        path = node.fan_out.manifest_path
        manifest_file = Path(state.get("workdir", ".")) / path
        # A declared manifest_path that can't be read is an author error
        # (typo / missing file / bad JSON) — halt loudly rather than
        # silently fanning out over zero items. (An empty-but-valid
        # manifest still returns [] below and routes to no_items.)
        try:
            raw = manifest_file.read_text()
        except (FileNotFoundError, OSError) as e:
            raise ManifestError(
                f"fan-out '{node.id}': manifest_path {path!r} could not be "
                f"read under {state.get('workdir', '.')!r} ({e})"
            )
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ManifestError(
                f"fan-out '{node.id}': manifest_path {path!r} is not valid "
                f"JSON ({e})"
            )
        if isinstance(data, dict) and "items" in data:
            return _normalize_items(data["items"])
        if isinstance(data, list):
            return _normalize_items(data)
        raise ManifestError(
            f"fan-out '{node.id}': manifest_path {path!r} must be a JSON "
            f"array or object with an 'items' array, got {type(data).__name__}"
        )

    return []
