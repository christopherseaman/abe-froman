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

_BASE_SAFE_FUNCS = {
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
    """Bind each dep's structured_output (else raw output) by id, plus
    history, ``evals[id]`` (latest result per node), and the
    ``passed(id)``/``score(id)``/``scores(id)`` helper functions.

    The helpers close over ``state`` and provide ergonomic access to
    the most-recent eval result for any node — alternative to the
    raw ``history[id][-1]['result'][...]`` chain. Returning safe
    defaults for nodes that haven't been evaluated keeps predicates
    from blowing up on missing-key errors.
    """
    ns: dict[str, Any] = {}
    structured = state.get("node_structured_outputs", {}) or {}
    outputs = state.get("node_outputs", {}) or {}
    for dep in deps:
        ns[dep] = structured.get(dep, outputs.get(dep))
    ns["history"] = state.get("evaluations", {}) or {}
    ns["state"] = dict(state)

    # evals[id] → latest result dict (or empty dict if no evaluation
    # has run for this node). One-line lookup beats history[id][-1]['result'].
    history = state.get("evaluations", {}) or {}
    ns["evals"] = {
        nid: (records[-1].get("result", {}) or {}) if records else {}
        for nid, records in history.items()
    }
    return ns


def build_safe_funcs(state: WorkflowState) -> dict[str, Any]:
    """Return _SAFE_FUNCS plus state-aware eval shortcut helpers.

    Kept separate from `build_route_namespace` because simpleeval
    accepts `names` and `functions` as distinct parameters: bound
    *values* go in `names`, callables go in `functions`.
    """
    completed = set(state.get("completed_nodes", []) or [])
    failed = set(state.get("failed_nodes", []) or [])
    history = state.get("evaluations", {}) or {}

    def _last(node_id: str) -> dict[str, Any]:
        records = history.get(node_id) or []
        if not records:
            return {}
        return records[-1].get("result", {}) or {}

    def passed(node_id: str) -> bool:
        # "Settled cleanly" — landed in completed_nodes and not in
        # failed_nodes. For evaluated nodes this means score >=
        # threshold OR (blocking=False AND retries exhausted with
        # pass-with-warning). For ungated nodes, just "ran without error."
        return node_id in completed and node_id not in failed

    def score(node_id: str) -> float:
        return float(_last(node_id).get("score", 0.0) or 0.0)

    def scores(node_id: str) -> dict[str, float]:
        return dict(_last(node_id).get("scores", {}) or {})

    return {
        **_BASE_SAFE_FUNCS,
        "passed": passed,
        "score": score,
        "scores": scores,
    }


def evaluate_case(
    when: str, namespace: dict[str, Any], functions: dict[str, Any] | None = None,
) -> bool:
    """Evaluate a `when:` expression against the namespace.

    Returns truthy/falsy as bool. Raises on parse error, name error,
    or sandbox violation — caller catches and re-raises with route id
    context.

    ``functions`` defaults to the base safe-funcs (len/any/all/...).
    Callers wanting the eval helpers (passed/score/scores) should
    pass ``build_safe_funcs(state)`` for the state-aware closures.
    """
    funcs = functions if functions is not None else _BASE_SAFE_FUNCS
    evaluator = EvalWithCompoundTypes(names=namespace, functions=funcs)
    return bool(evaluator.eval(when))
