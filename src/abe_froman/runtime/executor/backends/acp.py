from __future__ import annotations

import asyncio
import logging
from typing import Any

from acp import spawn_agent_process, text_block
from acp.interfaces import Client

from abe_froman.runtime.result import OverloadError
from abe_froman.runtime.result import ExecutionResult

logger = logging.getLogger(__name__)


def _is_overload_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    if "529" in msg or "overload" in msg:
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    return status == 529


class _ACPCallbacks(Client):
    """ACP Client that collects text chunks inline."""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    async def request_permission(
        self, options: Any, session_id: str, tool_call: Any, **kwargs: Any
    ) -> Any:
        from acp.schema import AllowedOutcome, RequestPermissionResponse

        option_id = options[0].id if options else "allow"
        return RequestPermissionResponse(
            outcome=AllowedOutcome(option_id=option_id, outcome="selected"),
        )

    async def session_update(
        self, session_id: str, update: Any, **kwargs: Any
    ) -> None:
        from acp.schema import AgentMessageChunk, TextContentBlock

        if isinstance(update, AgentMessageChunk):
            if isinstance(update.content, TextContentBlock):
                self.chunks.append(update.content.text)

    def reset(self) -> None:
        self.chunks.clear()

    def text(self) -> str:
        return "".join(self.chunks)


class ACPBackend:
    """ACP backend using claude-code-acp adapter.

    Spawns lazily on first send_prompt, reuses for subsequent calls.
    """

    def __init__(
        self,
        program: str = "npx",
        args: tuple[str, ...] = ("@zed-industries/claude-code-acp",),
    ):
        self._program = program
        self._args = args
        self._callbacks = _ACPCallbacks()
        self._conn: Any = None
        self._proc: Any = None
        self._session_id: str | None = None
        self._ctx_manager: Any = None
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()

    async def _ensure_initialized(self, workdir: str) -> None:
        if self._initialized:
            return
        async with self._init_lock:
            if self._initialized:
                return
            self._ctx_manager = spawn_agent_process(
                self._callbacks, self._program, *self._args
            )
            self._conn, self._proc = await self._ctx_manager.__aenter__()
            await self._conn.initialize(protocol_version=1)
            session = await self._conn.new_session(cwd=workdir, mcp_servers=[])
            self._session_id = session.session_id
            self._initialized = True

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        await self._ensure_initialized(workdir)

        async with self._send_lock:
            self._callbacks.reset()

            try:
                coro = self._conn.prompt(
                    session_id=self._session_id,
                    prompt=[text_block(prompt)],
                )
                if timeout is not None:
                    await asyncio.wait_for(coro, timeout=timeout)
                else:
                    await coro
            except Exception as e:
                if _is_overload_error(e):
                    raise OverloadError(str(e)) from e
                raise

            return ExecutionResult(output=self._callbacks.text())

    async def close(self) -> None:
        if self._ctx_manager is None:
            return
        try:
            await self._ctx_manager.__aexit__(None, None, None)
        except Exception:
            logger.warning("ACP process cleanup failed", exc_info=True)
            proc = self._proc
            if proc is not None and getattr(proc, "returncode", 0) is None:
                try:
                    proc.terminate()
                except Exception:
                    logger.warning("ACP process terminate failed", exc_info=True)
        self._conn = None
        self._proc = None
        self._session_id = None
        self._ctx_manager = None
        self._initialized = False
