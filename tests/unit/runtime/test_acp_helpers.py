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
# _ACPCallbacks.session_update — usage accumulation
# ---------------------------------------------------------------------------


def _amc_with_usage(text: str | None = None, usage=None):
    """Build an AgentMessageChunk-shaped object with optional usage.

    session_update checks isinstance(update, AgentMessageChunk) — we must use
    the real class. Pydantic v2 allows attribute assignment after construction
    (model_config is permissive here), so attach `usage` directly.
    """
    from acp.schema import AgentMessageChunk, TextContentBlock

    content = TextContentBlock(text=text or "", type="text")
    amc = AgentMessageChunk(content=content, session_update="agent_message_chunk")
    if usage is not None:
        # Bypass pydantic validation for the dynamic `usage` attribute the
        # claude-code-acp adapter attaches at runtime.
        object.__setattr__(amc, "usage", usage)
    return amc


class TestSessionUpdateUsageAccumulation:
    @pytest.mark.asyncio
    async def test_text_chunk_appended(self):
        cb = _ACPCallbacks()
        await cb.session_update("sid", _amc_with_usage(text="hello "))
        await cb.session_update("sid", _amc_with_usage(text="world"))
        assert cb.text() == "hello world"

    @pytest.mark.asyncio
    async def test_usage_accumulates_across_calls(self):
        cb = _ACPCallbacks()
        await cb.session_update(
            "sid",
            _amc_with_usage(
                text="a", usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            ),
        )
        await cb.session_update(
            "sid",
            _amc_with_usage(
                text="b", usage=SimpleNamespace(input_tokens=3, output_tokens=7),
            ),
        )
        assert cb.input_tokens == 13
        assert cb.output_tokens == 12
        assert cb.tokens_used() == {"input": 13, "output": 12}

    @pytest.mark.asyncio
    async def test_missing_usage_leaves_counters_at_zero(self):
        cb = _ACPCallbacks()
        await cb.session_update("sid", _amc_with_usage(text="no-usage"))
        assert cb.input_tokens == 0
        assert cb.output_tokens == 0
        assert cb.tokens_used() is None

    @pytest.mark.asyncio
    async def test_zero_usage_skipped(self):
        """usage with 0/0 tokens should not flip tokens_used from None."""
        cb = _ACPCallbacks()
        await cb.session_update(
            "sid",
            _amc_with_usage(
                text="x", usage=SimpleNamespace(input_tokens=0, output_tokens=0),
            ),
        )
        assert cb.tokens_used() is None

    @pytest.mark.asyncio
    async def test_partial_usage_input_only(self):
        cb = _ACPCallbacks()
        await cb.session_update(
            "sid",
            _amc_with_usage(
                text="x", usage=SimpleNamespace(input_tokens=42, output_tokens=0),
            ),
        )
        assert cb.tokens_used() == {"input": 42, "output": 0}

    @pytest.mark.asyncio
    async def test_none_valued_tokens_coerced_to_zero(self):
        """usage fields can arrive as None — `or 0` guards the addition."""
        cb = _ACPCallbacks()
        await cb.session_update(
            "sid",
            _amc_with_usage(
                text="x",
                usage=SimpleNamespace(input_tokens=None, output_tokens=None),
            ),
        )
        assert cb.input_tokens == 0
        assert cb.output_tokens == 0

    @pytest.mark.asyncio
    async def test_non_agent_message_chunk_ignored(self):
        """Other update types (e.g., plan updates) don't touch counters."""
        cb = _ACPCallbacks()
        await cb.session_update("sid", SimpleNamespace(content="irrelevant"))
        assert cb.text() == ""
        assert cb.tokens_used() is None
