import pytest
from langgraph.graph import END, START

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.schema.models import Graph

from helpers import make_config


def _edges(graph):
    """Return (source, target) tuples for every edge."""
    return {(e.source, e.target) for e in graph.get_graph().edges}


def _conditional_edges(graph):
    return {(e.source, e.target) for e in graph.get_graph().edges if e.conditional}


class TestSingleNodeGraph:
    def test_single_prompt_node(self):
        config = make_config(
            [{"id": "p1", "name": "P1", "execute": {"url": "t.md"}}]
        )
        graph = build_workflow_graph(config)
        assert "p1" in graph.get_graph().nodes
        assert _edges(graph) == {(START, "p1"), ("p1", END)}

    def test_single_command_node(self):
        config = make_config(
            [
                {
                    "id": "c1",
                    "name": "C1",
                    "execute": {
                        "url": "/usr/bin/echo",
                        "params": {"args": ["hello"]},
                    },
                }
            ]
        )
        graph = build_workflow_graph(config)
        assert "c1" in graph.get_graph().nodes
        assert _edges(graph) == {(START, "c1"), ("c1", END)}

    def test_single_gate_only_node(self):
        """Gated node compiles to exec → _eval_<id> → _decide_<id>
        (plain edges). Decision node returns Command(goto=...) at
        runtime; no static conditional edges remain."""
        config = make_config(
            [
                {
                    "id": "g1",
                    "name": "G1",
                    # gate-only-by-elision: no execute: block
                    "evaluation": {"validator": "v.md", "threshold": 0.9},
                }
            ]
        )
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "g1" in nodes
        assert "_eval_g1" in nodes
        assert "_decide_g1" in nodes
        edges = _edges(graph)
        assert (START, "g1") in edges
        assert ("g1", "_eval_g1") in edges
        assert ("_eval_g1", "_decide_g1") in edges
        # No conditional edges from the eval pair — Command(goto=) replaces them.
        assert _conditional_edges(graph) == set()


class TestLinearChain:
    def test_two_phase_chain(self, multi_phase_config_dict):
        config = Graph(**multi_phase_config_dict)
        graph = build_workflow_graph(config)
        assert _edges(graph) == {
            (START, "node-1"),
            ("node-1", "node-2"),
            ("node-2", END),
        }

    def test_three_phase_chain(self):
        config = make_config(
            [
                {"id": "a", "name": "A", "execute": {"url": "a.md"}},
                {"id": "b", "name": "B", "execute": {"url": "b.md"}, "depends_on": ["a"]},
                {"id": "c", "name": "C", "execute": {"url": "c.md"}, "depends_on": ["b"]},
            ]
        )
        graph = build_workflow_graph(config)
        assert _edges(graph) == {
            (START, "a"),
            ("a", "b"),
            ("b", "c"),
            ("c", END),
        }


class TestParallelNodes:
    def test_diamond_dependency(self, parallel_config_dict):
        """A → (B, C) → D — both forks must be wired, and D must fan in from both."""
        config = Graph(**parallel_config_dict)
        graph = build_workflow_graph(config)
        assert _edges(graph) == {
            (START, "a"),
            ("a", "b"),
            ("a", "c"),
            ("b", "d"),
            ("c", "d"),
            ("d", END),
        }

    def test_multiple_roots(self):
        config = make_config(
            [
                {"id": "a", "name": "A", "execute": {"url": "a.md"}},
                {"id": "b", "name": "B", "execute": {"url": "b.md"}},
            ]
        )
        graph = build_workflow_graph(config)
        assert _edges(graph) == {
            (START, "a"),
            (START, "b"),
            ("a", END),
            ("b", END),
        }


