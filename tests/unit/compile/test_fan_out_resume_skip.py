"""Fan-out child resume skip-guard: a child is frozen when its OWN id is in
the frozen skip snapshot — regardless of whether the parent re-fanned. Only
ids that completed cleanly last run are in the snapshot, so a completed child
is never re-billed even when a sibling failed and the parent re-fans; the
formerly-failed child (never in prior_completed → never in skip) runs."""
import pytest

from sqrlly.compile.dynamic import _make_fan_out_node
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Execute, FanOut, FanOutTemplate, Node, Graph, Settings
from mock_executor import MockExecutor


def _fan_parent():
    return Node(
        id="fan", name="Fan",
        fan_out=FanOut(
            manifest_path="m.json",
            template=FanOutTemplate(execute=Execute(url="t.md")),
        ),
    )


def _make(parent, executor):
    cfg = Graph(name="T", version="1.0", nodes=[parent], settings=Settings())
    return _make_fan_out_node(
        parent, cfg, executor,
        compile_fn=lambda *a, **k: None, base_dir=".", depth=0,
    )


@pytest.mark.asyncio
async def test_child_frozen_when_child_in_skip():
    """Child id in the frozen snapshot → frozen (not re-executed)."""
    ex = MockExecutor()
    nf = _make(_fan_parent(), ex)
    state = make_initial_state(
        _fan_out_item={"id": "item1"},
        _resume_skip={"fan", "fan::item1"},
    )
    assert await nf(state) == {}
    assert ex.execution_order == []


@pytest.mark.asyncio
async def test_completed_child_frozen_even_when_parent_refans():
    """Parent dirty (re-fanned, NOT in skip), but this child completed last
    run and IS in skip → it must stay frozen (no re-bill of a clean sibling)."""
    ex = MockExecutor()
    nf = _make(_fan_parent(), ex)
    state = make_initial_state(
        _fan_out_item={"id": "alpha"},
        _resume_skip={"fan::alpha", "fan::gamma"},  # parent 'fan' NOT in skip
    )
    assert await nf(state) == {}
    assert ex.execution_order == []


@pytest.mark.asyncio
async def test_failed_child_runs_when_not_in_skip():
    """The formerly-failed child id is NOT in the snapshot (never completed),
    so it runs on resume even though its siblings are frozen."""
    ex = MockExecutor()
    nf = _make(_fan_parent(), ex)
    state = make_initial_state(
        _fan_out_item={"id": "beta"},
        _resume_skip={"fan::alpha", "fan::gamma"},  # beta absent → runs
    )
    result = await nf(state)
    assert result != {}  # proceeded past the skip-guard
    assert ex.execution_order == ["fan::beta"]  # the formerly-failed child ran


@pytest.mark.asyncio
async def test_no_skip_set_runs_normally():
    """Absent _resume_skip (fresh run) → child runs."""
    ex = MockExecutor()
    nf = _make(_fan_parent(), ex)
    state = make_initial_state(_fan_out_item={"id": "x"})
    result = await nf(state)
    assert result != {}
