"""Real-ACP integration test for evaluate_gate_llm positive path.

The LLM gate path in runtime/gates.py::evaluate_gate_llm dispatches an .md
template through the phase's PromptBackend. Exercising it unit-style would
require a PromptBackend double — forbidden by feedback_no_fake_backends.md.
We run it against a real claude-code-acp process instead.
"""

import asyncio

import pytest

ACP_TIMEOUT = 180


@pytest.mark.acp
class TestEvaluateGateLLMPositivePath:
    @pytest.mark.asyncio
    async def test_high_score_from_real_llm(self, tmp_path):
        """A clear instruction to return {"score": 1.0, ...} yields a passing score.

        This pins the happy path: .md template rendering → backend dispatch →
        JSON parsing → GateResult population. Absent this test, the only
        coverage of evaluate_gate_llm is via the joke-workflow E2E, which
        exercises it implicitly as one piece of a larger flow.
        """
        from abe_froman.runtime.executor.backends.acp import ACPBackend
        from abe_froman.runtime.gates import evaluate_gate_llm
        from abe_froman.schema.models import QualityGate

        gate_file = tmp_path / "llm_gate.md"
        gate_file.write_text(
            "You are a test validator. The phase ran and produced this output:\n\n"
            "---\n"
            "{{output}}\n"
            "---\n\n"
            "Respond with EXACTLY this JSON object and nothing else — no prose, "
            'no markdown fences:\n\n'
            '{"score": 1.0, "feedback": "ok", "pass_criteria_met": ["ran"], '
            '"pass_criteria_unmet": []}\n'
        )

        gate = QualityGate(validator="llm_gate.md", threshold=0.8)
        backend = ACPBackend()
        try:
            async with asyncio.timeout(ACP_TIMEOUT):
                result = await evaluate_gate_llm(
                    gate=gate,
                    phase_id="test_phase",
                    workdir=str(tmp_path),
                    phase_output="some phase output",
                    backend=backend,
                    default_model="sonnet",
                )
            assert result.score >= 0.8, (
                f"expected passing score from LLM gate; got {result.score} "
                f"with feedback={result.feedback!r}"
            )
        finally:
            await backend.close()
