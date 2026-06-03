"""Structured JSONL event logging for workflow execution."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any


def _emit_update_events(emitter: Any, update: dict[str, Any]) -> None:
    """Derive workflow events from a single super-step partial update.

    A LangGraph ``stream_mode="updates"`` chunk is shaped
    ``{node_name: partial_state_update}``; the partial update is the
    delta the node returned (after reducer application). The event
    types we surface — ``node_model`` (LLM nodes only: which
    preset/model ran), ``node_completed``, ``node_failed``,
    ``gate_evaluated``, ``node_retried`` — each correspond to a
    specific shape inside that delta. We read them directly here
    rather than diffing successive snapshots.

    Free function rather than a method so ``JsonlLogger`` and
    ``SubgraphLogger`` share the derivation logic and only differ in
    how their ``emit()`` formats the event (``SubgraphLogger`` prefixes
    the ``node`` field). The ``emitter`` argument duck-types ``emit()``.

    Emission order is fixed: ``node_model`` → ``node_completed`` →
    ``node_failed`` → ``gate_evaluated`` → ``node_retried``. Tests
    asserting on event order depend on this.

    Command-only nodes (route dispatchers that emit ``Command(goto=...)``
    without any state update) appear in the updates stream as
    ``{node_name: None}``. Skip them — no events to derive.
    """
    if update is None:
        return
    for node, info in (update.get("node_models") or {}).items():
        emitter.emit({
            "event": "node_model",
            "node": node,
            "model": info.get("model"),
            "preset": info.get("preset"),
        })

    for node in update.get("completed_nodes") or []:
        emitter.emit({"event": "node_completed", "node": node})

    for node in update.get("failed_nodes") or []:
        error = ""
        for err in update.get("errors") or []:
            if err.get("node") == node:
                error = err.get("error", "")
                break
        emitter.emit({"event": "node_failed", "node": node, "error": error})

    for node, records in (update.get("evaluations") or {}).items():
        for record in records:
            result = record.get("result", {}) or {}
            event: dict[str, Any] = {
                "event": "gate_evaluated",
                "node": node,
                "invocation": record.get("invocation", 0),
                "score": result.get("score", 0.0),
            }
            # Multi-dim gates: emit per-dimension scores so viewers see
            # the actual signal, not the 0.0 top-level placeholder.
            if result.get("scores"):
                event["scores"] = result["scores"]
            # Surface the pass decision + its inputs (recorded by
            # _evaluation_result_payload) so a consumer distinguishes a pass
            # from a non-blocking warn-continue without recomputing.
            for k in ("passed", "blocking", "threshold"):
                if k in result:
                    event[k] = result[k]
            emitter.emit(event)

    for node, count in (update.get("retries") or {}).items():
        emitter.emit({"event": "node_retried", "node": node, "attempt": count})


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

    def log_update(self, update: dict[str, Any]) -> None:
        """Derive events from a LangGraph super-step partial update."""
        _emit_update_events(self, update)


class SubgraphLogger:
    """Decorate a JsonlLogger with a node-id prefix for subgraph events.

    A subgraph wrapper streams its inner ``astream(stream_mode=
    "updates")`` chunks through this decorator: events emitted from the
    derived update get their ``node`` field rewritten with
    ``{prefix}::`` before reaching the underlying JSONL, so subgraph-
    internal events appear in the parent log keyed as
    ``parent_node_id::inner_node_id``. Nested subgraphs compose
    naturally by nesting the prefix (``paper::reconcile::step1``).

    Stays langgraph-free; only consumes update dicts and delegates
    writes via ``JsonlLogger.emit`` (or another wrapped SubgraphLogger),
    preserving the runtime layer rule.
    """

    def __init__(self, base: "JsonlLogger | SubgraphLogger", prefix: str) -> None:
        self._base = base
        self._prefix = prefix

    def emit(self, event: dict[str, Any]) -> None:
        if "node" in event:
            event = {**event, "node": f"{self._prefix}::{event['node']}"}
        self._base.emit(event)

    def log_update(self, update: dict[str, Any]) -> None:
        # Same derivation logic as JsonlLogger; events flow through
        # this instance's prefixing emit().
        _emit_update_events(self, update)
