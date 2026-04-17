"""Tests for ACP backend — real integration tests against claude-code-acp."""

import re

import pytest

from abe_froman.runtime.result import ExecutionResult


# Common refusal phrasing Claude emits when it declines a prompt. If any of these
# appear we want the test to fail even if the asserted target word is also present
# (e.g. "I can't respond with only 'pong' but here it is: pong").
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
# _ResponseAccumulator (our own code, no external deps)
# ---------------------------------------------------------------------------


class TestResponseAccumulator:
    def test_collects_chunks(self):
        from abe_froman.runtime.executor.backends.acp import _ResponseAccumulator

        acc = _ResponseAccumulator()
        acc.append("Hello ")
        acc.append("world")
        assert acc.text() == "Hello world"

    def test_empty_accumulator(self):
        from abe_froman.runtime.executor.backends.acp import _ResponseAccumulator

        acc = _ResponseAccumulator()
        assert acc.text() == ""


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


class TestACPIntegration:
    @pytest.mark.asyncio
    async def test_send_prompt_returns_text(self):
        """Send a real prompt via ACP and verify we get a non-empty response."""
        from abe_froman.runtime.executor.backends.acp import ACPBackend

        backend = ACPBackend()
        try:
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
    async def test_session_reuse_internal_state(self):
        """Internal-state probe: session_id persists across calls.
        Deliberately reads private state — refactor canary, not behavioral test."""
        from abe_froman.runtime.executor.backends.acp import ACPBackend

        backend = ACPBackend()
        try:
            await backend.send_prompt('Say "a".', "sonnet", ".")
            session_after_first = backend._session_id

            await backend.send_prompt('Say "b".', "sonnet", ".")
            assert backend._session_id == session_after_first
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_close_resets_internal_state(self):
        """Internal-state probe: close() clears init flag.
        Deliberately reads private state — refactor canary."""
        from abe_froman.runtime.executor.backends.acp import ACPBackend

        backend = ACPBackend()
        try:
            await backend.send_prompt("Say hi.", "sonnet", ".")
            assert backend._initialized is True
        finally:
            await backend.close()
        assert backend._initialized is False
        assert backend._session_id is None

    @pytest.mark.asyncio
    async def test_full_pipeline_via_dispatch(self, tmp_path):
        """End-to-end: DispatchExecutor → PromptExecutor → ACPBackend."""
        from abe_froman.runtime.executor.backends.acp import ACPBackend
        from abe_froman.runtime.executor.dispatch import DispatchExecutor
        from abe_froman.schema.models import Phase, Settings

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
            phase = Phase(id="test", name="Test", prompt_file="test.md")
            result = await executor.execute(phase, {})
            assert result.success is True
            _assert_non_refusal_contains(result.output, r"\babe\s+froman\b")
        finally:
            await executor.close()
