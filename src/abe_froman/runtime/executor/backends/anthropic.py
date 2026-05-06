"""Direct Anthropic API backend.

Talks to Anthropic's native Messages API via the ``anthropic`` Python
SDK. Mirrors the OpenAI backend shape: lazy ``AsyncAnthropic`` client,
async ``send_prompt``, transient-error mapping to ``OverloadError`` so
the dormant model-downgrade chain in ``PromptExecutor`` activates.

Model name handling: ``Settings.default_model`` and
``model_downgrade_chain`` use generic shorthand (``sonnet`` /
``opus`` / ``haiku``); this backend keeps a small inline alias table
to vendor IDs and passes through anything else verbatim, so authors
can pin a specific version (``claude-sonnet-4-6-20250821``) when they
need to.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from abe_froman.runtime.executor.backends._overload import maybe_raise_overload
from abe_froman.runtime.result import ExecutionResult

logger = logging.getLogger(__name__)


# Generic-name → vendor-ID resolution. Pass-through on miss so authors
# can use full Anthropic model IDs (e.g., ``claude-sonnet-4-6``) when
# they want to pin a version. Update the table when Anthropic releases
# a new headline model under one of the family names.
_MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
    "haiku": "claude-haiku-4-5",
}

# Default ``max_tokens`` for the Messages API. Anthropic requires this
# parameter; a generous default keeps long-form outputs from being
# truncated. Authors who care can pass through PromptParams in a
# future revision.
_DEFAULT_MAX_TOKENS = 8192


def _resolve_model(model: str) -> str:
    """Generic alias → vendor ID; pass-through otherwise."""
    return _MODEL_ALIASES.get(model, model)


# Anthropic SDK exception classes that don't always carry a numeric
# status_code but should still trigger downgrade. Class-name string
# match (vs class import) keeps this robust to SDK version churn.
# Verified against anthropic 0.99.0 — extend if a future version adds
# a transient-failure class not covered by the status-code path.
_OVERLOAD_EXCEPTION_NAMES = frozenset({
    "RateLimitError",        # 429
    "APIConnectionError",    # transient network
    "APITimeoutError",       # request timeout
    "InternalServerError",   # 5xx
})


class AnthropicBackend:
    """Direct Anthropic Messages API PromptBackend.

    Lazy client construction — first ``send_prompt`` call creates the
    ``AsyncAnthropic`` instance. ``close()`` shuts down the underlying
    httpx pool.
    """

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._client: Any = None
        self._init_lock = asyncio.Lock()

    async def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._init_lock:
            if self._client is not None:
                return self._client
            try:
                from anthropic import AsyncAnthropic
            except ImportError as e:
                raise RuntimeError(
                    "Anthropic backend requires the `anthropic` package. "
                    "Install with: uv sync --extra anthropic"
                ) from e
            self._client = AsyncAnthropic(api_key=self._api_key)
            return self._client

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        client = await self._ensure_client()
        resolved_model = _resolve_model(model)

        try:
            coro = client.messages.create(
                model=resolved_model,
                max_tokens=_DEFAULT_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            if timeout is not None:
                resp = await asyncio.wait_for(coro, timeout=timeout)
            else:
                resp = await coro
        except Exception as e:
            maybe_raise_overload(e, class_names=_OVERLOAD_EXCEPTION_NAMES)
            raise

        if not resp.content:
            return ExecutionResult(
                success=False,
                error=(
                    f"Anthropic API returned no content blocks "
                    f"(model={resolved_model!r}). Likely a content "
                    f"filter, refusal, or upstream truncation."
                ),
            )

        # The Messages API response carries content as a list of
        # blocks (text / tool_use / etc.). For prompt-mode dispatch we
        # expect a text block; concatenate any non-empty text blocks
        # present. An empty-string text block is treated as no text
        # (asymmetry with "no text block" → loud failure would be a
        # silent-empty-success footgun otherwise).
        text_parts = [
            getattr(block, "text", "")
            for block in resp.content
            if getattr(block, "type", None) == "text"
            and getattr(block, "text", "")
        ]
        if not text_parts:
            return ExecutionResult(
                success=False,
                error=(
                    f"Anthropic API returned content blocks but no "
                    f"non-empty text block (model={resolved_model!r}). "
                    f"Got types: "
                    f"{[getattr(b, 'type', '?') for b in resp.content]!r}."
                ),
            )
        return ExecutionResult(output="".join(text_parts))

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.close()
        except Exception:
            logger.warning("Anthropic client close failed", exc_info=True)
        self._client = None
