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

Skip-completed resume (default ``--resume`` behavior since 0.6):
``a`` completed cleanly in phase 1 and is not downstream of any
failure, so it is frozen in phase 2 — runs counter stays at 1.
``b`` was dirty (failed); re-runs. ``c`` is downstream of the
dirty ``b``; runs for the first time. Use ``--rerun-all`` to restore
pre-0.6 full-replay behavior (``a`` re-executes).
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
    resume_from: tuple = (),
    rerun_all: bool = False,
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
            from sqrlly.compile.resume import compute_skip_set
            skip = (
                set()
                if rerun_all
                else compute_skip_set(
                    config,
                    set(old.get("completed_nodes", set())),
                    set(old.get("failed_nodes", set())),
                    set(resume_from),
                )
            )
            state = {
                **old,
                "failed_nodes": set(),
                "retries": {},
                "errors": [],
                "workdir": str(workdir),
                "dry_run": False,
                "_resume_skip": skip,
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

        Skip-completed resume: ``a`` completed cleanly and is NOT
        downstream of the failure, so it is frozen — runs counter
        stays at 1 across both phases.
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

        # Skip-completed resume: a completed cleanly and is NOT downstream of
        # the failure, so it is frozen — runs ONCE total.
        assert _read_runs(tmp_path, "a") == 1
        # b failed in phase 1, is dirty, re-runs in phase 2 — counter == 2.
        assert _read_runs(tmp_path, "b") == 2
        # c is downstream of the failed b (dirty), ran for the first time.
        assert _read_runs(tmp_path, "c") == 1

        # State preservation: phase 1's completed ``a`` is in
        # completed_nodes after phase 2. The set-union reducer merges
        # the skip-completed state write (a was frozen, not re-executed).
        assert result_2["completed_nodes"] == {"a", "b", "c"}

    @pytest.mark.asyncio
    async def test_rerun_all_restores_full_replay(self, tmp_path):
        """--rerun-all reproduces pre-0.6 behavior: a re-executes."""
        config = _build_chain(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "rerun-all"
        (tmp_path / "fail.txt").write_text("b")
        await _run_phase(tmp_path, config, db_path, thread_id, resume=False)
        (tmp_path / "fail.txt").unlink()
        await _run_phase(
            tmp_path, config, db_path, thread_id, resume=True, rerun_all=True,
        )
        assert _read_runs(tmp_path, "a") == 2  # full replay

    @pytest.mark.asyncio
    async def test_resume_from_reruns_node_and_downstream(self, tmp_path):
        """All clean; --resume-from b => a frozen, b & c re-run."""
        config = _build_chain(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "resume-from"
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert result_1["completed_nodes"] == {"a", "b", "c"}
        await _run_phase(
            tmp_path, config, db_path, thread_id,
            resume=True, resume_from=("b",),
        )
        assert _read_runs(tmp_path, "a") == 1   # frozen
        assert _read_runs(tmp_path, "b") == 2   # rerun target
        assert _read_runs(tmp_path, "c") == 2   # downstream of b