class TestGateRouting:
    """Post-Stage-5d wiring: gated nodes compile to
    ``exec → _eval_<id> → _decide_<id>`` with plain edges. The
    Decision node's ``Command(goto=...)`` replaces the static
    conditional-edge router; routing destinations are not visible
    in ``graph.get_graph().edges`` because they're chosen at runtime.
    """

    def test_terminal_gate_wiring(self):
        config = make_config(
            [
                {
                    "id": "p1",
                    "name": "P1",
                    "execute": {"url": "t.md"},
                    "evaluation": {"validator": "v.md", "threshold": 0.8},
                }
            ]
        )
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "_eval_p1" in nodes
        assert "_decide_p1" in nodes
        edges = _edges(graph)
        assert (START, "p1") in edges
        assert ("p1", "_eval_p1") in edges
        assert ("_eval_p1", "_decide_p1") in edges
        # No conditional edges — Command(goto=) replaces them.
        assert _conditional_edges(graph) == set()

    def test_non_terminal_gate_wiring(self):
        config = make_config(
            [
                {
                    "id": "a",
                    "name": "A",
                    "execute": {"url": "a.md"},
                    "evaluation": {"validator": "v.md", "threshold": 0.8},
                },
                {
                    "id": "b",
                    "name": "B",
                    "execute": {"url": "b.md"},
                    "depends_on": ["a"],
                },
            ]
        )
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "_decide_a" in nodes
        assert "b" in nodes  # b is reached via Command(goto=b) at runtime
        edges = _edges(graph)
        assert (START, "a") in edges
        assert ("a", "_eval_a") in edges
        assert ("_eval_a", "_decide_a") in edges
        # No conditional edges from the eval pair.
        assert _conditional_edges(graph) == set()
        # b's static `b → END` edge is added by the terminal-end wiring;
        # LangGraph may filter it from get_graph().edges when b has no
        # static incoming edges (its only path in is the runtime Command
        # from _decide_a). Not asserting on `(b, END) in edges` —
        # downstream connectivity is verified end-to-end by e2e tests.

    def test_gate_with_multiple_dependents_fans_out(self):
        config = make_config(
            [
                {
                    "id": "a",
                    "name": "A",
                    "execute": {"url": "a.md"},
                    "evaluation": {"validator": "v.md", "threshold": 0.8},
                },
                {"id": "b", "name": "B", "execute": {"url": "b.md"}, "depends_on": ["a"]},
                {"id": "c", "name": "C", "execute": {"url": "c.md"}, "depends_on": ["a"]},
            ]
        )
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "_after_a" not in nodes
        assert "_eval_a" in nodes
        assert "_decide_a" in nodes
        assert "b" in nodes
        assert "c" in nodes

        edges = _edges(graph)
        assert ("a", "_eval_a") in edges
        assert ("_eval_a", "_decide_a") in edges
        # decide → [b, c] is a runtime Command(goto=[b, c]); no static
        # conditional edges. b/c terminal-END edges are added but
        # LangGraph may filter them from get_graph().edges (see
        # test_non_terminal_gate_wiring); end-to-end connectivity is
        # verified by e2e tests.
        assert _conditional_edges(graph) == set()


class TestInlineRouteShape:
    """Stage 5c: `Node.route` block compiles to expected LangGraph shape."""

    def test_standalone_inline_route(self):
        """Inline route with no execute body: node IS the dispatcher."""
        config = make_config([
            {"id": "produce", "name": "P", "execute": {"url": "p.md"}},
            {"id": "dispatch", "name": "D", "depends_on": ["produce"],
             "route": {
                 "cases": [{"when": "True", "goto": "ship"}],
                 "else": "__end__",
             }},
            {"id": "ship", "name": "S", "execute": {"url": "s.md"}},
        ])
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        # No synthetic _route_<id> for standalone form — the node fn itself dispatches.
        assert "_route_dispatch" not in nodes
        assert "dispatch" in nodes
        assert "produce" in nodes
        assert "ship" in nodes

    def test_execute_plus_route_creates_synthetic(self):
        """execute + route: _route_<id> synthetic dispatcher fires post-execute."""
        config = make_config([
            {"id": "classify", "name": "C", "execute": {"url": "c.md"},
             "route": {"goto": "ship"}},
            {"id": "ship", "name": "S", "execute": {"url": "s.md"}},
        ])
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "classify" in nodes
        assert "_route_classify" in nodes
        edges = _edges(graph)
        # Plain edge: execute → synthetic dispatcher
        assert ("classify", "_route_classify") in edges

    def test_execute_plus_eval_plus_route_chains_through_eval(self):
        """execute + eval + route: pass target of the Decision node is
        _route_<id>. After Stage-5d the chain is execute → _eval_<id>
        → _decide_<id> (plain edges); _decide_<id> emits
        Command(goto=_route_<id>) on pass."""
        config = make_config([
            {"id": "classify", "name": "C",
             "execute": {"url": "c.md"},
             "evaluation": {"validator": "v.py", "threshold": 0.8},
             "route": {"goto": "ship"}},
            {"id": "ship", "name": "S", "execute": {"url": "s.md"}},
        ])
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert {
            "classify", "_eval_classify", "_decide_classify",
            "_route_classify", "ship",
        } <= set(nodes)
        edges = _edges(graph)
        # execute → eval → decide (all plain). The Command(goto=_route_<id>)
        # path is runtime; no static edge from _decide_classify.
        assert ("classify", "_eval_classify") in edges
        assert ("_eval_classify", "_decide_classify") in edges
        # No conditional edges from the eval pair.
        assert _conditional_edges(graph) == set()

    def test_list_valued_goto_no_terminal_end_edge(self):
        """`route: { goto: [a, b] }` produces Command(goto=[a,b]); the
        source node must NOT get a static node→END edge."""
        config = make_config([
            {"id": "src", "name": "S", "execute": {"url": "s.md"},
             "route": {"goto": ["a", "b"]}},
            {"id": "a", "name": "A", "execute": {"url": "a.md"}},
            {"id": "b", "name": "B", "execute": {"url": "b.md"}},
        ])
        graph = build_workflow_graph(config)
        edges = _edges(graph)
        assert ("src", "_route_src") in edges
        # No spurious static src→END (would compete with Command(goto=)).
        # LangGraph may infer _route_src→END as a graph-introspection
        # artifact since the synthetic node returns Command without
        # static edges; that's a framework display detail, not a
        # routing bug, so we don't assert against it.
        assert ("src", END) not in edges

    def test_inline_route_targets_skip_start_fallback(self):
        """Goto targets that are otherwise unreached should not get a
        START → fallback edge — they're meant to be reached only via
        Command(goto=...) from the route."""
        config = make_config([
            {"id": "src", "name": "S", "execute": {"url": "s.md"},
             "route": {"goto": "tgt"}},
            {"id": "tgt", "name": "T", "execute": {"url": "t.md"}},
        ])
        graph = build_workflow_graph(config)
        edges = _edges(graph)
        # src is the only entry node; tgt is reached via Command.
        assert (START, "src") in edges
        assert (START, "tgt") not in edges


