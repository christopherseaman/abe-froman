"""The non-subgraph fan-out synthetic child node inherits the template's
worktree override, so ForemanExecutor.effective_worktree resolves it.

Uses MockExecutor (the sanctioned NodeExecutor double) to capture the
Node object handed to execute(); no LLM, no git.
"""
from __future__ import annotations

import pytest

from sqrlly.compile.dynamic import _make_fan_out_node
from sqrlly.runtime.result import ExecutionResult
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import (
    Execute, FanOut, FanOutTemplate, Graph, Node, Settings,
)

from mock_executor import MockExecutor


def _parent(template_worktree):
    return Node(
        id="build", name="build",
        fan_out=FanOut(
            template=FanOutTemplate(
                execute=Execute(url="/bin/sh", params={"args": ["-c", "true"]}),
                worktree=template_worktree,
            ),
        ),
    )


class _CapturingExecutor(MockExecutor):
    """Captures the Node passed to execute(); returns a trivial success."""
    def __init__(self):
        super().__init__()
        self.captured: list[Node] = []

    async def execute(self, node, context, workdir=None, settings_override=None):
        self.captured.append(node)
        return ExecutionResult(success=True, output="ok")


@pytest.mark.asyncio
@pytest.mark.parametrize("template_worktree", ["off", "isolated", None])
async def test_synthetic_child_inherits_template_worktree(template_worktree):
    parent = _parent(template_worktree)
    config = Graph(name="t", version="1.0", nodes=[parent],
                   settings=Settings(worktree="isolated"))
    executor = _CapturingExecutor()
    node_fn = _make_fan_out_node(
        parent, config, executor,
        compile_fn=lambda *a, **k: None, base_dir=".", depth=0,
        effective_settings=config.settings,
    )
    state = make_initial_state(workdir=".")
    state["_fan_out_item"] = {"id": "x"}
    await node_fn(state)

    assert len(executor.captured) == 1
    synthetic = executor.captured[0]
    assert synthetic.id == "build::x"
    assert synthetic.worktree == template_worktree
    # effective_worktree resolves the per-fan-out override over settings.
    expected = (template_worktree or "isolated", None)
    assert synthetic.effective_worktree(config.settings) == expected
