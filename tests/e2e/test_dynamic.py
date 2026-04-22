"""Dynamic subphase fan-out tests.

Tests LangGraph Send-based fan-out for phases with dynamic_subphases enabled.
All tests use real subprocess execution via DispatchExecutor.
"""

import json

import pytest

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.runtime.state import make_initial_state
from abe_froman.runtime.executor.dispatch import DispatchExecutor

from helpers import cmd_phase, make_config


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def dynamic_parent(id, manifest_items, *, template_prompt="template.md",
                   depends_on=None, quality_gate=None, final_phases=None,
                   **kwargs):
    """Shorthand for a command phase that echoes a manifest JSON."""
    manifest = json.dumps({"items": manifest_items})
    phase = {
        "id": id,
        "name": id,
        "execution": {"type": "command", "command": "echo", "args": ["-n", manifest]},
        "dynamic_subphases": {
            "enabled": True,
            "template": {"prompt_file": template_prompt},
        },
        "depends_on": depends_on or [],
        **kwargs,
    }
    if quality_gate:
        phase["quality_gate"] = quality_gate
    if final_phases:
        phase["dynamic_subphases"]["final_phases"] = final_phases
    return phase


# ---------------------------------------------------------------------------
# Core fan-out
# ---------------------------------------------------------------------------


class TestDynamicFanOut:
    @pytest.mark.asyncio
    async def test_basic_fan_out(self, tmp_path):
        """Parent echoes manifest -> 3 subphases execute."""
        (tmp_path / "template.md").write_text("Process {{id}}")

        items = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        config = make_config([dynamic_parent("parent", items)])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "parent" in result["completed_phases"]
        assert "parent::a" in result["completed_phases"]
        assert "parent::b" in result["completed_phases"]
        assert "parent::c" in result["completed_phases"]

    @pytest.mark.asyncio
    async def test_subphase_outputs_recorded(self, tmp_path):
        """Subphase outputs stored in both phase_outputs and subphase_outputs."""
        (tmp_path / "template.md").write_text("Process {{id}}")

        items = [{"id": "x"}, {"id": "y"}]
        config = make_config([dynamic_parent("p", items)])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p::x" in result["phase_outputs"]
        assert "p::y" in result["phase_outputs"]
        assert "p::x" in result["subphase_outputs"]
        assert "p::y" in result["subphase_outputs"]

    @pytest.mark.asyncio
    async def test_single_item_manifest(self, tmp_path):
        """Fan-out with a single item still works."""
        (tmp_path / "template.md").write_text("Solo {{id}}")

        config = make_config([dynamic_parent("p", [{"id": "only"}])])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p::only" in result["completed_phases"]

    @pytest.mark.asyncio
    async def test_template_interpolation_in_subphases(self, tmp_path):
        """Each subphase renders the template with its own manifest item.

        Must wire StubBackend explicitly — without a prompt_backend the
        DispatchExecutor returns a literal `[prompt-stub] {id}: {file}`
        placeholder that never touches PromptExecutor, bypassing template
        rendering entirely.

        StubBackend echoes `prompt_length=N`. Items with different-length
        IDs produce different rendered-prompt lengths — if `{{id}}` were
        never substituted (literal `{{id}}` left in place), both subphases
        would report the same length and this assertion would fail.
        Indirect but sufficient evidence of interpolation.
        """
        from abe_froman.runtime.executor.backends.stub import StubBackend

        template = "Process {{id}}"
        (tmp_path / "template.md").write_text(template)

        items = [{"id": "a"}, {"id": "longer-id"}]
        config = make_config([dynamic_parent("parent", items)])
        executor = DispatchExecutor(
            workdir=str(tmp_path), prompt_backend=StubBackend(),
        )
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        expected_a = len("Process a")
        expected_long = len("Process longer-id")
        assert f"prompt_length={expected_a}" in result["subphase_outputs"]["parent::a"], (
            f"expected rendered 'Process a' ({expected_a} chars); got "
            f"{result['subphase_outputs']['parent::a']!r}"
        )
        assert f"prompt_length={expected_long}" in result["subphase_outputs"]["parent::longer-id"], (
            f"expected rendered 'Process longer-id' ({expected_long} chars); got "
            f"{result['subphase_outputs']['parent::longer-id']!r}"
        )
        assert expected_a != expected_long  # sanity: lengths actually differ