class TestModelConfig:
    def test_model_passthrough_in_config(self):
        config = make_config(
            [{"id": "p1", "name": "P1", "execute": {"url": "t.md"}, "model": "opus"}],
            default_model="haiku",
        )
        assert config.nodes[0].model == "opus"
        assert config.settings.default_model == "haiku"


class TestEvaluationNodeShape:
    def test_ungated_phase_no_eval_node(self):
        config = make_config(
            [{"id": "p1", "name": "P1", "execute": {"url": "t.md"}}]
        )
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "_eval_p1" not in nodes
        assert _edges(graph) == {(START, "p1"), ("p1", END)}


class TestDynamicGraphShape:
    def _dynamic_phase(self, template_evaluation=None):
        template = {"execute": {"url": "template.md"}}
        if template_evaluation is not None:
            template["evaluation"] = template_evaluation
        dsc = {
            "template": template,
            "final_nodes": [
                {"id": "f0", "name": "F0", "execute": {"url": "f0.md"}},
            ],
        }
        return {
            "id": "p",
            "name": "P",
            "execute": {
                "url": "/usr/bin/echo",
                "params": {"args": ["manifest"]},
            },
            "fan_out": dsc,
        }

    def test_ungated_template_registers_template_and_final_nodes(self):
        config = make_config([self._dynamic_phase()])
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "_sub_p" in nodes and "_final_p_f0" in nodes

    def test_gated_template_registers_nodes_and_parent_fanout(self):
        config = make_config([self._dynamic_phase(
            template_evaluation={"validator": "v.md", "threshold": 0.8},
        )])
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "_sub_p" in nodes
        conditional = _conditional_edges(graph)
        assert ("p", "_final_p_f0") in conditional


class TestCycleDetection:
    def test_self_dependency_rejected_at_schema(self):
        with pytest.raises(Exception, match="self-dependency"):
            make_config(
                [
                    {
                        "id": "a",
                        "name": "A",
                        "execute": {"url": "a.md"},
                        "depends_on": ["a"],
                    }
                ]
            )

    def test_circular_dependency_rejected(self):
        with pytest.raises(ValueError, match="[Cc]ircular"):
            config = make_config(
                [
                    {"id": "a", "name": "A", "execute": {"url": "a.md"}, "depends_on": ["b"]},
                    {"id": "b", "name": "B", "execute": {"url": "b.md"}, "depends_on": ["a"]},
                ]
            )
            build_workflow_graph(config)

    def test_three_node_cycle_rejected(self):
        with pytest.raises(ValueError, match="[Cc]ircular"):
            config = make_config(
                [
                    {"id": "a", "name": "A", "execute": {"url": "a.md"}, "depends_on": ["c"]},
                    {"id": "b", "name": "B", "execute": {"url": "b.md"}, "depends_on": ["a"]},
                    {"id": "c", "name": "C", "execute": {"url": "c.md"}, "depends_on": ["b"]},
                ]
            )
            build_workflow_graph(config)
