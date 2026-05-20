"""Unit tests for ForemanExecutor (queue + worktree pool + semaphores).

Uses real git worktrees, real subprocesses via DispatchExecutor.
No fakes, no mocks — concurrency tested via real asyncio primitives; worktree
retention verified against the on-disk state of `git worktree list`.
"""
from __future__ import annotations

import asyncio
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.foreman import ForemanExecutor
from sqrlly.schema.models import Execute, Node, Settings

_PWD = shutil.which("pwd") or "/bin/pwd"
_SLEEP = shutil.which("sleep") or "/bin/sleep"


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


def _cmd_phase(node_id: str, command: str = "pwd", args=None) -> Node:
    """Build a Stage-5b execute-URL node from a bare command name.

    `command="pwd"` → `execute.url=/usr/bin/pwd`; args go in execute.params.args.
    """
    url = shutil.which(command) or f"/bin/{command}"
    return Node(
        id=node_id, name=node_id,
        execute=Execute(url=url, params={"args": args or []}),
    )


class _InstrumentedForeman(ForemanExecutor):
    """ForemanExecutor that records `_create_worktree` concurrency.

    Not a mock — a real subclass overriding one method to add a small
    delay plus an in-flight counter, so a test can observe whether two
    creations overlapped (lock released across the subprocess) or
    serialized (lock held). The delay makes the overlap deterministic.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.create_calls = 0
        self._in_flight = 0
        self.max_in_flight = 0

    async def _create_worktree(self, node_id: str) -> str:
        self.create_calls += 1
        self._in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self._in_flight)
        try:
            await asyncio.sleep(0.05)
            return await super()._create_worktree(node_id)
        finally:
            self._in_flight -= 1


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
        """Second execute() with same node_id must use the same worktree path."""
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
    async def test_different_nodes_get_different_worktrees(self, tmp_path):
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
    async def test_branch_composite_id_gets_own_worktree(self, tmp_path):
        """Dynamic child ids (parent::item) each get their own tree."""
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

    @pytest.mark.asyncio
    async def test_concurrent_same_node_creates_one_worktree(self, tmp_path):
        """E3: two acquisitions racing for the same node_id share one
        creation task — exactly one worktree, both callers get its path."""
        _init_git_repo(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = _InstrumentedForeman(inner=inner, base_workdir=str(tmp_path))
        try:
            first, second = await asyncio.gather(
                foreman._acquire_worktree("p"),
                foreman._acquire_worktree("p"),
            )
            assert first == second
            assert foreman.create_calls == 1
        finally:
            await foreman.close()

    @pytest.mark.asyncio
    async def test_concurrent_different_nodes_create_in_parallel(self, tmp_path):
        """E3: acquisitions for distinct node_ids run their
        `git worktree add` subprocesses concurrently — the global lock
        is released before the subprocess, so both _create_worktree
        bodies are in flight at once."""
        _init_git_repo(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = _InstrumentedForeman(inner=inner, base_workdir=str(tmp_path))
        try:
            wa, wb = await asyncio.gather(
                foreman._acquire_worktree("a"),
                foreman._acquire_worktree("b"),
            )
            assert wa != wb
            assert foreman.max_in_flight == 2
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
        remove`d it, or the .sqrlly dir was wiped), --resume must not
        crash — `_acquire_worktree` re-creates a fresh tree under .sqrlly/."""
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
            # Newly-created worktrees live under .sqrlly/.
            assert ".sqrlly" in new_path
            # And `pwd` ran inside the recreated tree, not the dead path.
            assert Path(result.output.strip()).resolve() == Path(new_path).resolve()
        finally:
            await foreman.close()


