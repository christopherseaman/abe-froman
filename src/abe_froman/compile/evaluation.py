"""Criterion-based route walker and evaluation records.

This module is the internal cleanup of gate routing. Rather than an
`outcome: str` enum decided by a chain of `if/elif` statements, outcomes
are selected by walking an ordered list of `Route`s. Each route has
clauses (AND-combined) that are tested against a context (`result`,
`invocation`, history); first match wins; if nothing matches, `fallback`
fires.

Compile-time sugar: `gate_to_routes(gate)` turns a `QualityGate` into
routes with the equivalent semantics. The rest of the runtime keeps
using outcome names ("pass", "retry", "fail_blocking", "warn_continue")
as route destinations — those names are emergent labels, not a
primitive enum, and will become arbitrary node ids once graph-node
splitting lands (Stage 3b).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from abe_froman.schema.models import QualityGate


@dataclass
class Criterion:
    """One clause: compare a dotted field in the context against a value."""

    field: str
    op: str
    value: Any = None


@dataclass
class Route:
    """Ordered routing rule: when ALL `when` clauses match, fire `to`.

    `to` is the destination label (a string naming an outcome or a node
    id). `params` are optional context overrides merged into the
    destination's invocation — the mechanism that replaces the ad-hoc
    `_retry_reason` template variable.
    """

    when: list[Criterion]
    to: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvaluationRecord:
    """One evaluation invocation, persisted to state.evaluations[node_id]."""

    invocation: int
    result: dict[str, Any]
    timestamp: str

    @staticmethod
    def now(invocation: int, result: dict[str, Any]) -> "EvaluationRecord":
        return EvaluationRecord(
            invocation=invocation,
            result=dict(result),
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "invocation": self.invocation,
            "result": dict(self.result),
            "timestamp": self.timestamp,
        }


_OPS = {
    "==": lambda a, b: a == b,
    "!=": lambda a, b: a != b,
    ">": lambda a, b: a is not None and a > b,
    ">=": lambda a, b: a is not None and a >= b,
    "<": lambda a, b: a is not None and a < b,
    "<=": lambda a, b: a is not None and a <= b,
    "in": lambda a, b: a in b,
    "not_in": lambda a, b: a not in b,
    "has": lambda a, b: b in a if a is not None else False,
}


def _resolve_field(path: str, context: dict[str, Any]) -> Any:
    """Walk a dotted path through nested dicts. Missing keys → None."""
    cur: Any = context
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def criterion_matches(c: Criterion, context: dict[str, Any]) -> bool:
    op = _OPS.get(c.op)
    if op is None:
        raise ValueError(f"Unknown criterion operator: {c.op!r}")
    return bool(op(_resolve_field(c.field, context), c.value))


def clauses_match(clauses: list[Criterion], context: dict[str, Any]) -> bool:
    """Empty clause list always matches — useful for unconditional routes."""
    return all(criterion_matches(c, context) for c in clauses)


def walk_routes(
    routes: list[Route], context: dict[str, Any]
) -> Route | None:
    """First-match-wins over an ordered list. Returns None if none match."""
    for route in routes:
        if clauses_match(route.when, context):
            return route
    return None


def gate_to_routes(gate: QualityGate, max_retries: int) -> list[Route]:
    """Compile `QualityGate` sugar into the general route list.

    Produces a pass-route (all-dimensions-met, or score>=threshold) and
    a retry-route (not-met AND invocation<max_retries). Fallback is
    handled separately by the caller — `blocking: false` maps to a
    warn-continue destination, `true` to fail_blocking.
    """
    if gate.dimensions:
        pass_clauses = [
            Criterion(field=f"result.scores.{d.field}", op=">=", value=d.min)
            for d in gate.dimensions
        ]
        # Any single dim below its min → retry. Routes AND their clauses,
        # so encode the OR of per-dim failures as N separate retry routes.
        # All route to "retry" with identical params — first-match-wins
        # doesn't care which dim triggered.
        retry_routes = [
            Route(
                when=[
                    Criterion(field=f"result.scores.{d.field}", op="<", value=d.min),
                    Criterion(field="invocation", op="<", value=max_retries),
                ],
                to="retry",
            )
            for d in gate.dimensions
        ]
    else:
        pass_clauses = [
            Criterion(field="result.score", op=">=", value=gate.threshold)
        ]
        retry_routes = [
            Route(
                when=[
                    Criterion(field="result.score", op="<", value=gate.threshold),
                    Criterion(field="invocation", op="<", value=max_retries),
                ],
                to="retry",
            )
        ]

    return [Route(when=pass_clauses, to="pass"), *retry_routes]


def gate_fallback(gate: QualityGate) -> str:
    """Destination when no route matches (retries exhausted, pass failed)."""
    return "warn_continue" if not gate.blocking else "fail_blocking"


def build_eval_context(
    gate_result_payload: dict[str, Any], invocation: int, history: list[dict[str, Any]]
) -> dict[str, Any]:
    """Shape the context that routes' clauses are evaluated against."""
    return {
        "result": gate_result_payload,
        "invocation": invocation,
        "history": history,
    }
