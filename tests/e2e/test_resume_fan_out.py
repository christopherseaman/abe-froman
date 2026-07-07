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

import json
import shutil
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
                "completed_nodes": skip,
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


_ECHO = shutil.which("echo") or "/bin/echo"

# Flippable validator: passes (1.0) unless eval_fail.txt exists in the workdir,
# in which case it fails (0.0). CWD is the workdir when the validator subprocess
# runs (mirrors the fail.txt marker pattern used by worker.py).
_VALIDATOR_SRC = (
    "import sys; from pathlib import Path; sys.stdin.read(); "
    "print('0.0' if Path('eval_fail.txt').exists() else '1.0')"
)


def _build_gated_chain(workdir: Path) -> Graph:
    """Linear chain up -> g, where g has a BLOCKING evaluation (script validator).

    Used to pin the Decision node's `node_id in completed_nodes` guard: on
    resume with g dirty, the Decision node must consult the FRESH eval result
    (not short-circuit as pass). Pinned by flipping the validator to FAIL
    between phases and asserting g ends in failed_nodes — impossible if the
    Decision node short-circuits on stale completed_nodes.
    """
    worker = _worker_path(workdir)
    validator = workdir / "validator.py"
    validator.write_text(_VALIDATOR_SRC)
    return Graph(
        name="resume-gated",
        version="0.1.0",
        nodes=[
            {
                "id": "up", "name": "up",
                "execute": {
                    "url": _PYTHON,
                    "params": {"args": [str(worker), "up"]},
                },
            },
            {
                "id": "g", "name": "g", "depends_on": ["up"],
                "execute": {
                    "url": _PYTHON,
                    "params": {"args": [str(worker), "g"]},
                },
                "evaluation": {
                    "validator": str(validator),
                    "threshold": 0.9,
                    "blocking": True,
                    "max_retries": 0,
                },
            },
        ],
    )


def _build_fanout_chain(workdir: Path) -> Graph:
    """Fan-out chain: up -> fan (fans over one item) -> final_nodes=[agg].

    Used to pin that `_final_fan_agg` re-runs when an ancestor is dirty
    on resume (not frozen by the barrier's `node_id in completed_nodes`
    guard).
    """
    worker = _worker_path(workdir)
    manifest = json.dumps({"items": [{"id": "x"}]})
    return Graph(
        name="resume-fanout",
        version="0.1.0",
        nodes=[
            {
                "id": "up", "name": "up",
                "execute": {
                    "url": _PYTHON,
                    "params": {"args": [str(worker), "up"]},
                },
            },
            {
                "id": "fan", "name": "fan", "depends_on": ["up"],
                "execute": {
                    "url": _ECHO,
                    "params": {"args": ["-n", manifest]},
                },
                "fan_out": {
                    "template": {
                        "execute": {
                            "url": _ECHO,
                            "params": {"args": ["-n", "child-{{id}}"]},
                        },
                    },
                    "final_nodes": [
                        {
                            "id": "agg", "name": "agg",
                            "execute": {
                                "url": _PYTHON,
                                "params": {"args": [str(worker), "agg"]},
                            },
                        }
                    ],
                },
            },
        ],
    )


