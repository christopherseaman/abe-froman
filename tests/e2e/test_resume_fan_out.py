"""End-to-end resume-from-checkpoint test using a real ``AsyncSqliteSaver``.

Two-phase scenario:
  - Phase 1: linear chain a -> b -> c. ``b`` fails (env-injected),
    so ``c`` is gated and never fires. Phase ends with
    ``completed_nodes == ["a"]`` and ``failed_nodes == ["b", "c"]``
    (``c`` is marked failed because dep ``b`` failed).
  - Phase 2: clear the failure marker, resume from the same
    checkpoint. The previously-failed ``b`` now succeeds; ``c`` runs
    for the first time.

A side-channel runs-counter file per node lets us assert exactly how
many times each body executed across both phases. This is the
property no existing resume test pins down.

DOCUMENTED CURRENT BEHAVIOR: on ``--resume``, sqrlly re-executes
already-completed nodes. ``a`` runs twice (once in phase 1, once again
in phase 2). The ``_merge_sets`` reducer dedupes the resulting state
write, but the body still re-executes — the runs-counter pins it.
This is correct for goto-driven re-fires within a single run (audit
fix #19 deliberately enabled it for the wave pattern) but wrong for
the canonical cross-run resume use case (retry only the failed node).
The semantics are underspecified; see TODO item (26) for three
candidate API shapes. This test pins actual behavior so unintended
changes surface as failures.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.runner import run_workflow
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph


_PYTHON = sys.executable

# Worker increments a per-node run-counter file, then optionally
# fails based on a marker file. No env-var indirection — keeps the
# test process clean.
_WORKER_SRC = """
import sys
from pathlib import Path
qid = sys.argv[1]
counter = Path(f"runs_{qid}.txt")
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
fail_marker = Path("fail.txt")
if fail_marker.exists() and fail_marker.read_text().strip() == qid:
    print(f"failing:{qid}")
    sys.exit(1)
print(f"ok:{qid}")
"""


def _worker_path(workdir: Path) -> Path:
    p = workdir / "worker.py"
    p.write_text(_WORKER_SRC)
    return p


def _build_chain(workdir: Path) -> Graph:
    """Linear chain: a -> b -> c, each invoking worker.py with its id."""
    worker = _worker_path(workdir)
    return Graph(
        name="resume-chain",
        version="0.1.0",
        nodes=[
            {
                "id": "a", "name": "a",
                "execute": {
                    "url": _PYTHON,
                    "params": {"args": [str(worker), "a"]},
                },
            },
            {
                "id": "b", "name": "b", "depends_on": ["a"],
                "execute": {
                    "url": _PYTHON,
                    "params": {"args": [str(worker), "b"]},
                },
            },
            {
                "id": "c", "name": "c", "depends_on": ["b"],
                "execute": {
                    "url": _PYTHON,
                    "params": {"args": [str(worker), "c"]},
                },
            },
        ],
    )


def _read_runs(workdir: Path, qid: str) -> int:
    p = workdir / f"runs_{qid}.txt"
    return int(p.read_text()) if p.exists() else 0


async def _run_phase(
    workdir: Path,
    config: Graph,
    db_path: str,
    thread_id: str,
    *,
    resume: bool,
) -> dict:
    """Mirror cli/main.py's execute path with a real AsyncSqliteSaver."""
    async with AsyncSqliteSaver.from_conn_string(db_path) as cp:
        await cp.setup()
        if resume:
            prev = await cp.aget_tuple(
                {"configurable": {"thread_id": thread_id}}
            )
            assert prev is not None, "phase 2 expected a saved checkpoint"
            old = dict(prev.checkpoint.get("channel_values", {}))
            state = {
                **old,
                "failed_nodes": set(),
                "retries": {},
                "errors": [],
                "workdir": str(workdir),
                "dry_run": False,
            }
            await cp.adelete_thread(thread_id)
        else:
            await cp.adelete_thread(thread_id)
            state = make_initial_state(
                workdir=str(workdir), dry_run=False,
            )

        compiled = build_workflow_graph(
            config,
            DispatchExecutor(workdir=str(workdir)),
            checkpointer=cp,
        )
        return await run_workflow(
            compiled, state, config, thread_id=thread_id,
        )


class TestResumeFromCheckpoint:
    @pytest.mark.asyncio
    async def test_resume_after_mid_chain_failure(self, tmp_path):
        """a -> b(fail) -> c, then resume with b unblocked.

        Pins current behavior: resume re-executes already-completed
        nodes (``a``'s runs counter goes from 1 to 2).
        """
        config = _build_chain(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "resume-test"

        # Phase 1: fail b.
        (tmp_path / "fail.txt").write_text("b")
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert result_1["completed_nodes"] == {"a"}
        assert "b" in result_1["failed_nodes"]
        # ``c`` is marked failed because its dep failed; the body
        # never ran.
        assert "c" in result_1["failed_nodes"]
        assert _read_runs(tmp_path, "a") == 1
        assert _read_runs(tmp_path, "b") == 1
        assert _read_runs(tmp_path, "c") == 0

        # Phase 2: clear failure, resume.
        (tmp_path / "fail.txt").unlink()
        result_2 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=True,
        )
        assert "b" in result_2["completed_nodes"]
        assert "c" in result_2["completed_nodes"]
        assert result_2["failed_nodes"] == set()

        # Current resume semantics re-execute already-completed nodes.
        # See TODO (26) for the design discussion. When `--resume`
        # is given a "skip completed" mode, flip this assertion to
        # ``== 1`` and pin the new behavior.
        assert _read_runs(tmp_path, "a") == 2
        # ``b`` failed in phase 1, retried in phase 2 — counter == 2.
        assert _read_runs(tmp_path, "b") == 2
        # ``c`` ran for the first time in phase 2.
        assert _read_runs(tmp_path, "c") == 1

        # State preservation: phase 1's completed ``a`` is in
        # completed_nodes after phase 2. The set-union reducer
        # structurally dedupes the re-execution write — only the
        # runs-counter still records the duplicate work.
        assert result_2["completed_nodes"] == {"a", "b", "c"}

