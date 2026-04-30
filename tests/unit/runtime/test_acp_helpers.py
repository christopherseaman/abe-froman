"""Unit tests for pure helpers in runtime/executor/backends/acp.py.

Covers _is_overload_error (pure predicate on exceptions) and the usage
accumulation path inside _ACPCallbacks.session_update. No ACP process
needed — these are pure data transformations.
"""

from types import SimpleNamespace

import pytest

from abe_froman.runtime.executor.backends.acp import (
    _ACPCallbacks,
    _is_overload_error,
)


# ---------------------------------------------------------------------------
# _is_overload_error
# ---------------------------------------------------------------------------


class TestIsOverloadError:
    @pytest.mark.parametrize(
        "exc, expected",
        [
            (RuntimeError("529 overloaded, try again"), True),
            (RuntimeError("API is overloaded"), True),
            (RuntimeError("OVERLOAD detected"), True),  # case-insensitive
            (RuntimeError("http 529 response"), True),
            (RuntimeError("connection refused"), False),
            (ValueError("bad input"), False),
            (RuntimeError(""), False),
        ],
    )
    def test_message_heuristic(self, exc, expected):
        assert _is_overload_error(exc) is expected

    def test_status_code_529_attribute(self):
        exc = RuntimeError("something went wrong")
        exc.status_code = 529
        assert _is_overload_error(exc) is True

    def test_status_529_attribute(self):
        exc = RuntimeError("something")
        exc.status = 529
        assert _is_overload_error(exc) is True

    def test_status_code_non_529_not_overload(self):
        exc = RuntimeError("boom")
        exc.status_code = 500
        assert _is_overload_error(exc) is False

    def test_no_status_no_message_match(self):
        assert _is_overload_error(Exception("innocent")) is False


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
