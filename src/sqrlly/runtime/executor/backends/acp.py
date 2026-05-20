from __future__ import annotations

import asyncio
import logging
import os
import signal
from typing import Any

from acp import spawn_agent_process, text_block
from acp.interfaces import Client

from sqrlly.runtime.executor.backends._lazy_client import await_with_timeout
from sqrlly.runtime.executor.backends._overload import (
    ACP_OVERLOAD_SUBSTRINGS,
    maybe_raise_overload,
)
from sqrlly.runtime.result import ExecutionResult

logger = logging.getLogger(__name__)


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

    Process-tree cleanup (Phase 4 / WISHLIST 49–54): the SDK's
    ``spawn_agent_process`` exposes the spawned ``npx`` process but
    NOT its descendant tree (``node`` → ``claude``). Without explicit
    teardown, ``__aexit__`` returns before the children settle and
    long-running orchestrators accumulate zombie subprocesses.
    ``close()`` now SIGTERMs the spawned PID's process group, waits
    briefly for graceful shutdown, then SIGKILLs anything still alive
    — independent of whether the SDK or shell wrapper opted into a
    new session itself.
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
        self._proc_pid: int | None = None
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
            self._proc_pid = getattr(self._proc, "pid", None)
            # Best-effort: place the spawned process in its own process
            # group so the descendant tree is killable via os.killpg.
            # If the SDK already did this, setpgid is a no-op; if exec
            # has already happened the call may EACCES — both are fine,
            # the killpg path below tolerates either outcome.
            if self._proc_pid is not None:
                try:
                    os.setpgid(self._proc_pid, self._proc_pid)
                except (PermissionError, ProcessLookupError, OSError):
                    pass
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
                await await_with_timeout(coro, timeout)
            except Exception as e:
                # ACP errors are message-shaped — maybe_raise_overload
                # checks the substrings; raises OverloadError on a hit,
                # returns silently otherwise so we re-raise the original.
                maybe_raise_overload(e, message_substrings=ACP_OVERLOAD_SUBSTRINGS)
                raise

            return ExecutionResult(output=self._callbacks.text())

    async def close(self) -> None:
        if self._ctx_manager is None:
            return
        pid = self._proc_pid
        # Capture descendants BEFORE graceful shutdown. Once npx exits
        # during __aexit__, its grandchildren (node, claude) reparent
        # to PID 1 — at that point /proc walks from spawn_pid no
        # longer reach them. The pre-shutdown snapshot is what lets
        # us kill the orphaned tree below.
        pre_targets: set[int] = set()
        if pid is not None:
            pre_targets = self._collect_descendants(pid) | {pid}

        # Phase 1: graceful shutdown via the SDK's __aexit__. Capped
        # at 5s — past that we hard-kill rather than wait for a hung
        # adapter.
        # `asyncio.TimeoutError` is a subclass of `Exception` on
        # 3.11+; one bare except covers both the timeout and any
        # SDK-internal failure during teardown. Phase 2 below will
        # hard-kill regardless.
        try:
            await asyncio.wait_for(
                self._ctx_manager.__aexit__(None, None, None), timeout=5.0,
            )
        except Exception:
            logger.warning("ACP graceful shutdown failed", exc_info=True)

        # Phase 2: hard-kill the captured descendants (now orphans).
        if pre_targets:
            await self._kill_pids(pre_targets)

        self._conn = None
        self._proc = None
        self._proc_pid = None
        self._session_id = None
        self._ctx_manager = None
        self._initialized = False

    async def _kill_pids(self, pids: set[int]) -> None:
        """SIGTERM each PID, wait briefly, SIGKILL survivors.

        ``npx`` re-spawns ``node`` and ``claude`` in their own process
        groups, and once ``npx`` exits its descendants reparent to
        PID 1 — so a ``killpg`` on the spawn group misses most of the
        tree. Capturing PIDs pre-shutdown and signaling each one is
        robust against both decisions.
        """
        for p in pids:
            try:
                os.kill(p, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        await asyncio.sleep(0.5)
        for p in pids:
            try:
                os.kill(p, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass

    @staticmethod
    def _collect_descendants(pid: int) -> set[int]:
        """Recursively gather descendant PIDs of ``pid`` via ``/proc``.
        Empty set on non-Linux or when the PID has already been reaped."""
        out: set[int] = set()
        stack = [pid]
        while stack:
            cur = stack.pop()
            try:
                with open(f"/proc/{cur}/task/{cur}/children") as f:
                    children = [int(c) for c in f.read().split()]
            except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
                continue
            for c in children:
                if c not in out:
                    out.add(c)
                    stack.append(c)
        return out
