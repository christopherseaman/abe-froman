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
import logging
from typing import Any

from abe_froman.runtime.result import ExecutionResult, OverloadError

logger = logging.getLogger(__name__)


def _is_overload_status(status: int | None) -> bool:
    """429 (rate limit) and 5xx-overload (502/503/504/529) all warrant
    a model downgrade attempt."""
    if status is None:
        return False
    return status == 429 or status in {502, 503, 504, 529}


class OpenAIBackend:
    """OpenAI-compatible PromptBackend using the ``openai`` SDK.

    Lazy client construction — first ``send_prompt`` call creates the
    ``AsyncOpenAI`` instance. ``close()`` shuts down the underlying
    httpx pool.
    """

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
    ):
        self._api_key = api_key
        self._base_url = base_url
        self._client: Any = None
        self._init_lock = asyncio.Lock()

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._init_lock:
            if self._client is not None:
                return self._client
            try:
                from openai import AsyncOpenAI
            except ImportError as e:
                raise RuntimeError(
                    "OpenAI backend requires the `openai` package. "
                    "Install with: uv sync --extra openai"
                ) from e
            self._client = AsyncOpenAI(
                api_key=self._api_key, base_url=self._base_url,
            )
            return self._client

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
            status = getattr(e, "status_code", None) or getattr(e, "status", None)
            if _is_overload_status(status):
                raise OverloadError(str(e)) from e
            # openai.RateLimitError doesn't always carry status_code as int;
            # name-based fallback covers it without coupling to the SDK's
            # exception hierarchy.
            if type(e).__name__ in {"RateLimitError", "APIConnectionError"}:
                raise OverloadError(str(e)) from e
            raise

        content = ""
        if resp.choices and resp.choices[0].message:
            content = resp.choices[0].message.content or ""

        return ExecutionResult(output=content)

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.close()
        except Exception:
            logger.warning("OpenAI client close failed", exc_info=True)
        self._client = None
