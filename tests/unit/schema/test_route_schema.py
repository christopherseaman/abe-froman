"""Unit tests for inline ``Node.route`` (Stage 5c): schema parse + Graph-level validator.

Function-level tests cover:
    - YAML/dict parses into Route(cases=..., else=...)
    - else: alias is required (else_ in Python)
    - cases: may be empty (else-only is legal)
    - Graph validator: goto must resolve to a real node id or __end__
    - Graph validator: routes must be leaves in the dep DAG
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from abe_froman.schema.models import (
    Execute,
    Graph,
    Node,
    Route,
    RouteCase,
    RouteElse,
)


def test_route_parses_from_dict():
    payload = {
        "cases": [
            {"when": "score >= 0.8", "goto": "ship"},
            {"when": "len(history) >= 3", "goto": "__end__"},
        ],
        "else": "produce",
    }
    parsed = Route.model_validate(payload)
    assert len(parsed.cases) == 2
    assert parsed.cases[0] == RouteCase(when="score >= 0.8", goto="ship")
    # `else: <string>` shorthand auto-promotes to RouteElse
    assert isinstance(parsed.else_, RouteElse)
    assert parsed.else_.goto == "produce"


def test_route_parses_from_yaml():
    src = """
    cases:
      - when: "judge['score'] >= 0.8"
        goto: ship
    else: produce
    """
    parsed = Route.model_validate(yaml.safe_load(src))
    assert parsed.else_.goto == "produce"


def test_route_missing_else_raises():
    with pytest.raises(ValidationError) as ei:
        Route.model_validate({"cases": [{"when": "True", "goto": "a"}]})
    assert "else" in str(ei.value).lower()


def test_route_empty_cases_with_else_is_legal():
    """``cases: []`` with an ``else:`` target is treated as unconditional
    fall-through — preserves Stage 5b semantics."""
    parsed = Route.model_validate({"cases": [], "else": "always_here"})
    assert parsed.cases == []
    assert parsed.else_.goto == "always_here"


def test_route_populate_by_name():
    parsed = Route(
        cases=[RouteCase(when="True", goto="x")],
        else_=RouteElse(goto="y"),
    )
    assert parsed.else_.goto == "y"


def _cmd(id: str, **kw):
    """Stage-5b helper: bare echo node via execute.url."""
    import shutil
    return Node(
        id=id, name=id,
        execute=Execute(url=shutil.which("echo") or "/bin/echo", params={"args": [id]}),
        **kw,
    )


def _route(id: str, cases: list[dict], else_target: str, **kw):
    """Build a Node with inline Route (no execute body — standalone router)."""
    return Node(
        id=id, name=id,
        route=Route(
            cases=[RouteCase(**c) for c in cases],
            else_=RouteElse(goto=else_target),
        ),
        **kw,
    )


def test_graph_validator_resolves_real_goto():
    config = Graph(
        name="t", version="1.0",
        nodes=[
            _cmd("a"),
            _route("r", [{"when": "True", "goto": "a"}], "__end__", depends_on=["a"]),
        ],
    )
    assert config.nodes[1].route is not None
    assert config.nodes[1].route.cases[0].goto == "a"


def test_graph_validator_rejects_unresolved_goto():
    with pytest.raises(ValidationError) as ei:
        Graph(
            name="t", version="1.0",
            nodes=[
                _cmd("a"),
                _route(
                    "r",
                    [{"when": "True", "goto": "nonexistent"}],
                    "__end__",
                    depends_on=["a"],
                ),
            ],
        )
    msg = str(ei.value)
    assert "nonexistent" in msg
    assert "r" in msg


def test_graph_validator_rejects_unresolved_else():
    with pytest.raises(ValidationError) as ei:
        Graph(
            name="t", version="1.0",
            nodes=[
                _cmd("a"),
                _route(
                    "r",
                    [{"when": "True", "goto": "a"}],
                    "ghost",
                    depends_on=["a"],
                ),
            ],
        )
    assert "ghost" in str(ei.value)


def test_graph_validator_accepts_end_sentinel():
    config = Graph(
        name="t", version="1.0",
        nodes=[
            _cmd("a"),
            _route(
                "r", [{"when": "True", "goto": "__end__"}], "__end__",
                depends_on=["a"],
            ),
        ],
    )
    assert config.nodes[1].route.else_.goto == "__end__"


def test_graph_validator_rejects_route_in_depends_on():
    with pytest.raises(ValidationError) as ei:
        Graph(
            name="t", version="1.0",
            nodes=[
                _cmd("a"),
                _route("r", [{"when": "True", "goto": "a"}], "__end__", depends_on=["a"]),
                _cmd("downstream", depends_on=["r"]),
            ],
        )
    msg = str(ei.value)
    assert "downstream" in msg
    assert "route 'r'" in msg
