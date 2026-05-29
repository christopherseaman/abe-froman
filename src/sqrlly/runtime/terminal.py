"""Live terminal renderer for workflow events.

Subscribes to the same event stream `JsonlLogger` consumes; renders
per-node status in place to a TTY and ticks an aliveness spinner on a
clock-driven async task. Implements the `JsonlLogger`-shaped interface
(`emit`, `log_update`, `close`) so it slots into the runner without
any compile-layer or backend-layer changes.

Workflow events, not LLM-token streaming. sqrlly does not surface
per-model-token output and has no plans to.

TTY detection is the caller's responsibility — instantiate this only
when `sys.stdout.isatty()` and the user hasn't passed `--quiet`. The
renderer no-ops its rendering work on non-TTY streams as a defensive
fallback, but the state model still tracks events (harmless when paired
with a `JsonlLogger` via `TeeLogger`).
"""
from __future__ import annotations

import asyncio
import sys
import time
from typing import IO, Any

from sqrlly.runtime.logging import _emit_update_events
from sqrlly.schema.models import Graph

# Braille spinner — well-supported in modern terminals, low visual
# noise, 10 frames at 100ms → 1Hz overall rotation that reads as
# "alive" without being distracting.
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

_TICK_SECONDS = 0.1


class BrailleSpinner:
    """A simple rotating braille spinner. Aliveness only; no state."""

    def __init__(self) -> None:
        self._idx = 0

    def frame(self, *, pile_count: int, stash_count: int) -> str:
        self._idx = (self._idx + 1) % len(_SPINNER_FRAMES)
        return _SPINNER_FRAMES[self._idx]


