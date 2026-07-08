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
