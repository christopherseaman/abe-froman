"""Unit tests for the live terminal renderer.

State-model tests confirm that emit/log_update update internal state
correctly. Rendering itself is exercised lightly via a StringIO with
`isatty()` patched on — full ANSI assertions are brittle and not
load-bearing for the contract; the contract is that events translate
to state correctly and the TTY/non-TTY branches do the right thing.
"""
from __future__ import annotations

import re
from io import StringIO
from types import MethodType

import pytest

from sqrlly.runtime.terminal import (
    MascotScene,
    TeeLogger,
    TerminalRenderer,
    _display_width,
)
from sqrlly.schema.models import (
    Execute,
    LlmPreset,
    Graph,
    Node,
    Settings,
)

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def _mascot_index(frame: str) -> int:
    """Left-cell index of the 2-glyph mascot, whichever way it faces."""
    for pair in (MascotScene._MASCOT_L, MascotScene._MASCOT_R):
        i = frame.find(pair)
        if i != -1:
            return i
    return -1


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


class TestMascotScene:
    def test_frame_contains_tree_glyph(self):
        f = MascotScene().frame(pile_count=0, stash_count=2)
        assert "🌳" in f

    def test_mascot_idles_with_wiggle_when_no_dots(self):
        """No dots = idle wiggle. Mascot still moves (aliveness)."""
        s = MascotScene()
        positions = []
        for _ in range(8):
            f = s.frame(pile_count=0, stash_count=0)
            i = _mascot_index(f)
            assert i != -1
            positions.append(i)
        # At least some movement across the 8 frames
        assert len(set(positions)) > 1

    def test_mascot_faces_and_moves_both_directions_during_idle_wiggle(self):
        """Idle wiggle moves the mascot left AND right, and the glyph
        flips to the matching facing pair — the restored two-facing
        block-art mascot."""
        s = MascotScene()
        positions, seen_left, seen_right = [], False, False
        for _ in range(12):
            f = s.frame(pile_count=0, stash_count=0)
            positions.append(_mascot_index(f))
            seen_left |= MascotScene._MASCOT_L in f
            seen_right |= MascotScene._MASCOT_R in f
        deltas = [b - a for a, b in zip(positions, positions[1:])]
        assert any(d > 0 for d in deltas)   # moved right at some point
        assert any(d < 0 for d in deltas)   # moved left at some point
        assert seen_left and seen_right     # both facing pairs drawn

    def test_dots_appear_during_run(self):
        """Dots spawn at a constant rate during a run, regardless of
        stash_count — the scene is ambient flavor, not a literal
        per-node nut counter."""
        s = MascotScene()
        seen_fall = set()
        for _ in range(24):
            f = s.frame(pile_count=0, stash_count=0)   # stash=0 must not gate
            for g in ("⠁", "⠂", "⠄", "⡀"):
                if g in f:
                    seen_fall.add(g)
        assert "⠁" in seen_fall    # at least one freshly spawned
        assert "⡀" in seen_fall    # at least one landed

    def test_dot_count_capped(self):
        """Concurrent dots cap at DOTS_CAP — mascot can't catch up
        in tests so the cap is the natural ceiling."""
        s = MascotScene()
        max_dots = 0
        for _ in range(120):
            f = s.frame(pile_count=0, stash_count=0)
            # Total nut glyphs in frame (any state)
            total = sum(f.count(g) for g in ("⠁", "⠂", "⠄", "⡀"))
            max_dots = max(max_dots, total)
        assert max_dots <= MascotScene._DOTS_CAP

    def test_mascot_seeks_landed_dots(self):
        """When a dot lands at a specific column, the mascot moves
        toward it over the next few frames."""
        s = MascotScene()
        # Burn enough frames for at least one spawn → fall → land cycle
        # then look for consumption (landed-count decrease).
        landed_counts = []
        for _ in range(40):
            f = s.frame(pile_count=0, stash_count=2)
            landed_counts.append(f.count("⡀"))
        # If the seeking + consumption logic never fires, the landed
        # count would monotonically grow.
        decreases = sum(
            1 for a, b in zip(landed_counts, landed_counts[1:]) if b < a
        )
        assert decreases >= 1

    def test_mascot_and_tree_occupy_two_cells(self):
        """Each facing pair is two 1-col block glyphs (= 2 cells); the
        tree emoji is one 2-col glyph. Both must measure 2 so the grid
        layout and the clip math line up."""
        assert _display_width(MascotScene._MASCOT_L) == 2
        assert _display_width(MascotScene._MASCOT_R) == 2
        assert _display_width(MascotScene._TREE) == 2

    def test_scene_respects_custom_walkway(self):
        """Walkway sizes to the value passed (renderer feeds it the
        terminal width); scene line stays within tree-margin + walkway."""
        s = MascotScene(walkway=12)
        assert s._walkway == 12
        f = s.frame(pile_count=0, stash_count=2)
        assert _display_width(f) <= 3 + 12   # "🌳 " margin + 12 cells

    def test_walkway_has_a_floor(self):
        """Absurdly narrow requests clamp to the minimum, never 0/negative."""
        assert MascotScene(walkway=2)._walkway == MascotScene._MIN_WALKWAY

    def test_pile_count_does_not_affect_scene(self):
        """`pile_count` is accepted for interface stability but no
        longer surfaces in the scene; identical ticks should produce
        identical frames regardless of pile_count."""
        f0 = MascotScene().frame(pile_count=0, stash_count=0)
        f9 = MascotScene().frame(pile_count=9, stash_count=0)
        assert f0 == f9


class TestNarrowTerminal:
    def test_render_clips_every_line_to_terminal_width(self):
        """Regression: on a narrow terminal no rendered line may exceed
        the width. A wider line wraps to a second physical row, which
        desyncs the cursor-up redraw count and scrolls the display down
        every tick (observed over phone SSH)."""
        stream = _tty(StringIO())
        r = TerminalRenderer(
            _config([("a_very_long_node_identifier_indeed", [])]),
            stream=stream,
        )
        r._term_width = lambda: 24   # simulate a phone-width terminal
        r.emit({"event": "workflow_start",
                "workflow": "A Rather Long Workflow Name", "version": "0.1"})
        for _ in range(3):
            r._render()
        printed = [
            _ANSI.sub("", ln) for ln in stream.getvalue().split("\n")
        ]
        assert any(printed)  # something was drawn
        assert all(_display_width(p) <= 24 for p in printed)


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
