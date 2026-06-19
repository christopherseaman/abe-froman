"""Fan-out child resume skip-guard: a child is frozen only when its PARENT
is also in the skip-set (so the manifest isn't re-derived and child ids are
stable)."""
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
async def test_child_frozen_when_parent_and_child_in_skip():
    ex = MockExecutor()
    nf = _make(_fan_parent(), ex)
    state = make_initial_state(
        _fan_out_item={"id": "item1"},
        _resume_skip={"fan", "fan::item1"},
    )
    assert await nf(state) == {}
    assert ex.execution_order == []


@pytest.mark.asyncio
async def test_child_runs_when_parent_not_in_skip():
    # Parent dirty (re-fanned) => child must run even if its id is in skip.
    ex = MockExecutor()
    nf = _make(_fan_parent(), ex)
    state = make_initial_state(
        _fan_out_item={"id": "item1"},
        _resume_skip={"fan::item1"},  # parent "fan" NOT in skip
    )
    result = await nf(state)
    assert result != {}  # proceeded past the skip-guard
