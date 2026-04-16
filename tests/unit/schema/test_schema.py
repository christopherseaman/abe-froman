import pytest
import yaml
from pydantic import ValidationError

from abe_froman.schema.models import (
    CommandExecution,
    DynamicPhaseConfig,
    GateOnlyExecution,
    OutputContract,
    Phase,
    PromptExecution,
    QualityGate,
    Settings,
    WorkflowConfig,
)


class TestMinimalWorkflow:
    def test_minimal_config(self, minimal_config_dict):
        config = WorkflowConfig(**minimal_config_dict)
        assert config.name == "Test Workflow"
        assert config.version == "1.0.0"
        assert len(config.phases) == 1

    def test_name_required(self):
        with pytest.raises(ValidationError):
            WorkflowConfig(version="1.0.0", phases=[])

    def test_version_required(self):
        with pytest.raises(ValidationError):
            WorkflowConfig(name="Test", phases=[])

    def test_empty_phases_allowed(self):
        config = WorkflowConfig(name="Test", version="1.0.0", phases=[])
        assert config.phases == []


class TestPromptFileShorthand:
    """prompt_file at phase level auto-converts to PromptExecution."""

    def test_prompt_file_creates_prompt_execution(self):
        phase = Phase(id="p1", name="P1", prompt_file="test.md")
        assert isinstance(phase.execution, PromptExecution)
        assert phase.execution.prompt_file == "test.md"

    def test_prompt_file_cleared_after_normalization(self):
        phase = Phase(id="p1", name="P1", prompt_file="test.md")
        assert phase.prompt_file is None

    def test_explicit_execution_takes_precedence(self):
        phase = Phase(
            id="p1",
            name="P1",
            execution=CommandExecution(command="node", args=["test.js"]),
        )
        assert isinstance(phase.execution, CommandExecution)

    def test_both_prompt_file_and_execution_keeps_execution(self):
        """When both prompt_file and execution are set, execution wins."""
        phase = Phase(
            id="p1",
            name="P1",
            prompt_file="should-be-ignored.md",
            execution=CommandExecution(command="node"),
        )
        assert isinstance(phase.execution, CommandExecution)


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
        phase_prompt = Phase(
            id="p1",
            name="P1",
            execution={"type": "prompt", "prompt_file": "test.md"},
        )
        assert isinstance(phase_prompt.execution, PromptExecution)

        phase_cmd = Phase(
            id="p2",
            name="P2",
            execution={"type": "command", "command": "node", "args": ["x.js"]},
        )
        assert isinstance(phase_cmd.execution, CommandExecution)

        phase_gate = Phase(
            id="p3",
            name="P3",
            execution={"type": "gate_only"},
        )
        assert isinstance(phase_gate.execution, GateOnlyExecution)


class TestQualityGate:
    def test_basic_gate(self):
        gate = QualityGate(validator="gates/v.py", threshold=0.85)
        assert gate.validator == "gates/v.py"
        assert gate.threshold == 0.85
        assert gate.blocking is False
        # max_retries defaults to None (defers to Settings.max_retries)
        assert gate.max_retries is None

    def test_blocking_gate(self):
        gate = QualityGate(validator="v.md", threshold=0.9, blocking=True)
        assert gate.blocking is True

    def test_threshold_bounds(self):
        with pytest.raises(ValidationError):
            QualityGate(validator="v.md", threshold=1.5)
        with pytest.raises(ValidationError):
            QualityGate(validator="v.md", threshold=-0.1)

    def test_custom_max_retries(self):
        gate = QualityGate(validator="v.md", threshold=0.8, max_retries=5)
        assert gate.max_retries == 5


class TestEffectiveMaxRetries:
    """Phase.effective_max_retries resolves gate override vs settings default."""

    def test_no_gate_uses_settings(self):
        settings = Settings(max_retries=5)
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        assert phase.effective_max_retries(settings) == 5

    def test_gate_with_override(self):
        settings = Settings(max_retries=5)
        phase = Phase(
            id="p1",
            name="P1",
            prompt_file="t.md",
            quality_gate=QualityGate(validator="v.md", threshold=0.8, max_retries=2),
        )
        assert phase.effective_max_retries(settings) == 2

    def test_gate_without_override_uses_settings(self):
        settings = Settings(max_retries=7)
        phase = Phase(
            id="p1",
            name="P1",
            prompt_file="t.md",
            quality_gate=QualityGate(validator="v.md", threshold=0.8),
        )
        assert phase.effective_max_retries(settings) == 7


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
        phase = Phase(id="p1", name="P1", prompt_file="t.md", model="opus")
        assert phase.model == "opus"

    def test_phase_model_default_none(self):
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        assert phase.model is None

    def test_settings_default_model(self):
        settings = Settings(default_model="haiku")
        assert settings.default_model == "haiku"

    def test_settings_default_model_default_value(self):
        settings = Settings()
        assert settings.default_model == "sonnet"


class TestParseOutputAsJson:
    def test_phase_parse_output_as_json_true(self):
        phase = Phase(id="p1", name="P1", prompt_file="t.md", parse_output_as_json=True)
        assert phase.parse_output_as_json is True

    def test_parse_output_as_json_default_false(self):
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        assert phase.parse_output_as_json is False


