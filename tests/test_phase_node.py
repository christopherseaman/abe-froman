"""Unit tests for _make_gate_router from builder.py.

Pure function tests for the gate routing logic — pass/retry/fail decisions
based on score, threshold, retries, and blocking flag.
"""

import pytest

from abe_froman.compile.routers import _make_gate_router
from abe_froman.runtime.state import make_initial_state
from abe_froman.schema.models import Phase, QualityGate


class TestGateRouter:
    def _make_phase_with_gate(self, threshold=0.8, blocking=True):
        return Phase(
            id="p1", name="P1", prompt_file="t.md",
            quality_gate=QualityGate(
                validator="v.py", threshold=threshold, blocking=blocking,
            ),
        )

    def test_pass(self):
        phase = self._make_phase_with_gate(threshold=0.8)
        router = _make_gate_router(phase, max_retries=3)
        state = make_initial_state(gate_scores={"p1": 0.9}, retries={"p1": 0})
        assert router(state) == "pass"

    def test_retry(self):
        phase = self._make_phase_with_gate(threshold=0.8)
        router = _make_gate_router(phase, max_retries=3)
        state = make_initial_state(gate_scores={"p1": 0.5}, retries={"p1": 1})
        assert router(state) == "retry"

    def test_fail_blocking(self):
        phase = self._make_phase_with_gate(threshold=0.8, blocking=True)
        router = _make_gate_router(phase, max_retries=3)
        state = make_initial_state(gate_scores={"p1": 0.5}, retries={"p1": 3})
        assert router(state) == "fail"

    def test_pass_non_blocking(self):
        phase = self._make_phase_with_gate(threshold=0.8, blocking=False)
        router = _make_gate_router(phase, max_retries=3)
        state = make_initial_state(gate_scores={"p1": 0.5}, retries={"p1": 3})
        assert router(state) == "pass"

    def test_score_exactly_at_threshold(self):
        phase = self._make_phase_with_gate(threshold=0.8)
        router = _make_gate_router(phase, max_retries=3)
        state = make_initial_state(gate_scores={"p1": 0.8}, retries={"p1": 0})
        assert router(state) == "pass"

    def test_missing_score_defaults_to_zero(self):
        """When gate_scores has no entry for the phase, the router should
        treat the score as 0.0 (builder.py uses .get(phase.id, 0.0))."""
        phase = self._make_phase_with_gate(threshold=0.8)
        router = _make_gate_router(phase, max_retries=3)
        state = make_initial_state(gate_scores={}, retries={"p1": 0})
        assert router(state) == "retry"
