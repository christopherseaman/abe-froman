"""Top-level graph builder: YAML config → compiled LangGraph StateGraph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from abe_froman.compile.dynamic import _make_final_phase_node, _make_subphase_node
from abe_froman.compile.nodes import _make_phase_node
from abe_froman.runtime.state import WorkflowState
from abe_froman.schema.models import Phase, WorkflowConfig

if TYPE_CHECKING:
    from abe_froman.runtime.result import PhaseExecutor


def _find_terminal_phases(config: WorkflowConfig) -> set[str]:
    depended_on: set[str] = set()
    for phase in config.phases:
        depended_on.update(phase.depends_on)
    return {p.id for p in config.phases if p.id not in depended_on}


def _detect_cycles(config: WorkflowConfig) -> None:
    adj: dict[str, list[str]] = {p.id: list(p.depends_on) for p in config.phases}
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {pid: WHITE for pid in adj}

    def dfs(node: str) -> None:
        color[node] = GRAY
        for dep in adj[node]:
            if dep not in color:
                continue
            if color[dep] == GRAY:
                raise ValueError(
                    f"Circular dependency detected involving '{node}' and '{dep}'"
                )
            if color[dep] == WHITE:
                dfs(dep)
        color[node] = BLACK

    for node in adj:
        if color[node] == WHITE:
            dfs(node)


def _make_gate_router(phase: Phase, pass_targets: list[str] | None = None):
    targets = pass_targets or [END]

    def router(state: WorkflowState) -> str | list[str]:
        if phase.id in state.get("failed_phases", []):
            return END
        if phase.id in state.get("completed_phases", []):
            return targets[0] if len(targets) == 1 else targets
        return phase.id

    return router


def _read_manifest(state: WorkflowState, phase: Phase) -> list[dict]:
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
    template_node_id = f"_sub_{phase.id}"

    dsc = phase.dynamic_subphases
    if dsc.final_phases:
        no_items_target = f"_final_{phase.id}_{dsc.final_phases[0].id}"
    else:
        no_items_target = None

    def router(state: WorkflowState):
        if phase.id in state.get("failed_phases", []):
            return "fail"
        if phase.quality_gate and phase.id not in state.get("completed_phases", []):
            return "retry"

        items = _read_manifest(state, phase)
        if not items:
            return "no_items"

        return [
            Send(template_node_id, {**state, "_subphase_item": item})
            for item in items
        ]

    return router, no_items_target


def build_workflow_graph(
    config: WorkflowConfig,
    executor: PhaseExecutor | None = None,
    checkpointer: Any = None,
) -> Any:
    """Build a compiled LangGraph StateGraph from workflow config.

    If `checkpointer` is provided, the compiled graph will persist state
    after each node via LangGraph's checkpointer protocol.
    """
    _detect_cycles(config)

    builder = StateGraph(WorkflowState)
    terminal_ids = _find_terminal_phases(config)
    phase_map = {p.id: p for p in config.phases}

    gated_phase_ids: set[str] = set()
    dynamic_phase_ids: set[str] = set()

    for phase in config.phases:
        if phase.quality_gate:
            gated_phase_ids.add(phase.id)
        if phase.dynamic_subphases and phase.dynamic_subphases.enabled:
            dynamic_phase_ids.add(phase.id)

    for phase in config.phases:
        builder.add_node(phase.id, _make_phase_node(phase, config, executor))

    for phase_id in dynamic_phase_ids:
        phase = phase_map[phase_id]
        dsc = phase.dynamic_subphases

        template_node_id = f"_sub_{phase.id}"
        builder.add_node(
            template_node_id,
            _make_subphase_node(phase, config, executor),
        )

        for final_phase in dsc.final_phases:
            final_node_id = f"_final_{phase.id}_{final_phase.id}"
            builder.add_node(
                final_node_id,
                _make_final_phase_node(phase, final_phase, config, executor),
            )

    exit_node: dict[str, str] = {}
    needs_conditional: set[str] = set()

    for phase in config.phases:
        if phase.id in dynamic_phase_ids:
            dsc = phase.dynamic_subphases
            if dsc.final_phases:
                exit_node[phase.id] = (
                    f"_final_{phase.id}_{dsc.final_phases[-1].id}"
                )
            else:
                exit_node[phase.id] = f"_sub_{phase.id}"
            needs_conditional.add(phase.id)
        elif phase.id in gated_phase_ids:
            exit_node[phase.id] = phase.id
            needs_conditional.add(phase.id)
        else:
            exit_node[phase.id] = phase.id

    has_incoming: set[str] = set()

    for phase in config.phases:
        if not phase.depends_on:
            continue

        for dep in phase.depends_on:
            if dep in needs_conditional:
                pass
            else:
                builder.add_edge(exit_node[dep], phase.id)
            has_incoming.add(phase.id)

    for phase in config.phases:
        if phase.id not in has_incoming:
            builder.add_edge(START, phase.id)

    for phase_id in dynamic_phase_ids:
        phase = phase_map[phase_id]
        dsc = phase.dynamic_subphases
        template_node_id = f"_sub_{phase.id}"

        if dsc.final_phases:
            first_final = f"_final_{phase.id}_{dsc.final_phases[0].id}"
            builder.add_edge(template_node_id, first_final)

            for i in range(len(dsc.final_phases) - 1):
                current = f"_final_{phase.id}_{dsc.final_phases[i].id}"
                next_ = f"_final_{phase.id}_{dsc.final_phases[i + 1].id}"
                builder.add_edge(current, next_)

        exit_id = exit_node[phase.id]
        dependents = [
            p.id for p in config.phases if phase.id in p.depends_on
        ]

        if not dependents:
            builder.add_edge(exit_id, END)
        elif len(dependents) == 1:
            builder.add_edge(exit_id, dependents[0])
        else:
            for dep_id in dependents:
                builder.add_edge(exit_id, dep_id)

        router, no_items_target = _make_dynamic_router(phase, config)
        route_map: dict[str, str] = {"retry": phase.id, "fail": END}

        if no_items_target:
            route_map["no_items"] = no_items_target
        elif dependents:
            route_map["no_items"] = dependents[0]
        else:
            route_map["no_items"] = END

        builder.add_conditional_edges(phase.id, router, route_map)

    for phase in config.phases:
        if phase.id not in gated_phase_ids or phase.id in dynamic_phase_ids:
            continue

        dependents = [
            p.id for p in config.phases if phase.id in p.depends_on
        ]
        pass_targets = dependents if dependents else [END]

        router = _make_gate_router(phase, pass_targets)
        all_targets = [phase.id, END] + dependents
        builder.add_conditional_edges(phase.id, router, all_targets)

    for phase in config.phases:
        if (
            phase.id in terminal_ids
            and phase.id not in gated_phase_ids
            and phase.id not in dynamic_phase_ids
        ):
            builder.add_edge(phase.id, END)

    return builder.compile(checkpointer=checkpointer)
