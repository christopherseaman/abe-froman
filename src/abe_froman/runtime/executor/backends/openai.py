"""OpenAI-compatible chat-completions backend.

Supports any provider that ships an OpenAI-compatible REST surface
(OpenAI, DeepSeek, Mistral, Together, Fireworks, local Ollama, etc.)
by overriding ``base_url``. The factory wires DeepSeek as the default
non-OpenAI use case via ``base_url="https://api.deepseek.com/v1"``.

Maps 429 / 5xx-overload errors to ``OverloadError`` so the dormant
model-downgrade chain in ``PromptExecutor`` activates the same way it
does for ACP.
"""
from __future__ import annotations

import asyncio
from typing import Any

from abe_froman.runtime.executor.backends._lazy_client import LazyClientMixin
from abe_froman.runtime.executor.backends._overload import maybe_raise_overload
from abe_froman.runtime.result import ExecutionResult

# OpenAI/DeepSeek SDK exception classes that don't always carry a
# numeric status_code but should still trigger downgrade.
_OVERLOAD_EXCEPTION_NAMES = frozenset({"RateLimitError", "APIConnectionError"})


class OpenAIBackend(LazyClientMixin):
    """OpenAI-compatible PromptBackend using the ``openai`` SDK.

    Lazy client construction — first ``send_prompt`` call creates the
    ``AsyncOpenAI`` instance. ``close()`` shuts down the underlying
    httpx pool.
    """

    _close_label = "OpenAI"

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
    ):
        super().__init__()
        self._api_key = api_key
        self._base_url = base_url

    async def _create_client(self) -> Any:
        try:
            from openai import AsyncOpenAI
        except ImportError as e:
            raise RuntimeError(
                "OpenAI backend requires the `openai` package. "
                "Install with: uv sync --extra openai"
            ) from e
        return AsyncOpenAI(api_key=self._api_key, base_url=self._base_url)

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        client = await self._ensure_client()
        try:
            coro = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
            )
            if timeout is not None:
                resp = await asyncio.wait_for(coro, timeout=timeout)
            else:
                resp = await coro
        except Exception as e:
            maybe_raise_overload(e, class_names=_OVERLOAD_EXCEPTION_NAMES)
            raise

        if not resp.choices:
            return ExecutionResult(
                success=False,
                error=(
                    f"OpenAI-compatible API returned no choices "
                    f"(model={model!r}). Likely a content filter, "
                    f"refusal, or upstream truncation."
                ),
            )
        content = resp.choices[0].message.content or ""
        return ExecutionResult(output=content)
