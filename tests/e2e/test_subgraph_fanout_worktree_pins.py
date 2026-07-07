"""Regression-pin tests for subgraph fan-out branch worktree contracts.

Tasks 5-9 of the subgraph-fanout-worktree fix plan.  All tests run
against current code and should PASS; they are guards, not TDD drivers.

Each test includes a "would fail if X regressed" note in its docstring.

Task 5 — explicit worktree directive on inner node does NOT escape branch tree.
Task 6 — node_worktrees populated with branch-keyed entries + branch_map context.
Task 7 — resume rehydrates branch trees from saved node_worktrees.
Task 8 — GC reclaims exactly the branch trees (no stray inner-node trees).
Task 9b — capability fallthrough: non-foreman executor → no wt-* dirs, worktree=None.
Task 9a — existing e2e suites stay green (verified via the separate pytest run; no
           new test needed here per spec).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.compile.nodes import build_context
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.foreman import ForemanExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph, Node, Settings
from helpers import init_git_repo

_PARENT_ID = "writer_pool"


# ---------------------------------------------------------------------------
# Shared fixtures helpers (mirror test_subgraph_fanout_worktree.py)
# ---------------------------------------------------------------------------


def _init_git_repo(path):
    init_git_repo(path)

def _write_sub_yaml(tmp_path: Path, *, gen_worktree: str | None = None) -> str:
    """Two-node subgraph: gen writes a file, polish reads and extends it.

    ``gen_worktree`` — when set, injects a ``worktree:`` directive on the
    ``gen`` node so Task-5 can verify the directive is neutralised.
    """
    gen_node: dict[str, Any] = {
        "id": "gen",
        "name": "Generate",
        "execute": {
            "url": "/bin/sh",
            "params": {"args": ["-c", "echo generated-{{item_id}} > gen.txt"]},
        },
    }
    if gen_worktree is not None:
        gen_node["worktree"] = gen_worktree

    sub = {
        "name": "write-sub",
        "version": "1.0",
        "nodes": [
            gen_node,
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
    return Graph(
        name="subgraph-fanout-worktree-pin-test",
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


def _make_foreman(tmp_path: Path) -> tuple[ForemanExecutor, DispatchExecutor]:
    inner = DispatchExecutor(workdir=str(tmp_path))
    foreman = ForemanExecutor(inner, base_workdir=str(tmp_path), max_parallel_jobs=4)
    return foreman, inner


# ---------------------------------------------------------------------------
# Task 5 — inner node with explicit worktree directive does NOT escape branch tree
# ---------------------------------------------------------------------------


class TestInnerWorktreeDirectiveNeutralised:
    @pytest.mark.asyncio
    async def test_explicit_isolated_on_gen_does_not_create_per_inner_trees(
        self, tmp_path
    ):
        """gen declares worktree:isolated; _strip_worktree forces it to off.

        After the run:
        - Still exactly 3 wt-writer_pool__* branch trees (one per branch).
        - No wt-gen-* trees on disk (the directive was neutralised).
        - gen.txt + polish.txt still co-exist inside each branch tree
          (inner nodes ran in the branch tree, not somewhere else).

        Would fail if: _BranchScopedExecutor.execute stopped calling
        _strip_worktree on the node before forwarding to the inner executor,
        allowing the isolated directive to create a wt-gen-* tree (foreman's
        pool-key dedup means the three branches' `gen` calls would share one
        such tree — the `gen_trees == []` assertion catches any).
        """
        _init_git_repo(tmp_path)
        # Write subgraph with an explicit worktree:isolated on the gen node.
        sub_yaml = _write_sub_yaml(tmp_path, gen_worktree="isolated")
        config = _build_parent_config(tmp_path, sub_yaml)

        foreman, _inner = _make_foreman(tmp_path)
        graph = build_workflow_graph(config, foreman, _base_dir=tmp_path)
        state = make_initial_state(workdir=str(tmp_path))
        final_state = await graph.ainvoke(state)

        sqrlly_dir = tmp_path / ".sqrlly"

        # All three branches must have completed.
        for item_id in ("alpha", "beta", "gamma"):
            child_id = f"{_PARENT_ID}::{item_id}"
            assert child_id in final_state["completed_nodes"], (
                f"{child_id} not in completed_nodes: {final_state['completed_nodes']}"
            )

        # Exactly 3 branch worktrees exist.
        branch_trees = list(sqrlly_dir.glob(f"wt-{_PARENT_ID}__*"))
        assert len(branch_trees) == 3, (
            f"Expected 3 branch trees, got {len(branch_trees)}: "
            f"{[d.name for d in branch_trees]}"
        )

        # No per-inner-node wt-gen-* trees — the directive was neutralised.
        gen_trees = list(sqrlly_dir.glob("wt-gen-*"))
        assert gen_trees == [], (
            f"Expected no wt-gen-* trees (directive neutralised by _strip_worktree), "
            f"but found: {[d.name for d in gen_trees]}"
        )

        # Files co-exist inside the branch tree (inner nodes used the branch tree).
        alpha_trees = [d for d in branch_trees if f"{_PARENT_ID}__alpha" in d.name]
        assert len(alpha_trees) == 1, (
            f"Expected 1 alpha branch tree, got: {[d.name for d in alpha_trees]}"
        )
        alpha_tree = alpha_trees[0]
        assert (alpha_tree / "gen.txt").exists(), (
            f"gen.txt missing from alpha branch tree {alpha_tree} — "
            "inner node did not run in the branch tree"
        )
        assert (alpha_tree / "polish.txt").exists(), (
            f"polish.txt missing from alpha branch tree {alpha_tree}"
        )


# ---------------------------------------------------------------------------
# Task 6 — node_worktrees[child_id] populated + branch_map context
# ---------------------------------------------------------------------------


class TestNodeWorktreesPopulated:
    @pytest.mark.asyncio
    async def test_node_worktrees_keyed_by_branch_id(self, tmp_path):
        """After a subgraph fan-out run, node_worktrees has one entry per branch.

        Each key is writer_pool::<item> and the value is an existing directory
        equal to the corresponding on-disk branch tree.

        Would fail if: dynamic.py lines 210-211 (exec_result.worktree recording)
        were removed, or if make_fan_out_subgraph_invoker stopped setting
        ExecutionResult.worktree when use_branch is True.
        """
        _init_git_repo(tmp_path)
        sub_yaml = _write_sub_yaml(tmp_path)
        config = _build_parent_config(tmp_path, sub_yaml)

        foreman, _inner = _make_foreman(tmp_path)
        graph = build_workflow_graph(config, foreman, _base_dir=tmp_path)
        state = make_initial_state(workdir=str(tmp_path))
        final_state = await graph.ainvoke(state)

        wts = final_state["node_worktrees"]
        branch_keys = [k for k in wts if k.startswith(f"{_PARENT_ID}::")]
        assert len(branch_keys) == 3, (
            f"Expected 3 branch entries in node_worktrees, got {len(branch_keys)}. "
            f"node_worktrees keys: {sorted(wts.keys())}"
        )

        sqrlly_dir = tmp_path / ".sqrlly"
        for key in branch_keys:
            path = Path(wts[key])
            assert path.is_dir(), (
                f"node_worktrees[{key!r}] = {wts[key]!r} is not an existing dir"
            )
            # The recorded path must be one of the branch trees on disk.
            branch_trees = list(sqrlly_dir.glob(f"wt-{_PARENT_ID}__*"))
            tree_paths = {str(t.resolve()) for t in branch_trees}
            assert str(path.resolve()) in tree_paths, (
                f"node_worktrees[{key!r}] path {path!r} not among branch trees "
                f"on disk: {sorted(tree_paths)}"
            )

    @pytest.mark.asyncio
    async def test_branch_map_context_has_worktree_per_child(self, tmp_path):
        """build_context synthesises writer_pool_branch_map with a non-null
        worktree per child once node_worktrees is populated.

        A fan-in node depending on writer_pool would see
        {{writer_pool_branch_map}} render to a JSON object where each child
        id maps to {"output": ..., "worktree": "<existing dir>"}.

        Would fail if: build_context's branch_map synthesis (nodes.py) stopped
        reading node_worktrees, or if node_worktrees was never populated for
        branch children.
        """
        _init_git_repo(tmp_path)
        sub_yaml = _write_sub_yaml(tmp_path)
        config = _build_parent_config(tmp_path, sub_yaml)

        foreman, _inner = _make_foreman(tmp_path)
        graph = build_workflow_graph(config, foreman, _base_dir=tmp_path)
        state = make_initial_state(workdir=str(tmp_path))
        final_state = await graph.ainvoke(state)

        # Construct a synthetic fan-in node that depends on writer_pool and
        # call build_context directly — avoids wiring a full fan-in node while
        # still testing the real code path.
        fan_in_node = Node(
            id="fan_in",
            name="Fan In",
            depends_on=[_PARENT_ID],
            execute={"url": "/bin/echo", "params": {"args": []}},
        )
        context = build_context(fan_in_node, final_state)

        assert f"{_PARENT_ID}_branch_map" in context, (
            f"Expected {_PARENT_ID}_branch_map in context; got keys: "
            f"{sorted(context.keys())}"
        )
        branch_map = json.loads(context[f"{_PARENT_ID}_branch_map"])
        assert len(branch_map) == 3, (
            f"Expected 3 entries in branch_map, got {len(branch_map)}: "
            f"{list(branch_map.keys())}"
        )
        for child_id, entry in branch_map.items():
            wt = entry.get("worktree")
            assert wt is not None, (
                f"branch_map[{child_id!r}]['worktree'] is None — "
                "node_worktrees not propagated into context"
            )
            assert Path(wt).is_dir(), (
                f"branch_map[{child_id!r}]['worktree'] = {wt!r} is not an existing dir"
            )


# ---------------------------------------------------------------------------
# Task 7 — resume rehydrates branch trees
# ---------------------------------------------------------------------------


class TestResumeRehydratesBranchTrees:
    @pytest.mark.asyncio
    async def test_rehydrated_foreman_sees_branch_worktrees(self, tmp_path):
        """After a run, node_worktrees holds branch paths.  A new ForemanExecutor
        rehydrated from those paths exposes them via get_worktree() and reuses
        existing on-disk trees (no new wt-* dirs created on re-acquire).

        Would fail if: branch_id keys were not recorded in node_worktrees (so
        rehydrate= would have nothing to consume), or if _acquire_worktree's
        "existing live tree" fast-path did not fire for rehydrated keys.
        """
        _init_git_repo(tmp_path)
        sub_yaml = _write_sub_yaml(tmp_path)
        config = _build_parent_config(tmp_path, sub_yaml)

        # Phase 1: run, collect node_worktrees.
        foreman1, inner1 = _make_foreman(tmp_path)
        graph1 = build_workflow_graph(config, foreman1, _base_dir=tmp_path)
        state1 = make_initial_state(workdir=str(tmp_path))
        final_state = await graph1.ainvoke(state1)

        saved_worktrees = final_state["node_worktrees"]
        branch_keys = [k for k in saved_worktrees if k.startswith(f"{_PARENT_ID}::")]
        assert len(branch_keys) == 3, (
            f"Phase-1 run must record 3 branch entries in node_worktrees; "
            f"got {len(branch_keys)}: {sorted(saved_worktrees.keys())}"
        )

        # Phase 2: rehydrate a fresh ForemanExecutor from saved_worktrees.
        inner2 = DispatchExecutor(workdir=str(tmp_path))
        foreman2 = ForemanExecutor(
            inner2,
            base_workdir=str(tmp_path),
            max_parallel_jobs=4,
            rehydrate=saved_worktrees,
        )

        # Every branch key must be accessible and point at an existing dir.
        for key in branch_keys:
            path = foreman2.get_worktree(key)
            assert path is not None, (
                f"Rehydrated foreman missing worktree for {key!r}"
            )
            assert Path(path).is_dir(), (
                f"Rehydrated worktree {path!r} for {key!r} is not an existing dir"
            )
            assert path == saved_worktrees[key], (
                f"Rehydrated path for {key!r} ({path!r}) != saved path "
                f"({saved_worktrees[key]!r})"
            )

        # Re-acquiring a branch id that exists returns the same path (no new tree).
        sqrlly_dir = tmp_path / ".sqrlly"
        trees_before = set(sqrlly_dir.glob(f"wt-{_PARENT_ID}__*"))

        for key in branch_keys:
            reacquired = await foreman2.acquire_branch_worktree(key)
            assert reacquired == saved_worktrees[key], (
                f"Re-acquire for {key!r} returned a different path; "
                f"expected {saved_worktrees[key]!r}, got {reacquired!r}"
            )

        trees_after = set(sqrlly_dir.glob(f"wt-{_PARENT_ID}__*"))
        assert trees_before == trees_after, (
            f"Re-acquire created new branch trees: "
            f"before={[t.name for t in trees_before]}, "
            f"after={[t.name for t in trees_after]}"
        )

        await foreman2.close()


# ---------------------------------------------------------------------------
# Task 8 — GC reclaims branch trees
# ---------------------------------------------------------------------------


class TestGcReclaimsBranchTrees:
    @pytest.mark.asyncio
    async def test_reclaim_removes_branch_trees_no_inner_trees(
        self, tmp_path
    ):
        """reclaim() after a clean run removes all foreman-tracked trees.

        The foreman tracks 4 trees in this scenario: the parent node's own
        isolation tree (wt-writer_pool-*) plus the 3 branch trees
        (wt-writer_pool__alpha-*, etc.).  No wt-gen-* or wt-polish-*
        trees should ever have been created — inner subgraph nodes are
        pinned to the branch trees by _BranchScopedExecutor / _strip_worktree.

        Key assertions:
        - Exactly 3 branch worktrees (wt-writer_pool__*) exist pre-reclaim.
        - Zero wt-gen-* or wt-polish-* trees — no inner-node leak.
        - reclaim() removes all 4 tracked paths (parent + 3 branches).
        - All wt-* dirs are gone from disk after reclaim.

        Would fail if: inner nodes created their own wt-gen-*/wt-polish-* trees
        (meaning _strip_worktree regressed), or if branch trees were not
        registered in _worktrees (meaning reclaim() had nothing to remove for
        the branch children), or if the parent's own tree was unexpectedly
        absent from the tracked set.
        """
        _init_git_repo(tmp_path)
        sub_yaml = _write_sub_yaml(tmp_path)
        config = _build_parent_config(tmp_path, sub_yaml)

        foreman, _inner = _make_foreman(tmp_path)
        graph = build_workflow_graph(config, foreman, _base_dir=tmp_path)
        state = make_initial_state(workdir=str(tmp_path))
        final_state = await graph.ainvoke(state)

        sqrlly_dir = tmp_path / ".sqrlly"

        # Pre-reclaim: confirm 3 branch trees and no inner-node trees.
        branch_trees_before = list(sqrlly_dir.glob(f"wt-{_PARENT_ID}__*"))
        gen_trees_before = list(sqrlly_dir.glob("wt-gen-*"))
        polish_trees_before = list(sqrlly_dir.glob("wt-polish-*"))
        assert len(branch_trees_before) == 3, (
            f"Expected 3 branch trees (wt-{_PARENT_ID}__*) before reclaim, got "
            f"{len(branch_trees_before)}: {[d.name for d in branch_trees_before]}"
        )
        assert gen_trees_before == [], (
            f"Stray wt-gen-* trees before reclaim: {[d.name for d in gen_trees_before]}"
        )
        assert polish_trees_before == [], (
            f"Stray wt-polish-* trees before reclaim: "
            f"{[d.name for d in polish_trees_before]}"
        )

        # The foreman tracks 4 distinct paths: 1 parent + 3 branches.
        # (The parent writer_pool node also runs under worktree:isolated.)
        tracked = set(foreman.worktree_map().values())
        assert len(tracked) == 4, (
            f"Expected 4 tracked paths (1 parent + 3 branches), got {len(tracked)}: "
            f"{sorted(tracked)}"
        )

        # None of the tracked paths should be inner-node trees.
        for path in tracked:
            name = Path(path).name
            assert not name.startswith("wt-gen-"), (
                f"Inner-node gen tree leaked into foreman._worktrees: {path!r}"
            )
            assert not name.startswith("wt-polish-"), (
                f"Inner-node polish tree leaked into foreman._worktrees: {path!r}"
            )

        # Reclaim removes all 4 distinct paths.
        removed = await foreman.reclaim()
        assert len(removed) == 4, (
            f"Expected reclaim() to return 4 removed paths (1 parent + 3 branches), "
            f"got {len(removed)}: {removed}"
        )

        # All wt-* dirs must now be gone from disk.
        all_wt_after = list(sqrlly_dir.glob("wt-*")) if sqrlly_dir.exists() else []
        assert all_wt_after == [], (
            f"wt-* dirs still on disk after reclaim: "
            f"{[d.name for d in all_wt_after]}"
        )

        # The foreman's internal map is cleared.
        assert foreman.worktree_map() == {}, (
            f"foreman._worktrees not cleared after reclaim: {foreman.worktree_map()}"
        )


# ---------------------------------------------------------------------------
# Task 9b — capability fallthrough: non-foreman executor → no wt-* dirs
# ---------------------------------------------------------------------------


class TestCapabilityFallthrough:
    @pytest.mark.asyncio
    async def test_non_foreman_executor_runs_in_workdir_no_wt_dirs(self, tmp_path):
        """A subgraph fan-out with plain DispatchExecutor (no acquire_branch_worktree)
        runs branches in the shared workdir — no wt-* directories are created
        and exec_result.worktree is None (so node_worktrees gets no branch entries).

        This pins the documented no-isolation contract: callers without foreman
        get in-workdir execution rather than silent breakage.

        Would fail if: make_fan_out_subgraph_invoker started requiring foreman
        unconditionally (breaking DispatchExecutor paths), or if it silently
        created worktrees for non-foreman executors.

        Note: tmp_path is NOT a git repo here — isolation requires git;
        DispatchExecutor just uses the directory as-is.
        """
        # Build the same 3-item subgraph fan-out but with plain DispatchExecutor.
        sub_yaml = _write_sub_yaml(tmp_path)
        config = _build_parent_config(tmp_path, sub_yaml)

        # No git init — DispatchExecutor does not require a git repo.
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor, _base_dir=tmp_path)
        state = make_initial_state(workdir=str(tmp_path))
        final_state = await graph.ainvoke(state)

        # All 3 branches must complete (no-isolation is a valid execution mode).
        for item_id in ("alpha", "beta", "gamma"):
            child_id = f"{_PARENT_ID}::{item_id}"
            assert child_id in final_state["completed_nodes"], (
                f"{child_id} not in completed_nodes: {final_state['completed_nodes']}"
            )

        # No wt-* directories were created (no foreman = no worktree allocation).
        all_wt_dirs = list(tmp_path.rglob("wt-*"))
        assert all_wt_dirs == [], (
            f"Expected no wt-* dirs with plain DispatchExecutor, found: "
            f"{[str(d) for d in all_wt_dirs]}"
        )

        # node_worktrees has no branch entries (ExecutionResult.worktree == None).
        wts = final_state.get("node_worktrees", {})
        branch_wt_keys = [k for k in wts if k.startswith(f"{_PARENT_ID}::")]
        assert branch_wt_keys == [], (
            f"Expected no branch entries in node_worktrees with plain executor, "
            f"found: {branch_wt_keys}"
        )


# ---------------------------------------------------------------------------
# Gap 2 — foreman present + fan-out scope worktree:off → NO branch isolation
# ---------------------------------------------------------------------------


class TestOffGateWithForeman:
    @pytest.mark.asyncio
    async def test_off_gate_with_foreman_no_branch_isolation(self, tmp_path):
        """ForemanExecutor + settings worktree:off → branches run in base workdir.

        The gate ``_isolate = parent_settings.worktree != "off"`` evaluates to
        False, so ``use_branch`` is False for every branch and no branch worktree
        is acquired. This is the WITH-foreman counterpart to the non-foreman
        fallthrough already pinned in TestCapabilityFallthrough.

        After the run:
        - Zero ``wt-*`` directories under ``<repo>/.sqrlly`` (none created).
        - ``node_worktrees`` has no ``writer_pool::*`` branch entries
          (``ExecutionResult.worktree`` is None when ``use_branch`` is False).
        - Files written by inner subgraph nodes (gen.txt, polish.txt) landed
          in the BASE repo directory (branches shared the base workdir).

        Would fail if: the off-gate regressed and branches started isolating
        under ``settings.worktree="off"`` — the wt-* assertion would catch the
        newly-created branch trees, the node_worktrees assertion would catch the
        recorded paths, and the file-location assertion would detect files
        inside a worktree rather than the base dir.
        """
        _init_git_repo(tmp_path)
        sub_yaml = _write_sub_yaml(tmp_path)

        # Build config with worktree:off — the critical difference from the
        # isolated fixture used by every other test in this file.
        items = [{"id": "alpha"}, {"id": "beta"}, {"id": "gamma"}]
        manifest = json.dumps({"items": items})
        config = Graph(
            name="subgraph-fanout-off-gate-test",
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
            settings=Settings(worktree="off"),
        )

        foreman, _inner = _make_foreman(tmp_path)
        graph = build_workflow_graph(config, foreman, _base_dir=tmp_path)
        state = make_initial_state(workdir=str(tmp_path))
        final_state = await graph.ainvoke(state)

        # All three branches must have completed.
        for item_id in ("alpha", "beta", "gamma"):
            child_id = f"{_PARENT_ID}::{item_id}"
            assert child_id in final_state["completed_nodes"], (
                f"{child_id} not in completed_nodes: {final_state['completed_nodes']}"
            )

        sqrlly_dir = tmp_path / ".sqrlly"

        # Zero wt-* directories (no branch trees, no inner trees).
        wt_dirs = list(sqrlly_dir.glob("wt-*")) if sqrlly_dir.exists() else []
        assert wt_dirs == [], (
            f"Expected zero wt-* dirs with worktree:off, found: "
            f"{[d.name for d in wt_dirs]}"
        )

        # node_worktrees must have no writer_pool::* branch entries.
        wts = final_state.get("node_worktrees", {})
        branch_entries = {k: v for k, v in wts.items() if k.startswith(f"{_PARENT_ID}::")}
        assert branch_entries == {}, (
            f"Expected no branch entries in node_worktrees under worktree:off, "
            f"found: {branch_entries}"
        )

        # Inner nodes ran in the base workdir — at least one output file landed
        # there (branches run sequentially via the shared base dir, last write wins).
        gen_in_base = (tmp_path / "gen.txt").exists()
        polish_in_base = (tmp_path / "polish.txt").exists()
        assert gen_in_base or polish_in_base, (
            "Expected at least one of gen.txt / polish.txt in base workdir "
            f"({tmp_path}) after worktree:off run — inner nodes should write "
            "to the base directory when branch isolation is off"
        )


# ---------------------------------------------------------------------------
# Gap 3 — nested fan-out inside a branch shares the OUTER branch tree
# ---------------------------------------------------------------------------

_OUTER_ID = "outer_pool"
_INNER_ID = "inner_pool"


def _write_leaf_sub_yaml(tmp_path: Path) -> str:
    """Leaf subgraph: single node writes leaf.txt — used as inner template."""
    leaf = {
        "name": "leaf-sub",
        "version": "1.0",
        "nodes": [
            {
                "id": "leaf_writer",
                "name": "Leaf Writer",
                "execute": {
                    "url": "/bin/sh",
                    "params": {
                        "args": [
                            "-c",
                            "echo leaf-{{outer_id}}-{{inner_id}} > leaf.txt",
                        ]
                    },
                },
            },
        ],
    }
    leaf_path = tmp_path / "leaf_sub.yaml"
    leaf_path.write_text(yaml.safe_dump(leaf))
    return "leaf_sub.yaml"


def _write_middle_sub_yaml(tmp_path: Path, leaf_yaml: str) -> str:
    """Middle subgraph: one fan-out node that fans over 2 inner items,
    each running the leaf subgraph. Passes outer_id through as input."""
    inner_items = [{"id": "p"}, {"id": "q"}]
    inner_manifest = json.dumps({"items": inner_items})
    middle = {
        "name": "middle-sub",
        "version": "1.0",
        "nodes": [
            {
                "id": _INNER_ID,
                "name": "Inner Pool",
                "execute": {
                    "url": "/bin/sh",
                    "params": {"args": ["-c", f"printf '%s' '{inner_manifest}'"]},
                },
                "fan_out": {
                    "template": {
                        "execute": {
                            "url": leaf_yaml,
                            "params": {
                                "inputs": {
                                    "outer_id": "{{outer_id}}",
                                    "inner_id": "{{id}}",
                                }
                            },
                        },
                    },
                },
            },
        ],
    }
    middle_path = tmp_path / "middle_sub.yaml"
    middle_path.write_text(yaml.safe_dump(middle))
    return "middle_sub.yaml"


def _build_nested_fanout_config(tmp_path: Path, middle_yaml: str) -> Graph:
    """Top-level config: outer fan-out over 2 items; template is the middle subgraph."""
    outer_items = [{"id": "x"}, {"id": "y"}]
    outer_manifest = json.dumps({"items": outer_items})
    return Graph(
        name="nested-fanout-shared-outer-tree-test",
        version="1.0",
        nodes=[
            {
                "id": _OUTER_ID,
                "name": "Outer Pool",
                "execute": {
                    "url": "/bin/sh",
                    "params": {"args": ["-c", f"printf '%s' '{outer_manifest}'"]},
                },
                "fan_out": {
                    "template": {
                        "execute": {
                            "url": middle_yaml,
                            "params": {"inputs": {"outer_id": "{{id}}"}},
                        },
                    },
                },
            },
        ],
        settings=Settings(worktree="isolated"),
    )


class TestNestedFanOutSharesOuterBranchTree:
    @pytest.mark.asyncio
    async def test_nested_fanout_shares_outer_branch_tree(self, tmp_path):
        """Nested fan-out: inner branches run inside the outer branch tree.

        Topology:
          outer_pool fans over {x, y} → each branch runs middle_sub.yaml
          middle_sub.yaml contains inner_pool fans over {p, q} → each runs leaf_sub.yaml

        The outer fan-out acquires branch worktrees (ForemanExecutor +
        worktree:isolated → ``use_branch = True``).  The outer subgraph is
        compiled against a ``_BranchScopedExecutor`` that deliberately omits
        ``acquire_branch_worktree``.  Therefore inside the middle subgraph,
        ``make_fan_out_subgraph_invoker`` sees ``hasattr(executor,
        "acquire_branch_worktree") == False`` and falls through to
        ``use_branch = False``, running inner branches in the outer branch tree
        rather than spinning up their own trees.

        After the run:
        - Exactly 2 outer branch trees (``wt-outer_pool__x-*`` and
          ``wt-outer_pool__y-*``).
        - No inner-level trees (no ``wt-inner_pool__*``) and no leaf-level
          trees (no ``wt-leaf_writer-*``).
        - leaf.txt written by an inner branch exists inside at least one outer
          branch tree, confirming the inner writes landed there.

        Would fail if: ``_BranchScopedExecutor`` started forwarding
        ``acquire_branch_worktree`` to the underlying foreman, allowing nested
        fan-out branches to spin up their own trees.
        """
        _init_git_repo(tmp_path)
        leaf_yaml = _write_leaf_sub_yaml(tmp_path)
        middle_yaml = _write_middle_sub_yaml(tmp_path, leaf_yaml)
        config = _build_nested_fanout_config(tmp_path, middle_yaml)

        foreman, _inner = _make_foreman(tmp_path)
        graph = build_workflow_graph(config, foreman, _base_dir=tmp_path)
        state = make_initial_state(workdir=str(tmp_path))
        final_state = await graph.ainvoke(state)

        # Both outer branches must have completed.
        for outer_id in ("x", "y"):
            child_id = f"{_OUTER_ID}::{outer_id}"
            assert child_id in final_state["completed_nodes"], (
                f"{child_id} not in completed_nodes: {final_state['completed_nodes']}"
            )

        sqrlly_dir = tmp_path / ".sqrlly"

        # Exactly 2 outer branch trees.
        outer_trees = list(sqrlly_dir.glob(f"wt-{_OUTER_ID}__*"))
        assert len(outer_trees) == 2, (
            f"Expected 2 outer branch trees (wt-{_OUTER_ID}__*), "
            f"got {len(outer_trees)}: {[d.name for d in outer_trees]}"
        )

        # No inner-pool trees — nested branches must NOT spin up their own trees.
        inner_trees = list(sqrlly_dir.glob(f"wt-{_INNER_ID}__*"))
        assert inner_trees == [], (
            f"Expected no wt-{_INNER_ID}__* trees (nested branches share outer "
            f"branch tree), found: {[d.name for d in inner_trees]}"
        )

        # No leaf-writer trees — leaf nodes inside nested branches must also
        # not create their own isolation trees.
        leaf_trees = list(sqrlly_dir.glob("wt-leaf_writer-*"))
        assert leaf_trees == [], (
            f"Expected no wt-leaf_writer-* trees, found: "
            f"{[d.name for d in leaf_trees]}"
        )

        # leaf.txt must exist inside at least one outer branch tree, confirming
        # that inner branch writes landed in the outer branch tree, not elsewhere.
        leaf_found = any((t / "leaf.txt").exists() for t in outer_trees)
        assert leaf_found, (
            f"Expected leaf.txt inside at least one outer branch tree "
            f"({[d.name for d in outer_trees]}), but found none — inner branches "
            "may have run in the wrong working directory"
        )