# ---------------------------------------------------------------------------
# Final phases
# ---------------------------------------------------------------------------


class TestFinalPhases:
    @pytest.mark.asyncio
    async def test_final_phase_runs_after_subphases(self, tmp_path):
        """Final phase executes after all subphases complete."""
        (tmp_path / "template.md").write_text("Sub {{id}}")

        items = [{"id": "a"}, {"id": "b"}]
        finals = [{"id": "summary", "name": "Summary",
                   "execution": {"type": "command", "command": "echo",
                                 "args": ["-n", "summarized"]}}]

        config = make_config([dynamic_parent("p", items, final_phases=finals)])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p::a" in result["completed_phases"]
        assert "p::b" in result["completed_phases"]
        assert "_final_p_summary" in result["completed_phases"]

    @pytest.mark.asyncio
    async def test_chained_final_phases(self, tmp_path):
        """Multiple final phases execute sequentially."""
        (tmp_path / "template.md").write_text("Sub {{id}}")

        items = [{"id": "a"}]
        finals = [
            {"id": "step1", "name": "Step 1",
             "execution": {"type": "command", "command": "echo",
                           "args": ["-n", "s1"]}},
            {"id": "step2", "name": "Step 2",
             "execution": {"type": "command", "command": "echo",
                           "args": ["-n", "s2"]}},
        ]

        config = make_config([dynamic_parent("p", items, final_phases=finals)])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "_final_p_step1" in result["completed_phases"]
        assert "_final_p_step2" in result["completed_phases"]


# ---------------------------------------------------------------------------
# Downstream wiring
# ---------------------------------------------------------------------------


