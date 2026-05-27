"""Shared transient-error → ``OverloadError`` mapping for SDK backends.

The ACP backend needs to translate the adapter's transient failures
(rate limits, 5xx-overloads from the upstream Claude API) into
``OverloadError`` so the model-downgrade chain in ``PromptExecutor``
activates. The status-code set lives here so future backends can reuse
the same shape if/when the api transport (or another) is restored.
"""
from __future__ import annotations

from sqrlly.runtime.result import OverloadError

# 429 = rate limit; 502/503/504/529 = upstream overload / gateway timeout.
_OVERLOAD_STATUSES = frozenset({429, 502, 503, 504, 529})

# ACP errors are message-shaped, not attribute-shaped — the ACP
# adapter surfaces overloads as plain exceptions whose text carries
# the signal. These substrings (lower-cased) trigger downgrade.
ACP_OVERLOAD_SUBSTRINGS = frozenset({"529", "overload"})


def _is_overload_status(status: int | None) -> bool:
    return status is not None and status in _OVERLOAD_STATUSES


def maybe_raise_overload(
    exc: BaseException,
    *,
    class_names: frozenset[str] = frozenset(),
    message_substrings: frozenset[str] = frozenset(),
) -> None:
    """If ``exc`` looks like a transient SDK error, raise ``OverloadError``.

    Returns silently otherwise — the caller re-raises ``exc`` to let
    real failures (4xx, ValueError, etc.) propagate.

    Three checks, in order:
      1. Numeric status on the exception (``status_code`` then ``status``)
         falls in the overload set.
      2. The exception's class name appears in ``class_names`` —
         provider-specific (e.g. Anthropic adds ``OverloadedError``,
         OpenAI uses just ``RateLimitError`` / ``APIConnectionError``).
      3. Any of ``message_substrings`` (case-insensitive) appears in
         ``str(exc)`` — for backends like ACP whose errors are
         message-shaped, not attribute-shaped.
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
    if message_substrings:
        msg = str(exc).lower()
        if any(sub in msg for sub in message_substrings):
            raise OverloadError(str(exc)) from exc
