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
