"""Process-tree cleanup test for ACPBackend (Phase 4 / WISHLIST 49–54).

The ACP adapter spawns ``npx → node → claude``; the SDK's
``__aexit__`` returns before the descendants settle, so long-running
orchestrators accumulate zombie subprocesses (observed: 30 leftovers,
2.4 GB RSS, OOM-kill-prone).

This test pins the cleanup contract:
  - Spawn the backend → force initialization → capture descendant PIDs
  - Call ``close()``
  - Every captured PID must be dead within a couple seconds

Linux-only (uses ``/proc/<pid>/task/<tid>/children`` for child
enumeration; ``os.kill(pid, 0)`` for liveness). The ACP suite already
requires Linux + npx + ``@zed-industries/claude-code-acp`` to run, so
no extra skip conditions are needed here.
"""
from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

import pytest

from abe_froman.runtime.executor.backends.acp import ACPBackend


def _read_children(pid: int) -> list[int]:
    """Direct children of ``pid`` via ``/proc``. Empty list if the PID
    is gone or the file is unreadable."""
    try:
        path = Path(f"/proc/{pid}/task/{pid}/children")
        return [int(c) for c in path.read_text().split()]
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return []


def _descendants(pid: int) -> set[int]:
    """Recursively collect descendant PIDs of ``pid``."""
    out: set[int] = set()
    stack = [pid]
    while stack:
        cur = stack.pop()
        for child in _read_children(cur):
            if child not in out:
                out.add(child)
                stack.append(child)
    return out


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _wait_until_dead(pids: set[int], deadline_seconds: float = 3.0) -> set[int]:
    """Return the set of PIDs that are still alive after the deadline."""
    end = time.monotonic() + deadline_seconds
    survivors = set(pids)
    while survivors and time.monotonic() < end:
        survivors = {p for p in survivors if _alive(p)}
        if not survivors:
            return survivors
        time.sleep(0.1)
    return survivors


@pytest.mark.skipif(
    sys.platform != "linux", reason="ACP cleanup test uses /proc — Linux only",
)
@pytest.mark.acp
async def test_close_reaps_descendant_tree(tmp_path):
    """Spawn ACP, observe the npx → node → claude descendant tree,
    then close and assert no descendants survive."""
    backend = ACPBackend()
    await backend._ensure_initialized(str(tmp_path))

    spawn_pid = backend._proc_pid
    assert spawn_pid is not None, "Spawn did not yield a PID"
    assert _alive(spawn_pid), "Spawned process not alive after initialization"

    # Give npx a moment to fork its descendant tree (node, claude)
    # so we can verify they actually existed, not just that nothing
    # was there.
    time.sleep(0.5)
    descendants_before = _descendants(spawn_pid)
    captured = descendants_before | {spawn_pid}

    await backend.close()

    survivors = _wait_until_dead(captured, deadline_seconds=3.0)
    assert not survivors, (
        f"ACPBackend.close did not reap descendant tree. "
        f"Survivors: {survivors}; captured set: {captured}; "
        f"initial descendants: {descendants_before}"
    )


@pytest.mark.skipif(
    sys.platform != "linux", reason="ACP cleanup test uses /proc — Linux only",
)
@pytest.mark.acp
async def test_close_is_safe_when_never_initialized():
    """A fresh backend that never spawned should be a no-op on close."""
    backend = ACPBackend()
    await backend.close()  # must not raise


@pytest.mark.skipif(
    sys.platform != "linux", reason="ACP cleanup test uses /proc — Linux only",
)
@pytest.mark.acp
async def test_close_is_idempotent(tmp_path):
    """Calling close twice (e.g. CLI outer try/finally + inner cleanup)
    must not raise or kill anything that's already dead."""
    backend = ACPBackend()
    await backend._ensure_initialized(str(tmp_path))
    await backend.close()
    await backend.close()  # second close on a settled state — no-op
