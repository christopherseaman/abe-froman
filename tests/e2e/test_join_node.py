"""End-to-end: explicit join nodes synchronize parallel branches.

Multi-function flow tests:
    - Implicit join: any node with multiple deps auto-syncs (LangGraph default;
      already worked pre-Stage-4b — included as a control)
    - Explicit join: execution: { type: join } parses, dispatches as no-op,
      and downstream nodes consume the join's empty output
    - Join with evaluation: a join node can be gated (gate runs against the
      empty join output) — sanity check that downstream eval-node wiring
      doesn't choke on JoinExecution
"""

from __future__ import annotations

import pytest

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.state import make_initial_state

from helpers import cmd_phase, make_config


class TestImplicitJoin:
    """Control: multi-pred sync without explicit JoinExecution."""

    @pytest.mark.asyncio
    async def test_implicit_join_via_multiple_deps(self, tmp_path):
        """A regular cmd node depending on >1 pred waits for all of them.

        LangGraph's default behavior — a node with multiple incoming edges
        runs at the super-step boundary after all predecessors complete.
        """
        config = make_config([
            cmd_phase("a", output="from-a"),
            cmd_phase("b", output="from-b"),
            cmd_phase("merge", depends_on=["a", "b"], output="merged"),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "a" in result["completed_nodes"]
        assert "b" in result["completed_nodes"]
        assert "merge" in result["completed_nodes"]
        assert result["node_outputs"]["merge"] == "merged"


class TestExplicitJoin:
    """Stage 4b: execution: { type: join } as authored topology marker."""

    @pytest.mark.asyncio
    async def test_explicit_join_dispatches_as_noop(self, tmp_path):
        config = make_config([
            cmd_phase("a", output="from-a"),
            cmd_phase("b", output="from-b"),
            {
                "id": "checkpoint",
                "name": "Checkpoint",
                "execution": {"type": "join"},
                "depends_on": ["a", "b"],
            },
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "checkpoint" in result["completed_nodes"]
        # Join node produces empty output (no work).
        assert result["node_outputs"]["checkpoint"] == ""

    @pytest.mark.asyncio
    async def test_join_followed_by_downstream(self, tmp_path):
        """Downstream of a join node runs after join completes."""
        config = make_config([
            cmd_phase("a", output="out-a"),
            cmd_phase("b", output="out-b"),
            {
                "id": "sync",
                "name": "Sync",
                "execution": {"type": "join"},
                "depends_on": ["a", "b"],
            },
            cmd_phase("after", depends_on=["sync"], output="post-sync"),
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        for nid in ("a", "b", "sync", "after"):
            assert nid in result["completed_nodes"], f"{nid} not completed"
        assert result["node_outputs"]["after"] == "post-sync"

    @pytest.mark.asyncio
    async def test_join_with_evaluation_is_gated(self, tmp_path):
        """A join node with evaluation runs the gate against its empty output.

        Validates that JoinExecution composes cleanly with the
        Evaluation-node split — gate logic doesn't special-case execution
        type, it just reads the node's output and invokes the validator.
        """
        validator = tmp_path / "always_pass.py"
        validator.write_text("import sys; sys.stdin.read(); print('1.0')")

        config = make_config([
            cmd_phase("a", output="out-a"),
            cmd_phase("b", output="out-b"),
            {
                "id": "gated_join",
                "name": "Gated Join",
                "execution": {"type": "join"},
                "depends_on": ["a", "b"],
                "evaluation": {"validator": str(validator), "threshold": 0.9},
            },
        ])
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        assert "gated_join" in result["completed_nodes"]
        records = result["evaluations"]["gated_join"]
        assert len(records) == 1
        assert records[0]["result"]["score"] == 1.0
