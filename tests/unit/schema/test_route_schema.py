"""Unit tests for execute.type=route: schema parse + Graph-level validator.

Function-level tests cover:
    - YAML/dict parses into Execute(type='route', ...)
    - else: alias is required (else_ in Python)
    - cases: may be empty (else-only is legal)
    - Graph validator: goto must resolve to a real node id or __end__
    - Graph validator: routes must be leaves in the dep DAG
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from abe_froman.schema.models import Execute, Graph, Node, RouteCase


def test_route_execute_parses_from_dict():
    payload = {
        "type": "route",
        "cases": [
            {"when": "score >= 0.8", "goto": "ship"},
            {"when": "len(history) >= 3", "goto": "__end__"},
        ],
        "else": "produce",
    }
    parsed = Execute.model_validate(payload)
    assert parsed.type == "route"
    assert len(parsed.cases) == 2
    assert parsed.cases[0] == RouteCase(when="score >= 0.8", goto="ship")
    assert parsed.else_ == "produce"


def test_route_execute_parses_from_yaml():
    src = """
    type: route
    cases:
      - when: "judge['score'] >= 0.8"
        goto: ship
    else: produce
    """
    parsed = Execute.model_validate(yaml.safe_load(src))
    assert parsed.type == "route"
    assert parsed.else_ == "produce"


def test_route_execute_missing_else_raises():
    with pytest.raises(ValidationError) as ei:
        Execute.model_validate({"type": "route", "cases": []})
    assert "else" in str(ei.value).lower()


def test_route_execute_empty_cases_with_else_is_legal():
    parsed = Execute.model_validate(
        {"type": "route", "cases": [], "else": "always_here"}
    )
    assert parsed.type == "route"
    assert parsed.cases == []
    assert parsed.else_ == "always_here"


def test_route_execute_populate_by_name():
    parsed = Execute(type="route", cases=[], else_="x")
    assert parsed.else_ == "x"


def _cmd(id: str, **kw):
    """Stage-5b helper: bare echo node via execute.url."""
    import shutil
    return Node(
        id=id, name=id,
        execute=Execute(url=shutil.which("echo") or "/bin/echo", params={"args": [id]}),
        **kw,
    )


def _route(id: str, cases: list[dict], else_target: str, **kw):
    return Node(
        id=id, name=id,
        execute=Execute(
            type="route",
            cases=[RouteCase(**c) for c in cases],
            else_=else_target,
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
    assert config.nodes[1].execute.type == "route"


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
                _route("r", [], "ghost", depends_on=["a"]),
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
    assert config.nodes[1].execute.else_ == "__end__"


def test_graph_validator_rejects_route_in_depends_on():
    with pytest.raises(ValidationError) as ei:
        Graph(
            name="t", version="1.0",
            nodes=[
                _cmd("a"),
                _route("r", [], "__end__", depends_on=["a"]),
                _cmd("downstream", depends_on=["r"]),
            ],
        )
    msg = str(ei.value)
    assert "downstream" in msg
    assert "route 'r'" in msg
