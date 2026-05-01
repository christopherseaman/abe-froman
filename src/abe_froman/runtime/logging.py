"""Structured JSONL event logging for workflow execution."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any


class JsonlLogger:
    """Emits structured JSONL events to a file, one JSON object per line."""

    def __init__(self, dest: str | Path | IO[str]) -> None:
        if isinstance(dest, (str, Path)):
            self._file: IO[str] = open(dest, "a")
            self._owns_file = True
        else:
            self._file = dest
            self._owns_file = False

    def close(self) -> None:
        if self._owns_file:
            self._file.close()

    def emit(self, event: dict[str, Any]) -> None:
        """Write a single event as a JSON line with a timestamp."""
        record = {"ts": datetime.now(timezone.utc).isoformat(), **event}
        self._file.write(json.dumps(record) + "\n")
        self._file.flush()

    def log_snapshot(
        self,
        prev: dict[str, Any],
        curr: dict[str, Any],
    ) -> None:
        """Diff two state snapshots and emit events for all transitions."""
        prev_completed = set(prev.get("completed_nodes", []))
        curr_completed = set(curr.get("completed_nodes", []))
        for node in curr_completed - prev_completed:
            event: dict[str, Any] = {"event": "node_completed", "node": node}
            self.emit(event)

        prev_failed = set(prev.get("failed_nodes", []))
        curr_failed = set(curr.get("failed_nodes", []))
        for node in curr_failed - prev_failed:
            error = ""
            for err in curr.get("errors", []):
                if err.get("node") == node:
                    error = err.get("error", "")
                    break
            self.emit({"event": "node_failed", "node": node, "error": error})

        prev_evals = prev.get("evaluations", {})
        curr_evals = curr.get("evaluations", {})
        for node, records in curr_evals.items():
            prev_count = len(prev_evals.get(node, []))
            for record in records[prev_count:]:
                result = record.get("result", {})
                event: dict[str, Any] = {
                    "event": "gate_evaluated",
                    "node": node,
                    "invocation": record.get("invocation", 0),
                    "score": result.get("score", 0.0),
                }
                # Multi-dim gates: emit per-dimension scores so viewers
                # see the actual signal, not the 0.0 top-level placeholder.
                if result.get("scores"):
                    event["scores"] = result["scores"]
                self.emit(event)

        prev_retries = prev.get("retries", {})
        curr_retries = curr.get("retries", {})
        for node, count in curr_retries.items():
            if count > prev_retries.get(node, 0):
                self.emit({"event": "node_retried", "node": node, "attempt": count})


class SubgraphLogger:
    """Decorate a JsonlLogger with a node-id prefix for subgraph events.

    A subgraph wrapper streams its inner `astream(stream_mode="values")`
    snapshots through this decorator: the inner state's node ids get
    rewritten with `{prefix}::` before reaching the underlying JSONL,
    so subgraph-internal completions appear in the parent log keyed as
    `parent_node_id::inner_node_id`. Nested subgraphs compose naturally
    by nesting the prefix (`paper::reconcile::step1`).

    Stays langgraph-free; only consumes state-dict snapshots and
    delegates writes to JsonlLogger.emit, preserving the runtime layer
    rule.
    """

    def __init__(self, base: "JsonlLogger | SubgraphLogger", prefix: str) -> None:
        self._base = base
        self._prefix = prefix

    def emit(self, event: dict[str, Any]) -> None:
        if "node" in event:
            event = {**event, "node": f"{self._prefix}::{event['node']}"}
        self._base.emit(event)

    def log_snapshot(self, prev: dict[str, Any], curr: dict[str, Any]) -> None:
        # Reuse the base logger's diff logic but rewrite node ids in
        # each emitted event. The cheapest implementation is to
        # construct a thin proxy that intercepts emit() calls; that
        # avoids duplicating JsonlLogger.log_snapshot's diffing rules.
        proxy = _PrefixingProxy(self._base, self._prefix)
        # JsonlLogger.log_snapshot is a method on the JsonlLogger
        # instance, but it only calls self.emit — so we can rebind it
        # to the proxy for this single call.
        JsonlLogger.log_snapshot(proxy, prev, curr)


class _PrefixingProxy:
    """Internal: presents `emit()` matching JsonlLogger but prepends a
    prefix to the `node` field before delegating to the base logger.
    """

    def __init__(self, base: "JsonlLogger | SubgraphLogger", prefix: str) -> None:
        self._base = base
        self._prefix = prefix

    def emit(self, event: dict[str, Any]) -> None:
        if "node" in event:
            event = {**event, "node": f"{self._prefix}::{event['node']}"}
        self._base.emit(event)
