from __future__ import annotations

import asyncio
from typing import Any

from abe_froman.executor.prompt_backend import PromptBackendResult

try:
    from acp import spawn_agent_process, text_block
    from acp.interfaces import Client

    HAS_ACP = True
except ImportError:
    HAS_ACP = False
    Client = object  # type: ignore[assignment, misc]


class _ResponseAccumulator:
    """Collects text chunks from session_update callbacks."""

    def __init__(self) -> None:
        self.chunks: list[str] = []

    def append(self, text: str) -> None:
        self.chunks.append(text)

    def text(self) -> str:
        return "".join(self.chunks)


class _ACPClient(Client):
    """Minimal ACP Client that accumulates agent responses."""

    def __init__(self) -> None:
        self.accumulator: _ResponseAccumulator | None = None

    async def request_permission(
        self, options: Any, session_id: str, tool_call: Any, **kwargs: Any
    ) -> dict:
        return {"outcome": {"outcome": "approved"}}

    async def session_update(
        self, session_id: str, update: Any, **kwargs: Any
    ) -> None:
        if self.accumulator is None:
            return
        # Import lazily to avoid issues when ACP is not installed
        from acp.schema import AgentMessageChunk, TextContentBlock

        if isinstance(update, AgentMessageChunk):
            for block in getattr(update, "content", []):
                if isinstance(block, TextContentBlock):
                    self.accumulator.append(block.text)


class ACPBackend:
    """ACP backend using claude-code-acp adapter.

    Manages a single ACP process lifecycle. The process is spawned lazily
    on the first send_prompt call and reused for subsequent calls.
    """

    def __init__(
        self,
        program: str = "npx",
        args: tuple[str, ...] = ("@zed-industries/claude-code-acp",),
    ):
        if not HAS_ACP:
            raise ImportError(
                "agent-client-protocol is not installed. "
                "Install with: uv add agent-client-protocol"
            )
        self._program = program
        self._args = args
        self._client = _ACPClient()
        self._conn: Any = None
        self._proc: Any = None
        self._session_id: str | None = None
        self._ctx_manager: Any = None
        self._initialized = False

    async def _ensure_initialized(self, workdir: str) -> None:
        """Lazy init: spawn process and create session on first use."""
        if self._initialized:
            return

        self._ctx_manager = spawn_agent_process(
            self._client, self._program, *self._args
        )
        self._conn, self._proc = await self._ctx_manager.__aenter__()
        await self._conn.initialize(protocol_version=1)
        session = await self._conn.new_session(cwd=workdir, mcp_servers=[])
        self._session_id = session.session_id
        self._initialized = True

    async def send_prompt(
        self, prompt: str, model: str, workdir: str
    ) -> PromptBackendResult:
        await self._ensure_initialized(workdir)

        accumulator = _ResponseAccumulator()
        self._client.accumulator = accumulator

        await self._conn.prompt(
            session_id=self._session_id,
            prompt=[text_block(prompt)],
        )

        output = accumulator.text()
        self._client.accumulator = None

        return PromptBackendResult(output=output)

    async def close(self) -> None:
        if self._ctx_manager is not None:
            try:
                await self._ctx_manager.__aexit__(None, None, None)
            except Exception:
                pass
            self._conn = None
            self._proc = None
            self._session_id = None
            self._initialized = False