class SquirrelScene:
    """A walking squirrel gathering nuts that fall and stack on a pile.

    Layout: ``🌳 [pile] [walkway with falling nuts and walking squirrel]``

    Each walkway cell is a per-column state machine:

    - ``None`` — empty
    - ``0``  — ``⠁`` (top dot, just spawned, falling)
    - ``1``  — ``⠂``
    - ``2``  — ``⠄``
    - ``3``  — ``⡀`` (landed, available to be eaten)

    The squirrel walks back and forth on the walkway; landed nuts under
    or just-ahead of it are consumed (set back to ``None``). New
    fallers spawn into empty cells, keeping walkway density roughly
    proportional to ``stash_count``. Walking is clock-driven aliveness
    — the squirrel keeps moving regardless of workflow activity. The
    pile size is event-driven (set fresh each frame from the
    renderer's `completed_nodes` count) and rendered as a vertically
    filling block-element glyph (``▁`` → ``█``).
    """

    _CYCLE = 24            # ticks per round trip
    _WALKWAY = 16          # walkway cell width
    _PILE_MAX = 8          # last block-glyph index; overflows show "+N"

    _TREE = "🌳"
    _SQ_R = "🬢🭠"           # right-facing squirrel glyph pair
    _SQ_L = "🭠🬢"           # left-facing (mirrored ordering)
    _FALL = ["⠁", "⠂", "⠄", "⡀"]   # in-place fall progression
    _PILE_GLYPHS = ["", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]

    def __init__(self) -> None:
        self._tick = 0
        # Per-column nut state. None = empty; 0..3 = falling/landed.
        self._dots: list[int | None] = [None] * self._WALKWAY

    def _squirrel_position(self, going_right: bool) -> int:
        half = self._CYCLE // 2
        cycle_pos = self._tick % self._CYCLE
        if going_right:
            pos = (cycle_pos * (self._WALKWAY - 1)) // max(half - 1, 1)
        else:
            pos = ((self._CYCLE - 1 - cycle_pos) * (self._WALKWAY - 1)) // max(half - 1, 1)
        return max(0, min(pos, self._WALKWAY - 2))

    def frame(self, *, pile_count: int, stash_count: int) -> str:
        self._tick += 1
        going_right = (self._tick % self._CYCLE) < (self._CYCLE // 2)
        sq_pos = self._squirrel_position(going_right)

        # 1. Advance any in-flight fall (0/1/2 → next state); landed (3) stays.
        for i, d in enumerate(self._dots):
            if d is not None and d < 3:
                self._dots[i] = d + 1

        # 2. The squirrel consumes landed dots beneath it (2-cell footprint).
        for offset in (0, 1):
            p = sq_pos + offset
            if 0 <= p < self._WALKWAY and self._dots[p] == 3:
                self._dots[p] = None

        # 3. Keep walkway density proportional to stash. Spawn one
        #    falling nut per tick when on-screen count is below
        #    the desired population. Skip cells the squirrel is in
        #    so a new nut doesn't visually overlay the squirrel.
        on_screen = sum(1 for d in self._dots if d is not None)
        desired = min(stash_count, self._WALKWAY - 3)
        if on_screen < desired:
            empties = [
                i for i, d in enumerate(self._dots)
                if d is None and not (sq_pos <= i <= sq_pos + 1)
            ]
            if empties:
                # Deterministic empty-cell choice from the tick — avoids
                # importing `random` and keeps frame output reproducible
                # for tests.
                self._dots[empties[self._tick % len(empties)]] = 0

        # 4. Render the walkway cells.
        cells = [" "] * self._WALKWAY
        for i, d in enumerate(self._dots):
            if d is not None:
                cells[i] = self._FALL[d]
        sq_glyph = self._SQ_R if going_right else self._SQ_L
        cells[sq_pos] = sq_glyph[0]
        if sq_pos + 1 < self._WALKWAY:
            cells[sq_pos + 1] = sq_glyph[1]
        walkway = "".join(cells)

        # 5. Pile: single block-element cell scaled by completed count;
        #    overflow shows "+N" badge.
        idx = min(pile_count, len(self._PILE_GLYPHS) - 1)
        pile = self._PILE_GLYPHS[idx]
        if pile_count > len(self._PILE_GLYPHS) - 1:
            pile += f"+{pile_count - (len(self._PILE_GLYPHS) - 1)}"

        return f"{self._TREE}{pile} {walkway}"


# Per-node status icons. ASCII fallbacks would be nicer for legacy
# terminals; defer that polish to a future iteration.
_ICONS: dict[str, str] = {
    "waiting": "○",
    "running": "◐",
    "passed": "✓",
    "failed": "✗",
    "retrying": "↻",
}


class TerminalRenderer:
    """Render workflow events in place to a TTY.

    Construction is cheap and side-effect-free. The clock-driven tick
    task starts only on `emit(workflow_start)` and stops on
    `emit(workflow_end)` (or `close()`). Per-node state is computed
    from accumulated events plus the schema's `depends_on` graph
    (a node whose deps are all complete and that hasn't completed or
    failed itself is "running").
    """

    def __init__(self, config: Graph, stream: IO[str] | None = None) -> None:
        self._config = config
        self._stream = stream or sys.stdout
        self._enabled = self._stream.isatty()

        # Schema-derived
        self._node_ids: list[str] = [n.id for n in config.nodes]
        self._deps: dict[str, list[str]] = {
            n.id: list(n.depends_on) for n in config.nodes
        }
        self._id_width = max((len(i) for i in self._node_ids), default=10)

        # Accumulated state
        self._completed: set[str] = set()
        self._failed: set[str] = set()
        self._retrying: set[str] = set()
        self._errors: dict[str, str] = {}
        self._gate_scores: dict[str, float] = {}
        self._attempts: dict[str, int] = {}

        # Workflow-level
        self._started_at: float | None = None
        self._ended_at: float | None = None
        self._end_state: str | None = None  # None | "done" | "failed"

        # Render state
        self._spinner_idx = 0           # per-node braille tick (running indicator)
        self._scene = SquirrelScene()   # header aliveness scene
        self._tick_task: asyncio.Task | None = None
        self._last_lines_drawn = 0
        self._closed = False

    # ---- Logger interface (matches JsonlLogger) ----

    def emit(self, event: dict[str, Any]) -> None:
        et = event.get("event")
        if et == "workflow_start":
            self._started_at = time.monotonic()
            self._start_tick()
            self._render()
        elif et == "workflow_end":
            self._ended_at = time.monotonic()
            self._end_state = "failed" if event.get("failed", 0) > 0 else "done"
            self._stop_tick()
            self._render()
        elif et == "node_completed":
            node = event["node"]
            self._completed.add(node)
            self._retrying.discard(node)
        elif et == "node_failed":
            node = event["node"]
            self._failed.add(node)
            self._errors[node] = event.get("error", "")
        elif et == "gate_evaluated":
            node = event["node"]
            self._gate_scores[node] = float(event.get("score", 0.0))
            self._attempts[node] = max(
                self._attempts.get(node, 0),
                int(event.get("invocation", 0)) + 1,
            )
        elif et == "node_retried":
            node = event["node"]
            self._retrying.add(node)
            self._attempts[node] = max(
                self._attempts.get(node, 0), int(event.get("attempt", 0))
            )

    def log_update(self, update: dict[str, Any]) -> None:
        _emit_update_events(self, update)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop_tick()
        # Leave the cursor at the end of the final render with a newline
        # so the next shell prompt or summary output starts clean.
        if self._enabled:
            self._stream.write("\n")
            self._stream.flush()

    # ---- Internals ----

    def _start_tick(self) -> None:
        if not self._enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — synchronous caller (e.g., unit test).
            # State updates still work; rendering won't auto-refresh.
            return
        if self._tick_task is None or self._tick_task.done():
            self._tick_task = loop.create_task(self._tick_loop())

    def _stop_tick(self) -> None:
        if self._tick_task is not None and not self._tick_task.done():
            self._tick_task.cancel()
        self._tick_task = None

    async def _tick_loop(self) -> None:
        try:
            while True:
                self._spinner_idx = (self._spinner_idx + 1) % len(_SPINNER_FRAMES)
                self._render()
                await asyncio.sleep(_TICK_SECONDS)
        except asyncio.CancelledError:
            pass

    def _node_status(self, node_id: str) -> str:
        if node_id in self._failed:
            return "failed"
        if node_id in self._completed:
            return "passed"
        if node_id in self._retrying:
            return "retrying"
        # "running" = all deps complete + not yet finished. Approximate
        # but accurate enough for visual feedback. Pure-waiting nodes
        # (deps not yet satisfied) sit at "waiting".
        if all(d in self._completed for d in self._deps.get(node_id, [])):
            return "running"
        return "waiting"

    def _header_line(self) -> str:
        if self._end_state == "done":
            elapsed = (self._ended_at or 0) - (self._started_at or 0)
            return f"sqrlly · {self._config.name}  done in {elapsed:.1f}s"
        if self._end_state == "failed":
            return f"sqrlly · {self._config.name}  failed"
        # Live: name + walking squirrel scene tied to current state.
        pile = len(self._completed)
        failed = len(self._failed)
        stash = max(0, len(self._node_ids) - pile - failed)
        scene = self._scene.frame(pile_count=pile, stash_count=stash)
        return f"sqrlly · {self._config.name}  {scene}"

    def _node_line(self, node_id: str, spinner: str) -> str:
        status = self._node_status(node_id)
        icon = _ICONS[status]
        # Spinner glyph appears next to a running node; blank otherwise.
        glyph = spinner if status == "running" else " "
        extras: list[str] = []
        if node_id in self._gate_scores:
            extras.append(f"gate={self._gate_scores[node_id]:.2f}")
        attempts = self._attempts.get(node_id, 0)
        if attempts > 1:
            extras.append(f"{attempts} attempts")
        if status == "failed" and self._errors.get(node_id):
            msg = self._errors[node_id].splitlines()[0][:60]
            extras.append(f'"{msg}"')
        extra = "  " + ", ".join(extras) if extras else ""
        return (
            f"  {icon} {node_id:<{self._id_width}}  "
            f"{status:<8} {glyph}{extra}"
        )

    def _render(self) -> None:
        if not self._enabled:
            return
        spinner = _SPINNER_FRAMES[self._spinner_idx]
        lines = [self._header_line(), ""]
        lines.extend(self._node_line(n, spinner) for n in self._node_ids)

        if self._last_lines_drawn > 0:
            # Move cursor up and clear from there to end of screen.
            self._stream.write(f"\033[{self._last_lines_drawn}A\033[J")

        # Trailing newline so the cursor lands one row below the last
        # rendered line. `_last_lines_drawn = len(lines)` then matches
        # the number of rows we need to move up next time — without the
        # trailing newline the cursor stays on the last row and we'd
        # over-shoot by one each cycle, scrolling the output up.
        self._stream.write("\n".join(lines) + "\n")
        self._stream.flush()
        self._last_lines_drawn = len(lines)


class TeeLogger:
    """Fan ``emit`` / ``log_update`` / ``close`` to multiple subscribers.

    Lets `sqrlly run` write a JSONL log AND render to terminal at the
    same time. Both subscribers implement the JsonlLogger-shaped
    interface; TeeLogger forwards calls in registration order.
    """

    def __init__(self, *subscribers: Any) -> None:
        self._subs: list[Any] = list(subscribers)

    def emit(self, event: dict[str, Any]) -> None:
        for sub in self._subs:
            sub.emit(event)

    def log_update(self, update: dict[str, Any]) -> None:
        for sub in self._subs:
            sub.log_update(update)

    def close(self) -> None:
        for sub in self._subs:
            close = getattr(sub, "close", None)
            if callable(close):
                close()
