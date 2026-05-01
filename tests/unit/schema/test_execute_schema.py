"""Unit tests for the Stage 5b `execute:` schema.

Function-level tests cover:
    - Execute.validate_shape: each of the three modes parses cleanly
    - Mutual exclusion of mode-specific fields
    - Node mutual-exclusion validator: execute / execution / config
    - Settings extension parses with new remote-URL fields
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from abe_froman.schema.models import (
    Execute,
    Node,
    RouteCase,
    Settings,
)


class TestExecuteURLMode:
    def test_url_only_parses(self):
        e = Execute(url="prompts/x.md")
        assert e.url == "prompts/x.md"
        assert e.type is None
        assert e.params == {}

    def test_url_with_params_parses(self):
        e = Execute(url="prompts/x.md", params={"model": "opus"})
        assert e.params == {"model": "opus"}

    def test_url_rejects_cases(self):
        with pytest.raises(ValidationError):
            Execute(url="x.md", cases=[RouteCase(when="True", goto="y")])

    def test_url_rejects_else(self):
        with pytest.raises(ValidationError):
            Execute(url="x.md", else_="y")


class TestExecuteJoinMode:
    def test_join_only_parses(self):
        e = Execute(type="join")
        assert e.type == "join"
        assert e.url is None

    def test_join_rejects_url(self):
        with pytest.raises(ValidationError):
            Execute(type="join", url="x.md")

    def test_join_rejects_cases(self):
        with pytest.raises(ValidationError):
            Execute(type="join", cases=[RouteCase(when="True", goto="y")])

    def test_join_rejects_params(self):
        with pytest.raises(ValidationError):
            Execute(type="join", params={"x": "y"})


class TestExecuteRouteMode:
    def test_route_with_cases_and_else(self):
        e = Execute(
            type="route",
            cases=[RouteCase(when="x > 0", goto="ship")],
            else_="produce",
        )
        assert e.type == "route"
        assert len(e.cases) == 1
        assert e.else_ == "produce"

    def test_route_else_only_legal(self):
        e = Execute(type="route", cases=[], else_="anywhere")
        assert e.cases == []
        assert e.else_ == "anywhere"

    def test_route_missing_else_rejected(self):
        with pytest.raises(ValidationError) as ei:
            Execute(type="route", cases=[RouteCase(when="True", goto="x")])
        assert "else" in str(ei.value).lower()

    def test_route_rejects_url(self):
        with pytest.raises(ValidationError):
            Execute(type="route", url="x.md", else_="y")

    def test_route_rejects_params(self):
        with pytest.raises(ValidationError):
            Execute(type="route", params={"x": "y"}, else_="y")


class TestExecuteAlias:
    def test_else_alias_in_yaml(self):
        src = """
        type: route
        cases:
          - when: "True"
            goto: ship
        else: produce
        """
        e = Execute.model_validate(yaml.safe_load(src))
        assert e.else_ == "produce"

    def test_else_round_trip_via_alias(self):
        e = Execute(cases=[], else_="x", type="route")
        dumped = e.model_dump(by_alias=True)
        assert "else" in dumped
        assert "else_" not in dumped


class TestExecuteEmpty:
    def test_no_mode_set_rejected(self):
        with pytest.raises(ValidationError) as ei:
            Execute()
        assert "exactly one" in str(ei.value).lower()


class TestNodeExecuteShape:
    """After Stage-5b hard cutover, Node carries only `execute: Execute | None`.
    Legacy `execution`/`config`/`prompt_file` fields no longer exist."""

    def test_node_with_execute_url(self):
        n = Node(id="a", name="A", execute=Execute(url="x.md"))
        assert n.execute.url == "x.md"

    def test_node_with_execute_join(self):
        n = Node(id="a", name="A", execute=Execute(type="join"), depends_on=["x"])
        assert n.execute.type == "join"

    def test_node_with_execute_subgraph_yaml(self):
        n = Node(id="a", name="A", execute=Execute(url="sub.yaml"))
        assert n.execute.url == "sub.yaml"

    def test_node_rejects_legacy_execution_field(self):
        with pytest.raises(ValidationError):
            Node.model_validate({
                "id": "a", "name": "A",
                "execution": {"type": "command", "command": "echo"},
            })

    def test_node_rejects_legacy_config_field(self):
        with pytest.raises(ValidationError):
            Node(id="a", name="A", config="sub.yaml")

    def test_node_rejects_legacy_prompt_file_field(self):
        with pytest.raises(ValidationError):
            Node(id="a", name="A", prompt_file="x.md")

    def test_node_with_no_execute_is_gate_only(self):
        # A bare Node (no execute) is gate-only-by-elision.
        n = Node(id="a", name="A")
        assert n.execute is None


class TestSettingsExtension:
    def test_defaults_reproduce_today(self):
        s = Settings()
        assert s.base_url is None
        assert s.allow_remote_urls is False
        assert s.allow_remote_scripts is False
        assert s.allowed_url_hosts == []
        assert s.url_headers == {}
        assert s.max_remote_fetch_bytes == 5_000_000

    def test_parses_all_new_fields(self):
        s = Settings(
            base_url="https://prompts.example.com/v1/",
            allow_remote_urls=True,
            allow_remote_scripts=True,
            allowed_url_hosts=["*.internal.example.com"],
            url_headers={"https://prompts.example.com/": {"Authorization": "Bearer x"}},
            max_remote_fetch_bytes=1_000_000,
        )
        assert s.base_url == "https://prompts.example.com/v1/"
        assert s.allow_remote_urls is True
        assert s.allow_remote_scripts is True
        assert s.allowed_url_hosts == ["*.internal.example.com"]
        assert s.url_headers["https://prompts.example.com/"] == {
            "Authorization": "Bearer x"
        }
        assert s.max_remote_fetch_bytes == 1_000_000


class TestGraphValidatorOnExecuteRoutes:
    """Stage-5b routes (execute.type=route) get the same Graph-level
    validation as Stage-5a routes (execution.type=route)."""

    def test_resolves_real_goto_target(self):
        from abe_froman.schema.models import Graph

        Graph(
            name="t", version="1.0",
            nodes=[
                Node(id="a", name="A", execute=Execute(url="/usr/bin/echo")),
                Node(
                    id="r", name="R", depends_on=["a"],
                    execute=Execute(
                        type="route",
                        cases=[{"when": "True", "goto": "a"}],
                        else_="__end__",
                    ),
                ),
            ],
        )

    def test_rejects_unresolved_goto(self):
        from abe_froman.schema.models import Graph

        with pytest.raises(ValidationError) as ei:
            Graph(
                name="t", version="1.0",
                nodes=[
                    Node(id="a", name="A", execute=Execute(url="/usr/bin/echo")),
                    Node(
                        id="r", name="R", depends_on=["a"],
                        execute=Execute(
                            type="route",
                            cases=[{"when": "True", "goto": "ghost"}],
                            else_="__end__",
                        ),
                    ),
                ],
            )
        assert "ghost" in str(ei.value)
        assert "Route 'r'" in str(ei.value)

    def test_rejects_unresolved_else(self):
        from abe_froman.schema.models import Graph

        with pytest.raises(ValidationError) as ei:
            Graph(
                name="t", version="1.0",
                nodes=[
                    Node(id="a", name="A", execute=Execute(url="/usr/bin/echo")),
                    Node(
                        id="r", name="R", depends_on=["a"],
                        execute=Execute(
                            type="route",
                            cases=[],
                            else_="ghost-else",
                        ),
                    ),
                ],
            )
        assert "ghost-else" in str(ei.value)

    def test_rejects_depends_on_execute_route(self):
        from abe_froman.schema.models import Graph

        with pytest.raises(ValidationError) as ei:
            Graph(
                name="t", version="1.0",
                nodes=[
                    Node(id="a", name="A", execute=Execute(url="/usr/bin/echo")),
                    Node(
                        id="r", name="R", depends_on=["a"],
                        execute=Execute(type="route", cases=[], else_="__end__"),
                    ),
                    Node(
                        id="downstream", name="D", depends_on=["r"],
                        execute=Execute(url="/usr/bin/echo"),
                    ),
                ],
            )
        msg = str(ei.value)
        assert "downstream" in msg
        assert "route 'r'" in msg


class TestExecuteFromYAML:
    def test_url_mode_yaml(self):
        src = """
        url: prompts/x.md
        params:
          model: opus
        """
        e = Execute.model_validate(yaml.safe_load(src))
        assert e.url == "prompts/x.md"
        assert e.params == {"model": "opus"}

    def test_subgraph_mode_yaml(self):
        src = """
        url: subgraphs/sub.yaml
        params:
          inputs:
            topic: "{{paper}}"
          outputs:
            summary: "{{step2}}"
        """
        e = Execute.model_validate(yaml.safe_load(src))
        assert e.url == "subgraphs/sub.yaml"
        assert "inputs" in e.params
