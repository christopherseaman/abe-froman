"""Compile-time integration tests for the Stage-5b execute.url shape.

Function-level tests cover compile-time recognition of:
    - execute.url=*.yaml → subgraph reference
    - execute.type=join → join sentinel
    - execute.type=route → route node
    - cycle detection across execute.url refs
    - subgraph state projection via execute.params.{inputs,outputs}

Each test pairs a positive (compiles correctly) with a negative
(cycle detected, malformed shape, etc.) where applicable.
"""

from __future__ import annotations

import shutil

import pytest

from abe_froman.compile.graph import (
    _is_route,
    _is_subgraph_ref,
    build_workflow_graph,
)
from abe_froman.compile.subgraph import SubgraphCycleError, node_subgraph_path
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.state import make_initial_state
from abe_froman.schema.models import (
    Execute,
    Graph,
    Node,
    RouteCase,
)

_ECHO = shutil.which("echo") or "/bin/echo"


class TestSubgraphRefDetection:
    def test_execute_yaml_recognized(self):
        n = Node(id="a", name="A", execute=Execute(url="sub.yaml"))
        assert _is_subgraph_ref(n) is True
        assert node_subgraph_path(n) == "sub.yaml"

    def test_execute_yml_recognized(self):
        n = Node(id="a", name="A", execute=Execute(url="sub.yml"))
        assert _is_subgraph_ref(n) is True

    def test_execute_md_not_subgraph(self):
        n = Node(id="a", name="A", execute=Execute(url="prompt.md"))
        assert _is_subgraph_ref(n) is False
        assert node_subgraph_path(n) is None

    def test_no_execute_not_subgraph(self):
        n = Node(id="a", name="A")
        assert _is_subgraph_ref(n) is False


class TestRouteDetection:
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
        assert n.execute.cases[0].when == "x > 0"
        assert n.execute.else_ == "produce"

    def test_url_node_not_route(self):
        n = Node(id="a", name="A", execute=Execute(url="x.md"))
        assert _is_route(n) is False

    def test_no_execute_not_route(self):
        n = Node(id="a", name="A")
        assert _is_route(n) is False


class TestSubgraphCompileViaExecuteURL:
    """Compile a graph with execute.url=*.yaml and verify subgraph wiring."""

    @pytest.mark.asyncio
    async def test_compiles_via_execute_url_yaml(self, tmp_path):
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text(
            "name: sub\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: inner\n    name: Inner\n"
            f"    execute:\n      url: {_ECHO}\n"
            "      params:\n        args: ['from-sub']\n"
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
        assert "p" in result["completed_nodes"]
        assert "from-sub" in result["node_outputs"]["p"]

    @pytest.mark.asyncio
    async def test_execute_params_inputs_outputs_project_through(self, tmp_path):
        """Stage-5b subgraph with execute.params.{inputs,outputs} actually
        projects state across the boundary (the HIGH 1 audit fix)."""
        sub_yaml = tmp_path / "sub.yaml"
        sub_yaml.write_text(
            "name: sub\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: step1\n    name: Step1\n"
            f"    execute:\n      url: {_ECHO}\n"
            "      params:\n        args: ['{{topic}}']\n"
            "  - id: step2\n    name: Step2\n    depends_on: [step1]\n"
            f"    execute:\n      url: {_ECHO}\n"
            "      params:\n        args: ['final-{{step1}}']\n"
        )
        producer = Node(
            id="producer", name="Producer",
            execute=Execute(url=_ECHO, params={"args": ["alpha"]}),
        )
        wrapper = Node(
            id="p", name="P", depends_on=["producer"],
            execute=Execute(
                url="sub.yaml",
                params={
                    "inputs": {"topic": "{{producer}}"},
                    "outputs": {"second": "{{step2}}"},
                },
            ),
        )
        config = Graph(name="parent", version="1.0", nodes=[producer, wrapper])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor, _base_dir=tmp_path)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p" in result["completed_nodes"]
        assert "p.second" in result["node_outputs"]
        assert "final-alpha" in result["node_outputs"]["p.second"]


class TestCycleDetectionAcrossShapes:
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


class TestRouteCompileViaExecuteShape:
    """A route authored in execute.{type:route, cases, else} compiles and
    dispatches via Command(goto=)."""

    @pytest.mark.asyncio
    async def test_route_via_execute_shape_dispatches(self, tmp_path):
        config = Graph(
            name="t", version="1.0",
            nodes=[
                Node(
                    id="produce", name="produce",
                    execute=Execute(url=_ECHO, params={"args": ["draft"]}),
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
                    execute=Execute(url=_ECHO, params={"args": ["shipped"]}),
                ),
            ],
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor, _base_dir=tmp_path)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "ship" in result["completed_nodes"]
        assert "shipped" in result["node_outputs"]["ship"]


class TestJoinViaExecuteShape:
    """execute.type=join is a no-op topology marker for fan-in."""

    @pytest.mark.asyncio
    async def test_join_runs_after_deps(self, tmp_path):
        config = Graph(
            name="t", version="1.0",
            nodes=[
                Node(
                    id="a", name="A",
                    execute=Execute(url=_ECHO, params={"args": ["alpha"]}),
                ),
                Node(
                    id="b", name="B",
                    execute=Execute(url=_ECHO, params={"args": ["beta"]}),
                ),
                Node(
                    id="merge", name="Merge",
                    depends_on=["a", "b"],
                    execute=Execute(type="join"),
                ),
            ],
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor, _base_dir=tmp_path)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
        assert "merge" in result["completed_nodes"]
        assert "a" in result["completed_nodes"]
        assert "b" in result["completed_nodes"]
