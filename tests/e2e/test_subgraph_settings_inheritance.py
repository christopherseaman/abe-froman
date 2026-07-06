"""End-to-end subgraph settings inheritance (Phase 3 / scope-aware).

Two artifact-driven layers:

  1. **MockExecutor capture** — fast unit-style e2e proving each node
     receives the scope's *effective* Settings. Asserts the merged
     fields directly via ``MockExecutor.received_settings[node_id]``,
     covering parent vs subgraph vs nested-subgraph and the
     "child-explicitly-set wins, otherwise parent wins" merge rule.

  2. **Real-subprocess timeout** — uses ``sleep`` scripts to artifact-
     prove that ``default_timeout`` flowing from a subgraph YAML
     actually reaches the executor. Reverse polarity (no override
     → parent's timeout wins) proves silent-loss is gone.

These tests are the "did we actually wire it" gate for Phase 3 — the
seam threads merged settings from compile-time through the wrapper,
into ``executor.execute(settings_override=)``, into every read site.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph
from mock_executor import MockExecutor

_ECHO = shutil.which("echo") or "/bin/echo"


def _yaml(path: Path, body: dict) -> None:
    path.write_text(yaml.safe_dump(body))


# ---------------------------------------------------------------------
# Scope capture via MockExecutor (Phase 3 receives_settings field)
# ---------------------------------------------------------------------

class TestScopeCapture:
    """Each node should see its scope's effective Settings — parent for
    top-level nodes, merged child-on-parent for subgraph nodes."""

    async def test_parent_node_sees_parent_settings(self, tmp_path):
        _yaml(tmp_path / "wf.yaml", {
            "name": "wf", "version": "1.0",
            "nodes": [
                {"id": "p", "name": "Parent",
                 "execute": {"url": _ECHO, "params": {"args": ["-n", "x"]}}},
            ],
            "settings": {"preamble_file": "parent-out", "max_retries": 7},
        })
        config = Graph(**yaml.safe_load((tmp_path / "wf.yaml").read_text()))
        executor = MockExecutor()
        compiled = build_workflow_graph(config, executor=executor, _base_dir=tmp_path)

        await compiled.ainvoke(make_initial_state(
            workflow_name="wf", workdir=str(tmp_path), dry_run=False,
        ))

        seen = executor.received_settings["p"]
        assert seen is not None
        assert seen.preamble_file == "parent-out"
        assert seen.max_retries == 7

    async def test_subgraph_node_sees_merged_settings(self, tmp_path):
        """Parent preamble_file=parent-out; subgraph preamble_file=sub-out.
        Parent node sees parent's; subgraph node sees subgraph's."""
        _yaml(tmp_path / "sub.yaml", {
            "name": "sub", "version": "1.0",
            "nodes": [
                {"id": "child", "name": "Child",
                 "execute": {"url": _ECHO, "params": {"args": ["-n", "y"]}}},
            ],
            "settings": {"preamble_file": "sub-out"},
        })
        _yaml(tmp_path / "wf.yaml", {
            "name": "wf", "version": "1.0",
            "nodes": [
                {"id": "top", "name": "Top",
                 "execute": {"url": _ECHO, "params": {"args": ["-n", "x"]}}},
                {"id": "sub_ref", "name": "SubRef",
                 "depends_on": ["top"],
                 "execute": {"url": "sub.yaml"}},
            ],
            "settings": {"preamble_file": "parent-out", "max_retries": 4},
        })
        config = Graph(**yaml.safe_load((tmp_path / "wf.yaml").read_text()))
        executor = MockExecutor()
        compiled = build_workflow_graph(config, executor=executor, _base_dir=tmp_path)

        await compiled.ainvoke(make_initial_state(
            workflow_name="wf", workdir=str(tmp_path), dry_run=False,
        ))

        top_seen = executor.received_settings["top"]
        child_seen = executor.received_settings["child"]
        assert top_seen is not None and child_seen is not None
        assert top_seen.preamble_file == "parent-out"   # parent scope
        assert child_seen.preamble_file == "sub-out"    # subgraph wins
        # Subgraph inherited parent's max_retries (didn't author it).
        assert child_seen.max_retries == 4

    async def test_subgraph_inherits_when_unset(self, tmp_path):
        """Subgraph YAML with NO settings: block inherits everything
        from parent — the silent-loss case Phase 3 fixes."""
        _yaml(tmp_path / "sub.yaml", {
            "name": "sub", "version": "1.0",
            "nodes": [
                {"id": "child", "name": "Child",
                 "execute": {"url": _ECHO, "params": {"args": ["-n", "y"]}}},
            ],
        })
        _yaml(tmp_path / "wf.yaml", {
            "name": "wf", "version": "1.0",
            "nodes": [
                {"id": "sub_ref", "name": "SubRef",
                 "execute": {"url": "sub.yaml"}},
            ],
            "settings": {"default_timeout": 60.0, "preamble_file": "x.md"},
        })
        config = Graph(**yaml.safe_load((tmp_path / "wf.yaml").read_text()))
        executor = MockExecutor()
        compiled = build_workflow_graph(config, executor=executor, _base_dir=tmp_path)

        await compiled.ainvoke(make_initial_state(
            workflow_name="wf", workdir=str(tmp_path), dry_run=False,
        ))

        child_seen = executor.received_settings["child"]
        assert child_seen.default_timeout == 60.0
        assert child_seen.preamble_file == "x.md"

    async def test_three_level_inheritance(self, tmp_path):
        """top → mid → bot. Each layer can override; merges compose."""
        _yaml(tmp_path / "bot.yaml", {
            "name": "bot", "version": "1.0",
            "nodes": [{"id": "leaf", "name": "Leaf",
                       "execute": {"url": _ECHO, "params": {"args": ["-n", "z"]}}}],
            "settings": {"max_retries": 9},
        })
        _yaml(tmp_path / "mid.yaml", {
            "name": "mid", "version": "1.0",
            "nodes": [{"id": "ref_bot", "name": "Mid",
                       "execute": {"url": "bot.yaml"}}],
            "settings": {"preamble_file": "mid-out"},
        })
        _yaml(tmp_path / "wf.yaml", {
            "name": "wf", "version": "1.0",
            "nodes": [{"id": "ref_mid", "name": "Top",
                       "execute": {"url": "mid.yaml"}}],
            "settings": {
                "preamble_file": "top-out", "max_retries": 2,
                "default_timeout": 60.0,
            },
        })
        config = Graph(**yaml.safe_load((tmp_path / "wf.yaml").read_text()))
        executor = MockExecutor()
        compiled = build_workflow_graph(config, executor=executor, _base_dir=tmp_path)

        await compiled.ainvoke(make_initial_state(
            workflow_name="wf", workdir=str(tmp_path), dry_run=False,
        ))

        leaf = executor.received_settings["leaf"]
        # top→mid won preamble_file=mid-out, mid→bot kept it; bot won max_retries=9.
        # default_timeout flowed top→mid→bot untouched.
        assert leaf.preamble_file == "mid-out"
        assert leaf.max_retries == 9
        assert leaf.default_timeout == 60.0


# ---------------------------------------------------------------------
# Real-subprocess timeout — artifact gate for inheritance reaching the
# runtime, not just the schema.
# ---------------------------------------------------------------------

class TestSubprocessTimeoutInheritance:
    """A subgraph that overrides ``default_timeout`` must apply that
    timeout to its own nodes — not silently fall through to parent's."""

    async def test_subgraph_timeout_override_lets_long_node_pass(self, tmp_path):
        """Parent timeout=1s would kill ``sleep 1.5``. Subgraph
        timeout=10s lets it complete. Proves the override reaches the
        runtime executor."""
        sleep_script = tmp_path / "sleep15.sh"
        sleep_script.write_text("#!/bin/bash\nsleep 1.5\necho slept-15\n")
        sleep_script.chmod(0o755)

        _yaml(tmp_path / "sub.yaml", {
            "name": "sub", "version": "1.0",
            "nodes": [
                {"id": "slow", "name": "Slow",
                 "execute": {"url": str(sleep_script)}},
            ],
            "settings": {"default_timeout": 10.0},
        })
        _yaml(tmp_path / "wf.yaml", {
            "name": "wf", "version": "1.0",
            "nodes": [
                {"id": "ref", "name": "Ref",
                 "execute": {"url": "sub.yaml"}},
            ],
            "settings": {"default_timeout": 1.0},  # would kill the sleep
        })

        config = Graph(**yaml.safe_load((tmp_path / "wf.yaml").read_text()))
        executor = DispatchExecutor(workdir=str(tmp_path), settings=config.settings)
        compiled = build_workflow_graph(config, executor=executor, _base_dir=tmp_path)

        result = await compiled.ainvoke(make_initial_state(
            workflow_name="wf", workdir=str(tmp_path), dry_run=False,
        ))

        # Subgraph nodes don't surface to the parent's completed_nodes —
        # the wrapper records its parent ref id with the subgraph's
        # terminal output. So check the parent ref completed cleanly and
        # the long sleep's output flowed up.
        assert "ref" in result.get("completed_nodes", []), (
            f"Subgraph timeout override didn't apply. State: {result}"
        )
        assert "slept-15" in result.get("node_outputs", {}).get("ref", "")

    async def test_subgraph_inherits_parent_timeout_when_unset(self, tmp_path):
        """Reverse polarity: subgraph doesn't override; parent's 1s wins;
        ``sleep 2`` times out. Proves inheritance still flows downward."""
        sleep_script = tmp_path / "sleep2.sh"
        sleep_script.write_text("#!/bin/bash\nsleep 2\necho slept-2\n")
        sleep_script.chmod(0o755)

        _yaml(tmp_path / "sub.yaml", {
            "name": "sub", "version": "1.0",
            "nodes": [
                {"id": "slow", "name": "Slow",
                 "execute": {"url": str(sleep_script)}},
            ],
            # NO default_timeout — must inherit parent's 1.0
        })
        _yaml(tmp_path / "wf.yaml", {
            "name": "wf", "version": "1.0",
            "nodes": [
                {"id": "ref", "name": "Ref",
                 "execute": {"url": "sub.yaml"}},
            ],
            "settings": {"default_timeout": 1.0},
        })

        config = Graph(**yaml.safe_load((tmp_path / "wf.yaml").read_text()))
        executor = DispatchExecutor(workdir=str(tmp_path), settings=config.settings)
        compiled = build_workflow_graph(config, executor=executor, _base_dir=tmp_path)

        result = await compiled.ainvoke(make_initial_state(
            workflow_name="wf", workdir=str(tmp_path), dry_run=False,
        ))

        # The sub-node failed (timed out) — surfaces as failed_nodes
        # under the subgraph reference's parent id.
        failed = result.get("failed_nodes", [])
        assert "ref" in failed or "slow" in failed, (
            f"Expected timeout to fail the subgraph; state: {result}"
        )
