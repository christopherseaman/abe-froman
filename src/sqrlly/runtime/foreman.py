"""ForemanExecutor: queue + per-model semaphores + worktree pool.

Wraps an inner `NodeExecutor` (typically `DispatchExecutor`) and adds:
  - A **global** `asyncio.Semaphore` bounding parallel jobs.
  - **Per-model** semaphores layered inside the global cap.
  - **Memory back-pressure** — when ``settings.memory_threshold_pct`` is
    set, blocks new dispatches while host memory percent is above it.
    Composes (AND) with the semaphores; in-flight jobs are never
    aborted by this gate.
  - A **worktree pool** — each `node.id` gets a dedicated git worktree, reused
    across retries so the agent can iterate on its own prior files.

Foreman is LangGraph-agnostic: it imports nothing from `compile/` or `langgraph`.
The retry decision lives at the compile layer; foreman just runs what's handed
to it.

Worktree lifecycle: foreman creates worktrees on first `execute()` per
`node_id`. It does NOT clean them up — author-written reconciliation nodes
copy outputs out, and stray worktrees are `git worktree remove`d by the user.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
import uuid
from pathlib import Path
from typing import Any

import psutil

from sqrlly.runtime.executor.prompt import resolve_model
from sqrlly.runtime.result import ExecutionResult, NodeExecutor, PromptBackend
from sqrlly.schema.models import Node, Settings

logger = logging.getLogger(__name__)

# How long to wait between memory-pressure re-checks while gated.
# Short enough to react quickly when an in-flight job releases
# memory; long enough to avoid burning CPU polling.
_MEMORY_POLL_INTERVAL_S = 1.0


class ForemanExecutor:
    """NodeExecutor wrapper adding concurrency caps + worktree pool."""

    def __init__(
        self,
        inner: NodeExecutor,
        base_workdir: str,
        max_parallel_jobs: int = 4,
        per_model_limits: dict[str, int] | None = None,
        rehydrate: dict[str, str] | None = None,
        settings: Settings | None = None,
        memory_poll_interval_s: float = _MEMORY_POLL_INTERVAL_S,
    ):
        self._inner = inner
        self._base = base_workdir
        self._global_sem = asyncio.Semaphore(max_parallel_jobs)
        self._model_sems: dict[str, asyncio.Semaphore] = {
            model: asyncio.Semaphore(n)
            for model, n in (per_model_limits or {}).items()
        }
        self._worktrees: dict[str, str] = dict(rehydrate or {})
        self._worktree_lock = asyncio.Lock()
        self._settings = settings or Settings()
        self._memory_poll_s = memory_poll_interval_s

    async def execute(
        self,
        node: Node,
        context: dict[str, Any],
        workdir: str | None = None,
        settings_override: Settings | None = None,
    ) -> ExecutionResult:
        # Per-model semaphore selection respects the scope's settings —
        # a subgraph that overrides default_model gets its concurrency
        # accounted under the subgraph's tier, not the parent's.
        s = settings_override or self._settings
        model = resolve_model(node, s)
        model_sem = self._model_sems.get(model)

        # Memory back-pressure runs OUTSIDE the semaphores so that gated
        # acquisitions don't sit holding a slot while waiting for memory
        # to drop. Both gates default to ``None`` (disabled) and AND-
        # compose when both set.
        await self._wait_for_memory(
            threshold_pct=s.memory_threshold_pct,
            min_available_bytes=s.memory_min_available_bytes,
            node_id=node.id,
        )

        async with self._global_sem:
            async with (model_sem or _null_async_cm()):
                wt = await self._acquire_worktree(node.id)
                return await self._inner.execute(
                    node, context, workdir=wt,
                    settings_override=settings_override,
                )

    async def _wait_for_memory(
        self,
        *,
        threshold_pct: float | None,
        min_available_bytes: int | None,
        node_id: str,
    ) -> None:
        """Block until BOTH memory gates allow dispatch.

        ``None`` for either argument disables that gate; ``None`` for
        both is a fast no-op. Gates AND-compose: dispatch only proceeds
        when every set gate is satisfied. In-flight jobs are never
        affected — only new acquisitions wait.
        """
        if threshold_pct is None and min_available_bytes is None:
            return
        first_block = True
        while True:
            mem = psutil.virtual_memory()
            pct_blocked = (
                threshold_pct is not None and mem.percent > threshold_pct
            )
            avail_blocked = (
                min_available_bytes is not None
                and mem.available < min_available_bytes
            )
            if not (pct_blocked or avail_blocked):
                return
            if first_block:
                logger.info(
                    "foreman: memory back-pressure gating dispatch of %r "
                    "(percent=%.1f, available=%.0f MB; "
                    "pct_blocked=%s, avail_blocked=%s)",
                    node_id, mem.percent, mem.available / 1e6,
                    pct_blocked, avail_blocked,
                )
                first_block = False
            await asyncio.sleep(self._memory_poll_s)

    async def _acquire_worktree(self, node_id: str) -> str:
        async with self._worktree_lock:
            existing = self._worktrees.get(node_id)
            if existing and Path(existing).is_dir():
                return existing
            path = await self._create_worktree(node_id)
            self._worktrees[node_id] = path
            return path

    async def _create_worktree(self, node_id: str) -> str:
        """Create a git worktree at base/.sqrlly/wt-<id>-<uuid>."""
        safe_id = node_id.replace("::", "__").replace("/", "_")
        dest_dir = Path(self._base) / ".sqrlly"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"wt-{safe_id}-{uuid.uuid4().hex[:8]}"

        # git worktree add <dest> HEAD — uses the current HEAD as starting point.
        # Runs synchronously; short-lived. Raises on failure — loud by design.
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", self._base, "worktree", "add", "-q",
            str(dest), "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"foreman: 'git worktree add' failed for {node_id}: "
                f"{stderr.decode().strip()}"
            )
        return str(dest)

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


def _null_async_cm():
    """A no-op async context manager for when no per-model semaphore exists."""
    return contextlib.nullcontext()
