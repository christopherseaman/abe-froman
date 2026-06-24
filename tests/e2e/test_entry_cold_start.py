"""End-to-end: `--entry <node>` cold-start. A 3-node linear workflow a -> b -> c
is run with entry=b from a FRESH workdir (no checkpoint). `a` must NOT execute
(its run-counter stays 0); `b` (reading an on-disk input file the test
pre-creates) and `c` DO run. This mirrors cli/main.py's `elif entry is not
None:` seed branch with a real AsyncSqliteSaver + DispatchExecutor."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.compile.resume import compute_skip_set
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.runner import run_workflow
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph

_PYTHON = sys.executable

# Each node runs this worker keyed by its id. It bumps a per-id run-counter,
# then — for nodes after the first — reads `in_<qid>.txt` (an on-disk input)
# and writes `out_<qid>.txt`. The `a` node would write `out_a.txt`, but with
# --entry b it never runs, so the test pre-creates `in_b.txt` to stand in for
# a's on-disk artifact.
_WORKER_SRC = """
import sys
from pathlib import Path
qid = sys.argv[1]
counter = Path(f"runs_{qid}.txt")
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
infile = Path(f"in_{qid}.txt")
content = infile.read_text() if infile.exists() else f"no-input-for-{qid}"
Path(f"out_{qid}.txt").write_text(content + f"|{qid}")
print(f"ok:{qid}")
"""


def _worker_path(workdir: Path) -> Path:
    p = workdir / "worker.py"
    p.write_text(_WORKER_SRC)
    return p


def _read_runs(workdir: Path, qid: str) -> int:
    p = workdir / f"runs_{qid}.txt"
    return int(p.read_text()) if p.exists() else 0


def _build_chain(workdir: Path) -> Graph:
    """a -> b -> c, each running worker.py keyed by its id. b reads in_b.txt
    (pre-seeded by the test to stand in for a's on-disk artifact, since a
    never runs under --entry b). c reads in_c.txt, also pre-seeded by the
    test — not b's out_b.txt. The two downstream inputs are independent
    on-disk files created before the run."""
    worker = _worker_path(workdir)

    def node(nid, deps=None):
        return {
            "id": nid, "name": nid,
            "depends_on": deps or [],
            "execute": {"url": _PYTHON, "params": {"args": [str(worker), nid]}},
        }

    return Graph(
        name="entry-chain",
        version="0.1.0",
        nodes=[node("a"), node("b", ["a"]), node("c", ["b"])],
    )


async def _run_entry(
    workdir: Path, config: Graph, db_path: str, thread_id: str, *, entry: str,
) -> dict:
    """Mirror cli/main.py's `elif entry is not None:` cold-start branch."""
    async with AsyncSqliteSaver.from_conn_string(db_path) as cp:
        await cp.setup()
        await cp.adelete_thread(thread_id)
        all_ids = {n.id for n in config.nodes}
        skip = compute_skip_set(config, all_ids, set(), {entry})
        state = make_initial_state(workdir=str(workdir), dry_run=False, workflow_name=config.name)
        state["completed_nodes"] = set(skip)
        state["_resume_skip"] = set(skip)
        compiled = build_workflow_graph(
            config, DispatchExecutor(workdir=str(workdir)), checkpointer=cp,
        )
        return await run_workflow(
            compiled, state, config, thread_id=thread_id,
        )


class TestEntryColdStart:
    @pytest.mark.asyncio
    async def test_entry_runs_node_and_downstream_not_upstream(self, tmp_path):
        config = _build_chain(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "entry-cold"

        # Pre-create the ON-DISK input that `a` would normally have produced.
        # With --entry b, `a` never runs, so b must read this file (not a
        # `{{a}}` template var — node_outputs is empty on a cold start).
        (tmp_path / "in_b.txt").write_text("disk-artifact-from-a")
        (tmp_path / "in_c.txt").write_text("disk-artifact-for-c")

        result = await _run_entry(
            tmp_path, config, db_path, thread_id, entry="b",
        )

        # `a` is frozen (upstream of the entry) — it never executed.
        assert _read_runs(tmp_path, "a") == 0, "upstream node a must NOT run"
        assert not (tmp_path / "out_a.txt").exists()

        # `b` (the entry) and `c` (downstream) ran exactly once.
        assert _read_runs(tmp_path, "b") == 1
        assert _read_runs(tmp_path, "c") == 1
        assert "b" in result["completed_nodes"]
        assert "c" in result["completed_nodes"]
        assert result["failed_nodes"] == set()

        # `b` read the on-disk artifact, not an (empty) upstream var.
        assert (tmp_path / "out_b.txt").read_text() == "disk-artifact-from-a|b"

        # The frozen `a` is reseeded into completed_nodes (the skip set), so the
        # join/barrier on `b` saw its upstream as satisfied without running it.
        assert "a" in result["completed_nodes"]

    @pytest.mark.asyncio
    async def test_entry_at_head_runs_all(self, tmp_path):
        """--entry a (the head) freezes nothing → the whole chain runs."""
        config = _build_chain(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "entry-head"
        (tmp_path / "in_a.txt").write_text("seed")
        (tmp_path / "in_b.txt").write_text("seed-b")
        (tmp_path / "in_c.txt").write_text("seed-c")
        result = await _run_entry(
            tmp_path, config, db_path, thread_id, entry="a",
        )
        assert _read_runs(tmp_path, "a") == 1
        assert _read_runs(tmp_path, "b") == 1
        assert _read_runs(tmp_path, "c") == 1
        assert result["completed_nodes"] == {"a", "b", "c"}
