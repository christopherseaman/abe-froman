"""Tests for ACP backend — real integration tests against claude-code-acp.

Flakiness vectors (documented for CI triage):
- LLM non-determinism: Claude may embellish "say exactly X" prompts
- Process startup: spawn_agent_process needs ~5-10s cold start
- API rate limits: back-to-back runs can hit 429/529
- Async cleanup: Python 3.14 aclose() warning on context manager exit

All live-API tests are marked @pytest.mark.acp so CI can isolate them
via ``pytest -m acp`` or ``pytest -m "not acp"``.  Each carries a 120s
asyncio timeout to prevent indefinite hangs.
"""

import asyncio
import re

import pytest

from abe_froman.runtime.result import ExecutionResult

ACP_TIMEOUT = 120

_REFUSAL_MARKERS = (
    "i can't",
    "i cannot",
    "i'm sorry",
    "i am sorry",
    "i'm unable",
    "i am unable",
    "i'm not able",
    "i am not able",
    "unable to",
)


def _assert_non_refusal_contains(output: str, target_pattern: str) -> None:
    """Assert `output` contains `target_pattern` (regex) and no refusal markers."""
    assert output, "ACP returned empty output"
    lower = output.lower()
    for marker in _REFUSAL_MARKERS:
        assert marker not in lower, f"ACP refusal detected ({marker!r}) in: {output!r}"
    assert re.search(target_pattern, lower), (
        f"expected pattern {target_pattern!r} not found in: {output!r}"
    )


# ---------------------------------------------------------------------------
# _ACPCallbacks (our own code, no external deps)
# ---------------------------------------------------------------------------


class TestACPCallbacks:
    def test_collects_chunks(self):
        from abe_froman.runtime.executor.backends.acp import _ACPCallbacks

        cb = _ACPCallbacks()
        cb.chunks.extend(["Hello ", "world"])
        assert cb.text() == "Hello world"

    def test_reset_clears_state(self):
        from abe_froman.runtime.executor.backends.acp import _ACPCallbacks

        cb = _ACPCallbacks()
        cb.chunks.append("data")
        cb.reset()
        assert cb.text() == ""


# ---------------------------------------------------------------------------
# Factory (our own code)
# ---------------------------------------------------------------------------


class TestFactory:
    def test_stub_backend_created(self):
        from abe_froman.runtime.executor.backends.factory import create_prompt_backend
        from abe_froman.runtime.executor.backends.stub import StubBackend

        backend = create_prompt_backend("stub")
        assert isinstance(backend, StubBackend)

    def test_acp_backend_created(self):
        from abe_froman.runtime.executor.backends.factory import create_prompt_backend
        from abe_froman.runtime.executor.backends.acp import ACPBackend

        backend = create_prompt_backend("acp")
        assert isinstance(backend, ACPBackend)

    def test_unknown_type_raises(self):
        from abe_froman.runtime.executor.backends.factory import create_prompt_backend

        with pytest.raises(ValueError, match="Unknown executor type"):
            create_prompt_backend("nonexistent")


# ---------------------------------------------------------------------------
# ACPBackend integration — real claude-code-acp process
# ---------------------------------------------------------------------------


@pytest.mark.acp
class TestACPIntegration:
    @pytest.mark.asyncio
    async def test_send_prompt_returns_text(self):
        """Send a real prompt via ACP and verify we get a non-empty response."""
        from abe_froman.runtime.executor.backends.acp import ACPBackend

        backend = ACPBackend()
        try:
            async with asyncio.timeout(ACP_TIMEOUT):
                result = await backend.send_prompt(
                    'Respond with exactly the word "pong" and nothing else.',
                    "sonnet",
                    ".",
                )
            assert isinstance(result, ExecutionResult)
            _assert_non_refusal_contains(result.output, r"\bpong\b")
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_two_prompts_succeed_on_same_backend(self):
        """Behavioral: two prompts on one backend both return correct outputs."""
        from abe_froman.runtime.executor.backends.acp import ACPBackend

        backend = ACPBackend()
        try:
            async with asyncio.timeout(ACP_TIMEOUT):
                r1 = await backend.send_prompt(
                    'Respond with exactly "one".',
                    "sonnet",
                    ".",
                )
                r2 = await backend.send_prompt(
                    'Respond with exactly "two".',
                    "sonnet",
                    ".",
                )
            _assert_non_refusal_contains(r1.output, r"\bone\b")
            _assert_non_refusal_contains(r2.output, r"\btwo\b")
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_close_is_idempotent(self):
        """Behavioral: calling close() twice doesn't raise."""
        from abe_froman.runtime.executor.backends.acp import ACPBackend

        backend = ACPBackend()
        try:
            async with asyncio.timeout(ACP_TIMEOUT):
                await backend.send_prompt("Say hi.", "sonnet", ".")
        finally:
            await backend.close()
            await backend.close()

    @pytest.mark.asyncio
    async def test_full_pipeline_via_dispatch(self, tmp_path):
        """End-to-end: DispatchExecutor → PromptExecutor → ACPBackend."""
        from abe_froman.runtime.executor.backends.acp import ACPBackend
        from abe_froman.runtime.executor.dispatch import DispatchExecutor
        from abe_froman.schema.models import Node, Settings

        prompt_file = tmp_path / "test.md"
        prompt_file.write_text(
            'Respond with exactly "hello from abe froman" and nothing else.'
        )

        backend = ACPBackend()
        settings = Settings(default_model="sonnet")
        executor = DispatchExecutor(
            workdir=str(tmp_path), prompt_backend=backend, settings=settings,
        )
        try:
            async with asyncio.timeout(ACP_TIMEOUT):
                node = Node(id="test", name="Test", prompt_file="test.md")
                result = await executor.execute(node, {})
            assert result.success is True
            _assert_non_refusal_contains(result.output, r"\babe\s+froman\b")
        finally:
            await executor.close()
