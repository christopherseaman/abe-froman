"""Gate-only phase validates upstream content via DEPS_JSON env var.

End-to-end exercise of the WISHLIST 216 bug fix: today a gate-only
phase (a node with `evaluation:` and no `execute:`) has no useful
signal because validators only see the node's own stub output. After
the fix, validators read upstream node outputs from `DEPS_JSON`.

The replacement is for the existing workaround in
`examples/absurd-paper/gates/submission_check.py` which reads files
off disk via `$WORKDIR`. The new path doesn't need pre-persistence.
"""
from __future__ import annotations

import sys

import pytest

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.state import make_initial_state

from helpers import make_config


_PYTHON = sys.executable


_VALIDATOR = """\
import json, os, sys

# Read upstream content from DEPS_JSON.
deps = json.loads(os.environ["DEPS_JSON"])
upstream = deps.get("produce", "")

# Pass the gate iff the upstream content carries our marker.
if "MARKER:wired-up" in upstream:
    print("1.0")
else:
    print("0.0")
"""


class TestGateOnlyPhaseSeesUpstream:
    @pytest.mark.asyncio
    async def test_gate_only_phase_validates_upstream_via_DEPS_JSON(
        self, tmp_path,
    ):
        """`produce` writes a marker string; `check` is gate-only and
        validates it via DEPS_JSON without touching disk."""
        validator = tmp_path / "validate.py"
        validator.write_text(_VALIDATOR)

        config = make_config([
            {
                "id": "produce",
                "name": "Produce",
                "execute": {
                    "url": "/usr/bin/echo",
                    "params": {"args": ["-n", "MARKER:wired-up content here"]},
                },
            },
            {
                "id": "check",
                "name": "Check upstream content",
                "depends_on": ["produce"],
                # Gate-only: `evaluation:` but no `execute:`. The gate
                # validator must read upstream via DEPS_JSON.
                "evaluation": {
                    "validator": str(validator),
                    "threshold": 1.0,
                    "blocking": True,
                },
            },
        ])

        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(
            make_initial_state(workdir=str(tmp_path)),
        )

        # Both nodes complete; the gate validator passed because it
        # could see the upstream content.
        assert "produce" in result["completed_nodes"]
        assert "check" in result["completed_nodes"]
        assert "check" not in result["failed_nodes"]
