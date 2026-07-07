"""ForemanExecutor: worktree pool + creation throttle + memory gate.

Wraps an inner `NodeExecutor` (typically `DispatchExecutor`) and adds:
  - A **worktree pool** — per-node trees for ``auto``/``isolated`` nodes;
    group nodes share one tree keyed by group name; ``off`` nodes run in
    the base workdir.
  - A **worktree-creation throttle** (`asyncio.Semaphore`) bounding how many
    `git worktree add` run at once. The DISPATCH concurrency cap lives in
    `DispatchExecutor` (so it applies off-git too); the foreman gates only
    worktree creation, so a git child never holds two semaphores.
  - **Memory back-pressure** — when ``settings.memory_threshold_pct`` is
    set, blocks new dispatches while host memory percent is above it
    (see ``runtime/memory_gate.py``; default off).

Foreman is LangGraph-agnostic: it imports nothing from `compile/` or `langgraph`.
The retry decision lives at the compile layer; foreman just runs what's handed
to it.

Worktree lifecycle: foreman creates worktrees on first `execute()` per
`node_id`, reused across retries. With ``settings.worktree_gc: on_success``
the CLI calls ``reclaim()`` after a clean run to remove all allocated trees;
by default (``never``) they persist for inspection / ``--resume``.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import uuid
from pathlib import Path
from typing import Any

from sqrlly.runtime.gates import scaffold_output_directory
from sqrlly.runtime.memory_gate import POLL_INTERVAL_S, wait_for_memory
from sqrlly.runtime.result import ExecutionResult, NodeExecutor, PromptBackend
from sqrlly.runtime.worktree_share import ensure_setup, materialize_shares
from sqrlly.schema.models import Node, Settings

logger = logging.getLogger(__name__)


class ForemanExecutor:
    """NodeExecutor wrapper adding a concurrency cap + worktree pool."""

    def __init__(
        self,
        inner: NodeExecutor,
        base_workdir: str,
        max_parallel_jobs: int = 4,
        rehydrate: dict[str, str] | None = None,
        settings: Settings | None = None,
        memory_poll_interval_s: float = POLL_INTERVAL_S,
    ):
        self._inner = inner
        # Resolve to absolute so recorded worktree paths (node_worktrees,
        # branch_map.worktree, promote/GC targets) are unambiguous regardless
        # of the `--workdir` form — a fan-in consumer needn't re-resolve them.
        self._base = str(Path(base_workdir).resolve())
        # Throttles worktree CREATION only (not dispatch — that cap lives in
        # DispatchExecutor). Prevents a wide fan-out from firing N `git
        # worktree add` at once.
        self._worktree_create_sem = asyncio.Semaphore(max_parallel_jobs)
        self._worktrees: dict[str, str] = dict(rehydrate or {})
        self._created_paths: set[str] = set()
        self._worktree_lock = asyncio.Lock()
        # In-flight creations, keyed by node_id. The global lock is held
        # only to register/look up a task here — never across the
        # `git worktree add` subprocess — so concurrent fan-out children
        # create their worktrees in parallel instead of serializing.
        self._worktree_tasks: dict[str, asyncio.Task[str]] = {}
        self._settings = settings or Settings()
        self._memory_poll_s = memory_poll_interval_s

    async def execute(
        self,
        node: Node,
        context: dict[str, Any],
        workdir: str | None = None,
        settings_override: Settings | None = None,
    ) -> ExecutionResult:
        s = settings_override or self._settings

        # Memory back-pressure runs before any acquisition so a gated
        # dispatch doesn't sit holding resources. Defaults to ``None``
        # (disabled — a fast no-op).
        await wait_for_memory(
            s.memory_threshold_pct, node_id=node.id,
            poll_interval=self._memory_poll_s,
        )

        kind, group = node.effective_worktree(s)
        if kind == "off":
            run_dir = workdir or self._base
        else:
            pool_key = f"group:{group}" if kind == "group" else node.id
            # Throttle worktree CREATION only; released before dispatch, so
            # the inner DispatchExecutor's own cap is the sole dispatch gate
            # (no nested-semaphore double-count).
            async with self._worktree_create_sem:
                run_dir = await self._acquire_worktree(node.id, pool_key, group)
        if node.output_contract:
            scaffold_output_directory(node.output_contract, run_dir)
        result = await self._inner.execute(
            node, context, workdir=run_dir,
            settings_override=settings_override,
        )
        if kind != "off" and result.worktree is None:
            result.worktree = run_dir
        return result

    async def _acquire_worktree(
        self,
        node_id: str,
        pool_key: str | None = None,
        group: str | None = None,
    ) -> str:
        """Return the worktree path for ``node_id``, creating it once.

        ``pool_key`` controls deduplication of in-flight creation tasks:
        - For group nodes: ``"group:<name>"`` so all siblings share one task.
        - For per-node (auto/isolated): ``node_id`` (default).

        ``_worktrees`` is always keyed by ``node_id`` (never by pool_key), so
        ``get_worktree`` / ``worktree_map`` expose node-level paths as expected
        by state persistence and rehydration.

        The retry-reuse check on ``node_id`` runs first, before pool-key
        dedup, so a retried node immediately reuses its own recorded path.
        """
        if pool_key is None:
            pool_key = node_id

        # Retry / resume reuse: if this node already has a live tree, make it
        # ready (idempotent) OUTSIDE the lock and return it.
        async with self._worktree_lock:
            existing = self._worktrees.get(node_id)
        if existing and Path(existing).is_dir():
            await self._ensure_worktree_ready(existing)
            return existing
        # Dedup in-flight creation by pool_key (group siblings share one task).
        async with self._worktree_lock:
            task = self._worktree_tasks.get(pool_key)
            if task is None:
                task = asyncio.create_task(self._create_worktree(pool_key, group))
                self._worktree_tasks[pool_key] = task

        try:
            path = await task
        finally:
            # Drop the finished task so a failed creation can be retried
            # rather than re-awaiting a task that holds a stale error.
            async with self._worktree_lock:
                if self._worktree_tasks.get(pool_key) is task:
                    del self._worktree_tasks[pool_key]

        # Record under the node's own id (not pool_key) — invariant for
        # get_worktree / worktree_map / state persistence.
        async with self._worktree_lock:
            self._worktrees[node_id] = path
        return path

    async def _create_worktree(self, pool_key: str, group: str | None = None) -> str:
        """Create a git worktree for the given pool_key.

        - Group trees use a deterministic path ``base/.sqrlly/wt-group-<name>``
          so every member of the group, and resumed runs, reuse the same tree
          without any uuid suffix. An ``is_dir`` check skips ``git worktree add``
          when the tree already exists (sibling races or prior-run resume).
        - Per-node trees use ``base/.sqrlly/wt-<safe_id>-<uuid8>``.

        The node_id→path write is done by ``_acquire_worktree``, not here —
        ``_worktrees`` must only be keyed by real node ids.
        """
        dest_dir = Path(self._base) / ".sqrlly"
        dest_dir.mkdir(parents=True, exist_ok=True)
        if group is not None:
            safe = group.replace("/", "_").replace("::", "__")
            # NOTE: group names that differ only by '/' vs '_' (or '::') map to
            # the same safe name and therefore the same wt-group-<safe> path;
            # authors should use distinct names that remain unique after sanitization.
            dest = dest_dir / f"wt-group-{safe}"
            if dest.is_dir() and (dest / ".git").exists():
                # Shared live worktree already exists (sibling created it, or prior run).
                self._created_paths.add(str(dest))
                await self._ensure_worktree_ready(str(dest))
                return str(dest)
        else:
            safe = pool_key.replace("::", "__").replace("/", "_")
            dest = dest_dir / f"wt-{safe}-{uuid.uuid4().hex[:8]}"

        proc = await asyncio.create_subprocess_exec(
            "git", "-C", self._base, "worktree", "add", "-q", str(dest), "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"foreman: 'git worktree add' failed for {pool_key}: "
                f"{err.decode().strip()}"
            )
        self._created_paths.add(str(dest))
        await self._ensure_worktree_ready(str(dest))
        return str(dest)

    async def _ensure_worktree_ready(self, dest: str) -> None:
        """Materialize read-only shares + run rehydrate setup for a worktree
        about to be handed back. Idempotent (share symlinks + setup sentinel).
        MUST be called WITHOUT self._worktree_lock held — ensure_setup runs
        subprocesses; holding the lock would serialize all acquisitions."""
        materialize_shares(self._base, dest, self._settings.worktree_share)
        await ensure_setup(
            base=self._base, dest=dest,
            commands=self._settings.worktree_setup,
            excludes=self._settings.worktree_setup_exclude,
        )

    async def reclaim(self) -> list[str]:
        """Remove every distinct worktree this foreman created.

        Returns the removed paths. The caller decides WHEN (end-of-run,
        success only); this method does not gate on run status.
        Group nodes share a single path — deduplication ensures each
        distinct tree is removed exactly once.
        """
        distinct = sorted(set(self._worktrees.values()) | self._created_paths)
        for path in distinct:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", self._base, "worktree", "remove", "--force", path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _out, err = await proc.communicate()
            if proc.returncode != 0:
                logger.warning(
                    "foreman: 'git worktree remove --force %s' failed (ignored): %s",
                    path, err.decode().strip(),
                )
        self._worktrees.clear()
        self._created_paths.clear()
        return distinct

    async def acquire_branch_worktree(self, branch_id: str) -> str:
        """Acquire (or reuse) one isolated worktree keyed by a fan-out branch
        id — mirrors inline-child keying so GC/resume/retry-reuse see it."""
        return await self._acquire_worktree(branch_id, branch_id, None)

    def get_worktree(self, node_id: str) -> str | None:
        """Return the worktree path for a node_id, or None if not yet allocated."""
        return self._worktrees.get(node_id)

    def worktree_map(self) -> dict[str, str]:
        """Snapshot of node_id → worktree path, for state persistence."""
        return dict(self._worktrees)

    def get_backend(self) -> PromptBackend | None:
        """Pass through to inner executor's backend (for .md LLM gates)."""
        if hasattr(self._inner, "get_backend"):
            return self._inner.get_backend()
        return None

    async def close(self) -> None:
        if hasattr(self._inner, "close"):
            await self._inner.close()
