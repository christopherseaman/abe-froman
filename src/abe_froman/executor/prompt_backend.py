from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass
class PromptBackendResult:
    """Raw result from a prompt backend."""

    output: str
    structured_output: dict[str, Any] | None = None
    tokens_used: dict[str, int] | None = None


class OverloadError(Exception):
    """Raised by a PromptBackend when the API returns 529/overloaded."""

    pass


@runtime_checkable
class PromptBackend(Protocol):
    """Backend-agnostic protocol for sending rendered prompts to an agent.

    Implementations handle only the transport layer — template rendering
    and model resolution are handled upstream by PromptExecutor.
    """

    async def send_prompt(
        self, prompt: str, model: str, workdir: str
    ) -> PromptBackendResult: ...

    async def close(self) -> None: ...
