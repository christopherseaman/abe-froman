"""Unit tests for compile/route.py: sandboxed expression evaluator.

Function-level tests (known-good and known-bad pairs):
    - build_route_namespace: structured > raw, missing deps, history, state
    - evaluate_case: truthy expressions, sandbox blocks, parse errors
"""

from __future__ import annotations

import pytest

from langgraph.graph import END
from langgraph.types import Command

from abe_froman.compile.graph import _make_route_node, _resolve_goto
from abe_froman.compile.route import (
    build_route_namespace,
    evaluate_case,
)
from abe_froman.schema.models import Execute, Node, RouteCase


def _state(**overrides) -> dict:
    base = {
        "node_outputs": {},
        "node_structured_outputs": {},
        "evaluations": {},
        "completed_nodes": [],
        "workdir": ".",
        "dry_run": False,
    }
    base.update(overrides)
    return base


def test_namespace_binds_structured_output_by_dep_id():
    state = _state(node_structured_outputs={"judge": {"score": 0.9}})
    ns = build_route_namespace(state, ["judge"])
    assert ns["judge"] == {"score": 0.9}


def test_namespace_falls_back_to_raw_output_when_no_structured():
    state = _state(node_outputs={"produce": "draft text"})
    ns = build_route_namespace(state, ["produce"])
    assert ns["produce"] == "draft text"


def test_namespace_prefers_structured_over_raw():
    state = _state(
        node_structured_outputs={"j": {"x": 1}},
        node_outputs={"j": "raw"},
    )
    ns = build_route_namespace(state, ["j"])
    assert ns["j"] == {"x": 1}


def test_namespace_binds_missing_dep_as_none():
    state = _state()
    ns = build_route_namespace(state, ["never_ran"])
    assert ns["never_ran"] is None


def test_namespace_includes_history_from_evaluations():
    state = _state(
        evaluations={"judge": [{"score": 0.3}, {"score": 0.5}]}
    )
    ns = build_route_namespace(state, [])
    assert ns["history"] == {"judge": [{"score": 0.3}, {"score": 0.5}]}


def test_namespace_state_is_full_state_dict():
    state = _state(node_outputs={"x": "y"})
    ns = build_route_namespace(state, [])
    assert ns["state"]["node_outputs"] == {"x": "y"}
    assert ns["state"]["workdir"] == "."


def test_evaluate_case_score_threshold_truthy():
    ns = {"judge": {"score": 0.9}}
    assert evaluate_case("judge['score'] >= 0.8", ns) is True


def test_evaluate_case_score_threshold_falsy():
    ns = {"judge": {"score": 0.3}}
    assert evaluate_case("judge['score'] >= 0.8", ns) is False


def test_evaluate_case_history_length_via_safe_func():
    ns = {"history": {"j": [{}, {}, {}]}}
    assert evaluate_case("len(history['j']) >= 3", ns) is True


def test_evaluate_case_safe_funcs_callable():
    ns = {"items": [1, 2, 3]}
    assert evaluate_case("sum(items) == 6", ns) is True
    assert evaluate_case("any(x > 2 for x in items)", ns) is True
    assert evaluate_case("all(x > 0 for x in items)", ns) is True


def test_evaluate_case_malformed_expression_raises():
    with pytest.raises(Exception):
        evaluate_case("score >=", {"score": 0.5})


def test_evaluate_case_blocks_dunder_access():
    with pytest.raises(Exception):
        evaluate_case("().__class__.__bases__", {})


def test_evaluate_case_blocks_dunder_import():
    with pytest.raises(Exception):
        evaluate_case("__import__('os')", {})


def test_evaluate_case_unknown_name_raises():
    with pytest.raises(Exception):
        evaluate_case("ghost_var > 0", {})


def _route_node(cases: list[tuple[str, str]], else_target: str, deps: list[str] = None) -> Node:
    return Node(
        id="r", name="R", depends_on=deps or [],
        execute=Execute(
            type="route",
            cases=[RouteCase(when=w, goto=g) for w, g in cases],
            else_=else_target,
        ),
    )


def test_resolve_goto_end_sentinel():
    assert _resolve_goto("__end__") == END
    assert _resolve_goto("ship") == "ship"


async def test_route_node_first_match_wins():
    fn = _make_route_node(_route_node([("True", "X")], else_target="Y"))
    cmd = await fn({})
    assert isinstance(cmd, Command)
    assert cmd.goto == "X"


async def test_route_node_falls_through_to_else():
    fn = _make_route_node(_route_node([("False", "X")], else_target="Y"))
    cmd = await fn({})
    assert cmd.goto == "Y"


async def test_route_node_else_only_with_no_cases():
    fn = _make_route_node(_route_node([], else_target="always_here"))
    cmd = await fn({})
    assert cmd.goto == "always_here"


async def test_route_node_end_sentinel_resolves():
    fn = _make_route_node(_route_node([("True", "__end__")], else_target="x"))
    cmd = await fn({})
    assert cmd.goto == END


async def test_route_node_evaluates_against_dep_state():
    fn = _make_route_node(
        _route_node(
            [("judge['score'] >= 0.8", "ship")],
            else_target="produce",
            deps=["judge"],
        )
    )
    state = {
        "node_outputs": {},
        "node_structured_outputs": {"judge": {"score": 0.95}},
        "evaluations": {},
    }
    cmd = await fn(state)
    assert cmd.goto == "ship"


async def test_route_node_broken_when_raises_with_context():
    fn = _make_route_node(_route_node([("score >=", "X")], else_target="Y"))
    with pytest.raises(ValueError) as ei:
        await fn({})
    msg = str(ei.value)
    assert "Route 'r'" in msg
    assert "score >=" in msg


async def test_route_node_unknown_name_raises_with_context():
    fn = _make_route_node(
        _route_node([("ghost > 0", "X")], else_target="Y")
    )
    with pytest.raises(ValueError) as ei:
        await fn({})
    assert "Route 'r'" in str(ei.value)


async def test_route_node_history_length_drives_halt():
    fn = _make_route_node(
        _route_node(
            [("len(history.get('judge', [])) >= 3", "__end__")],
            else_target="produce",
        )
    )
    state_with_3 = {
        "node_outputs": {},
        "node_structured_outputs": {},
        "evaluations": {"judge": [{"score": 0.3}, {"score": 0.4}, {"score": 0.5}]},
    }
    cmd = await fn(state_with_3)
    assert cmd.goto == END

    state_with_1 = {
        "node_outputs": {},
        "node_structured_outputs": {},
        "evaluations": {"judge": [{"score": 0.3}]},
    }
    cmd = await fn(state_with_1)
    assert cmd.goto == "produce"
