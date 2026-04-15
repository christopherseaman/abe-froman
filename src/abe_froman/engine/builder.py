from __future__ import annotations

from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph

from abe_froman.compile.dynamic import _make_final_phase_node, _make_subphase_node
from abe_froman.compile.nodes import _get_retry_delay, _make_phase_node
from abe_froman.compile.routers import (
    _make_dynamic_router,
    _make_gate_router,
    _read_manifest,
)
from abe_froman.engine.state import WorkflowState
from abe_froman.schema.models import WorkflowConfig

if TYPE_CHECKING:
    from abe_froman.executor.base import PhaseExecutor

__all__ = [
    "build_workflow_graph",
    "_make_phase_node",
    "_make_gate_router",
    "_make_dynamic_router",
    "_read_manifest",
    "_get_retry_delay",
]


def _find_terminal_phases(config: WorkflowConfig) -> set[str]:
    """Return IDs of phases that no other phase depends on."""
    depended_on: set[str] = set()
    for phase in config.phases:
        depended_on.update(phase.depends_on)
    return {p.id for p in config.phases if p.id not in depended_on}


def _detect_cycles(config: WorkflowConfig) -> None:
    """Detect circular dependencies via DFS."""
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


def build_workflow_graph(
    config: WorkflowConfig,
    executor: PhaseExecutor | None = None,
) -> Any:
    """Build a compiled LangGraph StateGraph from workflow config.

    Each phase becomes a node. Dependencies become edges. Quality gates
    add conditional routing (pass/retry/fail) after the gated phase.
    Dynamic subphases use LangGraph Send for runtime fan-out.
    """
    _detect_cycles(config)

    builder = StateGraph(WorkflowState)
    terminal_ids = _find_terminal_phases(config)
    phase_map = {p.id: p for p in config.phases}

    # --- Identify special phase types ---
    gated_phase_ids: set[str] = set()
    dynamic_phase_ids: set[str] = set()

    for phase in config.phases:
        if phase.quality_gate:
            gated_phase_ids.add(phase.id)
        if phase.dynamic_subphases and phase.dynamic_subphases.enabled:
            dynamic_phase_ids.add(phase.id)

    # --- Add nodes ---
    for phase in config.phases:
        builder.add_node(phase.id, _make_phase_node(phase, config, executor))

    # Add template + final nodes for dynamic phases
    for phase_id in dynamic_phase_ids:
        phase = phase_map[phase_id]
        dsc = phase.dynamic_subphases

        # Template subphase node (dispatched N times via Send)
        template_node_id = f"_sub_{phase.id}"
        builder.add_node(
            template_node_id,
            _make_subphase_node(phase, config, executor),
        )

        # Final phase nodes
        for final_phase in dsc.final_phases:
            final_node_id = f"_final_{phase.id}_{final_phase.id}"
            builder.add_node(
                final_node_id,
                _make_final_phase_node(phase, final_phase, config, executor),
            )

    # --- Build exit_node map ---
    # For dynamic phases, downstream dependents wire from the exit node
    # (last final phase or template node) rather than the parent directly.
    exit_node: dict[str, str] = {}
    # Track which nodes need conditional edges (gated or dynamic)
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
            exit_node[phase.id] = phase.id  # handled by conditional edges
            needs_conditional.add(phase.id)
        else:
            exit_node[phase.id] = phase.id

    # --- Wire dependency edges ---
    has_incoming: set[str] = set()

    for phase in config.phases:
        if not phase.depends_on:
            continue

        for dep in phase.depends_on:
            if dep in needs_conditional:
                # Conditional edges handle routing from this dep
                pass
            else:
                builder.add_edge(exit_node[dep], phase.id)
            has_incoming.add(phase.id)

    # Root phases get edge from START
    for phase in config.phases:
        if phase.id not in has_incoming:
            builder.add_edge(START, phase.id)

    # --- Wire internal edges for dynamic phases ---
    for phase_id in dynamic_phase_ids:
        phase = phase_map[phase_id]
        dsc = phase.dynamic_subphases
        template_node_id = f"_sub_{phase.id}"

        # Chain: template -> final[0] -> final[1] -> ... -> exit
        if dsc.final_phases:
            first_final = f"_final_{phase.id}_{dsc.final_phases[0].id}"
            builder.add_edge(template_node_id, first_final)

            for i in range(len(dsc.final_phases) - 1):
                current = f"_final_{phase.id}_{dsc.final_phases[i].id}"
                next_ = f"_final_{phase.id}_{dsc.final_phases[i + 1].id}"
                builder.add_edge(current, next_)

        # Wire exit node to dependents or END
        exit_id = exit_node[phase.id]
        dependents = [
            p.id for p in config.phases if phase.id in p.depends_on
        ]

        if not dependents:
            # Terminal dynamic phase
            builder.add_edge(exit_id, END)
        elif len(dependents) == 1:
            builder.add_edge(exit_id, dependents[0])
        else:
            # Multiple dependents from exit node
            for dep_id in dependents:
                builder.add_edge(exit_id, dep_id)

        # Wire the conditional edge from parent -> Send fan-out
        router, no_items_target = _make_dynamic_router(phase, config)
        route_map: dict[str, str] = {"retry": phase.id, "fail": END}

        if no_items_target:
            route_map["no_items"] = no_items_target
        elif dependents:
            route_map["no_items"] = dependents[0]
        else:
            route_map["no_items"] = END

        builder.add_conditional_edges(phase.id, router, route_map)

    # --- Wire conditional edges for non-dynamic gated phases ---
    for phase in config.phases:
        if phase.id not in gated_phase_ids or phase.id in dynamic_phase_ids:
            continue

        max_retries = phase.effective_max_retries(config.settings)

        dependents = [
            p.id for p in config.phases if phase.id in p.depends_on
        ]

        if phase.id in terminal_ids:
            builder.add_conditional_edges(
                phase.id,
                _make_gate_router(phase, max_retries),
                {"pass": END, "retry": phase.id, "fail": END},
            )
        elif len(dependents) == 1:
            builder.add_conditional_edges(
                phase.id,
                _make_gate_router(phase, max_retries),
                {"pass": dependents[0], "retry": phase.id, "fail": END},
            )
        else:
            passthrough_id = f"_after_{phase.id}"

            async def passthrough(state: WorkflowState) -> dict[str, Any]:
                return {}

            passthrough.__name__ = f"passthrough_{phase.id}"
            builder.add_node(passthrough_id, passthrough)

            builder.add_conditional_edges(
                phase.id,
                _make_gate_router(phase, max_retries),
                {"pass": passthrough_id, "retry": phase.id, "fail": END},
            )
            for dep_id in dependents:
                builder.add_edge(passthrough_id, dep_id)

    # Terminal non-gated, non-dynamic phases get edge to END
    for phase in config.phases:
        if (
            phase.id in terminal_ids
            and phase.id not in gated_phase_ids
            and phase.id not in dynamic_phase_ids
        ):
            builder.add_edge(phase.id, END)

    return builder.compile()
