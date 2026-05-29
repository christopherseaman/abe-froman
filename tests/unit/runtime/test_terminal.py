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
    def test_frame_contains_tree_glyph(self):
        f = SquirrelScene().frame(pile_count=0, stash_count=2)
        assert "🌳" in f

    def test_pile_grows_with_completed_count(self):
        """Block-element pile glyph index scales with pile_count."""
        f0 = SquirrelScene().frame(pile_count=0, stash_count=0)
        f1 = SquirrelScene().frame(pile_count=1, stash_count=0)
        f3 = SquirrelScene().frame(pile_count=3, stash_count=0)
        # 0 nodes → no pile glyph; 1 → ▁; 3 → ▃
        assert "▁" not in f0
        assert "▁" in f1
        assert "▃" in f3

    def test_pile_overflow_shows_plus_badge(self):
        """Pile counts beyond the glyph table fall through to '+N'."""
        f = SquirrelScene().frame(pile_count=20, stash_count=0)
        assert "+" in f  # overflow badge

    def test_squirrel_glyph_appears(self):
        """The right-facing squirrel glyph (🬢🭠) appears during the
        going-right half of the cycle."""
        s = SquirrelScene()
        seen_right = False
        for _ in range(24):
            f = s.frame(pile_count=0, stash_count=0)
            if "🬢🭠" in f:
                seen_right = True
        assert seen_right

    def test_squirrel_changes_direction(self):
        """Both right (🬢🭠) and left (🭠🬢) forms appear within a cycle."""
        s = SquirrelScene()
        seen_right = seen_left = False
        for _ in range(24):
            f = s.frame(pile_count=0, stash_count=0)
            if "🬢🭠" in f:
                seen_right = True
            if "🭠🬢" in f:
                seen_left = True
        assert seen_right and seen_left

    def test_falling_dots_appear_when_stash_present(self):
        """When stash > 0, fall-state glyphs (⠁/⠂/⠄/⡀) populate
        the walkway over time."""
        s = SquirrelScene()
        seen_fall = set()
        for _ in range(24):
            f = s.frame(pile_count=0, stash_count=3)
            for g in ("⠁", "⠂", "⠄", "⡀"):
                if g in f:
                    seen_fall.add(g)
        # At least the spawned glyph (⠁) and landed glyph (⡀) should show.
        assert "⠁" in seen_fall
        assert "⡀" in seen_fall

    def test_squirrel_consumes_landed_dots(self):
        """Run a cycle and confirm that landed-dot count fluctuates —
        i.e., dots get consumed and re-spawned rather than monotonically
        accumulating."""
        s = SquirrelScene()
        landed_counts = []
        for _ in range(48):  # two full cycles
            f = s.frame(pile_count=0, stash_count=3)
            landed_counts.append(f.count("⡀"))
        # If consumption never happened, count would monotonically
        # rise. Expect at least one decrease somewhere.
        decreases = sum(
            1 for a, b in zip(landed_counts, landed_counts[1:]) if b < a
        )
        assert decreases >= 1

    def test_no_dots_when_stash_zero(self):
        """An empty stash leaves the walkway free of falling nuts."""
        s = SquirrelScene()
        for _ in range(24):
            f = s.frame(pile_count=2, stash_count=0)
            for g in ("⠁", "⠂", "⠄", "⡀"):
                assert g not in f


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
