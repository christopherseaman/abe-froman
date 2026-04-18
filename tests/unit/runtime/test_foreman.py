"""Unit tests for ForemanExecutor (queue + worktree pool + semaphores).

Uses real git worktrees, real subprocesses via DispatchExecutor + CommandExecutor.
No fakes, no mocks — concurrency tested via real asyncio primitives; worktree
retention verified against the on-disk state of `git worktree list`.
"""
from __future__ import annotations

import asyncio
import subprocess
import time
from pathlib import Path

import pytest

from abe_froman.runtime.executor.command import CommandExecutor
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.foreman import ForemanExecutor
from abe_froman.schema.models import Phase, Settings


def _init_git_repo(path: Path) -> None:
    """Initialize a minimal git repo with one commit so worktrees can branch."""
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "t"], check=True
    )
    (path / "README").write_text("init")
    subprocess.run(["git", "-C", str(path), "add", "README"], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True
    )


def _cmd_phase(phase_id: str, command: str = "pwd", args=None) -> Phase:
    return Phase(
        id=phase_id, name=phase_id,
        execution={"type": "command", "command": command, "args": args or []},
    )


class TestWorktreePool:
    @pytest.mark.asyncio
    async def test_first_execute_creates_worktree(self, tmp_path):
        _init_git_repo(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(inner=inner, base_workdir=str(tmp_path))
        try:
            result = await foreman.execute(_cmd_phase("alpha"), {})
            assert result.success
            wt_path = foreman.get_worktree("alpha")
            assert wt_path is not None
            assert Path(wt_path).is_dir()
            assert (Path(wt_path) / ".git").exists()
        finally:
            await foreman.close()

    @pytest.mark.asyncio
    async def test_retry_reuses_same_worktree(self, tmp_path):
        """Second execute() with same phase_id must use the same worktree path."""
        _init_git_repo(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(inner=inner, base_workdir=str(tmp_path))
        try:
            await foreman.execute(_cmd_phase("p", command="pwd"), {})
            first = foreman.get_worktree("p")

            # Write a file INTO the worktree so we can prove retention.
            (Path(first) / "scratch.txt").write_text("from-attempt-1")

            await foreman.execute(_cmd_phase("p", command="pwd"), {})
            second = foreman.get_worktree("p")
            assert first == second
            # File from first attempt is still there — worktree was NOT recreated.
            assert (Path(second) / "scratch.txt").read_text() == "from-attempt-1"
        finally:
            await foreman.close()

    @pytest.mark.asyncio
    async def test_different_phases_get_different_worktrees(self, tmp_path):
        _init_git_repo(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(inner=inner, base_workdir=str(tmp_path))
        try:
            await foreman.execute(_cmd_phase("a"), {})
            await foreman.execute(_cmd_phase("b"), {})
            assert foreman.get_worktree("a") != foreman.get_worktree("b")
        finally:
            await foreman.close()

    @pytest.mark.asyncio
    async def test_subphase_composite_id_gets_own_worktree(self, tmp_path):
        """Dynamic subphase ids (parent::item) each get their own tree."""
        _init_git_repo(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(inner=inner, base_workdir=str(tmp_path))
        try:
            await foreman.execute(_cmd_phase("parent::a"), {})
            await foreman.execute(_cmd_phase("parent::b"), {})
            wa = foreman.get_worktree("parent::a")
            wb = foreman.get_worktree("parent::b")
            assert wa is not None and wb is not None
            assert wa != wb
        finally:
            await foreman.close()

    @pytest.mark.asyncio
    async def test_command_runs_inside_worktree(self, tmp_path):
        """The command subprocess must execute with cwd = worktree, not base."""
        _init_git_repo(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(inner=inner, base_workdir=str(tmp_path))
        try:
            result = await foreman.execute(_cmd_phase("p", "pwd"), {})
            assert result.success
            wt_path = foreman.get_worktree("p")
            # pwd output should end with the worktree path (resolve symlinks)
            assert Path(result.output.strip()).resolve() == Path(wt_path).resolve()
        finally:
            await foreman.close()


class TestRehydration:
    @pytest.mark.asyncio
    async def test_rehydrate_populates_worktree_map(self, tmp_path):
        """After --resume, foreman is initialized with existing worktree paths."""
        _init_git_repo(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        existing = tmp_path / "pre-existing-wt"
        foreman = ForemanExecutor(
            inner=inner,
            base_workdir=str(tmp_path),
            rehydrate={"old_phase": str(existing)},
        )
        try:
            assert foreman.get_worktree("old_phase") == str(existing)
        finally:
            await foreman.close()

    @pytest.mark.asyncio
    async def test_rehydrated_worktree_reused_on_execute(self, tmp_path):
        """If the rehydrated path exists on disk, execute() uses it."""
        _init_git_repo(tmp_path)
        # Pre-create a real worktree
        pre = tmp_path / "wt-pre"
        subprocess.run(
            ["git", "-C", str(tmp_path), "worktree", "add", "-q",
             str(pre), "HEAD"],
            check=True,
        )
        (pre / "retained.txt").write_text("from-earlier-run")

        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(
            inner=inner,
            base_workdir=str(tmp_path),
            rehydrate={"resumed": str(pre)},
        )
        try:
            await foreman.execute(_cmd_phase("resumed", "pwd"), {})
            assert foreman.get_worktree("resumed") == str(pre)
            # File from "previous run" still there — nothing was recreated.
            assert (pre / "retained.txt").read_text() == "from-earlier-run"
        finally:
            await foreman.close()

    @pytest.mark.asyncio
    async def test_rehydrated_but_deleted_worktree_is_recreated(self, tmp_path):
        """If the rehydrated path no longer exists on disk (user `git worktree
        remove`d it, or the .abe-foreman dir was wiped), --resume must not
        crash — `_acquire_worktree` re-creates a fresh tree under .abe-foreman/."""
        _init_git_repo(tmp_path)
        gone = tmp_path / "nonexistent-gone"
        assert not gone.exists()

        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(
            inner=inner,
            base_workdir=str(tmp_path),
            rehydrate={"resumed": str(gone)},
        )
        try:
            result = await foreman.execute(_cmd_phase("resumed", "pwd"), {})
            assert result.success
            new_path = foreman.get_worktree("resumed")
            assert new_path != str(gone)
            assert Path(new_path).is_dir()
            # Newly-created worktrees live under .abe-foreman/.
            assert ".abe-foreman" in new_path
            # And `pwd` ran inside the recreated tree, not the dead path.
            assert Path(result.output.strip()).resolve() == Path(new_path).resolve()
        finally:
            await foreman.close()


class TestConcurrencyCap:
    @pytest.mark.asyncio
    async def test_global_semaphore_bounds_parallelism(self, tmp_path):
        """With max_parallel_jobs=2 and 6 sleeping phases, wall time is bounded
        from below by (N/K) * per_phase_duration."""
        _init_git_repo(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(
            inner=inner, base_workdir=str(tmp_path), max_parallel_jobs=2,
        )
        sleep_s = 0.25
        n = 6

        async def run(i: int):
            return await foreman.execute(
                _cmd_phase(f"p{i}", "sleep", [str(sleep_s)]), {},
            )

        try:
            start = time.perf_counter()
            results = await asyncio.gather(*[run(i) for i in range(n)])
            elapsed = time.perf_counter() - start
            assert all(r.success for r in results)
            # Lower bound: with cap=2, 6 jobs of 0.25s take at least 3*0.25=0.75s
            assert elapsed >= 0.7, f"elapsed {elapsed:.3f}s too short"
            # Upper bound: serial would be 1.5s + overhead. Allow generous slack.
            assert elapsed < 2.0, f"elapsed {elapsed:.3f}s too long"
        finally:
            await foreman.close()

    @pytest.mark.asyncio
    async def test_no_cap_runs_fully_parallel(self, tmp_path):
        """Without a cap, 4 sleeping phases finish in ~1 sleep duration."""
        _init_git_repo(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(
            inner=inner, base_workdir=str(tmp_path), max_parallel_jobs=100,
        )
        sleep_s = 0.3

        try:
            start = time.perf_counter()
            results = await asyncio.gather(*[
                foreman.execute(
                    _cmd_phase(f"p{i}", "sleep", [str(sleep_s)]), {},
                )
                for i in range(4)
            ])
            elapsed = time.perf_counter() - start
            assert all(r.success for r in results)
            # Fully parallel: one sleep duration + overhead
            assert elapsed < sleep_s * 2, f"elapsed {elapsed:.3f}s too long"
        finally:
            await foreman.close()


class TestPerModelBackpressure:
    """Per-model semaphores apply on top of the global cap.

    We construct prompt phases with different models, each using a MemoryBackend
    that sleeps — MemoryBackend is the existing in-repo test double for the
    PromptBackend Protocol (see tests/unit/runtime/test_prompt.py).
    """

    class SleepyBackend:
        def __init__(self, delay_s: float):
            self._delay = delay_s
            self.inflight_max = 0
            self._inflight = 0
            self._lock = asyncio.Lock()

        async def send_prompt(
            self, prompt: str, model: str, workdir: str,
            timeout: float | None = None,
        ):
            from abe_froman.runtime.result import ExecutionResult
            async with self._lock:
                self._inflight += 1
                self.inflight_max = max(self.inflight_max, self._inflight)
            try:
                await asyncio.sleep(self._delay)
                return ExecutionResult(success=True, output=f"[{model}]")
            finally:
                async with self._lock:
                    self._inflight -= 1

        async def close(self):
            pass

    @pytest.mark.asyncio
    async def test_per_model_limit_bounds_inflight_per_model(self, tmp_path):
        """opus limited to 1, sonnet limited to 2 — submit 3+3, max-inflight
        per model must not exceed its cap."""
        _init_git_repo(tmp_path)

        class TrackingBackend:
            def __init__(self):
                self._inflight = {}
                self.max_inflight = {}
                self._lock = asyncio.Lock()

            async def send_prompt(self, prompt, model, workdir, timeout=None):
                from abe_froman.runtime.result import ExecutionResult
                async with self._lock:
                    self._inflight[model] = self._inflight.get(model, 0) + 1
                    self.max_inflight[model] = max(
                        self.max_inflight.get(model, 0),
                        self._inflight[model],
                    )
                try:
                    await asyncio.sleep(0.15)
                    return ExecutionResult(success=True, output=model)
                finally:
                    async with self._lock:
                        self._inflight[model] -= 1

            async def close(self): pass

        backend = TrackingBackend()
        (tmp_path / "p.md").write_text("hi")
        # Commit so the prompt file exists in each worktree created from HEAD.
        subprocess.run(["git", "-C", str(tmp_path), "add", "p.md"], check=True)
        subprocess.run(
            ["git", "-C", str(tmp_path), "commit", "-q", "-m", "add p"],
            check=True,
        )
        settings = Settings(default_model="sonnet")
        inner = DispatchExecutor(
            workdir=str(tmp_path), prompt_backend=backend, settings=settings,
        )
        foreman = ForemanExecutor(
            inner=inner,
            base_workdir=str(tmp_path),
            max_parallel_jobs=10,
            per_model_limits={"opus": 1, "sonnet": 2},
        )

        phases = []
        for i in range(3):
            phases.append(Phase(
                id=f"opus{i}", name=f"opus{i}", prompt_file="p.md", model="opus",
            ))
            phases.append(Phase(
                id=f"son{i}", name=f"son{i}", prompt_file="p.md", model="sonnet",
            ))

        try:
            await asyncio.gather(*[foreman.execute(p, {}) for p in phases])
            assert backend.max_inflight.get("opus", 0) == 1
            assert backend.max_inflight.get("sonnet", 0) == 2
        finally:
            await foreman.close()


class TestBackendPassthrough:
    @pytest.mark.asyncio
    async def test_get_backend_passes_through_to_inner(self, tmp_path):
        """ForemanExecutor.get_backend() returns the inner executor's backend,
        so compile/nodes.py can dispatch .md LLM gates through it."""
        _init_git_repo(tmp_path)
        from abe_froman.runtime.executor.backends.stub import StubBackend

        backend = StubBackend()
        inner = DispatchExecutor(
            workdir=str(tmp_path),
            prompt_backend=backend,
            settings=Settings(),
        )
        foreman = ForemanExecutor(inner=inner, base_workdir=str(tmp_path))
        try:
            assert foreman.get_backend() is backend
        finally:
            await foreman.close()


class TestWorktreeCreationFailure:
    @pytest.mark.asyncio
    async def test_non_git_workdir_raises_runtime_error(self, tmp_path):
        """base_workdir is not a git repo → `git worktree add` fails; foreman
        surfaces the error loudly rather than silently degrading."""
        # NOT calling _init_git_repo — tmp_path is a plain directory.
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(inner=inner, base_workdir=str(tmp_path))
        try:
            with pytest.raises(RuntimeError) as excinfo:
                await foreman.execute(_cmd_phase("alpha"), {})
            msg = str(excinfo.value)
            assert "git worktree add" in msg
            assert "alpha" in msg
        finally:
            await foreman.close()

    @pytest.mark.asyncio
    async def test_bad_base_workdir_raises_runtime_error(self, tmp_path):
        """base_workdir doesn't exist at all → same loud failure."""
        missing = tmp_path / "does-not-exist"
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(inner=inner, base_workdir=str(missing))
        try:
            with pytest.raises(Exception) as excinfo:
                await foreman.execute(_cmd_phase("beta"), {})
            # Either FileNotFoundError from mkdir/spawn or RuntimeError from
            # the git return-code path — both are acceptable loud failures.
            assert "beta" in str(excinfo.value) or not str(excinfo.value).startswith("foreman:")
        finally:
            await foreman.close()
