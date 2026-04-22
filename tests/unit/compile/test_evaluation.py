"""Unit tests for criterion evaluator and route walker.

These primitives underpin Stage 3's generalized routing model: routes
are data (clause → destination), destinations are strings (outcome
labels in MVP; arbitrary node ids post-3b), first-match-wins.
"""

import pytest

from abe_froman.compile.evaluation import (
    Criterion,
    EvaluationRecord,
    Route,
    build_eval_context,
    clauses_match,
    criterion_matches,
    gate_fallback,
    gate_to_routes,
    walk_routes,
)
from abe_froman.schema.models import DimensionCheck, QualityGate


class TestCriterionMatches:
    @pytest.mark.parametrize(
        "op,field_value,value,expected",
        [
            ("==", "a", "a", True),
            ("==", "a", "b", False),
            ("!=", "a", "b", True),
            (">", 5, 3, True),
            (">", 3, 5, False),
            (">=", 3, 3, True),
            (">=", 2, 3, False),
            ("<", 1, 2, True),
            ("<", 3, 2, False),
            ("<=", 2, 2, True),
            ("in", "x", ["x", "y"], True),
            ("in", "z", ["x", "y"], False),
            ("not_in", "z", ["x", "y"], True),
            ("has", ["x", "y"], "x", True),
            ("has", ["x", "y"], "z", False),
        ],
    )
    def test_operators(self, op, field_value, value, expected):
        ctx = {"a": field_value}
        c = Criterion(field="a", op=op, value=value)
        assert criterion_matches(c, ctx) is expected

    def test_missing_field_is_none(self):
        """Dotted path to absent key resolves to None — numeric ops fail."""
        c = Criterion(field="result.score", op=">=", value=0.5)
        ctx = {"result": {}}  # no `score`
        assert criterion_matches(c, ctx) is False

    def test_dotted_path_traverses_nested_dicts(self):
        c = Criterion(field="a.b.c", op="==", value=42)
        assert criterion_matches(c, {"a": {"b": {"c": 42}}}) is True
        assert criterion_matches(c, {"a": {"b": {"c": 0}}}) is False

    def test_unknown_op_raises(self):
        c = Criterion(field="a", op="~~", value=1)
        with pytest.raises(ValueError, match="Unknown criterion operator"):
            criterion_matches(c, {"a": 1})


class TestClausesMatch:
    def test_empty_clauses_always_match(self):
        """Zero clauses — useful for unconditional/fallback-like routes."""
        assert clauses_match([], {"anything": 1}) is True

    def test_all_clauses_required(self):
        clauses = [
            Criterion(field="x", op=">", value=0),
            Criterion(field="y", op="==", value="ok"),
        ]
        assert clauses_match(clauses, {"x": 5, "y": "ok"}) is True
        assert clauses_match(clauses, {"x": 5, "y": "no"}) is False
        assert clauses_match(clauses, {"x": 0, "y": "ok"}) is False


class TestWalkRoutes:
    def test_first_match_wins(self):
        routes = [
            Route(when=[Criterion(field="x", op="==", value=1)], to="first"),
            Route(when=[Criterion(field="x", op="==", value=1)], to="second"),
        ]
        matched = walk_routes(routes, {"x": 1})
        assert matched is not None
        assert matched.to == "first"

    def test_no_match_returns_none(self):
        routes = [Route(when=[Criterion(field="x", op=">", value=99)], to="r")]
        assert walk_routes(routes, {"x": 0}) is None

    def test_params_carry_through(self):
        routes = [
            Route(
                when=[Criterion(field="score", op="<", value=0.5)],
                to="retry",
                params={"_retry_reason": "low"},
            )
        ]
        matched = walk_routes(routes, {"score": 0.2})
        assert matched.to == "retry"
        assert matched.params == {"_retry_reason": "low"}


