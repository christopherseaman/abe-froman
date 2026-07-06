"""Host-memory back-pressure — an optional, default-off dispatch gate.

Isolated in its own module so the sole ``psutil`` dependency lives here,
not in the foreman. When ``settings.memory_threshold_pct`` is ``None``
(the default) nothing here runs and ``psutil`` is never called.
"""
from __future__ import annotations

import asyncio
import logging

import psutil

logger = logging.getLogger(__name__)

# Seconds between memory-pressure re-checks while gated. Short enough to
# react when an in-flight job releases memory; long enough not to burn CPU.
POLL_INTERVAL_S = 1.0


async def wait_for_memory(
    threshold_pct: float | None,
    *,
    node_id: str,
    poll_interval: float = POLL_INTERVAL_S,
) -> None:
    """Block while host memory percent is above ``threshold_pct``.

    ``None`` disables the gate (a fast no-op — the common case). In-flight
    jobs are never affected; only new acquisitions wait. Runs OUTSIDE the
    foreman's semaphores so a gated dispatch doesn't hold a slot while
    waiting for memory to drop.
    """
    if threshold_pct is None:
        return
    first_block = True
    while True:
        mem = psutil.virtual_memory()
        if mem.percent <= threshold_pct:
            return
        if first_block:
            logger.info(
                "foreman: memory back-pressure gating dispatch of %r "
                "(percent=%.1f > threshold %.1f)",
                node_id, mem.percent, threshold_pct,
            )
            first_block = False
        await asyncio.sleep(poll_interval)