class TestResumeDirtyGuards:
    """Pins that within-run guards (Decision node, fan-out final barrier) treat
    dirty nodes as not-done when completed_nodes is reseeded to skip-only."""

    @pytest.mark.asyncio
    async def test_gated_node_reruns_eval_on_resume_from(self, tmp_path):
        """up -> g(blocking eval). Phase 1 passes. Flip eval to FAIL, resume-from up.

        Pins the Decision node's `node_id in completed_nodes` guard. Without
        the R8 fix (`completed_nodes` reseeded to full prior set), the Decision
        node short-circuits as pass (g stays completed) even though the fresh
        validator now returns 0.0. With the fix, completed_nodes is seeded to
        skip={} (g is dirty), so the Decision node reads the fresh failing eval
        and routes to fail_blocking → g ends in failed_nodes.
        """
        config = _build_gated_chain(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "gated-resume"

        # Phase 1: no eval_fail.txt → validator returns 1.0 → g completes.
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert result_1["completed_nodes"] == {"up", "g"}
        assert _read_runs(tmp_path, "g") == 1

        # Flip the validator to fail before phase 2.
        (tmp_path / "eval_fail.txt").write_text("fail")

        # Phase 2: --resume-from up → up and g are dirty.
        # The Decision node MUST consult the fresh eval (0.0 < 0.9 threshold)
        # and route to fail_blocking. If it short-circuits on stale
        # completed_nodes, g would stay completed and this assertion fails.
        result_2 = await _run_phase(
            tmp_path, config, db_path, thread_id,
            resume=True, resume_from=("up",),
        )
        assert "g" in result_2["failed_nodes"]
        assert "g" not in result_2["completed_nodes"]
        # Body re-ran (counter incremented on this run).
        assert _read_runs(tmp_path, "g") == 2

    @pytest.mark.asyncio
    async def test_fan_out_final_reruns_on_resume_from_ancestor(self, tmp_path):
        """up -> fan (fan-out, final_nodes=[agg]). Phase 1 completes cleanly.
        Resume with resume_from=up.

        Without Part A fix: _final_fan_agg is in prior_completed but the
        planner's BFS can't reach it (synthetic id, not in config.nodes) →
        it stays in skip → barrier sees it in completed_nodes → defers → agg
        body frozen at 1.
        Without Part B fix: barrier reads the fully reseeded completed_nodes
        (includes _final_fan_agg) → defers → agg body frozen at 1.
        With both fixes: _final_fan_agg is in dirty (via Part A wiring), not
        in skip, not in seeded completed_nodes → barrier re-runs → agg body
        count=2.
        """
        config = _build_fanout_chain(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "fanout-resume"

        # Phase 1: full run.
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert "fan" in result_1["completed_nodes"]
        assert "_final_fan_agg" in result_1["completed_nodes"]
        assert _read_runs(tmp_path, "agg") == 1

        # Phase 2: --resume-from up → fan and _final_fan_agg are dirty.
        result_2 = await _run_phase(
            tmp_path, config, db_path, thread_id,
            resume=True, resume_from=("up",),
        )
        assert "_final_fan_agg" in result_2["completed_nodes"]
        # Aggregation body re-ran.
        assert _read_runs(tmp_path, "agg") == 2


def _build_failable_fanout(workdir: Path) -> Graph:
    """up -> cfan (fans over alpha/beta/gamma) ; each child runs worker.py
    keyed by the per-item id, so a per-child run-counter (runs_<id>.txt) and
    the fail.txt marker (holding a bare item id) control exactly one child's
    failure. No final_nodes — the assertion is purely on per-child re-run."""
    worker = _worker_path(workdir)
    manifest = json.dumps(
        {"items": [{"id": "alpha"}, {"id": "beta"}, {"id": "gamma"}]}
    )
    return Graph(
        name="resume-failable-fanout",
        version="0.1.0",
        nodes=[
            {
                "id": "up", "name": "up",
                "execute": {
                    "url": _PYTHON,
                    "params": {"args": [str(worker), "up"]},
                },
            },
            {
                "id": "cfan", "name": "cfan", "depends_on": ["up"],
                "execute": {
                    "url": _ECHO,
                    "params": {"args": ["-n", manifest]},
                },
                "fan_out": {
                    "template": {
                        "execute": {
                            "url": _PYTHON,
                            "params": {"args": [str(worker), "{{id}}"]},
                        },
                    },
                },
            },
        ],
    )


class TestResumeFailedChild:
    @pytest.mark.asyncio
    async def test_only_failed_child_reruns_on_resume(self, tmp_path):
        """Fan-out over alpha/beta/gamma; beta fails in phase 1. On resume
        (bare --resume, no resume-from) ONLY beta re-runs; alpha and gamma
        are frozen — their run counters stay at 1. The parent re-fans (dirty
        via the failed child), beta succeeds, the run completes clean."""
        config = _build_failable_fanout(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "failed-child-resume"

        # Phase 1: beta fails.
        (tmp_path / "fail.txt").write_text("beta")
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert "cfan" in result_1["completed_nodes"]
        assert "cfan::alpha" in result_1["completed_nodes"]
        assert "cfan::gamma" in result_1["completed_nodes"]
        assert "cfan::beta" in result_1["failed_nodes"]
        assert _read_runs(tmp_path, "alpha") == 1
        assert _read_runs(tmp_path, "beta") == 1
        assert _read_runs(tmp_path, "gamma") == 1

        # Phase 2: clear the marker, bare --resume (no resume-from).
        (tmp_path / "fail.txt").unlink()
        result_2 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=True,
        )
        # The formerly-failed child now succeeds.
        assert "cfan::beta" in result_2["completed_nodes"]
        assert result_2["failed_nodes"] == set()

        # ONLY beta re-ran. alpha and gamma frozen — counters stay at 1.
        assert _read_runs(tmp_path, "alpha") == 1, "completed sibling re-billed"
        assert _read_runs(tmp_path, "gamma") == 1, "completed sibling re-billed"
        # beta ran once per phase = 2.
        assert _read_runs(tmp_path, "beta") == 2

        # 'up' completed cleanly and is upstream of the dirty parent (not
        # downstream), so it is frozen — runs once total.
        assert _read_runs(tmp_path, "up") == 1


# A parent that emits a DUPLICATE-id manifest until dedup.txt exists, then a
# unique one. Phase 1 → duplicate → _fan_ fails the parent. Phase 2 (after
# writing dedup.txt) → unique manifest → re-fans cleanly.
_DUP_PARENT_SRC = """
import json, sys
from pathlib import Path
if Path("dedup.txt").exists():
    items = [{"id": "a"}, {"id": "b"}]
else:
    items = [{"id": "dup"}, {"id": "dup"}]
print(json.dumps({"items": items}))
"""


def _build_dup_manifest_fanout(workdir: Path) -> Graph:
    parent = workdir / "dup_parent.py"
    parent.write_text(_DUP_PARENT_SRC)
    return Graph(
        name="resume-dup-manifest",
        version="0.1.0",
        nodes=[
            {
                "id": "dp", "name": "dp",
                "execute": {
                    "url": _PYTHON,
                    "params": {"args": [str(parent)]},
                },
                "fan_out": {
                    "template": {
                        "execute": {
                            "url": _ECHO,
                            "params": {"args": ["-n", "child-{{id}}"]},
                        },
                    },
                },
            },
        ],
    )


class TestResumeAfterDuplicateManifest:
    @pytest.mark.asyncio
    async def test_duplicate_manifest_fails_run_then_bare_resume_recovers(
        self, tmp_path,
    ):
        """A duplicate-id manifest FAILS the run (the _fan_ dispatcher writes
        a failure-update rather than raising), landing the parent in
        failed_nodes. Bare --resume then dirties the parent via prior_failed,
        it re-fans with a fresh (now-unique) manifest, and the run completes.

        This is the recovery path the design demands: had _fan_ raised, the
        parent would be completed-but-not-failed in the checkpoint and bare
        --resume would freeze it and re-fail deterministically forever.
        """
        config = _build_dup_manifest_fanout(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "dup-manifest-resume"

        # Phase 1: duplicate manifest → parent fails via _fan_ failure-update.
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert "dp" in result_1["failed_nodes"]
        assert not any(
            k.startswith("dp::") for k in result_1["completed_nodes"]
        )
        assert any(
            "duplicate" in e.get("error", "") for e in result_1["errors"]
        )

        # Phase 2: unique manifest now; bare --resume dirties dp (prior_failed)
        # → it re-fans and the children run.
        (tmp_path / "dedup.txt").write_text("go")
        result_2 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=True,
        )
        assert "dp" in result_2["completed_nodes"]
        assert "dp::a" in result_2["completed_nodes"]
        assert "dp::b" in result_2["completed_nodes"]
        assert result_2["failed_nodes"] == set()


# Dispatcher that DRIFTS its child ids across the re-fan: emits r1-* on the
# first run, r2-* once refan.txt exists. Mirrors a real dispatcher that mints
# non-deterministic ids (uuid / counter / positional index).
_DRIFT_DISPATCHER_SRC = """
import json
from pathlib import Path
prefix = "r2" if Path("refan.txt").exists() else "r1"
items = [{"id": prefix + "-alpha"}, {"id": prefix + "-beta"}, {"id": prefix + "-gamma"}]
print(json.dumps({"items": items}))
"""


def _build_drift_fanout(workdir: Path, drift_policy: str = "fail") -> Graph:
    worker = _worker_path(workdir)
    dispatcher = workdir / "drift_dispatcher.py"
    dispatcher.write_text(_DRIFT_DISPATCHER_SRC)
    return Graph(
        name="resume-drift-fanout",
        version="0.1.0",
        nodes=[
            {
                "id": "cfan", "name": "cfan",
                "execute": {
                    "url": _PYTHON,
                    "params": {"args": [str(dispatcher)]},
                },
                "fan_out": {
                    "template": {
                        "execute": {
                            "url": _PYTHON,
                            "params": {"args": [str(worker), "{{id}}"]},
                        },
                    },
                },
            },
        ],
        settings={"on_manifest_drift": drift_policy},
    )


# A 2-node member subgraph (step1 -> step2); each inner node runs the worker
# keyed by the per-branch {{childid}}. `__PY__` / `__WORKER__` are substituted
# (not f-string, to keep the literal `{{childid}}` Jinja var intact).
_MEMBER_SUBGRAPH_TMPL = """
name: member
version: "0.1.0"
nodes:
  - id: step1
    name: step1
    execute:
      url: "__PY__"
      params:
        args: ["__WORKER__", "{{childid}}", "step1"]
  - id: step2
    name: step2
    depends_on: [step1]
    execute:
      url: "__PY__"
      params:
        args: ["__WORKER__", "{{childid}}", "step2"]
"""


def _build_subgraph_template_fanout(workdir: Path) -> Graph:
    """Top-level fan-out over stable ids alpha/beta/gamma whose template is a
    2-node member subgraph — the shape whose branches record both
    `<parent>::<item>` and `<parent>::<item>::<inner>` in completed_nodes."""
    worker = _worker_path(workdir)
    member = workdir / "member.yaml"
    member.write_text(
        _MEMBER_SUBGRAPH_TMPL
        .replace("__PY__", _PYTHON)
        .replace("__WORKER__", str(worker))
    )
    manifest = json.dumps(
        {"items": [{"id": "alpha"}, {"id": "beta"}, {"id": "gamma"}]}
    )
    return Graph(
        name="resume-subgraph-template-fanout",
        version="0.1.0",
        nodes=[
            {
                "id": "cfan", "name": "cfan",
                "execute": {"url": _ECHO, "params": {"args": ["-n", manifest]}},
                "fan_out": {
                    "template": {
                        "execute": {
                            "url": str(member),
                            "params": {"inputs": {"childid": "{{id}}"}},
                        },
                    },
                },
            },
        ],
    )


async def _resume_via_real_seed_state(
    workdir: Path, config: Graph, db_path: str, thread_id: str,
) -> dict:
    """Phase 2 through the REAL cli `_seed_state` (which seeds
    `_fan_prior_children`), NOT the test's hand-rolled seeding — so the drift
    guard is exercised end-to-end on the production resume path."""
    from sqrlly.cli.main import _seed_state

    async with AsyncSqliteSaver.from_conn_string(db_path) as cp:
        await cp.setup()
        state = await _seed_state(
            cp, config, str(workdir),
            resume=True, resume_from=(), rerun_all=False,
            entry=None, thread_id=thread_id,
        )
        compiled = build_workflow_graph(
            config, DispatchExecutor(workdir=str(workdir)), checkpointer=cp,
        )
        return await run_workflow(compiled, state, config, thread_id=thread_id)


class TestResumeManifestDrift:
    @pytest.mark.asyncio
    async def test_drift_fails_loud_before_any_send(self, tmp_path):
        """Phase 1 fans over r1-*, r1-beta fails. Phase 2's dispatcher DRIFTS to
        r2-* ids. With `on_manifest_drift: fail` (default), the re-fan dispatcher
        detects that every prior child id vanished and HALTS the parent before
        any Send — so NO r2-* child is billed (the silent N-wide re-bill that
        would otherwise orphan r1-beta and vanish r1-alpha/r1-gamma)."""
        config = _build_drift_fanout(tmp_path, drift_policy="fail")
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "drift-fail"

        # Phase 1: r1-beta fails.
        (tmp_path / "fail.txt").write_text("r1-beta")
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert "cfan::r1-alpha" in result_1["completed_nodes"]
        assert "cfan::r1-gamma" in result_1["completed_nodes"]
        assert "cfan::r1-beta" in result_1["failed_nodes"]

        # Phase 2: drift to r2-*, clear the failure marker (irrelevant — the
        # guard halts before any child runs).
        (tmp_path / "fail.txt").unlink()
        (tmp_path / "refan.txt").write_text("go")
        result_2 = await _resume_via_real_seed_state(
            tmp_path, config, db_path, thread_id,
        )

        # Parent failed loud with a drift error; NO r2-* child was dispatched.
        assert "cfan" in result_2["failed_nodes"]
        assert any(
            "drift" in e.get("error", "").lower() for e in result_2["errors"]
        ), result_2["errors"]
        assert _read_runs(tmp_path, "r2-alpha") == 0, "drifted child was billed"
        assert _read_runs(tmp_path, "r2-beta") == 0, "drifted child was billed"
        assert _read_runs(tmp_path, "r2-gamma") == 0, "drifted child was billed"

    @pytest.mark.asyncio
    async def test_drift_to_empty_manifest_fails_loud(self, tmp_path):
        """The MAXIMAL drift: a resume dispatcher that re-reads its manifest to
        ZERO items drops every prior branch — the failed child is orphaned and
        completed siblings vanish. This must fail loud (default), not silently
        route to the no-items path and complete green. (Regression: the empty-
        manifest early return must not bypass the drift guard.)"""
        worker = _worker_path(tmp_path)
        dispatcher = tmp_path / "drain_dispatcher.py"
        dispatcher.write_text(
            "import json\n"
            "from pathlib import Path\n"
            "items = [] if Path('drain.txt').exists() else "
            "[{'id': 'alpha'}, {'id': 'beta'}, {'id': 'gamma'}]\n"
            "print(json.dumps({'items': items}))\n"
        )
        config = Graph(
            name="resume-drain-fanout", version="0.1.0",
            nodes=[{
                "id": "cfan", "name": "cfan",
                "execute": {"url": _PYTHON, "params": {"args": [str(dispatcher)]}},
                "fan_out": {
                    "template": {"execute": {
                        "url": _PYTHON,
                        "params": {"args": [str(worker), "{{id}}"]},
                    }},
                },
            }],
            settings={"on_manifest_drift": "fail"},
        )
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "drift-drain"

        (tmp_path / "fail.txt").write_text("beta")
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert "cfan::beta" in result_1["failed_nodes"]

        # Phase 2: manifest drains to empty. The failed child must NOT be
        # silently abandoned.
        (tmp_path / "fail.txt").unlink()
        (tmp_path / "drain.txt").write_text("go")
        result_2 = await _resume_via_real_seed_state(
            tmp_path, config, db_path, thread_id,
        )
        assert "cfan" in result_2["failed_nodes"], (
            "empty-manifest drift silently completed green"
        )
        assert any(
            "drift" in e.get("error", "").lower() for e in result_2["errors"]
        ), result_2["errors"]

    @pytest.mark.asyncio
    async def test_stable_id_resume_does_not_false_fire(self, tmp_path):
        """The guard's core safety claim on a FLAT-template fan-out: a stable-id
        fan-out resumed through the REAL `_seed_state` (guard live) must NOT
        fire — only the failed child re-runs, completed siblings stay frozen."""
        config = _build_failable_fanout(tmp_path)  # stable ids alpha/beta/gamma
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "stable-no-false-fire"

        (tmp_path / "fail.txt").write_text("beta")
        await _run_phase(tmp_path, config, db_path, thread_id, resume=False)

        (tmp_path / "fail.txt").unlink()
        result_2 = await _resume_via_real_seed_state(
            tmp_path, config, db_path, thread_id,
        )
        # Guard did NOT fire.
        assert "cfan" not in result_2["failed_nodes"]
        assert not any(
            "drift" in e.get("error", "").lower() for e in result_2["errors"]
        )
        # Only the failed child re-ran; siblings frozen.
        assert _read_runs(tmp_path, "alpha") == 1
        assert _read_runs(tmp_path, "gamma") == 1
        assert _read_runs(tmp_path, "beta") == 2

    @pytest.mark.asyncio
    async def test_subgraph_template_resume_does_not_false_fire(self, tmp_path):
        """The champion's actual shape — a SUBGRAPH-template fan-out — resumed
        through the real `_seed_state` with the guard live must NOT false-fire:
        only the failed branch's subgraph re-runs; siblings freeze. (A branch
        records only its `<parent>::<item>` id at the top level; the inner
        subgraph node ids stay in the subgraph's own state, so the top-level
        drift snapshot sees only the stable branch ids. The `direct_child_ids`
        inner-id exclusion is defensive; its own contract is pinned by
        `test_manifest_drift.py::TestDirectChildIds`.)"""
        config = _build_subgraph_template_fanout(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "subgraph-no-false-fire"

        # Phase 1: beta branch's step1 fails (so step2 never runs).
        (tmp_path / "fail.txt").write_text("beta")
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert "cfan::beta" in result_1["failed_nodes"]
        # alpha/gamma ran both inner steps (==2); beta failed at step1 (==1).
        assert _read_runs(tmp_path, "alpha") == 2
        assert _read_runs(tmp_path, "gamma") == 2
        assert _read_runs(tmp_path, "beta") == 1

        # Phase 2: resume through the real seed path — guard must NOT fire.
        (tmp_path / "fail.txt").unlink()
        result_2 = await _resume_via_real_seed_state(
            tmp_path, config, db_path, thread_id,
        )
        assert "cfan" not in result_2["failed_nodes"]
        assert not any(
            "drift" in e.get("error", "").lower() for e in result_2["errors"]
        )
        # Only beta's subgraph re-ran (step1+step2 → +2 = 3); siblings frozen.
        assert _read_runs(tmp_path, "alpha") == 2
        assert _read_runs(tmp_path, "gamma") == 2
        assert _read_runs(tmp_path, "beta") == 3

    @pytest.mark.asyncio
    async def test_drift_warn_proceeds(self, tmp_path):
        """Same drift, but `on_manifest_drift: warn` → the re-fan proceeds with
        the new manifest (opt-in for an author who intends a changed manifest on
        resume). The r2-* children run; the run completes clean."""
        config = _build_drift_fanout(tmp_path, drift_policy="warn")
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "drift-warn"

        (tmp_path / "fail.txt").write_text("r1-beta")
        await _run_phase(tmp_path, config, db_path, thread_id, resume=False)

        (tmp_path / "fail.txt").unlink()
        (tmp_path / "refan.txt").write_text("go")
        result_2 = await _resume_via_real_seed_state(
            tmp_path, config, db_path, thread_id,
        )

        # warn let the drifted manifest through: r2-* children ran, run clean.
        assert result_2["failed_nodes"] == set()
        assert _read_runs(tmp_path, "r2-alpha") == 1
        assert _read_runs(tmp_path, "r2-beta") == 1
        assert _read_runs(tmp_path, "r2-gamma") == 1
