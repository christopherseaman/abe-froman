import pytest
from langgraph.graph import END, START

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.schema.models import WorkflowConfig

from helpers import make_config


def _edges(graph):
    """Return (source, target) tuples for every edge."""
    return {(e.source, e.target) for e in graph.get_graph().edges}


def _conditional_edges(graph):
    return {(e.source, e.target) for e in graph.get_graph().edges if e.conditional}


class TestSingleNodeGraph:
    def test_single_prompt_node(self):
        config = make_config(
            [{"id": "p1", "name": "P1", "prompt_file": "t.md"}]
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
                    "execution": {
                        "type": "command",
                        "command": "echo",
                        "args": ["hello"],
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
                    "execution": {"type": "gate_only"},
                    "quality_gate": {"validator": "v.md", "threshold": 0.9},
                }
            ]
        )
        graph = build_workflow_graph(config)
        assert "g1" in graph.get_graph().nodes
        # Gate-only terminal phase emits conditional edges: pass → END, retry → self.
        assert (START, "g1") in _edges(graph)
        assert {("g1", END), ("g1", "g1")} <= _conditional_edges(graph)


class TestLinearChain:
    def test_two_phase_chain(self, multi_phase_config_dict):
        config = WorkflowConfig(**multi_phase_config_dict)
        graph = build_workflow_graph(config)
        assert _edges(graph) == {
            (START, "phase-1"),
            ("phase-1", "phase-2"),
            ("phase-2", END),
        }

    def test_three_phase_chain(self):
        config = make_config(
            [
                {"id": "a", "name": "A", "prompt_file": "a.md"},
                {"id": "b", "name": "B", "prompt_file": "b.md", "depends_on": ["a"]},
                {"id": "c", "name": "C", "prompt_file": "c.md", "depends_on": ["b"]},
            ]
        )
        graph = build_workflow_graph(config)
        assert _edges(graph) == {
            (START, "a"),
            ("a", "b"),
            ("b", "c"),
            ("c", END),
        }


class TestParallelPhases:
    def test_diamond_dependency(self, parallel_config_dict):
        """A → (B, C) → D — both forks must be wired, and D must fan in from both."""
        config = WorkflowConfig(**parallel_config_dict)
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
        """Roots both start from START and both reach END independently."""
        config = make_config(
            [
                {"id": "a", "name": "A", "prompt_file": "a.md"},
                {"id": "b", "name": "B", "prompt_file": "b.md"},
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
        """Terminal gated phase: pass → END, retry → self."""
        config = make_config(
            [
                {
                    "id": "p1",
                    "name": "P1",
                    "prompt_file": "t.md",
                    "quality_gate": {"validator": "v.md", "threshold": 0.8},
                }
            ]
        )
        graph = build_workflow_graph(config)
        # Entry is unconditional; routing decisions from p1 are conditional.
        assert (START, "p1") in _edges(graph)
        assert _conditional_edges(graph) == {("p1", END), ("p1", "p1")}

    def test_non_terminal_gate_wiring(self):
        """Gate with single dependent: pass → b, retry → a, fail → END."""
        config = make_config(
            [
                {
                    "id": "a",
                    "name": "A",
                    "prompt_file": "a.md",
                    "quality_gate": {"validator": "v.md", "threshold": 0.8},
                },
                {
                    "id": "b",
                    "name": "B",
                    "prompt_file": "b.md",
                    "depends_on": ["a"],
                },
            ]
        )
        graph = build_workflow_graph(config)
        edges = _edges(graph)
        assert (START, "a") in edges
        assert ("b", END) in edges
        assert _conditional_edges(graph) == {
            ("a", "b"),
            ("a", "a"),
            ("a", END),
        }

    def test_gate_with_multiple_dependents_uses_passthrough(self):
        """Gate with fan-out: a routes through _after_a, which then splits to b and c."""
        config = make_config(
            [
                {
                    "id": "a",
                    "name": "A",
                    "prompt_file": "a.md",
                    "quality_gate": {"validator": "v.md", "threshold": 0.8},
                },
                {"id": "b", "name": "B", "prompt_file": "b.md", "depends_on": ["a"]},
                {"id": "c", "name": "C", "prompt_file": "c.md", "depends_on": ["a"]},
            ]
        )
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "_after_a" in nodes

        edges = _edges(graph)
        # Passthrough distribution must be unconditional
        passthrough = {(s, t) for s, t in edges if s == "_after_a"}
        assert passthrough == {("_after_a", "b"), ("_after_a", "c")}
        # Downstream phases must reach END
        assert {("b", END), ("c", END)} <= edges
        # Gate routing conditional edges: pass → _after_a, retry → a, fail → END
        assert _conditional_edges(graph) == {
            ("a", "_after_a"),
            ("a", "a"),
            ("a", END),
        }


class TestModelConfig:
    def test_model_passthrough_in_config(self):
        config = make_config(
            [{"id": "p1", "name": "P1", "prompt_file": "t.md", "model": "opus"}],
            default_model="haiku",
        )
        assert config.phases[0].model == "opus"
        assert config.settings.default_model == "haiku"


class TestCycleDetection:
    def test_self_dependency_rejected_at_schema(self):
        with pytest.raises(Exception, match="self-dependency"):
            make_config(
                [
                    {
                        "id": "a",
                        "name": "A",
                        "prompt_file": "a.md",
                        "depends_on": ["a"],
                    }
                ]
            )

    def test_circular_dependency_rejected(self):
        with pytest.raises(ValueError, match="[Cc]ircular"):
            config = make_config(
                [
                    {"id": "a", "name": "A", "prompt_file": "a.md", "depends_on": ["b"]},
                    {"id": "b", "name": "B", "prompt_file": "b.md", "depends_on": ["a"]},
                ]
            )
            build_workflow_graph(config)

    def test_three_node_cycle_rejected(self):
        with pytest.raises(ValueError, match="[Cc]ircular"):
            config = make_config(
                [
                    {"id": "a", "name": "A", "prompt_file": "a.md", "depends_on": ["c"]},
                    {"id": "b", "name": "B", "prompt_file": "b.md", "depends_on": ["a"]},
                    {"id": "c", "name": "C", "prompt_file": "c.md", "depends_on": ["b"]},
                ]
            )
            build_workflow_graph(config)
