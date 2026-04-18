"""Concurrency safety for ACPBackend — shared-backend concurrent send_prompt.

Verifies the _send_lock serializes reset → send → callback-read so two
coroutines dispatched on a shared ACPBackend each receive their own response
without cross-contamination of the _ACPCallbacks accumulator.
"""

import asyncio

import pytest

ACP_TIMEOUT = 180


@pytest.mark.acp
class TestACPConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_sends_do_not_cross_contaminate(self):
        """Two send_prompt calls on one backend return distinct, correct outputs."""
        from abe_froman.runtime.executor.backends.acp import ACPBackend

        backend = ACPBackend()
        try:
            async with asyncio.timeout(ACP_TIMEOUT):
                r1_coro = backend.send_prompt(
                    'Respond with exactly "alpha" and nothing else.',
                    "sonnet",
                    ".",
                )
                r2_coro = backend.send_prompt(
                    'Respond with exactly "bravo" and nothing else.',
                    "sonnet",
                    ".",
                )
                r1, r2 = await asyncio.gather(r1_coro, r2_coro)
            assert "alpha" in r1.output.lower(), f"r1 got: {r1.output!r}"
            assert "bravo" in r2.output.lower(), f"r2 got: {r2.output!r}"
            assert "bravo" not in r1.output.lower(), (
                f"r1 contaminated with r2 content: {r1.output!r}"
            )
            assert "alpha" not in r2.output.lower(), (
                f"r2 contaminated with r1 content: {r2.output!r}"
            )
        finally:
            await backend.close()
