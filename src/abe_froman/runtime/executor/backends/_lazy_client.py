"""Shared lazy-init/close machinery for HTTP-based PromptBackends.

Anthropic and OpenAI backends both wrap an SDK client whose
construction is deferred until first call (zero import-time cost) and
whose teardown is idempotent. This mixin captures that pattern so
each backend file is just the SDK-specific wiring (model resolution,
response shape, error mapping). The ``await_with_timeout`` helper
captures the matching await-with-optional-timeout shape.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable

logger = logging.getLogger(__name__)


async def await_with_timeout(coro: Awaitable[Any], timeout: float | None) -> Any:
    """Await ``coro`` with optional timeout. ``timeout=None`` awaits
    without bound; otherwise delegates to ``asyncio.wait_for``."""
    if timeout is None:
        return await coro
    return await asyncio.wait_for(coro, timeout=timeout)


class LazyClientMixin:
    """Lazy-init + idempotent-close for an SDK client field.

    Subclasses must implement ``_create_client()`` and may override
    ``_close_label`` to label the close-failure log line. The mixin
    owns ``_client`` and ``_init_lock``; ``__init__`` order:
    subclass's ``__init__`` sets its own fields, then calls
    ``super().__init__()``.
    """

    _close_label: str = "Backend"

    def __init__(self) -> None:
        self._client: Any = None
        self._init_lock = asyncio.Lock()

    async def _create_client(self) -> Any:
        raise NotImplementedError

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._init_lock:
            if self._client is not None:
                return self._client
            self._client = await self._create_client()
            return self._client

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.close()
        except Exception:
            logger.warning(
                "%s client close failed", self._close_label, exc_info=True,
            )
        self._client = None
