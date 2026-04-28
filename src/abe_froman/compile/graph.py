"""Top-level graph builder: YAML config → compiled LangGraph StateGraph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from abe_froman.compile.dynamic import _make_final_fan_out_node, _make_fan_out_node
from abe_froman.compile.nodes import _make_evaluation_node, _make_execution_node
from abe_froman.runtime.state import WorkflowState
from abe_froman.schema.models import Node, Graph

if TYPE_CHECKING:
    from abe_froman.runtime.result import PhaseExecutor


def _find_terminal_phases(config: Graph) -> set[str]:
    depended_on: set[str] = set()
    for node in config.nodes:
        depended_on.update(node.depends_on)
    return {p.id for p in config.nodes if p.id not in depended_on}


def _detect_cycles(config: Graph) -> None:
    adj: dict[str, list[str]] = {p.id: list(p.depends_on) for p in config.nodes}
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

    ``node_id_resolver`` lets child routers derive the per-branch id
    from ``state._fan_out_item`` — the child node evaluates inline
    and loops back via a conditional edge, preserving per-branch state.
    """
    resolve = node_id_resolver or (lambda _s: execution_node_id)

    def router(state: WorkflowState) -> str | list[str]:
        node_id = resolve(state)
        if node_id in state.get("failed_nodes", []):
            return END
        if node_id in state.get("completed_nodes", []):
            return pass_targets[0] if len(pass_targets) == 1 else pass_targets
        return execution_node_id

    return router


def _subphase_id_resolver(parent_id: str) -> Callable[[WorkflowState], str]:
    """Resolve the per-branch child node_id from `_fan_out_item`."""
    def resolve(state: WorkflowState) -> str:
        item = state.get("_fan_out_item", {}) or {}
        return f"{parent_id}::{item.get('id', 'unknown')}"
    return resolve


def _register_evaluation_node(
    builder: StateGraph,
    node: Node,
    config: Graph,
    executor: PhaseExecutor | None,
    exec_node_id: str | None = None,
) -> str:
    """Register `_eval_{exec_node_id}` and return its id."""
    eval_id = f"_eval_{exec_node_id or node.id}"
    builder.add_node(eval_id, _make_evaluation_node(node, config, executor))
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


