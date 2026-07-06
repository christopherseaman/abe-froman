"""Dynamic child fan-out tests.

Tests LangGraph Send-based fan-out for nodes with fan_out enabled.
All tests use real subprocess execution via DispatchExecutor.
"""

import json
import shutil

import pytest

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.state import make_initial_state
from sqrlly.runtime.executor.dispatch import DispatchExecutor

from helpers import cmd_phase, make_config

_ECHO = shutil.which("echo") or "/bin/echo"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def dynamic_parent(id, manifest_items, *, template_execute=None,
                   depends_on=None, evaluation=None, final_nodes=None,
                   **kwargs):
    """Shorthand for a command node that echoes a manifest JSON.

    Default ``template_execute`` runs ``_ECHO`` with a Jinja-rendered
    arg per child — exercises fan-out structure without requiring a
    PromptBackend. Tests that need a different template execution
    shape (e.g. real prompt template, gate template) override.
    """
    manifest = json.dumps({"items": manifest_items})
    if template_execute is None:
        template_execute = {
            "url": _ECHO,
            "params": {"args": ["-n", "child-{{id}}"]},
        }
    node = {
        "id": id,
        "name": id,
        "execute": {"url": _ECHO, "params": {"args": ["-n", manifest]}},
        "fan_out": {
            "template": {"execute": template_execute},
        },
        "depends_on": depends_on or [],
        **kwargs,
    }
    if evaluation:
        node["evaluation"] = evaluation
    if final_nodes:
        node["fan_out"]["final_nodes"] = final_nodes
    return node


# ---------------------------------------------------------------------------
# Core fan-out
# ---------------------------------------------------------------------------


class TestDynamicFanOut:
    @pytest.mark.asyncio
    async def test_basic_fan_out(self, tmp_path):
        """Parent echoes manifest -> 3 children execute."""
        (tmp_path / "template.md").write_text("Process {{id}}")

        items = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        config = make_config([dynamic_parent("parent", items)])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "parent" in result["completed_nodes"]
        assert "parent::a" in result["completed_nodes"]
        assert "parent::b" in result["completed_nodes"]
        assert "parent::c" in result["completed_nodes"]

    @pytest.mark.asyncio
    async def test_branch_outputs_recorded(self, tmp_path):
        """Branch outputs stored in both node_outputs and child_outputs."""
        (tmp_path / "template.md").write_text("Process {{id}}")

        items = [{"id": "x"}, {"id": "y"}]
        config = make_config([dynamic_parent("p", items)])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p::x" in result["node_outputs"]
        assert "p::y" in result["node_outputs"]
        assert "p::x" in result["child_outputs"]
        assert "p::y" in result["child_outputs"]

    @pytest.mark.asyncio
    async def test_single_item_manifest(self, tmp_path):
        """Fan-out with a single item still works."""
        (tmp_path / "template.md").write_text("Solo {{id}}")

        config = make_config([dynamic_parent("p", [{"id": "only"}])])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p::only" in result["completed_nodes"]

# ---------------------------------------------------------------------------
# Final nodes
# ---------------------------------------------------------------------------


