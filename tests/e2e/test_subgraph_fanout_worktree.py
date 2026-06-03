"""Per-branch worktree isolation for subgraph fan-out.

When ForemanExecutor + worktree:isolated/auto is active and a fan-out
template is a subgraph, each Send branch must:

  1. Get its own branch worktree (keyed parent::item, safe-name), NOT one
     shared tree for inner nodes.
  2. Inner nodes (gen, polish) run INSIDE that branch tree — no per-inner
     wt-gen-* or wt-polish-* directories on disk.
  3. A ``polish`` inner node that depends_on ``gen`` can read gen's file
     because both ran in the same branch tree (intra-branch read-across).

Three manifest items → 3 distinct branch worktrees.

Uses /bin/sh script nodes (no LLM) so the test is self-contained and
deterministic.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.foreman import ForemanExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph, Settings

_PARENT_ID = "writer_pool"


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "init"],
        check=True, capture_output=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test.com",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test.com"},
    )


def _write_sub_yaml(tmp_path: Path) -> str:
    """Two-node subgraph: gen writes a file, polish reads and extends it.

    Both nodes use /bin/sh -c '...' so they operate in the current
    working directory (the branch worktree). The test asserts both
    files co-exist in the SAME branch tree.
    """
    sub = {
        "name": "write-sub",
        "version": "1.0",
        "nodes": [
            {
                "id": "gen",
                "name": "Generate",
                "execute": {
                    "url": "/bin/sh",
                    "params": {"args": ["-c", "echo generated-{{item_id}} > gen.txt"]},
                },
            },
            {
                "id": "polish",
                "name": "Polish",
                "depends_on": ["gen"],
                "execute": {
                    "url": "/bin/sh",
                    "params": {"args": ["-c", "echo polished-{{item_id}} > polish.txt"]},
                },
            },
        ],
    }
    sub_path = tmp_path / "write_sub.yaml"
    sub_path.write_text(yaml.safe_dump(sub))
    return "write_sub.yaml"


def _build_parent_config(tmp_path: Path, sub_yaml: str) -> Graph:
    """Parent fan-out over 3 manifest items; template is the subgraph."""
    items = [{"id": "alpha"}, {"id": "beta"}, {"id": "gamma"}]
    manifest = json.dumps({"items": items})
    # printf avoids echo quoting issues with JSON strings
    return Graph(
        name="subgraph-fanout-worktree-test",
        version="1.0",
        nodes=[
            {
                "id": _PARENT_ID,
                "name": "Writer Pool",
                "execute": {
                    "url": "/bin/sh",
                    "params": {"args": ["-c", f"printf '%s' '{manifest}'"]},
                },
                "fan_out": {
                    "template": {
                        "execute": {
                            "url": sub_yaml,
                            "params": {"inputs": {"item_id": "{{id}}"}},
                        },
                    },
                },
            },
        ],
        settings=Settings(worktree="isolated"),
    )


class TestSubgraphFanOutWorktrees:
    @pytest.mark.asyncio
    async def test_per_branch_worktree_isolation(self, tmp_path):
        """3 branches → 3 distinct wt-<parent>__<item>-* dirs; no inner-node trees."""
        _init_git_repo(tmp_path)
        sub_yaml = _write_sub_yaml(tmp_path)
        config = _build_parent_config(tmp_path, sub_yaml)

        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(inner, base_workdir=str(tmp_path), max_parallel_jobs=4)

        graph = build_workflow_graph(config, foreman, _base_dir=tmp_path)
        state = make_initial_state(workdir=str(tmp_path))
        final_state = await graph.ainvoke(state)

        sqrlly_dir = tmp_path / ".sqrlly"

        # --- 1. All three branches completed ---
        for item_id in ("alpha", "beta", "gamma"):
            child_id = f"{_PARENT_ID}::{item_id}"
            assert child_id in final_state["completed_nodes"], (
                f"{child_id} not in completed_nodes: {final_state['completed_nodes']}"
            )

        # --- 2. Exactly 3 branch worktrees on disk ---
        # wt-<safe_parent_id>-* where safe = writer_pool (no special chars)
        # Branch id is writer_pool::alpha, safe = writer_pool__alpha
        branch_trees = list(sqrlly_dir.glob(f"wt-{_PARENT_ID}__*"))
        assert len(branch_trees) == 3, (
            f"Expected 3 branch worktrees, got {len(branch_trees)}: "
            f"{[d.name for d in branch_trees]}"
        )

        # --- 3. No per-inner-node trees ---
        gen_trees = list(sqrlly_dir.glob("wt-gen-*"))
        polish_trees = list(sqrlly_dir.glob("wt-polish-*"))
        assert gen_trees == [], (
            f"Found unexpected per-inner gen worktrees: {[d.name for d in gen_trees]}"
        )
        assert polish_trees == [], (
            f"Found unexpected per-inner polish worktrees: {[d.name for d in polish_trees]}"
        )

        # --- 4. Intra-branch read-across: both files co-exist in ONE branch dir ---
        # Pick one branch tree (e.g. the alpha one) and verify both files are present.
        alpha_safe = f"{_PARENT_ID}__alpha"
        alpha_trees = [d for d in branch_trees if alpha_safe in d.name]
        assert len(alpha_trees) == 1, (
            f"Expected exactly 1 alpha branch tree, got: {[d.name for d in alpha_trees]}"
        )
        alpha_tree = alpha_trees[0]
        assert (alpha_tree / "gen.txt").exists(), (
            f"gen.txt not found in alpha branch tree {alpha_tree}"
        )
        assert (alpha_tree / "polish.txt").exists(), (
            f"polish.txt not found in alpha branch tree {alpha_tree}"
        )
        assert "generated-alpha" in (alpha_tree / "gen.txt").read_text()
        assert "polished-alpha" in (alpha_tree / "polish.txt").read_text()