def _read_manifest(state: WorkflowState, node: Node) -> list[dict]:
    output = state.get("node_outputs", {}).get(node.id, "")
    try:
        data = json.loads(output)
        if isinstance(data, dict) and "items" in data:
            return data["items"]
        if isinstance(data, list):
            return data
    except (json.JSONDecodeError, TypeError):
        pass

    if node.fan_out and node.fan_out.manifest_path:
        manifest_file = (
            Path(state.get("workdir", ".")) / node.fan_out.manifest_path
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


def _make_dynamic_router(node: Node, config: Graph):
    template_node_id = f"_sub_{node.id}"

    dsc = node.fan_out
    if dsc.final_nodes:
        no_items_target = f"_final_{node.id}_{dsc.final_nodes[0].id}"
    else:
        no_items_target = None

    def router(state: WorkflowState):
        if node.id in state.get("failed_nodes", []):
            return "fail"
        if node.evaluation and node.id not in state.get("completed_nodes", []):
            return "retry"

        items = _read_manifest(state, node)
        if not items:
            return "no_items"

        return [
            Send(template_node_id, {**state, "_fan_out_item": item})
            for item in items
        ]

    return router, no_items_target


def build_workflow_graph(
    config: Graph,
    executor: PhaseExecutor | None = None,
    checkpointer: Any = None,
    *,
    _depth: int = 0,
    _base_dir: Any = None,
) -> Any:
    """Build a compiled LangGraph StateGraph from workflow config.

    If `checkpointer` is provided, the compiled graph will persist state
    after each node via LangGraph's checkpointer protocol.

    `_depth` and `_base_dir` are internal: subgraph wrappers pass
    `_depth+1` to enforce `settings.max_subgraph_depth` and propagate
    the base directory so nested config: paths resolve correctly.
    """
    from pathlib import Path
    from abe_froman.compile.subgraph import (
        SubgraphDepthError,
        detect_config_cycle,
        load_graph,
        make_subgraph_node,
    )

    _detect_cycles(config)

    if _depth > config.settings.max_subgraph_depth:
        raise SubgraphDepthError(
            f"Subgraph nesting exceeded max_subgraph_depth="
            f"{config.settings.max_subgraph_depth}"
        )

    base_dir = Path(_base_dir) if _base_dir is not None else Path(".")

    builder = StateGraph(WorkflowState)
    terminal_ids = _find_terminal_phases(config)
    phase_map = {p.id: p for p in config.nodes}

    gated_phase_ids: set[str] = set()
    dynamic_phase_ids: set[str] = set()
    gated_dynamic_template_ids: set[str] = set()
    subgraph_node_ids: set[str] = set()

    for node in config.nodes:
        if node.evaluation:
            gated_phase_ids.add(node.id)
        if node.fan_out and node.fan_out.enabled:
            dynamic_phase_ids.add(node.id)
            if node.fan_out.template.evaluation:
                gated_dynamic_template_ids.add(node.id)
        if node.config:
            subgraph_node_ids.add(node.id)
            # Cycle detection happens once at top-level — nested calls
            # see _depth>0 so they skip this and rely on the depth cap.
            if _depth == 0:
                detect_config_cycle(node.config, base_dir=base_dir)

    # ----- Node registration -----

    # Execution nodes for every configured node.
    for node in config.nodes:
        if node.id in subgraph_node_ids:
            sub_config = load_graph(node.config, base_dir=base_dir)
            wrapper = make_subgraph_node(
                node, sub_config,
                compile_fn=lambda c, executor=None, _depth=0: build_workflow_graph(
                    c, executor=executor, _depth=_depth, _base_dir=base_dir,
                ),
                executor=executor,
                depth=_depth,
            )
            builder.add_node(node.id, wrapper)
        else:
            builder.add_node(node.id, _make_execution_node(node, config, executor))

    # Evaluation nodes for every gated node (top-level, dynamic parents,
    # and gated final nodes). Dynamic parents' eval runs before the fan-
    # out router so it sees completed/retries/failed state.
    for node in config.nodes:
        if node.evaluation:
            _register_evaluation_node(builder, node, config, executor)

    # Dynamic node child template + final nodes.
    final_node_ids: dict[tuple[str, str], str] = {}
    gated_final_ids: set[str] = set()
    for node_id in dynamic_phase_ids:
        node = phase_map[node_id]
        builder.add_node(f"_sub_{node.id}", _make_fan_out_node(node, config, executor))

        for final_phase in node.fan_out.final_nodes:
            fid = f"_final_{node.id}_{final_phase.id}"
            final_node_ids[(node.id, final_phase.id)] = fid
            builder.add_node(
                fid, _make_final_fan_out_node(node, final_phase, config, executor),
            )
            if final_phase.evaluation:
                gated_final_ids.add(fid)
                synthetic = Node(
                    id=fid, name=final_phase.name, evaluation=final_phase.evaluation,
                    model=node.model,
                )
                _register_evaluation_node(builder, synthetic, config, executor, exec_node_id=fid)

    # ----- exit_node: what downstream deps plain-edge from -----
    # For gated nodes, downstream waits on the eval node, not execution.

    exit_node: dict[str, str] = {}
    needs_conditional: set[str] = set()

    for node in config.nodes:
        if node.id in dynamic_phase_ids:
            dsc = node.fan_out
            if dsc.final_nodes:
                last_final_id = final_node_ids[(node.id, dsc.final_nodes[-1].id)]
                exit_node[node.id] = (
                    f"_eval_{last_final_id}"
                    if last_final_id in gated_final_ids
                    else last_final_id
                )
            else:
                exit_node[node.id] = f"_sub_{node.id}"
            needs_conditional.add(node.id)
        elif node.id in gated_phase_ids:
            exit_node[node.id] = f"_eval_{node.id}"
            needs_conditional.add(node.id)
        else:
            exit_node[node.id] = node.id

    # ----- Plain edges: start + dep-to-node -----

    has_incoming: set[str] = set()

    for node in config.nodes:
        if not node.depends_on:
            continue

        for dep in node.depends_on:
            if dep in needs_conditional:
                # Conditional edge from the dep (or its eval) handles routing.
                pass
            else:
                builder.add_edge(exit_node[dep], node.id)
            has_incoming.add(node.id)

    for node in config.nodes:
        if node.id not in has_incoming:
            builder.add_edge(START, node.id)

    # ----- Top-level gated node wiring (non-dynamic) -----

    for node in config.nodes:
        if node.id not in gated_phase_ids or node.id in dynamic_phase_ids:
            continue
        deps_of = [p.id for p in config.nodes if node.id in p.depends_on]
        _wire_evaluation_pair(builder, node.id, deps_of or [END])

    # ----- Dynamic node wiring -----

    for node_id in dynamic_phase_ids:
        node = phase_map[node_id]
        dsc = node.fan_out
        template_id = f"_sub_{node.id}"

        # Gated parent: plain edge node → _eval_phase so the dynamic router
        # attaches to the eval node (and sees completed/retries/failed).
        if node.id in gated_phase_ids:
            builder.add_edge(node.id, f"_eval_{node.id}")
            dynamic_source = f"_eval_{node.id}"
        else:
            dynamic_source = node.id

        # _sub_ → first_final (child evaluates inline per branch).
        # Final chain: each gated final is followed by its eval with retry
        # self-loop; ungated finals get a plain edge to the next in line.
        if dsc.final_nodes:
            builder.add_edge(template_id, final_node_ids[(node.id, dsc.final_nodes[0].id)])
            for i in range(len(dsc.final_nodes) - 1):
                cur = final_node_ids[(node.id, dsc.final_nodes[i].id)]
                nxt = final_node_ids[(node.id, dsc.final_nodes[i + 1].id)]
                if cur in gated_final_ids:
                    _wire_evaluation_pair(builder, cur, [nxt])
                else:
                    builder.add_edge(cur, nxt)

        # Branch exit → parent's dependents.
        deps_of = [p.id for p in config.nodes if node.id in p.depends_on]
        last_final = (
            final_node_ids[(node.id, dsc.final_nodes[-1].id)]
            if dsc.final_nodes else None
        )
        if last_final and last_final in gated_final_ids:
            _wire_evaluation_pair(builder, last_final, deps_of or [END])
        else:
            for tgt in (deps_of or [END]):
                builder.add_edge(exit_node[node.id], tgt)

        # Fan-out router at the dynamic source.
        router, no_items_target = _make_dynamic_router(node, config)
        route_map = {
            "retry": node.id, "fail": END,
            "no_items": no_items_target or (deps_of[0] if deps_of else END),
        }
        builder.add_conditional_edges(dynamic_source, router, route_map)

    # ----- Terminal plain-end edges for ungated, non-dynamic nodes -----

    for node in config.nodes:
        if (
            node.id in terminal_ids
            and node.id not in gated_phase_ids
            and node.id not in dynamic_phase_ids
        ):
            builder.add_edge(node.id, END)

    return builder.compile(checkpointer=checkpointer)