class TestDownstreamWiring:
    @pytest.mark.asyncio
    async def test_downstream_waits_for_dynamic_parent(self, tmp_path):
        """Phase depending on dynamic parent runs after finals complete."""
        (tmp_path / "template.md").write_text("Sub {{id}}")

        items = [{"id": "a"}, {"id": "b"}]
        finals = [{"id": "wrap", "name": "Wrap",
                   "execution": {"type": "command", "command": "echo",
                                 "args": ["-n", "wrapped"]}}]

        config = make_config([
            dynamic_parent("dyn", items, final_phases=finals),
            cmd_phase("next", depends_on=["dyn"]),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "dyn::a" in result["completed_phases"]
        assert "dyn::b" in result["completed_phases"]
        assert "_final_dyn_wrap" in result["completed_phases"]
        assert "next" in result["completed_phases"]

    @pytest.mark.asyncio
    async def test_downstream_without_finals(self, tmp_path):
        """Downstream wires from template node when no final phases."""
        (tmp_path / "template.md").write_text("Sub {{id}}")

        items = [{"id": "a"}]
        config = make_config([
            dynamic_parent("dyn", items),
            cmd_phase("next", depends_on=["dyn"]),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "dyn::a" in result["completed_phases"]
        assert "next" in result["completed_phases"]


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


class TestDynamicGates:
    @pytest.mark.asyncio
    async def test_parent_gate_pass_fans_out(self, tmp_path):
        """Parent gate passes -> subphases execute."""
        (tmp_path / "template.md").write_text("Sub {{id}}")
        script = tmp_path / "pass.py"
        script.write_text("print(1.0)")

        items = [{"id": "a"}, {"id": "b"}]
        config = make_config([
            dynamic_parent("p", items,
                           quality_gate={"validator": str(script),
                                         "threshold": 0.8}),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert result["gate_scores"]["p"] == 1.0
        assert "p::a" in result["completed_phases"]
        assert "p::b" in result["completed_phases"]

    @pytest.mark.asyncio
    async def test_parent_gate_fail_blocks_fanout(self, tmp_path):
        """Parent blocking gate fails -> no subphases run."""
        (tmp_path / "template.md").write_text("Sub {{id}}")
        script = tmp_path / "fail.py"
        script.write_text("print(0.1)")

        items = [{"id": "a"}]
        config = make_config([
            dynamic_parent("p", items,
                           quality_gate={"validator": str(script),
                                         "threshold": 0.8, "blocking": True,
                                         "max_retries": 0}),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p" in result["failed_phases"]
        assert "p::a" not in result["completed_phases"]

    @pytest.mark.asyncio
    async def test_template_gate_scores_recorded(self, tmp_path):
        """Template quality gate scores recorded per subphase."""
        (tmp_path / "template.md").write_text("Sub {{id}}")
        script = tmp_path / "score.py"
        script.write_text("print(0.9)")

        items = [{"id": "x"}, {"id": "y"}]
        config = make_config([{
            "id": "p",
            "name": "p",
            "execution": {"type": "command", "command": "echo",
                          "args": ["-n", json.dumps({"items": items})]},
            "dynamic_subphases": {
                "enabled": True,
                "template": {
                    "prompt_file": "template.md",
                    "quality_gate": {
                        "validator": str(script),
                        "threshold": 0.5,
                    },
                },
            },
        }])

        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert result["gate_scores"]["p::x"] == 0.9
        assert result["gate_scores"]["p::y"] == 0.9


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestDynamicEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_manifest_skips_to_end(self, tmp_path):
        """Empty manifest -> no subphases, goes to END or finals."""
        (tmp_path / "template.md").write_text("Sub {{id}}")

        config = make_config([dynamic_parent("p", [])])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p" in result["completed_phases"]
        # No subphases should have run
        sub_keys = [k for k in result.get("completed_phases", [])
                    if k.startswith("p::")]
        assert sub_keys == []

    @pytest.mark.asyncio
    async def test_dry_run_traces_subphases(self, tmp_path):
        """Dry run traces parent but doesn't fan out (no manifest to read)."""
        (tmp_path / "template.md").write_text("Sub {{id}}")

        items = [{"id": "a"}]
        config = make_config([dynamic_parent("p", items)])
        graph = build_workflow_graph(config)
        result = await graph.ainvoke(
            make_initial_state(workdir=str(tmp_path), dry_run=True)
        )

        assert "p" in result["completed_phases"]

    @pytest.mark.asyncio
    async def test_disabled_dynamic_builds_normally(self, tmp_path):
        """dynamic_subphases.enabled=false -> builds like a normal phase."""
        manifest = json.dumps({"items": [{"id": "a"}]})
        config = make_config([{
            "id": "p",
            "name": "P",
            "execution": {"type": "command", "command": "echo",
                          "args": ["-n", manifest]},
            "dynamic_subphases": {
                "enabled": False,
                "template": {"prompt_file": "t.md"},
            },
        }])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p" in result["completed_phases"]
        assert "p::a" not in result["completed_phases"]

    @pytest.mark.asyncio
    async def test_manifest_from_disk(self, tmp_path):
        """Manifest read from disk when phase output isn't JSON."""
        (tmp_path / "template.md").write_text("Sub {{id}}")
        (tmp_path / "manifest.json").write_text(
            json.dumps({"items": [{"id": "disk-item"}]})
        )

        config = make_config([{
            "id": "p",
            "name": "P",
            "execution": {"type": "command", "command": "echo",
                          "args": ["-n", "not json"]},
            "dynamic_subphases": {
                "enabled": True,
                "manifest_path": "manifest.json",
                "template": {"prompt_file": "template.md"},
            },
        }])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p::disk-item" in result["completed_phases"]


# ---------------------------------------------------------------------------
# Manifest field propagation (uses MockExecutor to observe context)
# ---------------------------------------------------------------------------


class TestManifestFieldPropagation:
    @pytest.mark.asyncio
    async def test_custom_fields_reach_subphase_context(self, tmp_path):
        """Manifest item fields beyond 'id' are passed into subphase context."""
        from mock_executor import MockExecutor
        from abe_froman.runtime.result import ExecutionResult

        manifest = [
            {"id": "x", "custom_field": "v123", "priority": "high"},
        ]
        mock = MockExecutor(results={
            "parent": ExecutionResult(
                success=True,
                output=json.dumps({"items": manifest}),
            ),
        })

        (tmp_path / "template.md").write_text("Process {{custom_field}}")

        config = make_config([dynamic_parent("parent", manifest)])
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "parent::x" in result["completed_phases"]
        ctx = mock.received_contexts["parent::x"]
        assert ctx["id"] == "x"
        assert ctx["custom_field"] == "v123"
        assert ctx["priority"] == "high"

    @pytest.mark.asyncio
    async def test_downstream_sees_subphase_aggregate(self, tmp_path):
        """Any downstream phase depending on a dynamic parent sees aggregates.

        Before Stage 2b, `{parent}_subphases` was synthesized only inside
        `_make_final_phase_node`'s local enriched dict — unreachable from
        a non-final downstream phase. Stage 2b moves the synthesis into
        `build_context`, which reads state directly, so both final and
        non-final downstream phases see the same aggregate.
        """
        from mock_executor import MockExecutor
        from abe_froman.runtime.result import ExecutionResult

        manifest = [{"id": "a"}, {"id": "b"}]
        mock = MockExecutor(results={
            "parent": ExecutionResult(
                success=True,
                output=json.dumps({"items": manifest}),
            ),
            "parent::a": ExecutionResult(success=True, output="out-a"),
            "parent::b": ExecutionResult(success=True, output="out-b"),
        })

        (tmp_path / "template.md").write_text("sub")

        phases = [
            dynamic_parent("parent", manifest),
            cmd_phase("downstream", depends_on=["parent"]),
        ]
        config = make_config(phases)
        # Replace the command executor for downstream with the mock so we
        # can inspect its context. Use the mock for everything: it returns
        # mock results for keys it knows, defaults otherwise.
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "downstream" in result["completed_phases"]
        ctx = mock.received_contexts["downstream"]
        assert "parent_subphases" in ctx, (
            f"downstream should see `parent_subphases`; got keys {list(ctx)}"
        )
        aggregate = json.loads(ctx["parent_subphases"])
        assert aggregate == {"parent::a": "out-a", "parent::b": "out-b"}

    @pytest.mark.asyncio
    async def test_subphase_context_inherits_parent_deps(self, tmp_path):
        """Subphase template sees its parent's upstream deps, not just parent output.

        Topology: upstream -> parent (dynamic fan-out) -> subphase
        The subphase template should be able to interpolate {{upstream}}
        because upstream is in parent.depends_on. Before Stage 2a, subphase
        context contained only {parent_id: output, ...item_fields} — any
        template that referenced a grandparent dep would render empty.
        """
        from mock_executor import MockExecutor
        from abe_froman.runtime.result import ExecutionResult

        manifest = [{"id": "item1"}]
        mock = MockExecutor(results={
            "upstream": ExecutionResult(
                success=True,
                output="upstream-value-42",
            ),
            "parent": ExecutionResult(
                success=True,
                output=json.dumps({"items": manifest}),
            ),
        })

        (tmp_path / "template.md").write_text("template")

        phases = [
            cmd_phase("upstream"),
            dynamic_parent("parent", manifest, depends_on=["upstream"]),
        ]
        config = make_config(phases)
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "parent::item1" in result["completed_phases"]
        ctx = mock.received_contexts["parent::item1"]
        assert ctx.get("upstream") == "upstream-value-42", (
            f"subphase context should inherit parent's upstream dep; got {ctx!r}"
        )
        assert ctx.get("parent") == json.dumps({"items": manifest}), (
            "parent output still present alongside upstream"
        )
