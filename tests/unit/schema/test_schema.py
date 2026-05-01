import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from abe_froman.schema.models import (
    DimensionCheck,
    Evaluation,
    Execute,
    FanOut,
    Graph,
    Node,
    OutputContract,
    RouteCase,
    Settings,
)

ECHO_BIN = shutil.which("echo") or "/bin/echo"
NODE_BIN = shutil.which("node") or "/usr/bin/node"


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


class TestExecuteUrl:
    """Stage 5b: Node.execute is the single execution shape; URL mode."""

    def test_url_mode_basic(self):
        node = Node(id="p1", name="P1", execute=Execute(url="test.md"))
        assert node.execute is not None
        assert node.execute.url == "test.md"
        assert node.execute.type is None
        assert node.execute.params == {}

    def test_url_mode_from_dict(self):
        node = Node(
            id="p1",
            name="P1",
            execute={"url": "test.md"},
        )
        assert node.execute is not None
        assert node.execute.url == "test.md"

    def test_url_mode_with_params(self):
        node = Node(
            id="p1",
            name="P1",
            execute=Execute(url=ECHO_BIN, params={"args": ["hello"]}),
        )
        assert node.execute.url == ECHO_BIN
        assert node.execute.params == {"args": ["hello"]}

    def test_url_mode_subgraph_with_inputs_outputs(self):
        node = Node(
            id="p1",
            name="P1",
            execute=Execute(
                url="sub.yaml",
                params={
                    "inputs": {"topic": "{{parent}}"},
                    "outputs": {"result": "{{terminal}}"},
                },
            ),
        )
        assert node.execute.url == "sub.yaml"
        assert node.execute.params["inputs"] == {"topic": "{{parent}}"}
        assert node.execute.params["outputs"] == {"result": "{{terminal}}"}

    def test_gate_only_by_elision(self):
        """No execute= means gate-only-by-elision."""
        node = Node(id="p1", name="P1")
        assert node.execute is None


class TestExecuteJoin:
    def test_join_mode(self):
        ex = Execute(type="join")
        assert ex.type == "join"
        assert ex.url is None
        assert ex.params == {}

    def test_join_from_dict(self):
        node = Node(id="p1", name="P1", execute={"type": "join"})
        assert node.execute.type == "join"

    def test_join_rejects_params(self):
        with pytest.raises(ValidationError):
            Execute(type="join", params={"foo": "bar"})

    def test_join_rejects_cases(self):
        with pytest.raises(ValidationError):
            Execute(type="join", cases=[RouteCase(when="x", goto="y")])

    def test_join_rejects_else(self):
        with pytest.raises(ValidationError):
            Execute.model_validate({"type": "join", "else": "fallback"})


class TestExecuteRoute:
    def test_route_basic(self):
        ex = Execute.model_validate(
            {
                "type": "route",
                "cases": [{"when": "x >= 0.8", "goto": "ship"}],
                "else": "produce",
            }
        )
        assert ex.type == "route"
        assert len(ex.cases) == 1
        assert ex.cases[0].when == "x >= 0.8"
        assert ex.cases[0].goto == "ship"
        assert ex.else_ == "produce"

    def test_route_requires_else(self):
        with pytest.raises(ValidationError):
            Execute(
                type="route",
                cases=[RouteCase(when="x", goto="ship")],
            )

    def test_route_rejects_params(self):
        with pytest.raises(ValidationError):
            Execute.model_validate(
                {
                    "type": "route",
                    "params": {"foo": "bar"},
                    "else": "fallback",
                }
            )

    def test_route_else_only_is_valid(self):
        ex = Execute.model_validate({"type": "route", "else": "fallback"})
        assert ex.cases == []
        assert ex.else_ == "fallback"


class TestExecuteShapeValidator:
    """Execute must set exactly one of: url, type=join, type=route."""

    def test_no_modes_set_rejected(self):
        with pytest.raises(ValidationError):
            Execute()

    def test_url_and_join_rejected(self):
        with pytest.raises(ValidationError):
            Execute(url="x.md", type="join")

    def test_url_and_route_rejected(self):
        with pytest.raises(ValidationError):
            Execute.model_validate(
                {"url": "x.md", "type": "route", "else": "z"}
            )

    def test_url_mode_rejects_cases(self):
        with pytest.raises(ValidationError):
            Execute(url="x.md", cases=[RouteCase(when="a", goto="b")])

    def test_url_mode_rejects_else(self):
        with pytest.raises(ValidationError):
            Execute.model_validate({"url": "x.md", "else": "fallback"})


