"""Shared transient-error → ``OverloadError`` mapping for SDK backends.

Both ``OpenAIBackend`` and ``AnthropicBackend`` need to translate a
provider's transient HTTP failures (rate limits, 5xx-overloads,
connection blips) into ``OverloadError`` so the model-downgrade chain
in ``PromptExecutor`` activates. The status-code set and the
class-name-string fallback live here so a change to one backend's set
surfaces alongside the other's at review time.

Class-name string match (rather than ``isinstance`` against imported
SDK exception classes) keeps the check robust to SDK version churn
and avoids importing the SDKs at module load time.
"""
from __future__ import annotations

from sqrlly.runtime.result import OverloadError

# 429 = rate limit; 502/503/504/529 = upstream overload / gateway timeout.
_OVERLOAD_STATUSES = frozenset({429, 502, 503, 504, 529})

# Anthropic SDK exception classes that don't always carry a numeric
# status_code but should still trigger downgrade. Verified against
# anthropic 0.99.0; extend if a future version adds a transient-failure
# class not covered by the status-code path.
ANTHROPIC_OVERLOAD_NAMES = frozenset({
    "RateLimitError",        # 429
    "APIConnectionError",    # transient network
    "APITimeoutError",       # request timeout
    "InternalServerError",   # 5xx
})

# OpenAI/DeepSeek SDK exception classes that don't always carry a
# numeric status_code but should still trigger downgrade.
OPENAI_OVERLOAD_NAMES = frozenset({"RateLimitError", "APIConnectionError"})


def _is_overload_status(status: int | None) -> bool:
    return status is not None and status in _OVERLOAD_STATUSES


def maybe_raise_overload(exc: BaseException, *, class_names: frozenset[str]) -> None:
    """If ``exc`` looks like a transient SDK error, raise ``OverloadError``.

    Returns silently otherwise — the caller re-raises ``exc`` to let
    real failures (4xx, ValueError, etc.) propagate.

    Two checks, in order:
      1. Numeric status on the exception (``status_code`` then ``status``)
         falls in the overload set.
      2. The exception's class name appears in ``class_names`` —
         provider-specific (e.g. Anthropic adds ``OverloadedError``,
         OpenAI uses just ``RateLimitError`` / ``APIConnectionError``).
    """
    # `status_code` first, `status` as fallback — `is not None` rather
    # than `or` so a literal 0 status_code isn't skipped over.
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(exc, "status", None)
    if _is_overload_status(status):
        raise OverloadError(str(exc)) from exc
    if type(exc).__name__ in class_names:
        raise OverloadError(str(exc)) from exc