class TestGateToRoutes:
    def test_threshold_gate_pass(self):
        gate = QualityGate(validator="g.py", threshold=0.8, blocking=False)
        routes = gate_to_routes(gate, max_retries=2)
        assert routes[0].to == "pass"
        # Pass route: score >= threshold
        assert len(routes[0].when) == 1
        c = routes[0].when[0]
        assert c.field == "result.score" and c.op == ">=" and c.value == 0.8

    def test_threshold_gate_retry_has_invocation_clause(self):
        gate = QualityGate(validator="g.py", threshold=0.8)
        routes = gate_to_routes(gate, max_retries=3)
        retry = routes[1]
        assert retry.to == "retry"
        ops = {c.field: (c.op, c.value) for c in retry.when}
        assert ops["result.score"] == ("<", 0.8)
        assert ops["invocation"] == ("<", 3)

    def test_multidim_gate_emits_per_dim_retry(self):
        gate = QualityGate(
            validator="g.py",
            dimensions=[
                DimensionCheck(field="rigor", min=0.7),
                DimensionCheck(field="humor", min=0.6),
            ],
        )
        routes = gate_to_routes(gate, max_retries=1)
        # pass route has N clauses (all dims must meet)
        assert routes[0].to == "pass"
        assert len(routes[0].when) == 2
        # N retry routes, one per dim
        retry_routes = [r for r in routes if r.to == "retry"]
        assert len(retry_routes) == 2


class TestGateFallback:
    def test_blocking_falls_to_fail(self):
        gate = QualityGate(validator="g.py", threshold=0.5, blocking=True)
        assert gate_fallback(gate) == "fail_blocking"

    def test_non_blocking_falls_to_warn(self):
        gate = QualityGate(validator="g.py", threshold=0.5, blocking=False)
        assert gate_fallback(gate) == "warn_continue"


class TestBuildEvalContext:
    def test_shape(self):
        ctx = build_eval_context({"score": 0.9}, invocation=2, history=[{"x": 1}])
        assert ctx["result"] == {"score": 0.9}
        assert ctx["invocation"] == 2
        assert ctx["history"] == [{"x": 1}]


class TestEvaluationRecord:
    def test_to_dict_roundtrip(self):
        rec = EvaluationRecord.now(invocation=0, result={"score": 0.7})
        d = rec.to_dict()
        assert d["invocation"] == 0
        assert d["result"] == {"score": 0.7}
        assert "T" in d["timestamp"]  # ISO-8601

    def test_result_copied_not_referenced(self):
        """Mutating the input dict after record creation must not alter history."""
        original = {"score": 0.5}
        rec = EvaluationRecord.now(invocation=0, result=original)
        original["score"] = 999
        assert rec.result["score"] == 0.5


class TestEndToEndRouteWalkFromGate:
    """Walk gate-derived routes against evaluated-gate contexts.

    Mirrors the real call sequence inside classify_gate_outcome.
    """

    def test_pass_single_threshold(self):
        gate = QualityGate(validator="g.py", threshold=0.8, blocking=False)
        routes = gate_to_routes(gate, max_retries=2)
        ctx = build_eval_context({"score": 0.9}, invocation=0, history=[])
        assert walk_routes(routes, ctx).to == "pass"

    def test_retry_single_threshold(self):
        gate = QualityGate(validator="g.py", threshold=0.8)
        routes = gate_to_routes(gate, max_retries=2)
        ctx = build_eval_context({"score": 0.5}, invocation=0, history=[])
        assert walk_routes(routes, ctx).to == "retry"

    def test_exhausted_falls_through(self):
        """Retries used up: pass fails, retry-with-invocation<2 fails → None."""
        gate = QualityGate(validator="g.py", threshold=0.8)
        routes = gate_to_routes(gate, max_retries=2)
        ctx = build_eval_context({"score": 0.5}, invocation=2, history=[])
        assert walk_routes(routes, ctx) is None  # caller uses fallback

    def test_multidim_one_failing_dim_triggers_retry(self):
        gate = QualityGate(
            validator="g.py",
            dimensions=[
                DimensionCheck(field="rigor", min=0.7),
                DimensionCheck(field="humor", min=0.6),
            ],
        )
        routes = gate_to_routes(gate, max_retries=2)
        ctx = build_eval_context(
            {"scores": {"rigor": 0.9, "humor": 0.3}}, invocation=0, history=[]
        )
        assert walk_routes(routes, ctx).to == "retry"

    def test_multidim_all_dims_pass(self):
        gate = QualityGate(
            validator="g.py",
            dimensions=[DimensionCheck(field="a", min=0.5)],
        )
        routes = gate_to_routes(gate, max_retries=2)
        ctx = build_eval_context({"scores": {"a": 0.8}}, invocation=0, history=[])
        assert walk_routes(routes, ctx).to == "pass"
