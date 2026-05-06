"""Shared transient-error → ``OverloadError`` mapping for SDK backends.

Both ``OpenAIBackend`` and ``AnthropicBackend`` need to translate a
provider's transient HTTP failures (rate limits, 5xx-overloads,
connection blips) into ``OverloadError`` so the model-downgrade chain
in ``PromptExecutor`` activates. The status-code set and the
class-name-string fallback are identical across providers — keeping
them in one place avoids drift between the two backends.

Class-name string match (rather than ``isinstance`` against imported
SDK exception classes) keeps the check robust to SDK version churn
and avoids importing the SDKs at module load time.
"""
from __future__ import annotations

from abe_froman.runtime.result import OverloadError

# 429 = rate limit; 502/503/504/529 = upstream overload / gateway timeout.
_OVERLOAD_STATUSES = frozenset({429, 502, 503, 504, 529})


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
    status = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    if _is_overload_status(status):
        raise OverloadError(str(exc)) from exc
    if type(exc).__name__ in class_names:
        raise OverloadError(str(exc)) from exc