class TestQualityGate:
    def test_basic_gate(self):
        gate = Evaluation(validator="gates/v.py", threshold=0.85)
        assert gate.validator == "gates/v.py"
        assert gate.threshold == 0.85
        assert gate.blocking is False
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
        node = Node(id="p1", name="P1", execute=Execute(url="t.md"))
        assert node.effective_max_retries(settings) == 5

    def test_gate_with_override(self):
        settings = Settings(max_retries=5)
        node = Node(
            id="p1",
            name="P1",
            execute=Execute(url="t.md"),
            evaluation=Evaluation(validator="v.md", threshold=0.8, max_retries=2),
        )
        assert node.effective_max_retries(settings) == 2

    def test_gate_without_override_uses_settings(self):
        settings = Settings(max_retries=7)
        node = Node(
            id="p1",
            name="P1",
            execute=Execute(url="t.md"),
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
        node = Node(
            id="p1", name="P1", execute=Execute(url="t.md"), model="opus"
        )
        assert node.model == "opus"

    def test_phase_model_default_none(self):
        node = Node(id="p1", name="P1", execute=Execute(url="t.md"))
        assert node.model is None

    def test_settings_default_model(self):
        settings = Settings(default_model="haiku")
        assert settings.default_model == "haiku"

    def test_settings_default_model_default_value(self):
        settings = Settings()
        assert settings.default_model == "sonnet"


class TestSettingsRemoteUrlGates:
    """Stage 5b: Settings extended for execute.url remote URL gates."""

    def test_defaults_safe(self):
        s = Settings()
        assert s.base_url is None
        assert s.allow_remote_urls is False
        assert s.allow_remote_scripts is False
        assert s.allowed_url_hosts == []
        assert s.url_headers == {}
        assert s.max_remote_fetch_bytes == 5_000_000

    def test_remote_url_gates_configurable(self):
        s = Settings(
            base_url="https://example.com/",
            allow_remote_urls=True,
            allow_remote_scripts=True,
            allowed_url_hosts=["*.example.com"],
            url_headers={"https://api.example.com/": {"X-Auth": "${TOKEN}"}},
            max_remote_fetch_bytes=10_000_000,
        )
        assert s.base_url == "https://example.com/"
        assert s.allow_remote_urls is True
        assert s.allow_remote_scripts is True
        assert s.allowed_url_hosts == ["*.example.com"]
        assert s.url_headers["https://api.example.com/"]["X-Auth"] == "${TOKEN}"
        assert s.max_remote_fetch_bytes == 10_000_000


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
                    {"id": "p1", "name": "P1", "execute": {"url": "t.md"}},
                    {
                        "id": "p2",
                        "name": "P2",
                        "execute": {"url": "t.md"},
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
                    {"id": "p1", "name": "P1", "execute": {"url": "t.md"}},
                    {"id": "p1", "name": "P1 Again", "execute": {"url": "t2.md"}},
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
                        "execute": {"url": "t.md"},
                        "depends_on": ["p1"],
                    }
                ],
            )

    def test_no_dependencies(self):
        node = Node(id="p1", name="P1", execute=Execute(url="t.md"))
        assert node.depends_on == []


class TestRouteValidation:
    """Graph-level route validation (Stage 5a, retained in Stage 5b)."""

    def test_route_unknown_goto_rejected(self):
        with pytest.raises(ValidationError, match="nonexistent"):
            Graph(
                name="T",
                version="1.0.0",
                nodes=[
                    {"id": "a", "name": "A", "execute": {"url": "a.md"}},
                    {
                        "id": "r",
                        "name": "R",
                        "depends_on": ["a"],
                        "execute": {
                            "type": "route",
                            "cases": [{"when": "True", "goto": "ghost"}],
                            "else": "a",
                        },
                    },
                ],
            )

    def test_route_cannot_be_dep_target(self):
        with pytest.raises(ValidationError, match="route"):
            Graph(
                name="T",
                version="1.0.0",
                nodes=[
                    {"id": "a", "name": "A", "execute": {"url": "a.md"}},
                    {
                        "id": "r",
                        "name": "R",
                        "depends_on": ["a"],
                        "execute": {
                            "type": "route",
                            "cases": [],
                            "else": "a",
                        },
                    },
                    {
                        "id": "b",
                        "name": "B",
                        "execute": {"url": "b.md"},
                        "depends_on": ["r"],
                    },
                ],
            )

    def test_route_end_sentinel_accepted(self):
        config = Graph(
            name="T",
            version="1.0.0",
            nodes=[
                {"id": "a", "name": "A", "execute": {"url": "a.md"}},
                {
                    "id": "r",
                    "name": "R",
                    "depends_on": ["a"],
                    "execute": {
                        "type": "route",
                        "cases": [{"when": "True", "goto": "__end__"}],
                        "else": "__end__",
                    },
                },
            ],
        )
        assert config.nodes[1].execute.cases[0].goto == "__end__"


