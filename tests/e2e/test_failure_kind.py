"""End-to-end: every failure path a node can take emits the right structured
`kind` on its `node_failed` JSONL event, and `workflow_end` carries the
failed-id→kind map.

Real subprocesses (no mocks): a script that exits non-zero (`node_error`), a
node blocked by a failed dependency (`upstream_failed`), a blocking script
gate scored below threshold (`gate_failure`), and a node that overruns its
timeout (`timeout`). The overload/backend_error kinds are LLM-backend paths
covered at the unit level (tests/unit/runtime/test_prompt.py); here we pin the
four script-reachable kinds through the full graph → event boundary.
"""
from __future__ import annotations

import json
import sys
from io import StringIO
from pathlib import Path

import pytest

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.logging import JsonlLogger
from sqrlly.runtime.runner import run_workflow
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph

_PYTHON = sys.executable


def _script(workdir: Path, name: str, body: str) -> str:
    p = workdir / name
    p.write_text(body)
    return str(p)


def _build(workdir: Path) -> Graph:
    fail = _script(workdir, "fail.py", "import sys; sys.exit(1)\n")
    ok = _script(workdir, "ok.py", "print('ok')\n")
    slow = _script(workdir, "slow.py", "import time; time.sleep(3)\n")
    # Blocking validator that always scores 0.0 (< threshold) → gate_failure.
    validator = _script(
        workdir, "val.py",
        "import sys; sys.stdin.read(); print('0.0')\n",
    )
    return Graph(
        name="failure-kinds", version="0.1.0",
        nodes=[
            {"id": "bad", "name": "bad",
             "execute": {"url": _PYTHON, "params": {"args": [fail]}}},
            {"id": "blocked", "name": "blocked", "depends_on": ["bad"],
             "execute": {"url": _PYTHON, "params": {"args": [ok]}}},
            {"id": "gate_fail", "name": "gate_fail",
             "execute": {"url": _PYTHON, "params": {"args": [ok]}},
             "evaluation": {"validator": validator, "threshold": 0.9,
                            "blocking": True, "max_retries": 0}},
            {"id": "slow", "name": "slow", "timeout": 1.0,
             "execute": {"url": _PYTHON, "params": {"args": [slow]}}},
        ],
    )


@pytest.mark.asyncio
async def test_node_failed_events_carry_the_right_kind(tmp_path):
    config = _build(tmp_path)
    buf = StringIO()
    logger = JsonlLogger(buf)
    compiled = build_workflow_graph(
        config, DispatchExecutor(workdir=str(tmp_path)), checkpointer=None,
    )
    state = make_initial_state(workdir=str(tmp_path), dry_run=False)
    result = await run_workflow(compiled, state, config, logger=logger)

    events = [json.loads(l) for l in buf.getvalue().strip().split("\n") if l]
    kinds = {
        e["node"]: e["kind"]
        for e in events if e.get("event") == "node_failed"
    }
    assert kinds.get("bad") == "node_error"            # script exit 1
    assert kinds.get("blocked") == "upstream_failed"   # dep 'bad' failed
    assert kinds.get("gate_fail") == "gate_failure"    # blocking eval < 0.9
    assert kinds.get("slow") == "timeout"              # overran node timeout

    # Every failed node has a kind; none defaulted silently to the fallback
    # for a site we claim to classify.
    assert all("kind" in e for e in events if e.get("event") == "node_failed")
    assert set(result["failed_nodes"]) >= {"bad", "blocked", "gate_fail", "slow"}


