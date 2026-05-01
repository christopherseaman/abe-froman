import pytest
import yaml
from pydantic import ValidationError

from abe_froman.schema.models import (
    CommandExecution,
    DimensionCheck,
    FanOut,
    GateOnlyExecution,
    OutputContract,
    Node,
    PromptExecution,
    Evaluation,
    Settings,
    Graph,
)


class TestMinimalWorkflow:
    def test_minimal_config(self, minimal_config_dict):
        config = Graph(**minimal_config_dict)
        assert config.name == "Test Workflow"
        assert config.version == "1.0.0"
        assert len(config.nodes) == 1

    def test_name_required(self):
        with pytest.raises(ValidationError):
            Graph(version="1.0.0", nodes=[])

    def test_version_required(self):
        with pytest.raises(ValidationError):
            Graph(name="Test", nodes=[])

    def test_empty_nodes_allowed(self):
        config = Graph(name="Test", version="1.0.0", nodes=[])
        assert config.nodes == []


class TestPromptFileShorthand:
    """prompt_file at node level auto-converts to PromptExecution."""

    def test_prompt_file_creates_prompt_execution(self):
        node = Node(id="p1", name="P1", prompt_file="test.md")
        assert isinstance(node.execution, PromptExecution)
        assert node.execution.prompt_file == "test.md"

    def test_prompt_file_cleared_after_normalization(self):
        node = Node(id="p1", name="P1", prompt_file="test.md")
        assert node.prompt_file is None

    def test_explicit_execution_takes_precedence(self):
        node = Node(
            id="p1",
            name="P1",
            execution=CommandExecution(command="node", args=["test.js"]),
        )
        assert isinstance(node.execution, CommandExecution)

    def test_both_prompt_file_and_execution_keeps_execution(self):
        """When both prompt_file and execution are set, execution wins."""
        node = Node(
            id="p1",
            name="P1",
            prompt_file="should-be-ignored.md",
            execution=CommandExecution(command="node"),
        )
        assert isinstance(node.execution, CommandExecution)


class TestExecutionTypes:
    def test_prompt_execution(self):
        ex = PromptExecution(prompt_file="test.md")
        assert ex.type == "prompt"
        assert ex.prompt_file == "test.md"

    def test_command_execution(self):
        ex = CommandExecution(command="node", args=["test.js"])
        assert ex.type == "command"
        assert ex.command == "node"
        assert ex.args == ["test.js"]

    def test_command_execution_no_args(self):
        ex = CommandExecution(command="validate")
        assert ex.args == []

    def test_gate_only_execution(self):
        ex = GateOnlyExecution()
        assert ex.type == "gate_only"

    def test_execution_discriminated_union_from_dict(self):
        """Execution types parse correctly from raw dicts (YAML deserialization)."""
        node_prompt = Node(
            id="p1",
            name="P1",
            execution={"type": "prompt", "prompt_file": "test.md"},
        )
        assert isinstance(node_prompt.execution, PromptExecution)

        node_cmd = Node(
            id="p2",
            name="P2",
            execution={"type": "command", "command": "node", "args": ["x.js"]},
        )
        assert isinstance(node_cmd.execution, CommandExecution)

        node_gate = Node(
            id="p3",
            name="P3",
            execution={"type": "gate_only"},
        )
        assert isinstance(node_gate.execution, GateOnlyExecution)


class TestQualityGate:
    def test_basic_gate(self):
        gate = Evaluation(validator="gates/v.py", threshold=0.85)
        assert gate.validator == "gates/v.py"
        assert gate.threshold == 0.85
        assert gate.blocking is False
        # max_retries defaults to None (defers to Settings.max_retries)
        assert gate.max_retries is None

    def test_blocking_gate(self):
        gate = Evaluation(validator="v.md", threshold=0.9, blocking=True)
        assert gate.blocking is True

    def test_threshold_bounds(self):
        with pytest.raises(ValidationError):
            Evaluation(validator="v.md", threshold=1.5)
        with pytest.raises(ValidationError):
            Evaluation(validator="v.md", threshold=-0.1)

    def test_custom_max_retries(self):
        gate = Evaluation(validator="v.md", threshold=0.8, max_retries=5)
        assert gate.max_retries == 5

    def test_dimension_gate(self):
        gate = Evaluation(
            validator="v.py",
            dimensions=[
                DimensionCheck(field="correctness", min=0.7),
                DimensionCheck(field="style", min=0.5),
            ],
        )
        assert len(gate.dimensions) == 2
        assert gate.dimensions[0].field == "correctness"
        assert gate.dimensions[0].min == 0.7

    def test_dimension_gate_from_yaml(self):
        raw = {
            "validator": "v.py",
            "dimensions": [
                {"field": "correctness", "min": 0.7},
                {"field": "style", "min": 0.5},
            ],
        }
        gate = Evaluation(**raw)
        assert gate.dimensions[1].field == "style"

    def test_dimension_check_bounds(self):
        with pytest.raises(ValidationError):
            DimensionCheck(field="x", min=1.5)
        with pytest.raises(ValidationError):
            DimensionCheck(field="x", min=-0.1)


