"""Compile-time lint warnings — non-fatal author footgun checks.

Distinct from schema validation (which raises ``ValidationError``) and
graph compilation (which builds the LangGraph): these are advisory
warnings about constructs that are *valid* but likely a mistake.

Pure functions over ``Graph`` — no langgraph imports, no I/O. Top-level
config only; subgraph configs are not loaded (a subgraph is linted when
run as its own top-level workflow).
"""
from __future__ import annotations

from sqrlly.schema.models import Graph


def collect_warnings(config: Graph) -> list[str]:
    """Advisory warnings for a workflow config, in declaration order."""
    return _hyphenated_id_warnings(config) + _advisory_gate_warnings(config)


def _advisory_gate_warnings(config: Graph) -> list[str]:
    """Flag a gate that can fail but won't halt.

    A node whose ``evaluation`` sets a positive ``threshold`` with
    ``blocking: false`` scores the gate but never stops the workflow — a
    below-threshold score is logged and execution continues. Easy to
    mistake a hollow "green" run for a real pass. (A ``threshold`` of 0.0
    can never fail, so it is not flagged.)
    """
    out: list[str] = []
    for node in config.nodes:
        ev = node.evaluation
        if ev is not None and not ev.blocking and ev.threshold > 0.0:
            out.append(
                f"node '{node.id}': gate sets threshold={ev.threshold} but "
                f"blocking is false — advisory only; a below-threshold score "
                f"won't halt the workflow (set 'blocking: true' to halt)."
            )
    return out


def _hyphenated_id_warnings(config: Graph) -> list[str]:
    """Flag node ids containing ``-``.

    A hyphenated id referenced as ``{{the-id}}`` in a Jinja template
    parses as subtraction (``the - id``), silently producing wrong or
    empty output rather than the node's value. Underscores are safe.
    Covers top-level node ids and fan-out final-node ids.
    """
    ids: list[str] = []
    for node in config.nodes:
        ids.append(node.id)
        if node.fan_out:
            ids.extend(fn.id for fn in node.fan_out.final_nodes)

    out: list[str] = []
    for node_id in ids:
        if "-" not in node_id:
            continue
        templated = "{{" + node_id + "}}"
        suggestion = node_id.replace("-", "_")
        out.append(
            f"node id '{node_id}' contains a hyphen — referenced in a "
            f"Jinja template as '{templated}' it parses as subtraction, "
            f"not a variable. Rename to '{suggestion}' if this id will "
            f"be templated."
        )
    return out
