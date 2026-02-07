"""Tests for ACP backend — real integration tests against claude-code-acp."""

import pytest

from abe_froman.executor.prompt_backend import PromptBackendResult


# ---------------------------------------------------------------------------
# _ResponseAccumulator (our own code, no external deps)
# ---------------------------------------------------------------------------


class TestResponseAccumulator:
    def test_collects_chunks(self):
        from abe_froman.executor.backends.acp import _ResponseAccumulator

        acc = _ResponseAccumulator()
        acc.append("Hello ")
        acc.append("world")
        assert acc.text() == "Hello world"

    def test_empty_accumulator(self):
        from abe_froman.executor.backends.acp import _ResponseAccumulator

        acc = _ResponseAccumulator()
        assert acc.text() == ""


# ---------------------------------------------------------------------------
# Factory (our own code)
# ---------------------------------------------------------------------------


class TestFactory:
    def test_stub_backend_created(self):
        from abe_froman.executor.backends.factory import create_prompt_backend
        from abe_froman.executor.backends.stub import StubBackend

        backend = create_prompt_backend("stub")
        assert isinstance(backend, StubBackend)

    def test_acp_backend_created(self):
        from abe_froman.executor.backends.factory import create_prompt_backend
        from abe_froman.executor.backends.acp import ACPBackend

        backend = create_prompt_backend("acp")
        assert isinstance(backend, ACPBackend)

    def test_unknown_type_raises(self):
        from abe_froman.executor.backends.factory import create_prompt_backend

        with pytest.raises(ValueError, match="Unknown executor type"):
            create_prompt_backend("nonexistent")


# ---------------------------------------------------------------------------
# ACPBackend integration — real claude-code-acp process
# ---------------------------------------------------------------------------


class TestACPIntegration:
    @pytest.mark.asyncio
    async def test_send_prompt_returns_text(self):
        """Send a real prompt via ACP and verify we get a non-empty response."""
        from abe_froman.executor.backends.acp import ACPBackend

        backend = ACPBackend()
        try:
            result = await backend.send_prompt(
                'Respond with exactly the word "pong" and nothing else.',
                "sonnet",
                ".",
            )
            assert isinstance(result, PromptBackendResult)
            assert len(result.output) > 0
            assert "pong" in result.output.lower()
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_session_reuse_across_prompts(self):
        """Two prompts on the same backend reuse the session (no re-init)."""
        from abe_froman.executor.backends.acp import ACPBackend

        backend = ACPBackend()
        try:
            r1 = await backend.send_prompt(
                'Respond with exactly "one".',
                "sonnet",
                ".",
            )
            session_after_first = backend._session_id

            r2 = await backend.send_prompt(
                'Respond with exactly "two".',
                "sonnet",
                ".",
            )
            assert backend._session_id == session_after_first
            assert len(r1.output) > 0
            assert len(r2.output) > 0
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_close_resets_state(self):
        """After close(), the backend is no longer initialized."""
        from abe_froman.executor.backends.acp import ACPBackend

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
        from abe_froman.executor.backends.acp import ACPBackend
        from abe_froman.executor.dispatch import DispatchExecutor
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
            assert len(result.output) > 0
            assert "abe froman" in result.output.lower()
        finally:
            await executor.close()
