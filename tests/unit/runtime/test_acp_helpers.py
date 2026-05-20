"""Unit tests for pure helpers in runtime/executor/backends/acp.py.

Covers ACP overload mapping (via the shared `maybe_raise_overload` +
`ACP_OVERLOAD_SUBSTRINGS`) and the usage-accumulation path inside
`_ACPCallbacks.session_update`. No ACP process needed — these are
pure data transformations.
"""

from types import SimpleNamespace

import pytest

from sqrlly.runtime.executor.backends._overload import (
    ACP_OVERLOAD_SUBSTRINGS,
    maybe_raise_overload,
)
from sqrlly.runtime.executor.backends.acp import _ACPCallbacks
from sqrlly.runtime.result import OverloadError


# ---------------------------------------------------------------------------
# ACP overload mapping — maybe_raise_overload + ACP_OVERLOAD_SUBSTRINGS
#
# ACP errors are message-shaped (generic exceptions, no status_code
# attribute), so ACP passes its overload substrings to the shared
# mapper. maybe_raise_overload RAISES OverloadError on a hit and
# returns silently otherwise (caller re-raises the original).
# ---------------------------------------------------------------------------


class TestACPOverloadMapping:
    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("529 overloaded, try again"),
            RuntimeError("API is overloaded"),
            RuntimeError("OVERLOAD detected"),  # case-insensitive
            RuntimeError("http 529 response"),
        ],
    )
    def test_overload_message_raises(self, exc):
        with pytest.raises(OverloadError):
            maybe_raise_overload(exc, message_substrings=ACP_OVERLOAD_SUBSTRINGS)

    @pytest.mark.parametrize(
        "exc",
        [
            RuntimeError("connection refused"),
            ValueError("bad input"),
            RuntimeError(""),
            Exception("innocent"),
        ],
    )
    def test_non_overload_returns_silently(self, exc):
        # No raise — returns None; the ACP caller then re-raises `exc`.
        maybe_raise_overload(exc, message_substrings=ACP_OVERLOAD_SUBSTRINGS)

    def test_status_529_attribute_still_caught(self):
        """maybe_raise_overload also checks numeric status — an exception
        carrying status_code 529 raises even without a message match."""
        exc = RuntimeError("something went wrong")
        exc.status_code = 529
        with pytest.raises(OverloadError):
            maybe_raise_overload(exc, message_substrings=ACP_OVERLOAD_SUBSTRINGS)

    def test_non_overload_status_returns_silently(self):
        exc = RuntimeError("boom")
        exc.status_code = 500
        maybe_raise_overload(exc, message_substrings=ACP_OVERLOAD_SUBSTRINGS)


# ---------------------------------------------------------------------------
# _ACPCallbacks.session_update — text accumulation
# ---------------------------------------------------------------------------


def _amc(text: str | None = None):
    """Build an AgentMessageChunk-shaped object.

    session_update checks isinstance(update, AgentMessageChunk) — we must use
    the real class.
    """
    from acp.schema import AgentMessageChunk, TextContentBlock

    content = TextContentBlock(text=text or "", type="text")
    return AgentMessageChunk(content=content, session_update="agent_message_chunk")


class TestSessionUpdateTextAccumulation:
    @pytest.mark.asyncio
    async def test_text_chunk_appended(self):
        cb = _ACPCallbacks()
        await cb.session_update("sid", _amc(text="hello "))
        await cb.session_update("sid", _amc(text="world"))
        assert cb.text() == "hello world"

    @pytest.mark.asyncio
    async def test_non_agent_message_chunk_ignored(self):
        """Other update types (e.g., plan updates) don't touch state."""
        cb = _ACPCallbacks()
        await cb.session_update("sid", SimpleNamespace(content="irrelevant"))
        assert cb.text() == ""
