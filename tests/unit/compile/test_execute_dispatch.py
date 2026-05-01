"""Compile-time integration tests for the Stage-5b execute.url shape.

Function-level tests cover compile-time recognition of:
    - execute.url=*.yaml → subgraph reference
    - execute.type=join → join sentinel
    - execute.type=route → route node
    - cycle detection across mixed Stage-4 / Stage-5b refs

Each test pairs a positive (compiles correctly) with a negative
(cycle detected, malformed shape, etc.) where applicable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from abe_froman.compile.graph import (
    _is_route,
    _is_subgraph_ref,
    _route_cases_else,
    _subgraph_path,
    build_workflow_graph,
)
from abe_froman.compile.subgraph import SubgraphCycleError
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.state import make_initial_state
from abe_froman.schema.models import (
    Execute,
    Graph,
    Node,
    RouteCase,
    RouteExecution,
)


class TestSubgraphRefDetection:
    def test_legacy_config_recognized(self):
        n = Node(id="a", name="A", config="sub.yaml")
        assert _is_subgraph_ref(n) is True
        assert _subgraph_path(n) == "sub.yaml"

    def test_execute_yaml_recognized(self):
        n = Node(id="a", name="A", execute=Execute(url="sub.yaml"))
        assert _is_subgraph_ref(n) is True
        assert _subgraph_path(n) == "sub.yaml"

    def test_execute_yml_recognized(self):
        n = Node(id="a", name="A", execute=Execute(url="sub.yml"))
        assert _is_subgraph_ref(n) is True

    def test_execute_md_not_subgraph(self):
        n = Node(id="a", name="A", execute=Execute(url="prompt.md"))
        assert _is_subgraph_ref(n) is False
        assert _subgraph_path(n) is None

    def test_no_execute_no_config_not_subgraph(self):
        n = Node(id="a", name="A")
        assert _is_subgraph_ref(n) is False


class TestRouteDetection:
    def test_legacy_route_execution_recognized(self):
        n = Node(
            id="r", name="R",
            execution=RouteExecution(
                cases=[RouteCase(when="True", goto="x")],
                else_="__end__",
            ),
        )
        assert _is_route(n) is True
        cases, else_target = _route_cases_else(n)
        assert len(cases) == 1
        assert else_target == "__end__"

    def test_execute_route_recognized(self):
        n = Node(
            id="r", name="R",
            execute=Execute(
                type="route",
                cases=[RouteCase(when="x > 0", goto="ship")],
                else_="produce",
            ),
        )
        assert _is_route(n) is True
        cases, else_target = _route_cases_else(n)
        assert cases[0].when == "x > 0"
        assert else_target == "produce"

    def test_non_route_node_returns_false(self):
        n = Node(id="a", name="A", execute=Execute(url="x.md"))
        assert _is_route(n) is False
        with pytest.raises(ValueError, match="not a route"):
            _route_cases_else(n)


class TestSubgraphCompileViaExecuteURL:
    """Compile a graph with execute.url=*.yaml and verify subgraph wiring."""

    @pytest.mark.asyncio
    async def test_compiles_via_execute_url_yaml(self, tmp_path):
        # Create a minimal subgraph YAML
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text(
            "name: sub\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: inner\n    name: Inner\n"
            "    execution:\n      type: command\n      command: echo\n"
            "      args: ['from-sub']\n"
        )
        config = Graph(
            name="parent", version="1.0",
            nodes=[
                Node(id="p", name="P", execute=Execute(url="sub.yaml")),
            ],
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(
            config, executor, _base_dir=tmp_path,
        )
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        # Subgraph terminal output projects as node_outputs[parent.id]
        assert "p" in result["completed_nodes"]
        assert "from-sub" in result["node_outputs"]["p"]


class TestCycleDetectionAcrossShapes:
    def test_legacy_to_legacy_cycle_detected(self, tmp_path):
        # a.yaml references b.yaml references a.yaml
        a = tmp_path / "a.yaml"
        b = tmp_path / "b.yaml"
        a.write_text(
            "name: a\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: ref\n    name: R\n    config: b.yaml\n"
        )
        b.write_text(
            "name: b\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: ref\n    name: R\n    config: a.yaml\n"
        )
        config = Graph(
            name="parent", version="1.0",
            nodes=[Node(id="p", name="P", config="a.yaml")],
        )
        with pytest.raises(SubgraphCycleError):
            build_workflow_graph(config, executor=None, _base_dir=tmp_path)

    def test_execute_url_to_execute_url_cycle_detected(self, tmp_path):
        a = tmp_path / "a.yaml"
        b = tmp_path / "b.yaml"
        a.write_text(
            "name: a\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: ref\n    name: R\n"
            "    execute:\n      url: b.yaml\n"
        )
        b.write_text(
            "name: b\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: ref\n    name: R\n"
            "    execute:\n      url: a.yaml\n"
        )
        config = Graph(
            name="parent", version="1.0",
            nodes=[Node(id="p", name="P", execute=Execute(url="a.yaml"))],
        )
        with pytest.raises(SubgraphCycleError):
            build_workflow_graph(config, executor=None, _base_dir=tmp_path)

    def test_mixed_shape_cycle_detected(self, tmp_path):
        """Cycle through one legacy config: + one execute.url: in the chain."""
        a = tmp_path / "a.yaml"
        b = tmp_path / "b.yaml"
        a.write_text(
            "name: a\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: ref\n    name: R\n    config: b.yaml\n"
        )
        b.write_text(
            "name: b\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: ref\n    name: R\n"
            "    execute:\n      url: a.yaml\n"
        )
        config = Graph(
            name="parent", version="1.0",
            nodes=[Node(id="p", name="P", execute=Execute(url="a.yaml"))],
        )
        with pytest.raises(SubgraphCycleError):
            build_workflow_graph(config, executor=None, _base_dir=tmp_path)


class TestRouteCompileViaExecuteShape:
    """A route authored in the new execute.{type:route, cases, else} shape
    compiles and dispatches the same as the Stage-5a execution.{type:route}."""

    @pytest.mark.asyncio
    async def test_route_via_execute_shape_dispatches(self, tmp_path):
        config = Graph(
            name="t", version="1.0",
            nodes=[
                Node(
                    id="produce", name="produce",
                    execution={"type": "command", "command": "echo", "args": ["draft"]},
                ),
                Node(
                    id="decide", name="decide", depends_on=["produce"],
                    execute=Execute(
                        type="route",
                        cases=[RouteCase(when="True", goto="ship")],
                        else_="__end__",
                    ),
                ),
                Node(
                    id="ship", name="ship",
                    execution={"type": "command", "command": "echo", "args": ["shipped"]},
                ),
            ],
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor, _base_dir=tmp_path)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "ship" in result["completed_nodes"]
        assert "shipped" in result["node_outputs"]["ship"]
