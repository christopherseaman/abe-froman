import pytest
from langgraph.graph import END, START

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.schema.models import Graph

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
        assert "g1" in nodes and "_eval_g1" in nodes
        edges = _edges(graph)
        assert (START, "g1") in edges
        assert ("g1", "_eval_g1") in edges
        assert {("_eval_g1", END), ("_eval_g1", "g1")} <= _conditional_edges(graph)


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
        edges = _edges(graph)
        assert (START, "p1") in edges
        assert ("p1", "_eval_p1") in edges
        assert _conditional_edges(graph) == {("_eval_p1", END), ("_eval_p1", "p1")}

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
        edges = _edges(graph)
        assert (START, "a") in edges
        assert ("a", "_eval_a") in edges
        assert ("b", END) in edges
        assert _conditional_edges(graph) == {
            ("_eval_a", "b"),
            ("_eval_a", "a"),
            ("_eval_a", END),
        }

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

        edges = _edges(graph)
        assert {("b", END), ("c", END)} <= edges
        assert ("a", "_eval_a") in edges
        assert _conditional_edges(graph) == {
            ("_eval_a", "b"),
            ("_eval_a", "c"),
            ("_eval_a", "a"),
            ("_eval_a", END),
        }


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
            "enabled": True,
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