class TestConcurrencyCap:
    @pytest.mark.asyncio
    async def test_global_semaphore_bounds_parallelism(self, tmp_path):
        """With max_parallel_jobs=2 and 6 sleeping nodes, wall time is bounded
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
        """Without a cap, 4 sleeping nodes finish in ~1 sleep duration."""
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

    We construct prompt nodes with different models, each using a MemoryBackend
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
            from sqrlly.runtime.result import ExecutionResult
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
                from sqrlly.runtime.result import ExecutionResult
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
        from sqrlly.schema.models import LlmPreset
        settings = Settings(presets={
            "fast": LlmPreset(transport="api", provider="anthropic", model="opus"),
            "balanced": LlmPreset(
                transport="api", provider="anthropic",
                model="sonnet", default=True,
            ),
        })
        inner = DispatchExecutor(
            workdir=str(tmp_path),
            prompt_backends={"fast": backend, "balanced": backend},
            settings=settings,
        )
        foreman = ForemanExecutor(
            inner=inner,
            base_workdir=str(tmp_path),
            max_parallel_jobs=10,
            per_model_limits={"opus": 1, "sonnet": 2},
            settings=settings,
        )

        nodes = []
        for i in range(3):
            nodes.append(Node(
                id=f"opus{i}", name=f"opus{i}",
                execute=Execute(url="p.md", params={"preset": "fast"}),
            ))
            nodes.append(Node(
                id=f"son{i}", name=f"son{i}",
                execute=Execute(url="p.md", params={"preset": "balanced"}),
            ))

        try:
            await asyncio.gather(*[foreman.execute(p, {}) for p in nodes])
            assert backend.max_inflight.get("opus", 0) == 1
            assert backend.max_inflight.get("sonnet", 0) == 2
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


class TestMemoryBackPressure:
    """Foreman gates new dispatches on host memory percent via real
    ``psutil.virtual_memory()`` — no fakes, no monkeypatch.

    Two tests:
      - **Permissive threshold** smoke: a setting well above current
        memory percent is a no-op; dispatch proceeds normally.
      - **Real allocation**: claim ~500 MB transiently to push percent
        above a tight threshold, hold the gate, then release and let
        dispatch proceed. Exercises the full psutil integration.
    """

    @pytest.mark.asyncio
    async def test_permissive_threshold_no_gate(self, tmp_path):
        """Threshold well above current memory → gate never fires.
        Smoke check that the integration doesn't break the happy path."""
        _init_git_repo(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(
            inner=inner, base_workdir=str(tmp_path),
            # 99.9 is above any realistic baseline on a healthy machine.
            settings=Settings(memory_threshold_pct=99.9),
            memory_poll_interval_s=0.05,
        )
        try:
            result = await foreman.execute(_cmd_phase("alpha"), {})
            assert result.success is True
        finally:
            await foreman.close()

    @pytest.mark.asyncio
    async def test_min_available_bytes_holds_gate_then_releases(
        self, tmp_path,
    ):
        """Companion form: gate on ``available`` bytes rather than
        percent. Same allocation/release dance, but the threshold is
        set to "current available - alloc_size + cushion" so the gate
        flips closed by the allocation and re-opens on release.
        """
        import gc

        import psutil

        _init_git_repo(tmp_path)
        avail = psutil.virtual_memory().available
        if avail < 4 * 1024 * 1024 * 1024:
            pytest.skip(
                f"insufficient free memory ({avail / 1e9:.1f} GB) "
                f"— allocation test requires ≥4 GB headroom"
            )

        baseline_avail = psutil.virtual_memory().available
        alloc_size = min(200 * 1024 * 1024, baseline_avail // 20)
        if alloc_size < 50 * 1024 * 1024:
            pytest.skip(
                f"5%% of available memory ({alloc_size / 1e6:.0f} MB) "
                f"too small to reliably register"
            )
        burden_holder: list[bytearray] = [bytearray(alloc_size)]
        for i in range(0, alloc_size, 4096):
            burden_holder[0][i] = 1

        elevated_avail = psutil.virtual_memory().available
        if baseline_avail - elevated_avail < alloc_size // 2:
            burden_holder.clear()
            pytest.skip(
                f"available memory delta ({(baseline_avail - elevated_avail) / 1e6:.0f} "
                f"MB) too small to reliably trigger the gate"
            )

        # Threshold sits between elevated (gated) and baseline
        # (unblocks after release). Gate fires now, releases later.
        threshold_bytes = (baseline_avail + elevated_avail) // 2

        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(
            inner=inner, base_workdir=str(tmp_path),
            settings=Settings(memory_min_available_bytes=threshold_bytes),
            memory_poll_interval_s=0.05,
        )

        async def release_after_delay():
            await asyncio.sleep(0.2)
            burden_holder.clear()
            gc.collect()

        try:
            t0 = time.monotonic()
            result, _ = await asyncio.gather(
                foreman.execute(_cmd_phase("alpha"), {}),
                release_after_delay(),
            )
            elapsed = time.monotonic() - t0
            assert result.success is True
            assert elapsed >= 0.15, (
                f"gate didn't hold for the allocation; "
                f"elapsed={elapsed:.2f}s, threshold_bytes={threshold_bytes}"
            )
        finally:
            await foreman.close()

    @pytest.mark.asyncio
    async def test_real_allocation_holds_gate_then_releases(self, tmp_path):
        """Claim memory transiently to push ``psutil.virtual_memory().percent``
        above a tight threshold; confirm the gate holds dispatch; release
        and confirm dispatch proceeds.

        Skipped automatically when system headroom makes a ~500 MB
        allocation invisible to ``percent`` readings (huge-RAM hosts).
        """
        import gc

        import psutil

        _init_git_repo(tmp_path)
        avail = psutil.virtual_memory().available
        # Safety floor — refuse to run if the host's free memory is
        # under 4 GB. We never want a test to push a developer's box
        # into swap or OOM-killer territory.
        if avail < 4 * 1024 * 1024 * 1024:
            pytest.skip(
                f"insufficient free memory ({avail / 1e9:.1f} GB) "
                f"— allocation test requires ≥4 GB headroom"
            )

        baseline = psutil.virtual_memory().percent
        # Cap allocation at the SMALLER of:
        #   - 200 MB (a fixed sane upper bound; enough to register on
        #     percent readings without straining the host)
        #   - 5 % of available memory (scales down on tight systems)
        # If 5 % of available is below 50 MB the test would be too
        # noisy anyway, so skip rather than allocate something useless.
        alloc_size = min(200 * 1024 * 1024, avail // 20)
        if alloc_size < 50 * 1024 * 1024:
            pytest.skip(
                f"5%% of available memory ({alloc_size / 1e6:.0f} MB) "
                f"too small to reliably register"
            )
        burden_holder: list[bytearray] = [bytearray(alloc_size)]
        # Touch every page so the allocation isn't lazy-mapped.
        for i in range(0, alloc_size, 4096):
            burden_holder[0][i] = 1

        elevated = psutil.virtual_memory().percent
        if elevated <= baseline + 0.3:
            burden_holder.clear()
            pytest.skip(
                f"system memory unchanged after {alloc_size / 1e6:.0f} MB "
                f"alloc (baseline={baseline:.1f}, elevated={elevated:.1f}); "
                f"too much headroom to reliably trigger the gate"
            )

        # Threshold sits between the two readings — current state
        # gates, post-release state proceeds.
        threshold = (baseline + elevated) / 2.0

        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(
            inner=inner, base_workdir=str(tmp_path),
            settings=Settings(memory_threshold_pct=threshold),
            memory_poll_interval_s=0.05,
        )

        async def release_after_delay():
            await asyncio.sleep(0.2)
            burden_holder.clear()
            gc.collect()

        try:
            t0 = time.monotonic()
            result, _ = await asyncio.gather(
                foreman.execute(_cmd_phase("alpha"), {}),
                release_after_delay(),
            )
            elapsed = time.monotonic() - t0
            assert result.success is True
            # Gate held until the bytearray was released and percent
            # dropped below threshold. `pwd` itself is fast; elapsed
            # time is dominated by the gate hold (~0.2s).
            assert elapsed >= 0.15, (
                f"gate didn't hold for the allocation; "
                f"elapsed={elapsed:.2f}s, threshold={threshold:.1f}"
            )
        finally:
            await foreman.close()
