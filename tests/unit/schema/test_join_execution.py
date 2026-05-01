"""Unit tests for execute.type=join: schema parse + dispatcher behavior.

Single-function tests cover known-good and known-bad inputs separately:
    - schema parses {type: join} into Execute(type='join')
    - schema rejects join with extra fields (cases/else/params)
    - dispatcher returns ExecutionResult(success=True, output="")

Multi-function tests in tests/builder/test_join_node_shape.py cover graph
shape (multi-pred sync at a join node) and tests/e2e/test_join_node.py
covers a full workflow with an explicit join.
"""

from __future__ import annotations

import pytest

from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.result import ExecutionResult
from abe_froman.schema.models import Execute, Node
from pydantic import ValidationError


class TestJoinExecuteSchema:
    """Schema-level: type=join carve-out of the Execute shape."""

    def test_parses_join_type(self):
        execute = Execute(type="join")
        assert execute.type == "join"
        assert execute.url is None

    def test_rejects_unknown_type(self):
        with pytest.raises(ValidationError):
            Execute(type="unknown_kind")

    def test_node_with_join_execute(self):
        node = Node(
            id="join1",
            name="Join 1",
            execute=Execute(type="join"),
            depends_on=["a", "b"],
        )
        assert node.execute is not None
        assert node.execute.type == "join"
        assert node.depends_on == ["a", "b"]

    def test_join_alongside_evaluation(self):
        """Join node can carry an evaluation — runs eval against its empty output."""
        node = Node(
            id="checkpoint",
            name="Checkpoint",
            execute=Execute(type="join"),
            evaluation={"validator": "v.py", "threshold": 0.5},
        )
        assert node.execute.type == "join"
        assert node.evaluation is not None

    def test_join_rejects_cases(self):
        with pytest.raises(ValidationError):
            Execute(type="join", cases=[{"when": "True", "goto": "x"}])

    def test_join_rejects_params(self):
        with pytest.raises(ValidationError):
            Execute(type="join", params={"args": ["nope"]})

    def test_join_with_url_is_invalid(self):
        """Exactly one of {url, type=join, type=route} must be set."""
        with pytest.raises(ValidationError):
            Execute(type="join", url="x.md")


class TestJoinExecuteDispatch:
    """Dispatcher routes execute.type=join to a no-op handler."""

    @pytest.mark.asyncio
    async def test_dispatch_returns_empty_success(self, tmp_path):
        executor = DispatchExecutor(workdir=str(tmp_path))
        node = Node(
            id="j", name="J",
            execute=Execute(type="join"), depends_on=["a", "b"],
        )
        result = await executor.execute(node, context={}, workdir=str(tmp_path))
        assert isinstance(result, ExecutionResult)
        assert result.success is True
        assert result.output == ""

    @pytest.mark.asyncio
    async def test_dispatch_ignores_context(self, tmp_path):
        executor = DispatchExecutor(workdir=str(tmp_path))
        node = Node(id="j", name="J", execute=Execute(type="join"))
        result = await executor.execute(
            node,
            context={"a": "upstream-output", "_retry_reason": "irrelevant"},
            workdir=str(tmp_path),
        )
        assert result.output == ""
        assert result.success is True

    @pytest.mark.asyncio
    async def test_dispatch_distinguishes_from_prompt(self, tmp_path):
        """Sanity: prompt execution still runs (and the stub backend returns a
        recognizable placeholder), proving the dispatcher's branching actually
        differentiates Join from URL-mode, rather than handling them the same."""
        executor = DispatchExecutor(workdir=str(tmp_path))
        prompt_node = Node(
            id="p", name="P", execute=Execute(url="missing.md"),
        )
        prompt_result = await executor.execute(
            prompt_node, context={}, workdir=str(tmp_path),
        )
        assert "[prompt-stub]" in prompt_result.output