class TestEffectiveMaxRetries:
    """Node.effective_max_retries resolves gate override vs settings default."""

    def test_no_gate_uses_settings(self):
        settings = Settings(max_retries=5)
        node = Node(id="p1", name="P1", prompt_file="t.md")
        assert node.effective_max_retries(settings) == 5

    def test_gate_with_override(self):
        settings = Settings(max_retries=5)
        node = Node(
            id="p1",
            name="P1",
            prompt_file="t.md",
            evaluation=Evaluation(validator="v.md", threshold=0.8, max_retries=2),
        )
        assert node.effective_max_retries(settings) == 2

    def test_gate_without_override_uses_settings(self):
        settings = Settings(max_retries=7)
        node = Node(
            id="p1",
            name="P1",
            prompt_file="t.md",
            evaluation=Evaluation(validator="v.md", threshold=0.8),
        )
        assert node.effective_max_retries(settings) == 7


class TestOutputContract:
    def test_basic_contract(self):
        contract = OutputContract(
            base_directory="output/", required_files=["result.json"]
        )
        assert contract.base_directory == "output/"
        assert contract.required_files == ["result.json"]

    def test_empty_required_files(self):
        contract = OutputContract(base_directory="output/", required_files=[])
        assert contract.required_files == []


class TestModelSelection:
    def test_phase_model(self):
        node = Node(id="p1", name="P1", prompt_file="t.md", model="opus")
        assert node.model == "opus"

    def test_phase_model_default_none(self):
        node = Node(id="p1", name="P1", prompt_file="t.md")
        assert node.model is None

    def test_settings_default_model(self):
        settings = Settings(default_model="haiku")
        assert settings.default_model == "haiku"

    def test_settings_default_model_default_value(self):
        settings = Settings()
        assert settings.default_model == "sonnet"


class TestDependencyValidation:
    def test_valid_dependencies(self, multi_phase_config_dict):
        config = Graph(**multi_phase_config_dict)
        assert config.nodes[1].depends_on == ["node-1"]

    def test_invalid_dependency_reference(self):
        with pytest.raises(ValidationError, match="nonexistent"):
            Graph(
                name="Test",
                version="1.0.0",
                nodes=[
                    {"id": "p1", "name": "P1", "prompt_file": "t.md"},
                    {
                        "id": "p2",
                        "name": "P2",
                        "prompt_file": "t.md",
                        "depends_on": ["nonexistent"],
                    },
                ],
            )

    def test_duplicate_phase_ids(self):
        with pytest.raises(ValidationError, match="[Dd]uplicate"):
            Graph(
                name="Test",
                version="1.0.0",
                nodes=[
                    {"id": "p1", "name": "P1", "prompt_file": "t.md"},
                    {"id": "p1", "name": "P1 Again", "prompt_file": "t2.md"},
                ],
            )

    def test_self_dependency(self):
        with pytest.raises(ValidationError, match="self-dependency"):
            Graph(
                name="Test",
                version="1.0.0",
                nodes=[
                    {
                        "id": "p1",
                        "name": "P1",
                        "prompt_file": "t.md",
                        "depends_on": ["p1"],
                    }
                ],
            )

    def test_no_dependencies(self):
        node = Node(id="p1", name="P1", prompt_file="t.md")
        assert node.depends_on == []


class TestFanOut:
    def test_dynamic_config(self):
        config = FanOut(
            enabled=True,
            manifest_path="manifest.json",
            template={"prompt_file": "template.md"},
        )
        assert config.enabled is True
        assert config.manifest_path == "manifest.json"

    def test_template_with_evaluation(self):
        config = FanOut(
            enabled=True,
            manifest_path="m.json",
            template={
                "prompt_file": "template.md",
                "evaluation": {"validator": "v.md", "threshold": 0.8},
            },
        )
        assert config.template.evaluation.threshold == 0.8

    def test_final_nodes(self):
        config = FanOut(
            enabled=True,
            manifest_path="m.json",
            template={"prompt_file": "t.md"},
            final_nodes=[
                {"id": "summary", "name": "Summary", "prompt_file": "s.md"}
            ],
        )
        assert len(config.final_nodes) == 1
        assert config.final_nodes[0].id == "summary"

    def test_disabled_by_default(self):
        config = FanOut()
        assert config.enabled is False


