"""Unit tests for RouteExecution: schema parse + Graph-level validator.

Function-level tests cover:
    - YAML/dict parses into RouteExecution
    - else: alias is required (else_ in Python)
    - cases: may be empty (else-only is legal)
    - Graph validator: goto must resolve to a real node id or __end__
    - Graph validator: routes must be leaves in the dep DAG
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import TypeAdapter, ValidationError

from abe_froman.schema.models import (
    Execution,
    Graph,
    Node,
    RouteCase,
    RouteExecution,
)

_ExecAdapter = TypeAdapter(Execution)


def test_route_execution_parses_from_dict():
    payload = {
        "type": "route",
        "cases": [
            {"when": "score >= 0.8", "goto": "ship"},
            {"when": "len(history) >= 3", "goto": "__end__"},
        ],
        "else": "produce",
    }
    parsed = _ExecAdapter.validate_python(payload)
    assert isinstance(parsed, RouteExecution)
    assert parsed.type == "route"
    assert len(parsed.cases) == 2
    assert parsed.cases[0] == RouteCase(when="score >= 0.8", goto="ship")
    assert parsed.else_ == "produce"


def test_route_execution_parses_from_yaml():
    src = """
    type: route
    cases:
      - when: "judge['score'] >= 0.8"
        goto: ship
    else: produce
    """
    parsed = _ExecAdapter.validate_python(yaml.safe_load(src))
    assert isinstance(parsed, RouteExecution)
    assert parsed.else_ == "produce"


def test_route_execution_missing_else_raises():
    with pytest.raises(ValidationError) as ei:
        _ExecAdapter.validate_python({"type": "route", "cases": []})
    assert "else" in str(ei.value).lower()


def test_route_execution_empty_cases_with_else_is_legal():
    parsed = _ExecAdapter.validate_python(
        {"type": "route", "cases": [], "else": "always_here"}
    )
    assert isinstance(parsed, RouteExecution)
    assert parsed.cases == []
    assert parsed.else_ == "always_here"


def test_route_execution_populate_by_name():
    parsed = RouteExecution(cases=[], else_="x")
    assert parsed.else_ == "x"


def test_graph_validator_resolves_real_goto():
    config = Graph(
        name="t", version="1.0",
        nodes=[
            Node(id="a", name="A", execution={"type": "command", "command": "echo"}),
            Node(
                id="r", name="R", depends_on=["a"],
                execution={
                    "type": "route",
                    "cases": [{"when": "True", "goto": "a"}],
                    "else": "__end__",
                },
            ),
        ],
    )
    assert isinstance(config.nodes[1].execution, RouteExecution)


def test_graph_validator_rejects_unresolved_goto():
    with pytest.raises(ValidationError) as ei:
        Graph(
            name="t", version="1.0",
            nodes=[
                Node(id="a", name="A", execution={"type": "command", "command": "echo"}),
                Node(
                    id="r", name="R", depends_on=["a"],
                    execution={
                        "type": "route",
                        "cases": [{"when": "True", "goto": "nonexistent"}],
                        "else": "__end__",
                    },
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
                Node(id="a", name="A", execution={"type": "command", "command": "echo"}),
                Node(
                    id="r", name="R", depends_on=["a"],
                    execution={
                        "type": "route",
                        "cases": [],
                        "else": "ghost",
                    },
                ),
            ],
        )
    assert "ghost" in str(ei.value)


def test_graph_validator_accepts_end_sentinel():
    config = Graph(
        name="t", version="1.0",
        nodes=[
            Node(id="a", name="A", execution={"type": "command", "command": "echo"}),
            Node(
                id="r", name="R", depends_on=["a"],
                execution={
                    "type": "route",
                    "cases": [{"when": "True", "goto": "__end__"}],
                    "else": "__end__",
                },
            ),
        ],
    )
    assert config.nodes[1].execution.else_ == "__end__"


def test_graph_validator_rejects_route_in_depends_on():
    with pytest.raises(ValidationError) as ei:
        Graph(
            name="t", version="1.0",
            nodes=[
                Node(id="a", name="A", execution={"type": "command", "command": "echo"}),
                Node(
                    id="r", name="R", depends_on=["a"],
                    execution={
                        "type": "route",
                        "cases": [],
                        "else": "__end__",
                    },
                ),
                Node(
                    id="downstream", name="D", depends_on=["r"],
                    execution={"type": "command", "command": "echo"},
                ),
            ],
        )
    msg = str(ei.value)
    assert "downstream" in msg
    assert "route 'r'" in msg
