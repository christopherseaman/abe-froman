import shutil
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from sqrlly.schema.models import (
    DimensionCheck,
    Evaluation,
    Execute,
    FanOut,
    Graph,
    LlmPreset,
    Node,
    OutputContract,
    Route,
    RouteElse,
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


class TestExecuteShapeValidator:
    """Execute must set exactly one of: url, type=join."""

    def test_no_modes_set_rejected(self):
        with pytest.raises(ValidationError):
            Execute()

    def test_url_and_join_rejected(self):
        with pytest.raises(ValidationError):
            Execute(url="x.md", type="join")


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
                DimensionCheck(field="correctness", threshold=0.7),
                DimensionCheck(field="style", threshold=0.5),
            ],
        )
        assert len(gate.dimensions) == 2
        assert gate.dimensions[0].field == "correctness"
        assert gate.dimensions[0].threshold == 0.7

    def test_dimension_gate_from_yaml(self):
        # `min` is the back-compat YAML alias for `threshold`.
        raw = {
            "validator": "v.py",
            "dimensions": [
                {"field": "correctness", "min": 0.7},
                {"field": "style", "min": 0.5},
            ],
        }
        gate = Evaluation(**raw)
        assert gate.dimensions[1].field == "style"
        assert gate.dimensions[0].threshold == 0.7

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


class TestPresetReferenceValidation:
    """`Graph._validate_preset_refs` catches typos at validate time
    instead of letting them crash with RuntimeError at first run."""

    def _preset_settings(self):
        return {
            "presets": {
                "default": {
                    "transport": "acp", "provider": "anthropic",
                    "model": "sonnet", "default": True,
                },
            },
        }

    def test_valid_preset_reference_validates(self):
        Graph.model_validate({
            "name": "T", "version": "1.0",
            "settings": self._preset_settings(),
            "nodes": [
                {
                    "id": "a", "name": "A",
                    "execute": {
                        "url": "t.md",
                        "params": {"preset": "default"},
                    },
                },
            ],
        })

    def test_unknown_preset_reference_rejected(self):
        with pytest.raises(ValidationError, match="not declared in settings.presets"):
            Graph.model_validate({
                "name": "T", "version": "1.0",
                "settings": self._preset_settings(),
                "nodes": [
                    {
                        "id": "a", "name": "A",
                        "execute": {
                            "url": "t.md",
                            "params": {"preset": "typo_name"},
                        },
                    },
                ],
            })

    def test_preset_reference_with_empty_presets_rejected(self):
        with pytest.raises(ValidationError, match="settings.presets is empty"):
            Graph.model_validate({
                "name": "T", "version": "1.0",
                "nodes": [
                    {
                        "id": "a", "name": "A",
                        "execute": {
                            "url": "t.md",
                            "params": {"preset": "anything"},
                        },
                    },
                ],
            })

    def test_pure_script_workflow_no_presets_no_refs_validates(self):
        """Script-only workflows don't need presets and don't reference
        any — must validate cleanly."""
        Graph.model_validate({
            "name": "T", "version": "1.0",
            "nodes": [
                {
                    "id": "a", "name": "A",
                    "execute": {
                        "url": "/bin/echo",
                        "params": {"args": ["hi"]},
                    },
                },
            ],
        })


class TestLlmPresetTransport:
    """Schema-level coverage of ``LlmPreset.transport``.

    Both ``acp`` and ``cli`` are valid; ``api`` (and any other value)
    is rejected — the second assertion is a regression guard for the
    0.2.x api-transport strip.
    """

    def test_cli_transport_validates(self):
        p = LlmPreset(
            transport="cli", provider="anthropic",
            model="sonnet", default=True,
        )
        assert p.transport == "cli"
        assert p.provider == "anthropic"

    def test_acp_transport_still_validates(self):
        """Coexistence regression — adding ``cli`` did not break ``acp``."""
        p = LlmPreset(
            transport="acp", provider="anthropic",
            model="sonnet", default=True,
        )
        assert p.transport == "acp"

    def test_api_transport_still_rejected(self):
        """The 0.2.x strip removed ``api``; cli's restoration must not
        leak a regression that re-accepts it."""
        with pytest.raises(ValidationError):
            LlmPreset(
                transport="api", provider="anthropic",
                model="sonnet", default=True,
            )

    def test_unknown_transport_rejected(self):
        with pytest.raises(ValidationError):
            LlmPreset(
                transport="bogus", provider="anthropic",
                model="sonnet", default=True,
            )

    def test_workflow_with_both_acp_and_cli_presets_validates(self):
        """A single workflow may declare both transports side-by-side;
        the schema-level constraint is only that exactly one
        ``LlmPreset`` is ``default: true``.
        """
        graph = Graph.model_validate({
            "name": "T", "version": "1.0",
            "settings": {
                "presets": {
                    "warm": {
                        "transport": "acp", "provider": "anthropic",
                        "model": "sonnet", "default": True,
                    },
                    "cold": {
                        "transport": "cli", "provider": "anthropic",
                        "model": "haiku", "default": False,
                    },
                },
            },
            "nodes": [
                {
                    "id": "a", "name": "A",
                    "execute": {
                        "url": "t.md", "params": {"preset": "cold"},
                    },
                },
            ],
        })
        assert graph.settings.presets["warm"].transport == "acp"
        assert graph.settings.presets["cold"].transport == "cli"