class TestFullExampleParse:
    def test_parse_example_yaml(self, example_workflow_path):
        with open(example_workflow_path) as f:
            raw = yaml.safe_load(f)
        config = Graph(**raw)

        assert config.name == "CFRA Default Workflow"
        assert config.version == "1.0.0"
        assert len(config.nodes) > 0
        assert config.settings.default_model == "sonnet"
        assert config.settings.output_directory == "prd"

    def test_example_has_all_execution_types(self, example_workflow_path):
        """Example YAML exercises script, prompt, and gate-only-by-elision."""
        from pathlib import Path
        with open(example_workflow_path) as f:
            raw = yaml.safe_load(f)
        config = Graph(**raw)

        # Stage 5b: classify by execute.url extension or by execute=None.
        prompt_exts = {".md", ".txt", ".prompt"}
        kinds: set[str] = set()
        for n in config.nodes:
            if n.execute is None:
                kinds.add("gate_only")
                continue
            if n.execute.type:
                kinds.add(n.execute.type)
                continue
            ext = Path(n.execute.url).suffix.lower()
            if ext in prompt_exts:
                kinds.add("prompt")
            elif ext == "":
                kinds.add("binary")
            else:
                kinds.add("script")
        assert "prompt" in kinds
        assert ("script" in kinds) or ("binary" in kinds)

    def test_example_phase_types(self, example_workflow_path):
        from pathlib import Path
        with open(example_workflow_path) as f:
            raw = yaml.safe_load(f)
        config = Graph(**raw)

        node_map = {p.id: p for p in config.nodes}

        # node-0 was a `command: node`, migrated to a binary url.
        n0 = node_map["node-0"]
        assert n0.execute is not None
        assert n0.execute.url.endswith("/node")

        # node-1 uses a prompt URL with a model override on the Node itself.
        n1 = node_map["node-1"]
        assert n1.execute is not None
        assert Path(n1.execute.url).suffix == ".md"
        assert n1.model == "sonnet"

    def test_example_dynamic_subphases(self, example_workflow_path):
        with open(example_workflow_path) as f:
            raw = yaml.safe_load(f)
        config = Graph(**raw)

        node_map = {p.id: p for p in config.nodes}
        node2 = node_map["node-2"]
        assert node2.fan_out is not None
        assert node2.fan_out.enabled is True
        assert len(node2.fan_out.final_nodes) > 0

    def test_example_fan_in_dependency(self, example_workflow_path):
        """node-4 depends on all node-3-* nodes (fan-in)."""
        with open(example_workflow_path) as f:
            raw = yaml.safe_load(f)
        config = Graph(**raw)

        node_map = {p.id: p for p in config.nodes}
        node4 = node_map["node-4"]
        # All node-3-* nodes should be in depends_on
        node3_ids = {p.id for p in config.nodes if p.id.startswith("node-3")}
        assert node3_ids == set(node4.depends_on)


# ---------------------------------------------------------------------------
# Node timeout fields + effective_timeout
# ---------------------------------------------------------------------------


class TestNodeTimeout:
    def test_phase_timeout_field(self):
        p = Node(id="a", name="A", timeout=30.0)
        assert p.timeout == 30.0

    def test_phase_timeout_defaults_none(self):
        p = Node(id="a", name="A")
        assert p.timeout is None

    def test_settings_default_timeout(self):
        s = Settings(default_timeout=60.0)
        assert s.default_timeout == 60.0

    def test_settings_default_timeout_defaults_none(self):
        s = Settings()
        assert s.default_timeout is None

    def test_effective_timeout_phase_overrides_settings(self):
        s = Settings(default_timeout=60.0)
        p = Node(id="a", name="A", timeout=10.0)
        assert p.effective_timeout(s) == 10.0

    def test_effective_timeout_falls_back_to_settings(self):
        s = Settings(default_timeout=60.0)
        p = Node(id="a", name="A")
        assert p.effective_timeout(s) == 60.0

    def test_effective_timeout_both_none(self):
        s = Settings()
        p = Node(id="a", name="A")
        assert p.effective_timeout(s) is None

