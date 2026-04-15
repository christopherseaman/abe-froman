from __future__ import annotations

from typing import Any

from acp import spawn_agent_process, text_block
from acp.interfaces import Client

from abe_froman.runtime.result import OverloadError
from abe_froman.runtime.result import ExecutionResult


def _is_overload_error(exc: Exception) -> bool:
    """Detect 529/overload errors from exception message or attributes."""
    msg = str(exc).lower()
    if "529" in msg or "overload" in msg:
        return True
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if status == 529:
        return True
    return False


class _ResponseAccumulator:
    """Collects text chunks and usage metadata from session_update callbacks."""

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.input_tokens: int = 0
        self.output_tokens: int = 0

    def append(self, text: str) -> None:
        self.chunks.append(text)

    def add_usage(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens

    def text(self) -> str:
        return "".join(self.chunks)

    def tokens_used(self) -> dict[str, int] | None:
        if self.input_tokens or self.output_tokens:
            return {"input": self.input_tokens, "output": self.output_tokens}
        return None


class _ACPClient(Client):
    """Minimal ACP Client that accumulates agent responses."""

    def __init__(self) -> None:
        self.accumulator: _ResponseAccumulator | None = None

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
        if self.accumulator is None:
            return
        from acp.schema import AgentMessageChunk, TextContentBlock

        if isinstance(update, AgentMessageChunk):
            if isinstance(update.content, TextContentBlock):
                self.accumulator.append(update.content.text)
            usage = getattr(update, "usage", None)
            if usage is not None:
                inp = getattr(usage, "input_tokens", 0) or 0
                out = getattr(usage, "output_tokens", 0) or 0
                if inp or out:
                    self.accumulator.add_usage(inp, out)


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
    ) -> ExecutionResult:
        await self._ensure_initialized(workdir)

        accumulator = _ResponseAccumulator()
        self._client.accumulator = accumulator

        try:
            await self._conn.prompt(
                session_id=self._session_id,
                prompt=[text_block(prompt)],
            )
        except Exception as e:
            if _is_overload_error(e):
                raise OverloadError(str(e)) from e
            raise

        output = accumulator.text()
        self._client.accumulator = None

        return ExecutionResult(output=output, tokens_used=accumulator.tokens_used())

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