class TestFanOut:
    def test_dynamic_config(self):
        config = FanOut(
            enabled=True,
            manifest_path="manifest.json",
            template={"execute": {"url": "template.md"}},
        )
        assert config.enabled is True
        assert config.manifest_path == "manifest.json"

    def test_template_with_evaluation(self):
        config = FanOut(
            enabled=True,
            manifest_path="m.json",
            template={
                "execute": {"url": "template.md"},
                "evaluation": {"validator": "v.md", "threshold": 0.8},
            },
        )
        assert config.template.evaluation.threshold == 0.8

    def test_final_nodes(self):
        config = FanOut(
            enabled=True,
            manifest_path="m.json",
            template={"execute": {"url": "t.md"}},
            final_nodes=[
                {
                    "id": "summary",
                    "name": "Summary",
                    "execute": {"url": "s.md"},
                }
            ],
        )
        assert len(config.final_nodes) == 1
        assert config.final_nodes[0].id == "summary"
        assert config.final_nodes[0].execute.url == "s.md"

    def test_disabled_by_default(self):
        config = FanOut()
        assert config.enabled is False


class TestFullExampleParse:
    """Parse the absurd-paper kitchen-sink workflow to verify the schema
    handles every Stage 4-5b feature combined: prompts, scripts, fan-out,
    evaluations, subgraph composition (`paper` references
    `subgraphs/compose_and_validate.yaml`)."""

    def test_parse_example_yaml(self, kitchen_sink_workflow_path):
        with open(kitchen_sink_workflow_path) as f:
            raw = yaml.safe_load(f)
        config = Graph(**raw)

        assert config.name == "Absurd Academic Paper"
        assert len(config.nodes) > 0
        assert config.settings.default_model == "sonnet"

    def test_example_has_all_execution_types(self, kitchen_sink_workflow_path):
        """Kitchen-sink YAML exercises prompt, subgraph reference, and fan-out."""
        with open(kitchen_sink_workflow_path) as f:
            raw = yaml.safe_load(f)
        config = Graph(**raw)

        prompt_exts = {".md", ".txt", ".prompt"}
        subgraph_exts = {".yaml", ".yml"}
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
            elif ext in subgraph_exts:
                kinds.add("subgraph")
            elif ext == "":
                kinds.add("binary")
            else:
                kinds.add("script")
        assert "prompt" in kinds
        assert "subgraph" in kinds  # `paper` node references compose_and_validate.yaml

    def test_example_node_shapes(self, kitchen_sink_workflow_path):
        """Spot-check named nodes carry the right execute shape."""
        with open(kitchen_sink_workflow_path) as f:
            raw = yaml.safe_load(f)
        config = Graph(**raw)
        node_map = {p.id: p for p in config.nodes}

        # `abstract` is a prompt node with the default sonnet model.
        abstract = node_map["abstract"]
        assert abstract.execute is not None
        assert Path(abstract.execute.url).suffix == ".md"

        # `paper` references a subgraph (compose_and_validate.yaml) and
        # projects state via execute.params.{inputs,outputs}.
        paper = node_map["paper"]
        assert paper.execute is not None
        assert Path(paper.execute.url).suffix == ".yaml"
        assert "inputs" in paper.execute.params

    def test_example_fan_out(self, kitchen_sink_workflow_path):
        """reviewer_pool fans out per reviewer; template runs a per-child
        subgraph (Stage 5b carve)."""
        with open(kitchen_sink_workflow_path) as f:
            raw = yaml.safe_load(f)
        config = Graph(**raw)
        node_map = {p.id: p for p in config.nodes}

        rp = node_map["reviewer_pool"]
        assert rp.fan_out is not None
        assert rp.fan_out.enabled is True
        # Stage 5b: template's execute.url ends in .yaml → per-child subgraph
        assert Path(rp.fan_out.template.execute.url).suffix == ".yaml"
        assert len(rp.fan_out.final_nodes) > 0


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
