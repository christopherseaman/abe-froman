"""End-to-end test for the wave-driven dynamic-task pattern.

Pattern under test:

    planner -> dispatcher (fan_out) -> workers (Send x N)
                    ^                       |
                    |                       v
                    +-- gate (route) -- reconcile

Each "wave" is one trip around dispatcher -> workers -> reconcile ->
gate. A wave that uncovers new pending tasks routes back to dispatcher;
a wave that uncovers nothing routes to __end__.

The example at ``examples/wave_planner/`` is the documentation surface;
this test exercises the same pattern programmatically with deterministic
Python scripts written into a tmp_path. Two assertions matter:

  1. ``dispatcher`` appears in ``completed_nodes`` exactly twice (one
     fire per wave; the goto-driven re-fire MUST execute the body).
  2. The dynamically-discovered child (``q_competitor_share``, added
     by reconcile during wave 1) lands in ``completed_nodes`` as a
     fan-out branch of dispatcher's second wave.

Both assertions would fail if the resume-mode "skip already-completed
nodes" guards were re-added to ``compile/nodes.py`` and
``compile/dynamic.py`` — the regression those guards' removal prevents.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph


_PYTHON = sys.executable


_PLANNER_SRC = """
import json
from pathlib import Path
state = {"questions": {
    "q_alpha": {"id": "q_alpha", "status": "pending"},
    "q_beta": {"id": "q_beta", "status": "pending"},
}}
Path("state.json").write_text(json.dumps(state))
print("planner_seeded")
"""

_DISPATCHER_SRC = """
import json
from pathlib import Path
state = json.loads(Path("state.json").read_text())
pending = [q for q in state["questions"].values() if q["status"] == "pending"]
print(json.dumps({"items": pending}))
"""

_WORKER_SRC = """
# fcntl.flock serializes parallel workers' read-modify-write on
# state.json — without it, the test was a latent flake under
# pytest-randomly's reordering.
import fcntl, json, sys
from pathlib import Path
qid = sys.argv[1]
with Path("state.json").open("r+") as fh:
    fcntl.flock(fh, fcntl.LOCK_EX)
    state = json.load(fh)
    state["questions"][qid]["status"] = "done"
    fh.seek(0)
    json.dump(state, fh)
    fh.truncate()
print(f"worker_done:{qid}")
"""

_RECONCILE_SRC = """
import json
from pathlib import Path
p = Path("state.json")
state = json.loads(p.read_text())
qs = state["questions"]
seeds = {"q_alpha", "q_beta"}
seeds_done = all(qs[i]["status"] == "done" for i in seeds if i in qs)
follow_up = "q_gamma"
if seeds_done and follow_up not in qs:
    qs[follow_up] = {"id": follow_up, "status": "pending"}
    p.write_text(json.dumps(state))
    print(f"reconcile_added:{follow_up}")
else:
    print("reconcile_clean")
"""


def _write_scripts(tmp_path: Path) -> dict[str, Path]:
    paths = {
        "planner.py": _PLANNER_SRC,
        "dispatcher.py": _DISPATCHER_SRC,
        "worker.py": _WORKER_SRC,
        "reconcile.py": _RECONCILE_SRC,
    }
    out: dict[str, Path] = {}
    for name, body in paths.items():
        path = tmp_path / name
        path.write_text(body)
        out[name] = path
    return out


def _build_wave_config(scripts: dict[str, Path]) -> Graph:
    """Programmatic Graph mirroring examples/wave_planner/workflow.yaml."""
    return Graph(
        name="wave-pattern-test",
        version="0.1.0",
        nodes=[
            {
                "id": "planner",
                "name": "planner",
                "execute": {
                    "url": _PYTHON,
                    "params": {"args": [str(scripts["planner.py"])]},
                },
            },
            {
                "id": "dispatcher",
                "name": "dispatcher",
                "depends_on": ["planner"],
                "execute": {
                    "url": _PYTHON,
                    "params": {"args": [str(scripts["dispatcher.py"])]},
                },
                "fan_out": {
                    "template": {
                        "execute": {
                            "url": _PYTHON,
                            "params": {
                                "args": [str(scripts["worker.py"]), "{{id}}"],
                            },
                        },
                    },
                },
            },
            {
                "id": "reconcile",
                "name": "reconcile",
                "depends_on": ["dispatcher"],
                "execute": {
                    "url": _PYTHON,
                    "params": {"args": [str(scripts["reconcile.py"])]},
                },
            },
            {
                "id": "gate",
                "name": "gate",
                "depends_on": ["reconcile"],
                "route": {
                    "cases": [
                        {
                            "when": "'reconcile_added' in reconcile",
                            "goto": "dispatcher",
                        },
                    ],
                    "else": "__end__",
                },
            },
        ],
    )


class TestWavePattern:
    @pytest.mark.asyncio
    async def test_two_waves_then_exit(self, tmp_path):
        """Two waves, dynamically-discovered child runs in wave 2, then exit."""
        scripts = _write_scripts(tmp_path)
        config = _build_wave_config(scripts)
        executor = DispatchExecutor(workdir=str(tmp_path))
        graph = build_workflow_graph(config, executor)
        result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

        completed = result["completed_nodes"]

        # Wave 1: planner + dispatcher + 2 workers + reconcile.
        assert "planner" in completed
        assert "dispatcher::q_alpha" in completed
        assert "dispatcher::q_beta" in completed

        # Wave 2: dispatcher re-fires (its body re-executes, manifest
        # re-emitted from updated state.json), the dynamically-added
        # q_gamma worker runs, reconcile clears.
        assert "dispatcher::q_gamma" in completed, (
            "Dynamically-discovered child (added by reconcile during wave 1) "
            "must run as a fan-out branch of wave 2's dispatcher. If this "
            "assertion fails, the goto-driven re-fire of dispatcher is being "
            "suppressed — likely a resume-mode guard regression."
        )

        # Direct "fired N times" is no longer expressible in state — the
        # set-union reducer dedupes the writes. The q_gamma assertion
        # above is the load-bearing regression check: q_gamma is only
        # in wave 2's manifest, so its presence in completed_nodes IS
        # proof that dispatcher re-fired. If a "skip already-completed"
        # guard re-emerges, q_gamma never dispatches and the assertion
        # above fails. To pin exact fire counts, run with `--log` and
        # count `node_completed` events (events fire per super-step,
        # not deduped).

        # Final state on disk: all three questions answered.
        final = json.loads((tmp_path / "state.json").read_text())
        assert all(
            q["status"] == "done" for q in final["questions"].values()
        )
        assert set(final["questions"].keys()) == {
            "q_alpha", "q_beta", "q_gamma",
        }

    def test_shipped_workflow_yaml_validates(self):
        """The shipped examples/wave_planner/workflow.yaml parses + compiles.

        Sanity check that the documentation surface stays in sync with
        the schema. Doesn't run — execution is covered by the
        programmatic test above. Relative URLs in the yaml resolve at
        run time against the workdir, so this test only validates
        Pydantic + LangGraph compile.
        """
        repo_root = Path(__file__).resolve().parents[2]
        yaml_path = repo_root / "examples" / "wave_planner" / "workflow.yaml"
        config = Graph(**yaml.safe_load(yaml_path.read_text()))
        assert config.name == "Wave-driven research planner"
        assert {n.id for n in config.nodes} == {
            "planner", "dispatcher", "reconcile", "gate",
        }
        # Compile succeeds (DispatchExecutor stand-in — we don't run).
        build_workflow_graph(config, DispatchExecutor(workdir=str(repo_root)))
