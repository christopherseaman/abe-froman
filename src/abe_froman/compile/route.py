"""Sandboxed predicate evaluator for the `route` execution type.

Routes evaluate `when:` expressions in a sandboxed simpleeval namespace
seeded with each dep's structured output (or raw output as fallback),
the full evaluations history, and the full state. The sandbox blocks
dunder access, imports, and statements — only Python expressions over
the bound names plus a small set of safe functions (len/any/all/min/
max/sum) are permitted.

This module is langgraph-free by design (enforced by
tests/architecture/test_layers.py) — it's a pure state-shape utility,
not a graph builder.
"""

from __future__ import annotations

from typing import Any

from simpleeval import EvalWithCompoundTypes

from abe_froman.runtime.state import WorkflowState

_SAFE_FUNCS = {
    "len": len,
    "any": any,
    "all": all,
    "min": min,
    "max": max,
    "sum": sum,
}


def build_route_namespace(
    state: WorkflowState, deps: list[str]
) -> dict[str, Any]:
    """Bind each dep's structured_output (else raw output) by id, plus history."""
    ns: dict[str, Any] = {}
    structured = state.get("node_structured_outputs", {}) or {}
    outputs = state.get("node_outputs", {}) or {}
    for dep in deps:
        ns[dep] = structured.get(dep, outputs.get(dep))
    ns["history"] = state.get("evaluations", {}) or {}
    ns["state"] = dict(state)
    return ns


def evaluate_case(when: str, namespace: dict[str, Any]) -> bool:
    """Evaluate a `when:` expression against the namespace.

    Returns truthy/falsy as bool. Raises on parse error, name error,
    or sandbox violation — caller catches and re-raises with route id
    context.
    """
    evaluator = EvalWithCompoundTypes(names=namespace, functions=_SAFE_FUNCS)
    return bool(evaluator.eval(when))
