"""Top-level graph builder: YAML config → compiled LangGraph StateGraph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from abe_froman.compile.dynamic import _make_final_phase_node, _make_subphase_node
from abe_froman.compile.nodes import _make_evaluation_node, _make_phase_node
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


def _make_evaluation_router(
    execution_node_id: str,
    pass_targets: list[str],
    node_id_resolver: Callable[[WorkflowState], str] | None = None,
):
    """Case-statement-style router at an Evaluation node (or inline-gated
    Execution node with a self-loop).

    Reads the state transitions written by the Evaluation logic and picks
    a destination: failed → END, completed → pass targets, else (retry)
    → the upstream execution node for another attempt.

    ``node_id_resolver`` lets subphase routers derive the per-branch id
    from ``state._subphase_item`` — the subphase node evaluates inline
    and loops back via a conditional edge, preserving per-branch state.
    """
    resolve = node_id_resolver or (lambda _s: execution_node_id)

    def router(state: WorkflowState) -> str | list[str]:
        node_id = resolve(state)
        if node_id in state.get("failed_phases", []):
            return END
        if node_id in state.get("completed_phases", []):
            return pass_targets[0] if len(pass_targets) == 1 else pass_targets
        return execution_node_id

    return router


def _subphase_id_resolver(parent_id: str) -> Callable[[WorkflowState], str]:
    """Resolve the per-branch subphase node_id from `_subphase_item`."""
    def resolve(state: WorkflowState) -> str:
        item = state.get("_subphase_item", {}) or {}
        return f"{parent_id}::{item.get('id', 'unknown')}"
    return resolve


def _register_evaluation_node(
    builder: StateGraph,
    phase: Phase,
    config: WorkflowConfig,
    executor: PhaseExecutor | None,
    exec_node_id: str | None = None,
) -> str:
    """Register `_eval_{exec_node_id}` and return its id."""
    eval_id = f"_eval_{exec_node_id or phase.id}"
    builder.add_node(eval_id, _make_evaluation_node(phase, config, executor))
    return eval_id


def _wire_evaluation_pair(
    builder: StateGraph, exec_id: str, pass_targets: list[str],
) -> None:
    """Plain edge exec → _eval_exec, conditional edge from eval routing
    retry → exec, pass → targets, fail → END.
    """
    eval_id = f"_eval_{exec_id}"
    builder.add_edge(exec_id, eval_id)
    router = _make_evaluation_router(exec_id, pass_targets)
    builder.add_conditional_edges(eval_id, router, [exec_id, END, *pass_targets])


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
        if phase.evaluation and phase.id not in state.get("completed_phases", []):
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
    gated_dynamic_template_ids: set[str] = set()

    for phase in config.phases:
        if phase.evaluation:
            gated_phase_ids.add(phase.id)
        if phase.dynamic_subphases and phase.dynamic_subphases.enabled:
            dynamic_phase_ids.add(phase.id)
            if phase.dynamic_subphases.template.evaluation:
                gated_dynamic_template_ids.add(phase.id)

    # ----- Node registration -----

    # Execution nodes for every configured phase.
    for phase in config.phases:
        builder.add_node(phase.id, _make_phase_node(phase, config, executor))

    # Evaluation nodes for every gated phase (top-level, dynamic parents,
    # and gated final phases). Dynamic parents' eval runs before the fan-
    # out router so it sees completed/retries/failed state.
    for phase in config.phases:
        if phase.evaluation:
            _register_evaluation_node(builder, phase, config, executor)

    # Dynamic phase subphase template + final nodes.
    final_node_ids: dict[tuple[str, str], str] = {}
    gated_final_ids: set[str] = set()
    for phase_id in dynamic_phase_ids:
        phase = phase_map[phase_id]
        builder.add_node(f"_sub_{phase.id}", _make_subphase_node(phase, config, executor))

        for final_phase in phase.dynamic_subphases.final_phases:
            fid = f"_final_{phase.id}_{final_phase.id}"
            final_node_ids[(phase.id, final_phase.id)] = fid
            builder.add_node(
                fid, _make_final_phase_node(phase, final_phase, config, executor),
            )
            if final_phase.evaluation:
                gated_final_ids.add(fid)
                synthetic = Phase(
                    id=fid, name=final_phase.name, evaluation=final_phase.evaluation,
                    model=phase.model,
                )
                _register_evaluation_node(builder, synthetic, config, executor, exec_node_id=fid)

    # ----- exit_node: what downstream deps plain-edge from -----
    # For gated phases, downstream waits on the eval node, not execution.

    exit_node: dict[str, str] = {}
    needs_conditional: set[str] = set()

    for phase in config.phases:
        if phase.id in dynamic_phase_ids:
            dsc = phase.dynamic_subphases
            if dsc.final_phases:
                last_final_id = final_node_ids[(phase.id, dsc.final_phases[-1].id)]
                exit_node[phase.id] = (
                    f"_eval_{last_final_id}"
                    if last_final_id in gated_final_ids
                    else last_final_id
                )
            else:
                exit_node[phase.id] = f"_sub_{phase.id}"
            needs_conditional.add(phase.id)
        elif phase.id in gated_phase_ids:
            exit_node[phase.id] = f"_eval_{phase.id}"
            needs_conditional.add(phase.id)
        else:
            exit_node[phase.id] = phase.id

    # ----- Plain edges: start + dep-to-phase -----

    has_incoming: set[str] = set()

    for phase in config.phases:
        if not phase.depends_on:
            continue

        for dep in phase.depends_on:
            if dep in needs_conditional:
                # Conditional edge from the dep (or its eval) handles routing.
                pass
            else:
                builder.add_edge(exit_node[dep], phase.id)
            has_incoming.add(phase.id)

    for phase in config.phases:
        if phase.id not in has_incoming:
            builder.add_edge(START, phase.id)

    # ----- Top-level gated phase wiring (non-dynamic) -----

    for phase in config.phases:
        if phase.id not in gated_phase_ids or phase.id in dynamic_phase_ids:
            continue
        deps_of = [p.id for p in config.phases if phase.id in p.depends_on]
        _wire_evaluation_pair(builder, phase.id, deps_of or [END])

    # ----- Dynamic phase wiring -----

    for phase_id in dynamic_phase_ids:
        phase = phase_map[phase_id]
        dsc = phase.dynamic_subphases
        template_id = f"_sub_{phase.id}"

        # Gated parent: plain edge phase → _eval_phase so the dynamic router
        # attaches to the eval node (and sees completed/retries/failed).
        if phase.id in gated_phase_ids:
            builder.add_edge(phase.id, f"_eval_{phase.id}")
            dynamic_source = f"_eval_{phase.id}"
        else:
            dynamic_source = phase.id

        # _sub_ → first_final (subphase evaluates inline per branch).
        # Final chain: each gated final is followed by its eval with retry
        # self-loop; ungated finals get a plain edge to the next in line.
        if dsc.final_phases:
            builder.add_edge(template_id, final_node_ids[(phase.id, dsc.final_phases[0].id)])
            for i in range(len(dsc.final_phases) - 1):
                cur = final_node_ids[(phase.id, dsc.final_phases[i].id)]
                nxt = final_node_ids[(phase.id, dsc.final_phases[i + 1].id)]
                if cur in gated_final_ids:
                    _wire_evaluation_pair(builder, cur, [nxt])
                else:
                    builder.add_edge(cur, nxt)

        # Branch exit → parent's dependents.
        deps_of = [p.id for p in config.phases if phase.id in p.depends_on]
        last_final = (
            final_node_ids[(phase.id, dsc.final_phases[-1].id)]
            if dsc.final_phases else None
        )
        if last_final and last_final in gated_final_ids:
            _wire_evaluation_pair(builder, last_final, deps_of or [END])
        else:
            for tgt in (deps_of or [END]):
                builder.add_edge(exit_node[phase.id], tgt)

        # Fan-out router at the dynamic source.
        router, no_items_target = _make_dynamic_router(phase, config)
        route_map = {
            "retry": phase.id, "fail": END,
            "no_items": no_items_target or (deps_of[0] if deps_of else END),
        }
        builder.add_conditional_edges(dynamic_source, router, route_map)

    # ----- Terminal plain-end edges for ungated, non-dynamic phases -----

    for phase in config.phases:
        if (
            phase.id in terminal_ids
            and phase.id not in gated_phase_ids
            and phase.id not in dynamic_phase_ids
        ):
            builder.add_edge(phase.id, END)

    return builder.compile(checkpointer=checkpointer)