class TestSettingsMemoryGates:
    """Memory back-pressure: percent + absolute-bytes forms with
    suffix parsing for the bytes form."""

    def test_defaults_disabled(self):
        s = Settings()
        assert s.memory_threshold_pct is None
        assert s.memory_min_available_bytes is None

    def test_pct_passthrough(self):
        s = Settings(memory_threshold_pct=80.0)
        assert s.memory_threshold_pct == 80.0

    def test_bytes_int_passthrough(self):
        s = Settings(memory_min_available_bytes=4_294_967_296)
        assert s.memory_min_available_bytes == 4_294_967_296

    @pytest.mark.parametrize(
        "value, expected",
        [
            ("8192", 8192),
            ("8192B", 8192),
            ("4K", 4 * 1024),
            ("4KB", 4 * 1024),
            ("4KiB", 4 * 1024),
            ("500M", 500 * 1024**2),
            ("500MB", 500 * 1024**2),
            ("500MiB", 500 * 1024**2),
            ("4G", 4 * 1024**3),
            ("4GB", 4 * 1024**3),
            ("4GiB", 4 * 1024**3),
            ("2T", 2 * 1024**4),
            ("2TB", 2 * 1024**4),
            ("0.5GB", 512 * 1024**2),  # fractional
            ("  4GB  ", 4 * 1024**3),  # whitespace
            ("4gb", 4 * 1024**3),  # lowercase
        ],
    )
    def test_bytes_string_suffixes(self, value, expected):
        s = Settings(memory_min_available_bytes=value)
        assert s.memory_min_available_bytes == expected

    def test_bytes_unknown_suffix_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="suffix"):
            Settings(memory_min_available_bytes="4XYZ")

    def test_bytes_malformed_rejected(self):
        from pydantic import ValidationError
        with pytest.raises(ValidationError, match="parse byte size"):
            Settings(memory_min_available_bytes="four-gigabytes")


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


