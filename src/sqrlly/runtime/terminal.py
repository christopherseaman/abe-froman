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
    """A foraging squirrel under a tree, gathering falling nuts.

    Layout: ``🌳 [walkway with falling nuts and a seeking squirrel]``

    Each walkway cell is a per-column state machine:

    - ``None`` — empty
    - ``0``  — ``⠁`` (just spawned, falling)
    - ``1``  — ``⠂``
    - ``2``  — ``⠄``
    - ``3``  — ``⡀`` (landed; available to be eaten)

    Nuts fall into pseudorandom walkway cells at a rate proportional to
    ``stash_count`` — `(tick, position)` is seeded so the spawn pattern
    is reproducible for tests. The squirrel moves one cell per tick
    toward the nearest landed nut; when none exists it wiggles in
    place so the aliveness contract still holds. Direction (left vs.
    right glyph) follows the most recent movement step.

    The pile concept is gone — completion progress is read from the
    per-node status grid below the scene, not duplicated in the header.
    """

    _WALKWAY = 40

    _TREE = "🌳"
    _SQ_L = "🬢🭠"           # left-facing squirrel glyph pair
    _SQ_R = "🭕🬖"           # right-facing squirrel glyph pair
    _FALL = ["⠁", "⠂", "⠄", "⡀"]   # in-place fall progression

    def __init__(self) -> None:
        self._tick = 0
        self._dots: list[int | None] = [None] * self._WALKWAY
        self._sq_pos = self._WALKWAY // 2
        self._facing_right = True

    def _nearest_landed(self) -> int | None:
        """Index of the nearest landed nut to the squirrel's position,
        or None if no nuts are landed.

        Direction the squirrel is currently facing acts as a tiebreaker:
        among equally-distant nuts, the one in the facing direction
        wins. Means a moving squirrel doesn't reverse course unless
        the nut behind it is strictly closer.
        """
        landed = [i for i, d in enumerate(self._dots) if d == 3]
        if not landed:
            return None

        def key(i: int) -> tuple[int, int]:
            dist = abs(i - self._sq_pos)
            if self._facing_right:
                same_dir_tier = 0 if i >= self._sq_pos else 1
            else:
                same_dir_tier = 0 if i <= self._sq_pos else 1
            return (dist, same_dir_tier)

        return min(landed, key=key)

    def _step_toward(self, target: int | None) -> None:
        """Move one cell toward `target`, or wiggle when target is None."""
        if target is None:
            # Idle wiggle: oscillate left/right every few ticks. Keeps
            # the squirrel visibly alive when nothing has fallen yet.
            wiggle = 1 if (self._tick // 3) % 2 == 0 else -1
            new_pos = self._sq_pos + wiggle
            if 0 <= new_pos <= self._WALKWAY - 2:
                self._sq_pos = new_pos
                self._facing_right = wiggle > 0
            return
        if target > self._sq_pos:
            self._sq_pos = min(self._sq_pos + 1, self._WALKWAY - 2)
            self._facing_right = True
        elif target < self._sq_pos:
            self._sq_pos = max(self._sq_pos - 1, 0)
            self._facing_right = False
        # target == sq_pos → consume happens below; no movement.

    def frame(self, *, pile_count: int, stash_count: int) -> str:
        # `pile_count` is accepted for interface stability; the scene
        # no longer surfaces completion progress (the per-node grid
        # does). It may earn a use later.
        del pile_count

        self._tick += 1

        # 1. Advance falls (0/1/2 → next state); landed (3) stays.
        for i, d in enumerate(self._dots):
            if d is not None and d < 3:
                self._dots[i] = d + 1

        # 2. Spawn new fallers pseudorandomly until walkway population
        #    matches stash_count. Reproducible: seeded RNG keyed on tick.
        on_screen = sum(1 for d in self._dots if d is not None)
        if stash_count > 0 and on_screen < min(stash_count, self._WALKWAY - 2):
            empties = [
                i for i, d in enumerate(self._dots)
                if d is None and not (self._sq_pos <= i <= self._sq_pos + 1)
            ]
            if empties:
                import random
                rng = random.Random(self._tick * 1009)
                self._dots[rng.choice(empties)] = 0

        # 3. Move squirrel toward nearest landed nut (or wiggle).
        self._step_toward(self._nearest_landed())

        # 4. Consume any landed dot under the squirrel's 2-cell footprint.
        for offset in (0, 1):
            p = self._sq_pos + offset
            if 0 <= p < self._WALKWAY and self._dots[p] == 3:
                self._dots[p] = None

        # 5. Render walkway.
        cells = [" "] * self._WALKWAY
        for i, d in enumerate(self._dots):
            if d is not None:
                cells[i] = self._FALL[d]
        sq_glyph = self._SQ_R if self._facing_right else self._SQ_L
        cells[self._sq_pos] = sq_glyph[0]
        if self._sq_pos + 1 < self._WALKWAY:
            cells[self._sq_pos + 1] = sq_glyph[1]

        return f"{self._TREE} {''.join(cells)}"


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
        return f"sqrlly · {self._config.name}"

    def _scene_line(self) -> str:
        """The aliveness scene on its own line. Empty after end_state
        is reached so the squirrel doesn't continue acting after the
        workflow stopped — line count stays stable for redraw."""
        if self._end_state is not None:
            return ""
        pile = len(self._completed)
        failed = len(self._failed)
        stash = max(0, len(self._node_ids) - pile - failed)
        return self._scene.frame(pile_count=pile, stash_count=stash)

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
        lines = [self._header_line(), self._scene_line(), ""]
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
