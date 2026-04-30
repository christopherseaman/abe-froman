"""Unit tests for JoinExecution: schema parse + dispatcher behavior.

Single-function tests cover known-good and known-bad inputs separately:
    - schema parses {type: join} into JoinExecution
    - schema rejects bad type
    - dispatcher returns ExecutionResult(success=True, output="")

Multi-function tests in tests/builder/test_join_node_shape.py cover graph
shape (multi-pred sync at a join node) and tests/e2e/test_join_node.py
covers a full workflow with an explicit join.
"""

from __future__ import annotations

import pytest

from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.result import ExecutionResult
from abe_froman.schema.models import (
    Execution,
    JoinExecution,
    Node,
    PromptExecution,
)
from pydantic import TypeAdapter, ValidationError


_ExecAdapter = TypeAdapter(Execution)


class TestJoinExecutionSchema:
    """Schema-level: discriminated union routing to JoinExecution."""

    def test_parses_join_type(self):
        execution = _ExecAdapter.validate_python({"type": "join"})
        assert isinstance(execution, JoinExecution)
        assert execution.type == "join"

    def test_rejects_unknown_type(self):
        with pytest.raises(ValidationError):
            _ExecAdapter.validate_python({"type": "unknown_kind"})

    def test_node_with_join_execution(self):
        node = Node(
            id="join1",
            name="Join 1",
            execution=JoinExecution(),
            depends_on=["a", "b"],
        )
        assert isinstance(node.execution, JoinExecution)
        assert node.depends_on == ["a", "b"]

    def test_join_alongside_evaluation(self):
        """Join node can carry an evaluation — runs eval against its empty output."""
        node = Node(
            id="checkpoint",
            name="Checkpoint",
            execution=JoinExecution(),
            evaluation={"validator": "v.py", "threshold": 0.5},
        )
        assert isinstance(node.execution, JoinExecution)
        assert node.evaluation is not None


class TestJoinExecutionDispatch:
    """Dispatcher routes JoinExecution to a no-op handler."""

    @pytest.mark.asyncio
    async def test_dispatch_returns_empty_success(self, tmp_path):
        executor = DispatchExecutor(workdir=str(tmp_path))
        node = Node(id="j", name="J", execution=JoinExecution(), depends_on=["a", "b"])
        result = await executor.execute(node, context={}, workdir=str(tmp_path))
        assert isinstance(result, ExecutionResult)
        assert result.success is True
        assert result.output == ""

    @pytest.mark.asyncio
    async def test_dispatch_ignores_context(self, tmp_path):
        """Join node ignores all context — it's a topology marker, no work."""
        executor = DispatchExecutor(workdir=str(tmp_path))
        node = Node(id="j", name="J", execution=JoinExecution())
        result = await executor.execute(
            node,
            context={"a": "upstream-output", "_retry_reason": "irrelevant"},
            workdir=str(tmp_path),
        )
        assert result.output == ""
        assert result.success is True

    @pytest.mark.asyncio
    async def test_dispatch_distinguishes_from_prompt(self, tmp_path):
        """Sanity: prompt execution still runs (and would fail without prompt_file).

        This proves the dispatcher's discriminated-union routing actually
        differentiates Join from other types, rather than handling them
        all the same way.
        """
        executor = DispatchExecutor(workdir=str(tmp_path))
        prompt_node = Node(
            id="p",
            name="P",
            execution=PromptExecution(prompt_file="missing.md"),
        )
        prompt_result = await executor.execute(prompt_node, context={}, workdir=str(tmp_path))
        # Stub backend (no prompt_executor) returns the placeholder output —
        # this is NOT the join's empty-output path.
        assert "[prompt-stub]" in prompt_result.output