class TestInlineRoute:
    """Stage 5c: `Node.route` block (inline forward dispatch).

    Coexists with `execute:` (executes, evaluates, then routes) or
    stands alone (node has no execute body, just a route).
    """

    def test_unconditional_goto_str(self):
        r = Route(goto="b")
        assert r.goto == "b"
        assert r.cases == []
        assert r.else_ is None
        assert r.include_eval is False

    def test_unconditional_goto_list(self):
        r = Route(goto=["b", "c"])
        assert r.goto == ["b", "c"]

    def test_unconditional_with_include_eval(self):
        r = Route(goto="b", include_eval=True)
        assert r.include_eval is True

    def test_cases_with_string_else(self):
        r = Route.model_validate({
            "cases": [{"when": "x>0", "goto": "ship"}],
            "else": "fallback",
        })
        assert r.cases[0].goto == "ship"
        assert r.cases[0].include_eval is False
        # else: string shorthand auto-promoted to RouteElse
        assert isinstance(r.else_, RouteElse)
        assert r.else_.goto == "fallback"
        assert r.else_.include_eval is False

    def test_cases_with_list_else(self):
        r = Route.model_validate({
            "cases": [{"when": "x>0", "goto": ["a", "b"]}],
            "else": ["c", "d"],
        })
        assert r.cases[0].goto == ["a", "b"]
        assert r.else_.goto == ["c", "d"]

    def test_cases_with_structured_else(self):
        r = Route.model_validate({
            "cases": [{"when": "x>0", "goto": "ship"}],
            "else": {"goto": "human_review", "include_eval": True},
        })
        assert r.else_.goto == "human_review"
        assert r.else_.include_eval is True

    def test_per_case_include_eval(self):
        r = Route.model_validate({
            "cases": [
                {"when": "passed('x')", "goto": "ship"},
                {"when": "score('x') < 0.4", "goto": "rewrite", "include_eval": True},
            ],
            "else": "review",
        })
        assert r.cases[0].include_eval is False
        assert r.cases[1].include_eval is True

    def test_goto_and_cases_are_mutex(self):
        with pytest.raises(ValidationError, match="mutually exclusive"):
            Route.model_validate({
                "goto": "a",
                "cases": [{"when": "x", "goto": "b"}],
                "else": "c",
            })

    def test_cases_require_else(self):
        with pytest.raises(ValidationError, match="requires `else"):
            Route.model_validate({
                "cases": [{"when": "x", "goto": "b"}],
            })

    def test_else_without_cases_rejected(self):
        """Bare `else:` without `cases:` is a silent unconditional
        redirect — confusing structure identical to `goto:` shorthand.
        Reject so authors pick the unambiguous form."""
        with pytest.raises(ValidationError, match="requires `cases"):
            Route.model_validate({"else": "fallback"})

    def test_route_must_set_at_least_one_form(self):
        with pytest.raises(ValidationError, match="requires either"):
            Route()

    def test_route_level_include_eval_only_with_goto_shorthand(self):
        # `cases:` form forbids route-level include_eval (per-case is canonical)
        with pytest.raises(ValidationError, match="route level"):
            Route.model_validate({
                "include_eval": True,
                "cases": [{"when": "x", "goto": "b"}],
                "else": "c",
            })

    def test_node_route_field(self):
        config = Graph(
            name="T", version="1.0.0",
            nodes=[
                {"id": "a", "name": "A", "execute": {"url": "a.md"},
                 "route": {"goto": "b"}},
                {"id": "b", "name": "B", "execute": {"url": "b.md"}},
            ],
        )
        assert config.nodes[0].route is not None
        assert config.nodes[0].route.goto == "b"

    def test_node_route_unknown_target_rejected(self):
        with pytest.raises(ValidationError, match="goto 'ghost'"):
            Graph(
                name="T", version="1.0.0",
                nodes=[
                    {"id": "a", "name": "A", "execute": {"url": "a.md"},
                     "route": {"goto": "ghost"}},
                ],
            )

    def test_node_route_else_unknown_target_rejected(self):
        with pytest.raises(ValidationError, match="else goto 'ghost'"):
            Graph(
                name="T", version="1.0.0",
                nodes=[
                    {"id": "a", "name": "A", "execute": {"url": "a.md"},
                     "route": {
                         "cases": [{"when": "x", "goto": "b"}],
                         "else": "ghost",
                     }},
                    {"id": "b", "name": "B", "execute": {"url": "b.md"}},
                ],
            )

    def test_node_route_list_goto_unknown_rejected(self):
        with pytest.raises(ValidationError, match="goto 'ghost'"):
            Graph(
                name="T", version="1.0.0",
                nodes=[
                    {"id": "a", "name": "A", "execute": {"url": "a.md"},
                     "route": {"goto": ["b", "ghost"]}},
                    {"id": "b", "name": "B", "execute": {"url": "b.md"}},
                ],
            )

    def test_inline_route_node_cannot_be_dep(self):
        # Inline-route nodes dispatch via Command; static depends_on to
        # them would double-trigger the goto target.
        with pytest.raises(ValidationError, match="depends on route"):
            Graph(
                name="T", version="1.0.0",
                nodes=[
                    {"id": "a", "name": "A", "execute": {"url": "a.md"},
                     "route": {"goto": "b"}},
                    {"id": "b", "name": "B", "execute": {"url": "b.md"}},
                    {"id": "c", "name": "C", "execute": {"url": "c.md"},
                     "depends_on": ["a"]},
                ],
            )

    def test_standalone_inline_route(self):
        # Node with route but no execute — pure forward-edge dispatcher.
        # Validates target resolution like any other route.
        config = Graph(
            name="T", version="1.0.0",
            nodes=[
                {"id": "produce", "name": "P", "execute": {"url": "p.md"}},
                {"id": "dispatch", "name": "D", "depends_on": ["produce"],
                 "route": {
                     "cases": [{"when": "True", "goto": "ship"}],
                     "else": "__end__",
                 }},
                {"id": "ship", "name": "S", "execute": {"url": "s.md"}},
            ],
        )
        assert config.nodes[1].execute is None
        assert config.nodes[1].route is not None


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