class TestDependencyValidation:
    def test_valid_dependencies(self, multi_phase_config_dict):
        config = WorkflowConfig(**multi_phase_config_dict)
        assert config.phases[1].depends_on == ["phase-1"]

    def test_invalid_dependency_reference(self):
        with pytest.raises(ValidationError, match="nonexistent"):
            WorkflowConfig(
                name="Test",
                version="1.0.0",
                phases=[
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
            WorkflowConfig(
                name="Test",
                version="1.0.0",
                phases=[
                    {"id": "p1", "name": "P1", "prompt_file": "t.md"},
                    {"id": "p1", "name": "P1 Again", "prompt_file": "t2.md"},
                ],
            )

    def test_self_dependency(self):
        with pytest.raises(ValidationError, match="self-dependency"):
            WorkflowConfig(
                name="Test",
                version="1.0.0",
                phases=[
                    {
                        "id": "p1",
                        "name": "P1",
                        "prompt_file": "t.md",
                        "depends_on": ["p1"],
                    }
                ],
            )

    def test_no_dependencies(self):
        phase = Phase(id="p1", name="P1", prompt_file="t.md")
        assert phase.depends_on == []


class TestDynamicSubphases:
    def test_dynamic_config(self):
        config = DynamicPhaseConfig(
            enabled=True,
            manifest_path="manifest.json",
            template={"prompt_file": "template.md"},
        )
        assert config.enabled is True
        assert config.manifest_path == "manifest.json"

    def test_template_with_quality_gate(self):
        config = DynamicPhaseConfig(
            enabled=True,
            manifest_path="m.json",
            template={
                "prompt_file": "template.md",
                "quality_gate": {"validator": "v.md", "threshold": 0.8},
            },
        )
        assert config.template.quality_gate.threshold == 0.8

    def test_final_phases(self):
        config = DynamicPhaseConfig(
            enabled=True,
            manifest_path="m.json",
            template={"prompt_file": "t.md"},
            final_phases=[
                {"id": "summary", "name": "Summary", "prompt_file": "s.md"}
            ],
        )
        assert len(config.final_phases) == 1
        assert config.final_phases[0].id == "summary"

    def test_disabled_by_default(self):
        config = DynamicPhaseConfig()
        assert config.enabled is False


class TestFullExampleParse:
    def test_parse_example_yaml(self, example_workflow_path):
        with open(example_workflow_path) as f:
            raw = yaml.safe_load(f)
        config = WorkflowConfig(**raw)

        assert config.name == "CFRA Default Workflow"
        assert config.version == "1.0.0"
        assert len(config.phases) > 0
        assert config.settings.default_model == "sonnet"
        assert config.settings.output_directory == "prd"

    def test_example_has_all_execution_types(self, example_workflow_path):
        """Example YAML exercises command, prompt, and gate_only types."""
        with open(example_workflow_path) as f:
            raw = yaml.safe_load(f)
        config = WorkflowConfig(**raw)

        exec_types = {type(p.execution).__name__ for p in config.phases if p.execution}
        assert "CommandExecution" in exec_types
        assert "PromptExecution" in exec_types

    def test_example_phase_types(self, example_workflow_path):
        with open(example_workflow_path) as f:
            raw = yaml.safe_load(f)
        config = WorkflowConfig(**raw)

        phase_map = {p.id: p for p in config.phases}

        # phase-0 is a command execution
        assert isinstance(phase_map["phase-0"].execution, CommandExecution)
        assert phase_map["phase-0"].execution.command == "node"

        # phase-1 is a prompt execution (via shorthand) with model override
        assert isinstance(phase_map["phase-1"].execution, PromptExecution)
        assert phase_map["phase-1"].model == "sonnet"

    def test_example_dynamic_subphases(self, example_workflow_path):
        with open(example_workflow_path) as f:
            raw = yaml.safe_load(f)
        config = WorkflowConfig(**raw)

        phase_map = {p.id: p for p in config.phases}
        phase2 = phase_map["phase-2"]
        assert phase2.dynamic_subphases is not None
        assert phase2.dynamic_subphases.enabled is True
        assert len(phase2.dynamic_subphases.final_phases) > 0

    def test_example_fan_in_dependency(self, example_workflow_path):
        """phase-4 depends on all phase-3-* phases (fan-in)."""
        with open(example_workflow_path) as f:
            raw = yaml.safe_load(f)
        config = WorkflowConfig(**raw)

        phase_map = {p.id: p for p in config.phases}
        phase4 = phase_map["phase-4"]
        # All phase-3-* phases should be in depends_on
        phase3_ids = {p.id for p in config.phases if p.id.startswith("phase-3")}
        assert phase3_ids == set(phase4.depends_on)

    def test_example_parse_output_as_json(self, example_workflow_path):
        with open(example_workflow_path) as f:
            raw = yaml.safe_load(f)
        config = WorkflowConfig(**raw)

        phase_map = {p.id: p for p in config.phases}
        phase5 = phase_map["phase-5"]
        assert phase5.parse_output_as_json is True
