"""Conditional routers for the compilation layer.

These functions return closures used as the `path` argument to
`StateGraph.add_conditional_edges`. They inspect state and return either
a routing string (pass/retry/fail/no_items) or a list of `Send` objects
for runtime fan-out.

The `_read_manifest` helper also lives here because it's invoked by
`_make_dynamic_router` and is conceptually part of the dynamic routing
decision.
"""

from __future__ import annotations

import json
from pathlib import Path

from langgraph.types import Send

from abe_froman.runtime.state import WorkflowState
from abe_froman.schema.models import Phase, WorkflowConfig


def _make_gate_router(phase: Phase, max_retries: int):
    """Create a conditional routing function for quality gates."""

    def router(state: WorkflowState) -> str:
        # Contract failure is always a hard fail — no retry, no pass-through
        if phase.id in state.get("failed_phases", []):
            return "fail"

        score = state.get("gate_scores", {}).get(phase.id, 0.0)
        threshold = phase.quality_gate.threshold
        retries = state.get("retries", {}).get(phase.id, 0)

        if score >= threshold:
            return "pass"
        elif retries < max_retries:
            return "retry"
        elif not phase.quality_gate.blocking:
            return "pass"
        else:
            return "fail"

    return router


def _read_manifest(state: WorkflowState, phase: Phase) -> list[dict]:
    """Read manifest items from phase output or from disk.

    Tries parsing the phase's output as JSON first (looking for an "items"
    key or a bare list). Falls back to reading manifest_path from disk.
    """
    output = state.get("phase_outputs", {}).get(phase.id, "")
    try:
        data = json.loads(output)
        if isinstance(data, dict) and "items" in data:
            return data["items"]
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, TypeError):
        pass

    if phase.dynamic_subphases and phase.dynamic_subphases.manifest_path:
        manifest_file = (
            Path(state.get("workdir", ".")) / phase.dynamic_subphases.manifest_path
        )
        try:
            data = json.loads(manifest_file.read_text())
            if isinstance(data, dict) and "items" in data:
                return data["items"]
            if isinstance(data, list):
                return data
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    return []


def _make_dynamic_router(phase: Phase, config: WorkflowConfig):
    """Create a conditional edge function that handles gate + Send fan-out.

    On pass (or non-blocking exhausted): reads the manifest and returns
    a list of Send objects to dispatch the template node for each item.
    On retry/fail: returns the routing string as usual.
    """
    max_retries = phase.effective_max_retries(config.settings)
    template_node_id = f"_sub_{phase.id}"

    # Determine what to route to when manifest is empty
    dsc = phase.dynamic_subphases
    if dsc.final_phases:
        no_items_target = f"_final_{phase.id}_{dsc.final_phases[0].id}"
    else:
        no_items_target = None  # will be set in build_workflow_graph

    def router(state: WorkflowState):
        # Contract failure is always a hard fail
        if phase.id in state.get("failed_phases", []):
            return "fail"

        # Gate check (same logic as _make_gate_router)
        if phase.quality_gate:
            score = state.get("gate_scores", {}).get(phase.id, 0.0)
            threshold = phase.quality_gate.threshold
            retries = state.get("retries", {}).get(phase.id, 0)

            if score < threshold:
                if retries < max_retries:
                    return "retry"
                elif phase.quality_gate.blocking:
                    return "fail"
                # non-blocking: fall through to fan-out

        # Read manifest and fan out
        items = _read_manifest(state, phase)
        if not items:
            return "no_items"

        return [
            Send(template_node_id, {**state, "_subphase_item": item})
            for item in items
        ]

    return router, no_items_target
