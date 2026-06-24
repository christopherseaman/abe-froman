"""fan_out.promote merges branch worktree deltas back to base.

Git workdir + a subgraph fan-out (2 items) where each branch writes a
distinct tracked file into its branch worktree; with fan_out.promote:true
the deltas land in the base workdir after a clean run. Pure /bin/sh nodes
— no LLM, deterministic. Models tests/e2e/test_subgraph_fanout_worktree.py.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.foreman import ForemanExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph
from sqrlly.runtime.promote import fanout_branch_specs, reconcile_promotions


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.asyncio
async def test_fan_out_promote_merges_branch_deltas(tmp_path):
    base = tmp_path
    _git("init", "-b", "main", ".", cwd=base)
    _git("config", "user.email", "t@t", cwd=base)
    _git("config", "user.name", "t", cwd=base)
    (base / "seed.txt").write_text("seed")
    _git("add", ".", cwd=base); _git("commit", "-qm", "init", cwd=base)

    # A branch worker subgraph: writes out/<item>.txt in its worktree.
    sub = base / "worker.yaml"
    sub.write_text(yaml.safe_dump({
        "name": "worker", "version": "0.1.0",
        "nodes": [{
            "id": "write", "name": "write",
            "execute": {
                "url": "/bin/sh",
                "params": {"args": ["-c", "mkdir -p out && echo {{item}} > out/{{item}}.txt"]},
            },
        }],
        "settings": {},
    }))
    manifest = base / "items.json"
    manifest.write_text(json.dumps([{"id": "a", "item": "a"}, {"id": "b", "item": "b"}]))

    cfg = Graph(**{
        "name": "fp", "version": "0.1.0",
        "nodes": [{
            "id": "build", "name": "build",
            "fan_out": {
                "manifest_path": "items.json",
                "template": {"execute": {"url": "worker.yaml", "params": {"inputs": {"item": "{{item}}"}}}},
                "promote": True,
            },
        }],
        "settings": {"worktree": "isolated"},
    })

    executor = ForemanExecutor(DispatchExecutor(workdir=str(base)), base_workdir=str(base))
    graph = build_workflow_graph(cfg, executor, _base_dir=base)
    result = await graph.ainvoke(make_initial_state(workflow_name="fp", workdir=str(base)))

    # Replicate the CLI promote step the test is proving end-to-end.
    promote_parents = {n.id for n in cfg.nodes if n.fan_out and n.fan_out.promote}
    specs = fanout_branch_specs(promote_parents, result.get("node_worktrees", {}))
    assert len(specs) == 2, f"expected 2 branch specs, got {specs}"
    reconcile_promotions(specs, str(base), cfg.settings.on_promote_conflict, excludes=None)
    await executor.close()

    # Branch deltas are now in the base workdir.
    assert (base / "out" / "a.txt").read_text().strip() == "a"
    assert (base / "out" / "b.txt").read_text().strip() == "b"
