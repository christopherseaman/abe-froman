"""Unit tests for _make_gate_router from compile/graph.py.

The router is a pure state-reader: it inspects `completed_phases` /
`failed_phases` written by the phase node and routes accordingly.
Classification logic (score vs threshold, blocking, retry budget) lives
in the phase node (`compile/nodes.py::classify_gate_outcome`), not here.
"""

from abe_froman.compile.graph import _make_gate_router
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

    def test_pass_when_completed(self):
        """Phase node wrote `completed_phases` → router returns pass."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase)
        state = make_initial_state(completed_phases=["p1"])
        assert router(state) == "pass"

    def test_fail_when_failed(self):
        """Phase node wrote `failed_phases` → router returns fail."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase)
        state = make_initial_state(failed_phases=["p1"])
        assert router(state) == "fail"

    def test_retry_when_neither(self):
        """Phase node bumped retries (not completed, not failed) → retry."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase)
        state = make_initial_state(retries={"p1": 1})
        assert router(state) == "retry"

    def test_failed_takes_precedence_over_completed(self):
        """Defensive: if both lists contain the id, fail wins."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase)
        state = make_initial_state(
            completed_phases=["p1"], failed_phases=["p1"]
        )
        assert router(state) == "fail"

    def test_retry_on_empty_state(self):
        """Fresh state with no markers → retry (phase hasn't executed yet)."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase)
        state = make_initial_state()
        assert router(state) == "retry"