@pytest.mark.asyncio
async def test_infra_abort_settles_as_node_failed_not_traceback(tmp_path):
    """A worktree-setup infra abort (a setup command exiting non-zero) settles
    the graph as a `node_failed` with `kind: infra` — the run RETURNS with the
    node in `failed_nodes` — instead of escaping as a raw traceback (which would
    leave `workflow_end` at 0/0). Proves the catch-and-settle (no reraise)
    constraint that makes the halt distinguishable from a clean run."""
    from sqrlly.runtime.foreman import ForemanExecutor
    from helpers import init_git_repo

    init_git_repo(tmp_path, files={"f": "x"})
    ok = _script(tmp_path, "ok.py", "print('ok')\n")
    config = Graph(
        name="infra-kind", version="0.1.0",
        nodes=[{"id": "build", "name": "build",
                "execute": {"url": _PYTHON, "params": {"args": [ok]}}}],
        settings={"worktree": "isolated", "worktree_setup": ["sh -c 'exit 7'"]},
    )
    buf = StringIO()
    logger = JsonlLogger(buf)
    foreman = ForemanExecutor(
        DispatchExecutor(workdir=str(tmp_path)), str(tmp_path),
        settings=config.settings,
    )
    compiled = build_workflow_graph(config, foreman, checkpointer=None)
    state = make_initial_state(workdir=str(tmp_path), dry_run=False)
    result = await run_workflow(compiled, state, config, logger=logger)

    # The run SETTLED (returned a result) — not a traceback.
    assert "build" in result["failed_nodes"]
    events = [json.loads(l) for l in buf.getvalue().strip().split("\n") if l]
    assert any(
        e.get("event") == "node_failed" and e["node"] == "build"
        and e["kind"] == "infra"
        for e in events
    ), events


@pytest.mark.asyncio
async def test_subgraph_fanout_child_infra_abort_settles(tmp_path):
    """An ISOLATED subgraph-template fan-out BRANCH whose worktree acquisition
    aborts (worktree_setup exits non-zero) settles as that child's
    node_failed(kind=infra). The branch worktree is acquired directly in the
    subgraph invoker (not through ForemanExecutor.execute), so this pins that
    the invoker catches the abort and the run RETURNS instead of escaping as a
    raw traceback (0/0). The parent runs worktree:off so only the isolated
    children hit the failing setup."""
    import yaml
    from sqrlly.runtime.foreman import ForemanExecutor
    from helpers import init_git_repo

    init_git_repo(tmp_path, files={"f": "x"})
    ok = _script(tmp_path, "ok.py", "print('ok')\n")
    (tmp_path / "member.yaml").write_text(yaml.safe_dump({
        "name": "member", "version": "1.0",
        "nodes": [{"id": "step", "name": "step",
                   "execute": {"url": _PYTHON, "params": {"args": [ok]}}}],
    }))
    manifest = json.dumps({"items": [{"id": "alpha"}, {"id": "beta"}]})
    config = Graph(
        name="sg-fanout-infra", version="0.1.0",
        nodes=[{
            "id": "cfan", "name": "cfan",
            "execute": {"url": "/bin/sh",
                        "params": {"args": ["-c", f"printf '%s' '{manifest}'"]}},
            "fan_out": {
                "template": {
                    "execute": {"url": "member.yaml",
                                "params": {"inputs": {"x": "{{id}}"}}},
                    "worktree": "isolated",
                },
            },
        }],
        settings={"worktree": "off", "worktree_setup": ["sh -c 'exit 1'"]},
    )
    buf = StringIO()
    logger = JsonlLogger(buf)
    foreman = ForemanExecutor(
        DispatchExecutor(workdir=str(tmp_path)), str(tmp_path),
        settings=config.settings,
    )
    compiled = build_workflow_graph(
        config, foreman, _base_dir=tmp_path, checkpointer=None,
    )
    state = make_initial_state(workdir=str(tmp_path), dry_run=False)
    result = await run_workflow(compiled, state, config, logger=logger)

    # The run SETTLED despite the branch worktree abort (no traceback).
    events = [json.loads(l) for l in buf.getvalue().strip().split("\n") if l]
    infra = [
        e for e in events
        if e.get("event") == "node_failed" and e.get("kind") == "infra"
        and str(e.get("node", "")).startswith("cfan::")
    ]
    assert infra, events
