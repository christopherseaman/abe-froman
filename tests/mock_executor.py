from __future__ import annotations

from typing import Any

from sqrlly.runtime.result import ExecutionResult
from sqrlly.schema.models import Node, Settings


class MockExecutor:
    def __init__(
        self,
        results: dict[str, ExecutionResult] | None = None,
    ):
        self._results = results or {}
        self.execution_order: list[str] = []
        self.received_contexts: dict[str, dict[str, Any]] = {}
        # Phase 3: capture which scope settings each node received so
        # tests can assert per-scope inheritance (e.g. parent vs subgraph
        # default_model, base_url, etc.).
        self.received_settings: dict[str, Settings | None] = {}

    async def execute(
        self,
        node: Node,
        context: dict[str, Any],
        workdir: str | None = None,
        settings_override: Settings | None = None,
    ) -> ExecutionResult:
        self.execution_order.append(node.id)
        self.received_contexts[node.id] = context
        self.received_settings[node.id] = settings_override

        if node.id in self._results:
            return self._results[node.id]

        return ExecutionResult(
            success=True,
            output=f"[mock] {node.id} completed",
        )