class TestFanOut:
    def test_dynamic_config(self):
        config = FanOut(
            manifest_path="manifest.json",
            template={"execute": {"url": "template.md"}},
        )
        assert config.manifest_path == "manifest.json"
        assert config.template is not None

    def test_template_with_evaluation(self):
        config = FanOut(
            manifest_path="m.json",
            template={
                "execute": {"url": "template.md"},
                "evaluation": {"validator": "v.md", "threshold": 0.8},
            },
        )
        assert config.template.evaluation.threshold == 0.8

    def test_legacy_enabled_field_rejected(self):
        """`enabled` was removed in the post-Stage-5c audit — the
        block's presence is the activation. Authors carrying old YAML
        get a loud Pydantic ValidationError pointing at the unsupported
        field instead of silent fan-out skipping."""
        from pydantic import ValidationError as _VE
        with pytest.raises(_VE, match="enabled"):
            FanOut(enabled=True, template={"execute": {"url": "t.md"}})

    def test_final_nodes(self):
        config = FanOut(
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

    def test_final_node_accepts_output_contract(self):
        """C1: `output_contract` is valid on a fan-out final node (it runs
        through the standard execution path, which enforces it)."""
        config = FanOut(
            template={"execute": {"url": "t.md"}},
            final_nodes=[
                {
                    "id": "summary",
                    "name": "Summary",
                    "execute": {"url": "s.md"},
                    "output_contract": {
                        "base_directory": ".",
                        "required_files": ["summary.md"],
                    },
                }
            ],
        )
        fn = config.final_nodes[0]
        assert fn.output_contract.required_files == ["summary.md"]

    def test_empty_block_is_well_formed(self):
        """A bare `FanOut()` is structurally valid (template is optional
        at the schema level — runtime validates required fields when
        the parent node is wired). Activation is by presence-of-block,
        not by an `enabled` flag."""
        config = FanOut()
        assert config.template is None
        assert config.manifest_path is None
        assert config.final_nodes == []

    def test_fan_out_promote_field(self):
        from sqrlly.schema.models import FanOut, FanOutTemplate, Execute
        fo = FanOut(template=FanOutTemplate(execute=Execute(url="x.md")), promote=True)
        assert fo.promote is True
        # default is False
        assert FanOut(template=FanOutTemplate(execute=Execute(url="x.md"))).promote is False


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
        # Migrated to presets; default preset's model is sonnet. The
        # registry now mixes LlmPresets with a CommandPreset (PDF render),
        # and CommandPresets carry no `default` flag — hence getattr.
        defaults = [
            p for p in config.settings.presets.values()
            if getattr(p, "default", False)
        ]
        assert len(defaults) == 1
        assert defaults[0].model == "sonnet"

    def test_example_has_all_execution_types(self, kitchen_sink_workflow_path):
        """Kitchen-sink YAML exercises prompt, subgraph reference, and script nodes."""
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


class TestOnPromoteConflict:
    def test_default_is_warn(self):
        from sqrlly.schema.models import Settings
        assert Settings().on_promote_conflict == "warn"

    def test_accepts_all_four_modes(self):
        from sqrlly.schema.models import Settings
        for mode in ("fail", "warn", "overwrite", "skip"):
            assert Settings(on_promote_conflict=mode).on_promote_conflict == mode

    def test_rejects_unknown_mode(self):
        import pytest
        from pydantic import ValidationError
        from sqrlly.schema.models import Settings
        with pytest.raises(ValidationError):
            Settings(on_promote_conflict="merge")


class TestPromoteExclude:
    def test_defaults_empty(self):
        from sqrlly.schema.models import Settings
        assert Settings().promote_exclude == []

    def test_accepts_list(self):
        from sqrlly.schema.models import Settings
        assert Settings(promote_exclude=["node_modules", ".next/cache"]).promote_exclude \
            == ["node_modules", ".next/cache"]


def test_settings_promote_include_field():
    from sqrlly.schema.models import Settings
    s = Settings(promote_include=["log/phases/*"])
    assert s.promote_include == ["log/phases/*"]
    assert Settings().promote_include == []   # default empty


class TestWorktreeSetupFields:
    def test_defaults(self):
        from sqrlly.schema.models import Settings
        s = Settings()
        assert s.worktree_setup == []
        assert s.worktree_setup_exclude == []
        assert s.worktree_setup_store_dir is None

    def test_accepts_values(self):
        from sqrlly.schema.models import Settings
        s = Settings(
            worktree_setup=["pnpm install --prefer-offline"],
            worktree_setup_exclude=["node_modules", "src/generated/prisma"],
            worktree_setup_store_dir=".sqrlly/.pnpm-store",
        )
        assert s.worktree_setup == ["pnpm install --prefer-offline"]
        assert s.worktree_setup_exclude == ["node_modules", "src/generated/prisma"]
        assert s.worktree_setup_store_dir == ".sqrlly/.pnpm-store"
