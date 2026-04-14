from __future__ import annotations

from typing import Protocol, runtime_checkable

from abe_froman.runtime.result import ExecutionResult, OverloadError

# PromptBackendResult is the historical name for the unified
# ExecutionResult. Kept as an alias so existing call sites continue to
# work during the refactor. Deleted in Step 14.
PromptBackendResult = ExecutionResult

__all__ = ["PromptBackend", "PromptBackendResult", "OverloadError"]


@runtime_checkable
class PromptBackend(Protocol):
    """Backend-agnostic protocol for sending rendered prompts to an agent.

    Implementations handle only the transport layer — template rendering
    and model resolution are handled upstream by PromptExecutor.

    Backends return ExecutionResult(success=True, ...) or raise
    OverloadError for 529/overload events. They never set success=False
    directly — the executor owns retry and classification policy.
    """

    async def send_prompt(
        self, prompt: str, model: str, workdir: str
    ) -> ExecutionResult: ...

    async def close(self) -> None: ...
