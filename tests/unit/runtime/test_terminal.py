"""Unit tests for the live terminal renderer.

State-model tests confirm that emit/log_update update internal state
correctly. Rendering itself is exercised lightly via a StringIO with
`isatty()` patched on — full ANSI assertions are brittle and not
load-bearing for the contract; the contract is that events translate
to state correctly and the TTY/non-TTY branches do the right thing.
"""
from __future__ import annotations

from io import StringIO
from types import MethodType

import pytest

from sqrlly.runtime.terminal import SquirrelScene, TeeLogger, TerminalRenderer
from sqrlly.schema.models import (
    Execute,
    LlmPreset,
    Graph,
    Node,
    Settings,
)


def _config(node_specs: list[tuple[str, list[str]]]) -> Graph:
    """Helper: build a Graph from a list of (id, depends_on) tuples."""
    return Graph(
        name="Test",
        version="0.1.0",
        nodes=[
            Node(
                id=node_id, name=node_id,
                execute=Execute(url="t.md"),
                depends_on=deps,
            )
            for node_id, deps in node_specs
        ],
        settings=Settings(presets={
            "default": LlmPreset(
                transport="cli", provider="anthropic", model="sonnet",
                default=True,
            ),
        }),
    )


def _tty(stream: StringIO) -> StringIO:
    """Patch a StringIO so renderer treats it as a TTY."""
    stream.isatty = MethodType(lambda self: True, stream)
    return stream


class TestRendererStateModel:
    def test_node_completed_marks_passed(self):
        r = TerminalRenderer(_config([("a", []), ("b", ["a"])]))
        r.emit({"event": "node_completed", "node": "a"})
        assert r._node_status("a") == "passed"
        # 'b' is now running because its dep 'a' is complete
        assert r._node_status("b") == "running"

    def test_node_failed_marks_failed_and_records_error(self):
        r = TerminalRenderer(_config([("a", [])]))
        r.emit({"event": "node_failed", "node": "a", "error": "boom"})
        assert r._node_status("a") == "failed"
        assert r._errors["a"] == "boom"

    def test_gate_evaluated_records_score_and_attempt(self):
        r = TerminalRenderer(_config([("a", [])]))
        r.emit({
            "event": "gate_evaluated", "node": "a",
            "invocation": 0, "score": 0.85,
        })
        assert r._gate_scores["a"] == 0.85
        assert r._attempts["a"] == 1

    def test_gate_evaluated_attempt_count_monotonic(self):
        """Multiple gate evaluations accumulate the max attempt seen."""
        r = TerminalRenderer(_config([("a", [])]))
        r.emit({"event": "gate_evaluated", "node": "a", "invocation": 0, "score": 0.3})
        r.emit({"event": "gate_evaluated", "node": "a", "invocation": 1, "score": 0.9})
        assert r._attempts["a"] == 2

    def test_node_retried_marks_retrying(self):
        r = TerminalRenderer(_config([("a", [])]))
        r.emit({"event": "node_retried", "node": "a", "attempt": 1})
        assert r._node_status("a") == "retrying"

    def test_completed_clears_retrying(self):
        r = TerminalRenderer(_config([("a", [])]))
        r.emit({"event": "node_retried", "node": "a", "attempt": 1})
        r.emit({"event": "node_completed", "node": "a"})
        assert r._node_status("a") == "passed"


class TestRendererStatusDerivation:
    def test_waiting_when_deps_unmet(self):
        r = TerminalRenderer(_config([("a", []), ("b", ["a"])]))
        # Nothing emitted yet — b's dep 'a' not yet complete
        assert r._node_status("b") == "waiting"

    def test_running_when_deps_met_no_completion_yet(self):
        r = TerminalRenderer(_config([("a", []), ("b", ["a"])]))
        r.emit({"event": "node_completed", "node": "a"})
        # b's only dep is satisfied; b hasn't fired yet
        assert r._node_status("b") == "running"

    def test_root_node_is_running_at_start(self):
        r = TerminalRenderer(_config([("a", [])]))
        # No deps + not complete = running
        assert r._node_status("a") == "running"


class TestLogUpdate:
    def test_derives_node_completed_from_update(self):
        r = TerminalRenderer(_config([("a", [])]))
        r.log_update({"completed_nodes": {"a"}})
        assert "a" in r._completed
        assert r._node_status("a") == "passed"

    def test_derives_gate_evaluated_from_update(self):
        r = TerminalRenderer(_config([("a", [])]))
        r.log_update({
            "evaluations": {
                "a": [{"invocation": 0, "result": {"score": 0.7}}]
            },
        })
        assert r._gate_scores["a"] == 0.7
        assert r._attempts["a"] == 1


