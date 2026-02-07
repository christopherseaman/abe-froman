import pytest

from abe_froman.engine.builder import build_workflow_graph
from abe_froman.schema.models import WorkflowConfig

from helpers import make_config


class TestSingleNodeGraph:
    def test_single_prompt_node(self):
        config = make_config(
            [{"id": "p1", "name": "P1", "prompt_file": "t.md"}]
        )
        graph = build_workflow_graph(config)
        assert "p1" in graph.get_graph().nodes

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


class TestLinearChain:
    def test_two_phase_chain(self, multi_phase_config_dict):
        config = WorkflowConfig(**multi_phase_config_dict)
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "phase-1" in nodes
        assert "phase-2" in nodes

    def test_three_phase_chain(self):
        config = make_config(
            [
                {"id": "a", "name": "A", "prompt_file": "a.md"},
                {"id": "b", "name": "B", "prompt_file": "b.md", "depends_on": ["a"]},
                {"id": "c", "name": "C", "prompt_file": "c.md", "depends_on": ["b"]},
            ]
        )
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert all(n in nodes for n in ["a", "b", "c"])


class TestParallelPhases:
    def test_diamond_dependency(self, parallel_config_dict):
        config = WorkflowConfig(**parallel_config_dict)
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert all(n in nodes for n in ["a", "b", "c", "d"])

    def test_multiple_roots(self):
        """Multiple phases with no dependencies all connect from START."""
        config = make_config(
            [
                {"id": "a", "name": "A", "prompt_file": "a.md"},
                {"id": "b", "name": "B", "prompt_file": "b.md"},
            ]
        )
        graph = build_workflow_graph(config)
        nodes = graph.get_graph().nodes
        assert "a" in nodes
        assert "b" in nodes


class TestGateRouting:
    def test_terminal_gate_compiles(self):
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
        assert graph is not None

    def test_non_terminal_gate_compiles(self):
        """Quality gate on a non-terminal phase should compile."""
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
        assert "a" in graph.get_graph().nodes
        assert "b" in graph.get_graph().nodes

    def test_gate_with_multiple_dependents_compiles(self):
        """Gate phase with fan-out to multiple dependents uses passthrough."""
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
        assert "a" in nodes
        assert "b" in nodes
        assert "c" in nodes
        # Passthrough node should exist
        assert "_after_a" in nodes


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
