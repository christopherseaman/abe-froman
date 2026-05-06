"""Wave-driven dynamic-task spike.

Goal: prove (or disprove) that current Stage 5b/5c primitives — fan_out
+ inline route + existing reducers — are sufficient to express a
wave-driven loop where new tasks discovered mid-run get picked up in
the next wave without restarting upstream work.

The shape under test (separate entry + loop point):

    planner ─→ dispatcher (fan_out) ─→ workers (Send×N)
                    ▲                       │
                    │                       ▼
                    └── gate (route) ←── reconcile

  * planner runs once at START, seeds initial tasks state.
  * dispatcher is the fan_out parent AND the goto target of gate's
    loop-back. Because dispatcher has a depends_on edge from planner,
    abe-froman happily wires both START → planner and the goto edge,
    no self-loop required.
  * Each wave: dispatcher reads state.json, fans out over ready tasks;
    workers mark tasks done; reconcile may add new tasks; gate decides
    loop or end.

Critical questions:

  Q1. Does `goto: dispatcher` re-execute dispatcher (regenerate its
      manifest output) AND re-fire its fan-out conditional edge with
      the post-reconcile state visible to the manifest read?

  Q2. Do the reducers compose correctly across waves (no trampling)?

If both pass: existing primitives suffice; the BUILDER-REQUESTS
"likely needed: custom reducer / verify fan_out re-read" is closed
without any new abe-froman code.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.state import make_initial_state
from abe_froman.schema.models import (
    Execute,
    FanOut,
    FanOutTemplate,
    Graph,
    Node,
    Route,
    RouteCase,
    RouteElse,
)


_PYTHON = sys.executable


def make_workflow(workdir: Path) -> Graph:
    # planner: writes initial tasks to state.json
    planner_script = workdir / "planner.py"
    planner_script.write_text(
        f"""
import json
from pathlib import Path
state = {{"tasks": {{
    "task_a": {{"id": "task_a", "status": "pending"}},
    "task_b": {{"id": "task_b", "status": "pending"}},
}}}}
Path({str(workdir / "state.json")!r}).write_text(json.dumps(state, indent=2))
print("planner_seeded:2")
"""
    )

    # dispatcher: reads state.json, emits manifest of currently-ready tasks
    dispatcher_script = workdir / "dispatcher.py"
    dispatcher_script.write_text(
        f"""
import json
from pathlib import Path
state = json.loads(Path({str(workdir / "state.json")!r}).read_text())
ready = [t for t in state["tasks"].values() if t["status"] == "pending"]
print(json.dumps({{"items": ready}}))
"""
    )

    # worker: marks one task done
    worker_script = workdir / "worker.py"
    worker_script.write_text(
        f"""
import json, sys
from pathlib import Path
task_id = sys.argv[1]
state_path = Path({str(workdir / "state.json")!r})
state = json.loads(state_path.read_text())
state["tasks"][task_id]["status"] = "done"
state_path.write_text(json.dumps(state, indent=2))
print(f"worker_done:{{task_id}}")
"""
    )

    # reconcile: simulate dynamic discovery — wave 1 (only A & B exist)
    # adds task_c. Wave 2 (after C is processed) does nothing.
    reconcile_script = workdir / "reconcile.py"
    reconcile_script.write_text(
        f"""
import json
from pathlib import Path
state_path = Path({str(workdir / "state.json")!r})
state = json.loads(state_path.read_text())
existing = set(state["tasks"].keys())
if existing == {{"task_a", "task_b"}}:
    state["tasks"]["task_c"] = {{"id": "task_c", "status": "pending"}}
    state_path.write_text(json.dumps(state, indent=2))
    print("reconcile_added:task_c")
else:
    print("reconcile_clean")
"""
    )

    return Graph(
        name="wave-spike",
        version="0.1.0",
        nodes=[
            Node(
                id="planner",
                name="Planner",
                execute=Execute(
                    url=_PYTHON, params={"args": [str(planner_script)]},
                ),
            ),
            Node(
                id="dispatcher",
                name="Dispatcher",
                depends_on=["planner"],
                execute=Execute(
                    url=_PYTHON, params={"args": [str(dispatcher_script)]},
                ),
                fan_out=FanOut(
                    enabled=True,
                    template=FanOutTemplate(
                        execute=Execute(
                            url=_PYTHON,
                            params={"args": [str(worker_script), "{{id}}"]},
                        ),
                    ),
                ),
            ),
            Node(
                id="reconcile",
                name="Reconcile",
                depends_on=["dispatcher"],
                execute=Execute(
                    url=_PYTHON, params={"args": [str(reconcile_script)]},
                ),
            ),
            Node(
                id="gate",
                name="Gate",
                depends_on=["reconcile"],
                route=Route(
                    cases=[
                        RouteCase(
                            when="'reconcile_added' in reconcile",
                            goto="dispatcher",
                        ),
                    ],
                    **{"else": RouteElse(goto="__end__")},
                ),
            ),
        ],
    )


async def main() -> None:
    workdir = Path(".temp/wave-spike-run").resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    # Clean residue from prior runs.
    if (workdir / "state.json").exists():
        (workdir / "state.json").unlink()

    config = make_workflow(workdir)
    executor = DispatchExecutor(workdir=str(workdir))
    graph = build_workflow_graph(config, executor=executor)

    print("=== TOPOLOGY ===")
    print(graph.get_graph().draw_mermaid())

    initial = make_initial_state(workdir=str(workdir))
    print("\n=== STREAMING UPDATES (limit=20 super-steps) ===")
    async for chunk_type, payload in graph.astream(
        initial, stream_mode=["updates", "values"],
        config={"recursion_limit": 20},
    ):
        if chunk_type == "updates":
            for name, update in payload.items():
                if update is None:
                    print(f"  [{name}] (Command-only / no state update)")
                    continue
                keys = sorted(k for k in update.keys() if not k.startswith("_"))
                print(f"  [{name}] keys={keys}")
                if "node_outputs" in update:
                    for k, v in update["node_outputs"].items():
                        print(f"      {k}: {v!r}")
                if "errors" in update and update["errors"]:
                    print(f"      ERRORS: {update['errors']!r}")
    print("\n=== state.json after run ===")
    if (workdir / "state.json").exists():
        print((workdir / "state.json").read_text())
    else:
        print("(state.json never written)")


if __name__ == "__main__":
    asyncio.run(main())