class TestWorkflowLifecycle:
    def test_workflow_start_records_started_at(self):
        r = TerminalRenderer(_config([("a", [])]))
        r.emit({"event": "workflow_start", "workflow": "T", "version": "0.1"})
        assert r._started_at is not None

    def test_workflow_end_done(self):
        r = TerminalRenderer(_config([("a", [])]))
        r.emit({"event": "workflow_start", "workflow": "T", "version": "0.1"})
        r.emit({"event": "workflow_end", "completed": 1, "failed": 0})
        assert r._end_state == "done"
        assert r._ended_at is not None

    def test_workflow_end_failed(self):
        r = TerminalRenderer(_config([("a", [])]))
        r.emit({"event": "workflow_start", "workflow": "T", "version": "0.1"})
        r.emit({"event": "workflow_end", "completed": 0, "failed": 1})
        assert r._end_state == "failed"


class TestRenderingOutput:
    def test_non_tty_produces_no_output(self):
        """Non-TTY streams short-circuit rendering; state still updates."""
        stream = StringIO()  # default isatty() returns False
        r = TerminalRenderer(_config([("a", [])]), stream=stream)
        r.emit({"event": "workflow_start", "workflow": "T", "version": "0.1"})
        r.emit({"event": "node_completed", "node": "a"})
        r.emit({"event": "workflow_end", "completed": 1, "failed": 0})
        r.close()
        assert stream.getvalue() == ""
        # But state IS tracked
        assert "a" in r._completed
        assert r._end_state == "done"

    def test_tty_writes_node_id_and_status(self):
        stream = _tty(StringIO())
        r = TerminalRenderer(_config([("alpha", []), ("beta", ["alpha"])]), stream=stream)
        r.emit({"event": "workflow_start", "workflow": "T", "version": "0.1"})
        r.emit({"event": "node_completed", "node": "alpha"})
        r.emit({"event": "workflow_end", "completed": 1, "failed": 0})
        r.close()
        out = stream.getvalue()
        assert "alpha" in out
        assert "beta" in out
        assert "passed" in out
        assert "running" in out  # beta after alpha completes
        assert "T" in out  # workflow name in header


class TestSquirrelScene:
    def test_frame_returns_consistent_width_string(self):
        s = SquirrelScene()
        # Repeated calls should produce strings of similar visual length.
        frames = [s.frame(pile_count=0, stash_count=3) for _ in range(24)]
        widths = {len(f) for f in frames}
        # Within ±2 characters across a cycle (squirrel position varies).
        assert max(widths) - min(widths) <= 2

    def test_frame_contains_tree_glyph(self):
        f = SquirrelScene().frame(pile_count=0, stash_count=2)
        assert "🌳" in f

    def test_pile_grows_with_completed_count(self):
        # Two fresh scenes at the same tick — squirrel position
        # identical; only the pile count differs, so the total `●`
        # count is a reliable comparison.
        f0 = SquirrelScene().frame(pile_count=0, stash_count=0)
        f3 = SquirrelScene().frame(pile_count=3, stash_count=0)
        assert f3.count("●") == f0.count("●") + 3

    def test_squirrel_walks(self):
        """Successive frames at the same state should show the squirrel
        in different positions (clock-driven aliveness)."""
        s = SquirrelScene()
        # Sample 4 frames at a steady state — squirrel positions should
        # not be identical across all of them.
        frames = [s.frame(pile_count=2, stash_count=2) for _ in range(4)]
        assert len(set(frames)) > 1

    def test_carrying_state_on_return_trip(self):
        s = SquirrelScene()
        # Tick through one full cycle and look at the right-facing vs
        # left-facing characters in the strip. The "going_left" frames
        # should show ●< (carrying), the "going_right" frames >○ (empty).
        going_right_frames = []
        going_left_frames = []
        for _ in range(24):
            f = s.frame(pile_count=0, stash_count=2)
            if ">○" in f:
                going_right_frames.append(f)
            if "●<" in f:
                going_left_frames.append(f)
        assert len(going_right_frames) > 0
        assert len(going_left_frames) > 0

    def test_no_carrying_when_stash_empty(self):
        """If the stash is empty, the squirrel has nothing to carry —
        the left-facing frame should still show the empty form."""
        s = SquirrelScene()
        seen_loaded = False
        for _ in range(24):
            f = s.frame(pile_count=5, stash_count=0)
            if "●<" in f:
                seen_loaded = True
        assert not seen_loaded


class TestTeeLogger:
    def test_emit_fans_to_all_subscribers(self):
        class _Sink:
            def __init__(self):
                self.events = []
            def emit(self, e):
                self.events.append(e)
            def log_update(self, u):
                pass
            def close(self):
                pass

        a, b = _Sink(), _Sink()
        tee = TeeLogger(a, b)
        tee.emit({"event": "node_completed", "node": "x"})
        assert a.events == [{"event": "node_completed", "node": "x"}]
        assert b.events == [{"event": "node_completed", "node": "x"}]

    def test_close_fans_to_all_subscribers(self):
        class _Sink:
            def __init__(self):
                self.closed = False
            def emit(self, e): pass
            def log_update(self, u): pass
            def close(self):
                self.closed = True

        a, b = _Sink(), _Sink()
        TeeLogger(a, b).close()
        assert a.closed and b.closed

    def test_close_tolerates_subscriber_without_close(self):
        class _NoClose:
            def emit(self, e): pass
            def log_update(self, u): pass

        # Should not raise.
        TeeLogger(_NoClose()).close()