class TestFinalNodes:
    @pytest.mark.asyncio
    async def test_final_node_runs_after_branches(self, tmp_path):
        """Final node executes after all children complete."""
        (tmp_path / "template.md").write_text("Sub {{id}}")

        items = [{"id": "a"}, {"id": "b"}]
        finals = [{"id": "summary", "name": "Summary",
                   "execute": {"url": _ECHO, "params": {"args": ["-n", "summarized"]}}}]

        config = make_config([dynamic_parent("p", items, final_nodes=finals)])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p::a" in result["completed_nodes"]
        assert "p::b" in result["completed_nodes"]
        assert "_final_p_summary" in result["completed_nodes"]

    @pytest.mark.asyncio
    async def test_final_node_sees_all_branch_outputs(self, tmp_path):
        """Regression: final node must see ALL children's outputs in
        `{parent_branches}`, not fire prematurely on first Send branch.

        Bug: with 3 manifest items and a final whose prompt references
        `{{parent_branches}}`, the final was being dispatched as soon as
        the first Send branch completed (because the static edge
        `_sub_parent → _final_first` fires per-branch). The fix adds a
        barrier in `_make_final_fan_out_node(..., is_first=True)` that
        returns no-op until all manifest items appear in completed_nodes.
        """
        from mock_executor import MockExecutor
        from sqrlly.runtime.result import ExecutionResult

        manifest = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        mock = MockExecutor(results={
            "parent": ExecutionResult(
                success=True, output=json.dumps({"items": manifest}),
            ),
            "parent::a": ExecutionResult(success=True, output="out-a"),
            "parent::b": ExecutionResult(success=True, output="out-b"),
            "parent::c": ExecutionResult(success=True, output="out-c"),
            "_final_parent_summary": ExecutionResult(success=True, output="summary"),
        })

        (tmp_path / "template.md").write_text("sub {{id}}")
        (tmp_path / "summary.md").write_text("aggregate {{parent_branches}}")

        finals = [{"id": "summary", "name": "Summary", "execute": {"url": "summary.md"}}]
        config = make_config([dynamic_parent("parent", manifest, final_nodes=finals)])

        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        # All children + final completed.
        for child_id in ("parent::a", "parent::b", "parent::c"):
            assert child_id in result["completed_nodes"], (
                f"missing child {child_id}; got {result['completed_nodes']}"
            )
        assert "_final_parent_summary" in result["completed_nodes"]

        # Final node's context must include ALL three children's outputs.
        ctx = mock.received_contexts["_final_parent_summary"]
        assert "parent_branches" in ctx
        aggregate = json.loads(ctx["parent_branches"])
        assert aggregate == {
            "parent::a": "out-a",
            "parent::b": "out-b",
            "parent::c": "out-c",
        }, f"final ran with incomplete aggregate: {aggregate}"

    @pytest.mark.asyncio
    async def test_first_final_barrier_when_no_manifest(self, tmp_path):
        """Direct unit-style call: barrier returns no-op when parent
        hasn't produced manifest yet (state.node_outputs[parent] empty)
        AND parent isn't in completed_nodes.

        Bug observed in absurd-paper: `_final_*` fires before parent's
        prompt produces a manifest. Without the parent-settled check,
        barrier reads empty items, falls through to inner, and the final
        runs against an empty `{{parent_branches}}` template var.
        """
        from sqrlly.compile.dynamic import _make_final_fan_out_node
        from sqrlly.runtime.state import make_initial_state
        from sqrlly.schema.models import Execute, Node, FanOut

        parent = Node(
            id="p", name="P",
            execute=Execute(url=_ECHO, params={"args": ["x"]}),
            fan_out=FanOut(manifest_path=None,
                           template={"execute": {"url": "t.md"}}),
        )
        final = type("F", (), dict(
            id="summary", name="Summary",
            description=None,
            execute=Execute(url="s.md"), evaluation=None,
            output_contract=None,
        ))()

        from helpers import make_config
        config = make_config([{"id": "p", "name": "P",
                               "execute": {"url": _ECHO, "params": {"args": ["x"]}}}])

        node_fn = _make_final_fan_out_node(parent, final, config, executor=None, is_first=True)
        # Parent not completed AND no manifest output → barrier should defer.
        state = make_initial_state(workdir=str(tmp_path))
        result = await node_fn(state)
        assert result == {}, (
            f"barrier should defer when parent hasn't settled; got {result}"
        )

    @pytest.mark.asyncio
    async def test_chained_final_nodes(self, tmp_path):
        """Multiple final nodes execute sequentially."""
        (tmp_path / "template.md").write_text("Sub {{id}}")

        items = [{"id": "a"}]
        finals = [
            {"id": "step1", "name": "Step 1",
             "execute": {"url": _ECHO, "params": {"args": ["-n", "s1"]}}},
            {"id": "step2", "name": "Step 2",
             "execute": {"url": _ECHO, "params": {"args": ["-n", "s2"]}}},
        ]

        config = make_config([dynamic_parent("p", items, final_nodes=finals)])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "_final_p_step1" in result["completed_nodes"]
        assert "_final_p_step2" in result["completed_nodes"]


# ---------------------------------------------------------------------------
# Downstream wiring
# ---------------------------------------------------------------------------


