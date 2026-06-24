"""Per-fan-out worktree override (FanOutTemplate.worktree) — the off-override
makes branch writes land in the SHARED base workdir where global
settings.worktree:isolated would have hidden them in per-branch trees.

ONE workflow, global settings.worktree:isolated. A fan-out whose template
sets worktree:off; each branch appends to a file in the base workdir; a
downstream join node reads it. Proves the override for BOTH execution
paths (subgraph .yaml template + non-subgraph /bin/sh template) and the
default-isolation contrast. Pure /bin/sh nodes — no LLM, deterministic.
Models tests/e2e/test_subgraph_fanout_worktree.py.
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

_PARENT_ID = "build"
_ITEMS = [{"id": "alpha"}, {"id": "beta"}, {"id": "gamma"}]


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "init"],
        check=True, capture_output=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test.com",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test.com"},
    )


def _write_manifest(tmp_path: Path) -> None:
    (tmp_path / "items.json").write_text(json.dumps({"items": _ITEMS}))


def _script_node(item_template: str) -> dict:
    """A /bin/sh node that appends '<item>\\n' to shared.txt in its CWD.

    `>>` append + a per-item line lets the join node count how many
    branches wrote to the SAME shared.txt. In a branch worktree these
    appends are invisible to base; with worktree:off they land in base.
    """
    return {
        "id": "appender", "name": "appender",
        "execute": {
            "url": "/bin/sh",
            "params": {"args": ["-c", f"echo {item_template} >> shared.txt"]},
        },
    }


def _build_config(
    *, template_worktree, subgraph: bool, tmp_path: Path,
) -> Graph:
    """Fan-out over 3 items; join node reads shared.txt from base.

    When `subgraph` is True the template is a one-node subgraph .yaml whose
    node appends to shared.txt; else the template is the /bin/sh appender
    directly. `template_worktree` is the per-fan-out override (None inherits).
    """
    template_execute: dict
    if subgraph:
        sub = {
            "name": "appender-sub", "version": "1.0",
            "nodes": [_script_node("{{item_id}}")],
        }
        (tmp_path / "appender_sub.yaml").write_text(yaml.safe_dump(sub))
        template_execute = {
            "url": "appender_sub.yaml",
            "params": {"inputs": {"item_id": "{{id}}"}},
        }
    else:
        template_execute = {
            "url": "/bin/sh",
            "params": {"args": ["-c", "echo {{id}} >> shared.txt"]},
        }

    template: dict = {"execute": template_execute}
    if template_worktree is not None:
        template["worktree"] = template_worktree

    return Graph(
        name="fanout-worktree-control", version="1.0",
        nodes=[
            {
                "id": _PARENT_ID, "name": "Build",
                "execute": {
                    "url": "/bin/sh",
                    "params": {"args": ["-c",
                                         f"printf '%s' '{json.dumps({'items': _ITEMS})}'"]},
                },
                "fan_out": {"template": template},
            },
            {
                "id": "join", "name": "Join",
                "depends_on": [_PARENT_ID],
                "execute": {
                    "url": "/bin/sh",
                    # Print the file if it exists, else nothing. Runs in base
                    # (join inherits isolated, but we only read what branches
                    # left in base — see assertions).
                    "params": {"args": ["-c",
                                         "cat shared.txt 2>/dev/null || true"]},
                },
            },
        ],
        settings=Settings(worktree="isolated"),
    )


async def _run(config: Graph, tmp_path: Path) -> dict:
    inner = DispatchExecutor(workdir=str(tmp_path))
    foreman = ForemanExecutor(inner, base_workdir=str(tmp_path), max_parallel_jobs=4)
    graph = build_workflow_graph(config, foreman, _base_dir=tmp_path)
    state = make_initial_state(workdir=str(tmp_path))
    return await graph.ainvoke(state)


class TestFanOutWorktreeControl:
    @pytest.mark.asyncio
    async def test_subgraph_template_off_writes_to_base(self, tmp_path):
        """Subgraph template + worktree:off → branch appends land in base."""
        _init_git_repo(tmp_path)
        config = _build_config(
            template_worktree="off", subgraph=True, tmp_path=tmp_path,
        )
        final = await _run(config, tmp_path)

        for item in _ITEMS:
            assert f"{_PARENT_ID}::{item['id']}" in final["completed_nodes"]

        # The off-override sent every branch's append to the shared base file.
        shared = tmp_path / "shared.txt"
        assert shared.exists(), "shared.txt missing from base — off-override failed"
        lines = sorted(shared.read_text().split())
        assert lines == ["alpha", "beta", "gamma"], (
            f"expected all 3 branch appends in base, got {lines!r}"
        )

    @pytest.mark.asyncio
    async def test_script_template_off_writes_to_base(self, tmp_path):
        """Non-subgraph /bin/sh template + worktree:off → appends land in base."""
        _init_git_repo(tmp_path)
        config = _build_config(
            template_worktree="off", subgraph=False, tmp_path=tmp_path,
        )
        final = await _run(config, tmp_path)

        for item in _ITEMS:
            assert f"{_PARENT_ID}::{item['id']}" in final["completed_nodes"]

        shared = tmp_path / "shared.txt"
        assert shared.exists(), "shared.txt missing from base — off-override failed"
        lines = sorted(shared.read_text().split())
        assert lines == ["alpha", "beta", "gamma"]

    @pytest.mark.asyncio
    async def test_default_isolated_hides_branch_writes_from_base(self, tmp_path):
        """Contrast: NO override → inherits settings.worktree:isolated → branch
        appends land in per-branch trees, NOT base. (subgraph template.)"""
        _init_git_repo(tmp_path)
        config = _build_config(
            template_worktree=None, subgraph=True, tmp_path=tmp_path,
        )
        final = await _run(config, tmp_path)

        for item in _ITEMS:
            assert f"{_PARENT_ID}::{item['id']}" in final["completed_nodes"]

        # Isolation hid the writes: nothing in base. The branch worktrees
        # under .sqrlly/ each hold their own shared.txt.
        assert not (tmp_path / "shared.txt").exists(), (
            "branch writes leaked into base under isolated — isolation broken"
        )
        sqrlly_dir = tmp_path / ".sqrlly"
        branch_trees = list(sqrlly_dir.glob(f"wt-{_PARENT_ID}__*"))
        assert len(branch_trees) == 3, (
            f"expected 3 branch worktrees, got {[d.name for d in branch_trees]}"
        )
        # Each branch tree holds its own one-line shared.txt.
        per_tree = sorted(
            (t / "shared.txt").read_text().strip()
            for t in branch_trees if (t / "shared.txt").exists()
        )
        assert per_tree == ["alpha", "beta", "gamma"]
