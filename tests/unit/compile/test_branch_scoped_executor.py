import pytest
from sqrlly.compile.subgraph import _strip_worktree, _BranchScopedExecutor
from sqrlly.schema.models import Node, Settings, Execute
from sqrlly.runtime.result import ExecutionResult


class _Recorder:
    def __init__(self):
        self.calls = []

    async def execute(self, node, context, workdir=None, settings_override=None):
        self.calls.append((node, workdir))
        return ExecutionResult(success=True, output="ok")


def test_strip_worktree_forces_off_for_explicit_and_inherit():
    explicit = Node(id="a", name="a", worktree="isolated")
    inherit = Node(id="b", name="b")
    assert _strip_worktree(explicit).effective_worktree(Settings(worktree="isolated")) == ("off", None)
    assert _strip_worktree(inherit).effective_worktree(Settings(worktree="auto")) == ("off", None)


@pytest.mark.asyncio
async def test_wrapper_pins_workdir_and_neutralizes_node():
    rec = _Recorder()
    wrapped = _BranchScopedExecutor(rec, "/branch/tree")
    node = Node(id="inner", name="inner", worktree="isolated",
                execute=Execute(url="/bin/true", params={"args": []}))
    result = await wrapped.execute(node, {})
    assert result.success
    seen_node, seen_workdir = rec.calls[0]
    assert seen_workdir == "/branch/tree"
    assert seen_node.effective_worktree(Settings(worktree="isolated")) == ("off", None)