class TestDownstreamWiring:
    @pytest.mark.asyncio
    async def test_downstream_waits_for_dynamic_parent(self, tmp_path):
        """Node depending on dynamic parent runs after finals complete."""
        (tmp_path / "template.md").write_text("Sub {{id}}")

        items = [{"id": "a"}, {"id": "b"}]
        finals = [{"id": "wrap", "name": "Wrap",
                   "execute": {"url": _ECHO, "params": {"args": ["-n", "wrapped"]}}}]

        config = make_config([
            dynamic_parent("dyn", items, final_nodes=finals),
            cmd_phase("next", depends_on=["dyn"]),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "dyn::a" in result["completed_nodes"]
        assert "dyn::b" in result["completed_nodes"]
        assert "_final_dyn_wrap" in result["completed_nodes"]
        assert "next" in result["completed_nodes"]

    @pytest.mark.asyncio
    async def test_downstream_without_finals(self, tmp_path):
        """Downstream wires from template node when no final nodes."""
        (tmp_path / "template.md").write_text("Sub {{id}}")

        items = [{"id": "a"}]
        config = make_config([
            dynamic_parent("dyn", items),
            cmd_phase("next", depends_on=["dyn"]),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "dyn::a" in result["completed_nodes"]
        assert "next" in result["completed_nodes"]


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


class TestDynamicGates:
    @pytest.mark.asyncio
    async def test_parent_gate_pass_fans_out(self, tmp_path):
        """Parent gate passes -> children execute."""
        (tmp_path / "template.md").write_text("Sub {{id}}")
        script = tmp_path / "pass.py"
        script.write_text("print(1.0)")

        items = [{"id": "a"}, {"id": "b"}]
        config = make_config([
            dynamic_parent("p", items,
                           evaluation={"validator": str(script),
                                         "threshold": 0.8}),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert result["evaluations"]["p"][-1]["result"]["score"] == 1.0
        assert "p::a" in result["completed_nodes"]
        assert "p::b" in result["completed_nodes"]

    @pytest.mark.asyncio
    async def test_parent_gate_pass_children_see_committed_state(self, tmp_path):
        """A gated fan-out parent that PASSES dispatches its Sends from the
        _fan_<id> node one super-step AFTER the gate committed, so each child's
        Send payload carries the parent's post-gate evaluations. Each child's
        template echoes {{evals.p.score}} — a value present ONLY once the gate's
        record is committed — so the rendered child output pins committed-state
        visibility end-to-end: a stale (pre-gate) payload would render an empty
        score. This is the timing the unit Send-payload pin can't prove (that
        one seeds state directly)."""
        script = tmp_path / "pass.py"
        script.write_text("print(1.0)")
        items = [{"id": "a"}, {"id": "b"}]
        config = make_config([
            dynamic_parent(
                "p", items,
                template_execute={
                    "url": _ECHO,
                    "params": {"args": ["-n", "score={{evals.p.score}}"]},
                },
                evaluation={"validator": str(script), "threshold": 0.8},
            ),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        # Parent gate committed before dispatch: record present, parent done.
        assert result["evaluations"]["p"][-1]["result"]["score"] == 1.0
        assert "p" in result["completed_nodes"]
        assert "p::a" in result["completed_nodes"]
        assert "p::b" in result["completed_nodes"]
        # Each child rendered the parent's committed eval score in its payload
        # — proving the Send saw post-gate evaluations, not a stale snapshot.
        assert result["node_outputs"]["p::a"] == "score=1.0"
        assert result["node_outputs"]["p::b"] == "score=1.0"

    @pytest.mark.asyncio
    async def test_parent_gate_fail_blocks_fanout(self, tmp_path):
        """Parent blocking gate fails -> no children run."""
        (tmp_path / "template.md").write_text("Sub {{id}}")
        script = tmp_path / "fail.py"
        script.write_text("print(0.1)")

        items = [{"id": "a"}]
        config = make_config([
            dynamic_parent("p", items,
                           evaluation={"validator": str(script),
                                         "threshold": 0.8, "blocking": True,
                                         "max_retries": 0}),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p" in result["failed_nodes"]
        assert "p::a" not in result["completed_nodes"]

    @pytest.mark.asyncio
    async def test_template_gate_scores_recorded(self, tmp_path):
        """Template quality gate scores recorded per child."""
        script = tmp_path / "score.py"
        script.write_text("print(0.9)")

        items = [{"id": "x"}, {"id": "y"}]
        config = make_config([{
            "id": "p",
            "name": "p",
            "execute": {"url": _ECHO, "params": {"args": ["-n", json.dumps({"items": items})]}},
            "fan_out": {
                "template": {
                    "execute": {
                        "url": _ECHO,
                        "params": {"args": ["-n", "child-{{id}}"]},
                    },
                    "evaluation": {
                        "validator": str(script),
                        "threshold": 0.5,
                    },
                },
            },
        }])

        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert result["evaluations"]["p::x"][-1]["result"]["score"] == 0.9
        assert result["evaluations"]["p::y"][-1]["result"]["score"] == 0.9
        # Stage 3b: child gates also flow through EvaluationRecord writes.
        assert len(result["evaluations"]["p::x"]) >= 1
        assert len(result["evaluations"]["p::y"]) >= 1

    @pytest.mark.asyncio
    async def test_branch_gate_triggers_retry(self, tmp_path):
        """Branch template gate with max_retries=2: validator fails
        first call, passes on retry. Proves each child branch keeps
        its own invocation counter via `_fan_out_item`-keyed state.
        """
        # Per-item counter files so each item retries independently.
        (tmp_path / "cnt-x.txt").write_text("0")
        (tmp_path / "cnt-y.txt").write_text("0")
        validator = tmp_path / "validator.py"
        validator.write_text(
            'import os, sys, re\n'
            'output = sys.stdin.read()\n'
            '# Derive the item id from the node output (echo emits "p::ID").\n'
            'm = re.search(r"p::([a-z])", output)\n'
            'item = m.group(1) if m else "x"\n'
            f'path = os.path.join({str(tmp_path)!r}, f"cnt-{{item}}.txt")\n'
            'n = int(open(path).read())\n'
            'open(path, "w").write(str(n+1))\n'
            'print(0.9 if n >= 1 else 0.3)\n'
        )

        items = [{"id": "x"}, {"id": "y"}]
        config = make_config([{
            "id": "p",
            "name": "p",
            "execute": {"url": _ECHO, "params": {"args": ["-n", json.dumps({"items": items})]}},
            "fan_out": {
                "template": {
                    "execute": {
                        "url": _ECHO,
                        "params": {"args": ["-n", "p::{{id}}"]},
                    },
                    "evaluation": {
                        "validator": str(validator),
                        "threshold": 0.5,
                        "blocking": True,
                        "max_retries": 2,
                    },
                },
            },
        }])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        # Each child should have TWO evaluation records (fail then pass).
        assert "p::x" in result["completed_nodes"]
        assert "p::y" in result["completed_nodes"]
        invs_x = [r["invocation"] for r in result["evaluations"]["p::x"]]
        invs_y = [r["invocation"] for r in result["evaluations"]["p::y"]]
        assert invs_x == [0, 1], f"expected invocations [0, 1], got {invs_x}"
        assert invs_y == [0, 1], f"expected invocations [0, 1], got {invs_y}"
        # Final record is the pass.
        assert result["evaluations"]["p::x"][-1]["result"]["score"] == 0.9
        assert result["evaluations"]["p::y"][-1]["result"]["score"] == 0.9


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestDynamicEdgeCases:
    @pytest.mark.asyncio
    async def test_empty_manifest_skips_to_end(self, tmp_path):
        """Empty manifest -> no children, goes to END or finals."""
        (tmp_path / "template.md").write_text("Sub {{id}}")

        config = make_config([dynamic_parent("p", [])])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p" in result["completed_nodes"]
        # No children should have run
        sub_keys = [k for k in result.get("completed_nodes", [])
                    if k.startswith("p::")]
        assert sub_keys == []

    @pytest.mark.asyncio
    async def test_empty_manifest_fans_to_all_dependents(self, tmp_path):
        """C1 regression: an empty manifest on a fan-out node with no
        final_nodes must route to *every* dependent, not just the first.
        The pre-fix router returned an abstract "no_items" key that a
        route_map could only map to a single node."""
        config = make_config([
            dynamic_parent("p", []),
            cmd_phase("d1", output="d1-ran", depends_on=["p"]),
            cmd_phase("d2", output="d2-ran", depends_on=["p"]),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p" in result["completed_nodes"]
        assert "d1" in result["completed_nodes"]
        assert "d2" in result["completed_nodes"]
        assert result["node_outputs"]["d1"] == "d1-ran"
        assert result["node_outputs"]["d2"] == "d2-ran"

    @pytest.mark.asyncio
    async def test_dry_run_traces_branches(self, tmp_path):
        """Dry run traces parent but doesn't fan out (no manifest to read)."""
        (tmp_path / "template.md").write_text("Sub {{id}}")

        items = [{"id": "a"}]
        config = make_config([dynamic_parent("p", items)])
        graph = build_workflow_graph(config)
        result = await graph.ainvoke(
            make_initial_state(workdir=str(tmp_path), dry_run=True)
        )

        assert "p" in result["completed_nodes"]

    def test_legacy_enabled_false_rejected_at_validate(self, tmp_path):
        """`fan_out: { enabled: false, template: ... }` was historically
        a silent no-op — confusing author footgun. Post-Stage-5c audit
        removed the field entirely; legacy YAML carrying `enabled` now
        fails at `validate` time."""
        from pydantic import ValidationError
        manifest = json.dumps({"items": [{"id": "a"}]})
        with pytest.raises(ValidationError, match="enabled"):
            make_config([{
                "id": "p",
                "name": "P",
                "execute": {
                    "url": _ECHO, "params": {"args": ["-n", manifest]},
                },
                "fan_out": {
                    "enabled": False,
                    "template": {"execute": {"url": "t.md"}},
                },
            }])

    @pytest.mark.asyncio
    async def test_manifest_from_disk(self, tmp_path):
        """Manifest read from disk when node output isn't JSON."""
        (tmp_path / "manifest.json").write_text(
            json.dumps({"items": [{"id": "disk-item"}]})
        )

        config = make_config([{
            "id": "p",
            "name": "P",
            "execute": {"url": _ECHO, "params": {"args": ["-n", "not json"]}},
            "fan_out": {
                "manifest_path": "manifest.json",
                "template": {"execute": {
                    "url": _ECHO,
                    "params": {"args": ["-n", "child-{{id}}"]},
                }},
            },
        }])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "p::disk-item" in result["completed_nodes"]


# ---------------------------------------------------------------------------
# Manifest field propagation (uses MockExecutor to observe context)
# ---------------------------------------------------------------------------


class TestManifestFieldPropagation:
    @pytest.mark.asyncio
    async def test_custom_fields_reach_branch_context(self, tmp_path):
        """Manifest item fields beyond 'id' are passed into child context."""
        from mock_executor import MockExecutor
        from sqrlly.runtime.result import ExecutionResult

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

        assert "parent::x" in result["completed_nodes"]
        ctx = mock.received_contexts["parent::x"]
        assert ctx["id"] == "x"
        assert ctx["custom_field"] == "v123"
        assert ctx["priority"] == "high"

    @pytest.mark.asyncio
    async def test_downstream_sees_branch_aggregate(self, tmp_path):
        """Any downstream node depending on a dynamic parent sees aggregates.

        Before Stage 2b, `{parent}_branches` was synthesized only inside
        `_make_final_fan_out_node`'s local enriched dict — unreachable from
        a non-final downstream node. Stage 2b moves the synthesis into
        `build_context`, which reads state directly, so both final and
        non-final downstream nodes see the same aggregate.
        """
        from mock_executor import MockExecutor
        from sqrlly.runtime.result import ExecutionResult

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

        nodes = [
            dynamic_parent("parent", manifest),
            cmd_phase("downstream", depends_on=["parent"]),
        ]
        config = make_config(nodes)
        # Replace the command executor for downstream with the mock so we
        # can inspect its context. Use the mock for everything: it returns
        # mock results for keys it knows, defaults otherwise.
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "downstream" in result["completed_nodes"]
        ctx = mock.received_contexts["downstream"]
        assert "parent_branches" in ctx, (
            f"downstream should see `parent_branches`; got keys {list(ctx)}"
        )
        aggregate = json.loads(ctx["parent_branches"])
        assert aggregate == {"parent::a": "out-a", "parent::b": "out-b"}

    @pytest.mark.asyncio
    async def test_branch_context_inherits_parent_deps(self, tmp_path):
        """Branch template sees its parent's upstream deps, not just parent output.

        Topology: upstream -> parent (dynamic fan-out) -> child
        The child template should be able to interpolate {{upstream}}
        because upstream is in parent.depends_on. Before Stage 2a, child
        context contained only {parent_id: output, ...item_fields} — any
        template that referenced a grandparent dep would render empty.
        """
        from mock_executor import MockExecutor
        from sqrlly.runtime.result import ExecutionResult

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

        nodes = [
            cmd_phase("upstream"),
            dynamic_parent("parent", manifest, depends_on=["upstream"]),
        ]
        config = make_config(nodes)
        graph = build_workflow_graph(config, mock)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "parent::item1" in result["completed_nodes"]
        ctx = mock.received_contexts["parent::item1"]
        assert ctx.get("upstream") == "upstream-value-42", (
            f"child context should inherit parent's upstream dep; got {ctx!r}"
        )
        assert ctx.get("parent") == json.dumps({"items": manifest}), (
            "parent output still present alongside upstream"
        )
