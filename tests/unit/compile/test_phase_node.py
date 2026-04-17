"""Unit tests for _make_gate_router from compile/graph.py.

The router is a pure state-reader: it inspects `completed_phases` /
`failed_phases` written by the phase node and returns concrete node
targets (END, the phase id for retry, or dependent phase ids for pass).
Classification logic (score vs threshold, blocking, retry budget) lives
in the phase node (`compile/nodes.py::classify_gate_outcome`), not here.
"""

from langgraph.graph import END

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

    def test_pass_single_target(self):
        """Completed with one pass target → return that target directly."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase, pass_targets=["b"])
        state = make_initial_state(completed_phases=["p1"])
        assert router(state) == "b"

    def test_pass_multiple_targets_fans_out(self):
        """Completed with multiple pass targets → return list for fan-out."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase, pass_targets=["b", "c"])
        state = make_initial_state(completed_phases=["p1"])
        assert router(state) == ["b", "c"]

    def test_pass_defaults_to_end(self):
        """Terminal gated phase → pass routes to END."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase)
        state = make_initial_state(completed_phases=["p1"])
        assert router(state) == END

    def test_fail_routes_to_end(self):
        """failed_phases contains id → router returns END."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase, pass_targets=["b"])
        state = make_initial_state(failed_phases=["p1"])
        assert router(state) == END

    def test_retry_returns_phase_id(self):
        """Phase node bumped retries (not completed, not failed) → re-enter phase."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase, pass_targets=["b"])
        state = make_initial_state(retries={"p1": 1})
        assert router(state) == "p1"

    def test_failed_takes_precedence_over_completed(self):
        """Defensive: if both lists contain the id, fail wins."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase, pass_targets=["b"])
        state = make_initial_state(
            completed_phases=["p1"], failed_phases=["p1"]
        )
        assert router(state) == END

    def test_retry_on_empty_state(self):
        """Fresh state with no markers → re-enter phase (hasn't executed yet)."""
        phase = self._make_phase_with_gate()
        router = _make_gate_router(phase, pass_targets=["b"])
        state = make_initial_state()
        assert router(state) == "p1"
