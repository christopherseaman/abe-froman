"""Fan-out children must record node_worktrees (parity with static nodes).

When ForemanExecutor is wired and a fan-out child runs under worktree
isolation, each child's worktree path must appear in the final state's
``node_worktrees`` dict keyed by ``<parent>::<item_id>``.

The plain fan-out e2e tests (test_dynamic.py) use bare DispatchExecutor
— no foreman — so exec_result.worktree is None there and this check is
inert for those tests. This test wires ForemanExecutor + a real git repo
to exercise the code path.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.foreman import ForemanExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph, Settings

_ECHO = shutil.which("echo") or "/bin/echo"


def _init_git_repo(path: Path) -> None:
    """Initialise a minimal git repo so ForemanExecutor can create worktrees."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "init"],
        check=True,
        capture_output=True,
        env={**__import__("os").environ, "GIT_AUTHOR_NAME": "test",
             "GIT_AUTHOR_EMAIL": "test@test.com",
             "GIT_COMMITTER_NAME": "test",
             "GIT_COMMITTER_EMAIL": "test@test.com"},
    )


def _build_fan_out_config(workdir: Path) -> Graph:
    """2-leaf fan-out: parent echoes a 2-item manifest; children echo their id.

    Uses worktree="isolated" — ForemanExecutor allocates a per-node worktree
    for any non-off mode, so exec_result.worktree is set for children.
    """
    manifest = json.dumps({"items": [{"id": "0"}, {"id": "1"}]})
    return Graph(
        name="fanout-worktree-test",
        version="0.1.0",
        nodes=[
            {
                "id": "parent",
                "name": "parent",
                "execute": {
                    "url": _ECHO,
                    "params": {"args": ["-n", manifest]},
                },
                "fan_out": {
                    "template": {
                        "execute": {
                            "url": _ECHO,
                            "params": {"args": ["-n", "child-{{id}}"]},
                        },
                    },
                },
            },
        ],
        settings=Settings(worktree="isolated"),
    )


class TestFanOutWorktrees:
    @pytest.mark.asyncio
    async def test_children_record_node_worktrees(self, tmp_path):
        """Fan-out children with ForemanExecutor must populate node_worktrees.

        Each child id (parent::0, parent::1) must appear as a key in the
        final state's node_worktrees, and the value must be an existing directory.
        """
        _init_git_repo(tmp_path)

        config = _build_fan_out_config(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(
            inner,
            base_workdir=str(tmp_path),
            max_parallel_jobs=4,
        )

        graph = build_workflow_graph(config, foreman)
        state = make_initial_state(workdir=str(tmp_path))
        final_state = await graph.ainvoke(state)

        # Parent should complete
        assert "parent" in final_state["completed_nodes"]

        # Both children should complete
        assert "parent::0" in final_state["completed_nodes"]
        assert "parent::1" in final_state["completed_nodes"]

        # Each child must have recorded its worktree
        wts = final_state["node_worktrees"]
        child_keys = [k for k in wts if k.startswith("parent::")]
        assert len(child_keys) == 2, (
            f"Expected 2 child worktree entries, got {len(child_keys)}. "
            f"node_worktrees keys: {list(wts.keys())}"
        )
        for k in child_keys:
            assert Path(wts[k]).is_dir(), (
                f"Worktree for {k!r} is not an existing directory: {wts[k]!r}"
            )
