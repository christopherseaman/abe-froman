"""Top-level graph builder: YAML config → compiled LangGraph StateGraph."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send

from abe_froman.compile.dynamic import _make_final_fan_out_node, _make_fan_out_node
from abe_froman.compile.nodes import _make_evaluation_node, _make_execution_node
from abe_froman.compile.route import build_route_namespace, evaluate_case
from abe_froman.compile.subgraph import node_subgraph_path
from abe_froman.runtime.gates import build_eval_preamble
from abe_froman.runtime.state import WorkflowState
from abe_froman.schema.models import Graph, Node, Settings

if TYPE_CHECKING:
    from abe_froman.runtime.result import NodeExecutor


def _find_terminal_nodes(config: Graph) -> set[str]:
    depended_on: set[str] = set()
    for node in config.nodes:
        depended_on.update(node.depends_on)
    return {p.id for p in config.nodes if p.id not in depended_on}


def _resolve_goto(target: str | list[str]) -> str | list[str]:
    """Normalize ``__end__`` → ``END`` for str or list-valued goto.

    Supports list-valued goto (Stage 5c) for static fan-out via
    `Command(goto=[...])` — LangGraph 1.x dispatches each target as
    its own concurrent edge in the next super-step.
    """
    if isinstance(target, list):
        return [END if t == "__end__" else t for t in target]
    return END if target == "__end__" else target


# Subgraph-ref + route detection helpers. `node_subgraph_path` lives
# in compile/subgraph.py (single source of truth); _is_route is local
# since route semantics are compile-time-only.

def _is_subgraph_ref(node: Node) -> bool:
    return node_subgraph_path(node) is not None


def _has_inline_route(node: Node) -> bool:
    """Stage 5c inline route: `Node.route` block. Coexists with execute,
    or stands alone."""
    return node.route is not None


def _is_route(node: Node) -> bool:
    """Standalone route — node whose only forward dispatch is a route
    block, no execute body."""
    return _has_inline_route(node) and node.execute is None


def _has_synthetic_route(node: Node) -> bool:
    """Node that pairs `execute:` with inline `route:` — needs a
    synthetic `_route_<id>` dispatcher node post-execute (and post-
    eval if present)."""
    return _has_inline_route(node) and node.execute is not None


def _collect_route_targets(node: Node) -> set[str]:
    """Every node id this node's inline route block can dispatch to.
    Used to mark goto-targets so they don't get a stray START →
    fallback edge.
    """
    targets: set[str] = set()

    def _add(tgt: Any) -> None:
        ids = tgt if isinstance(tgt, list) else [tgt]
        for t in ids:
            if t != "__end__" and t is not None:
                targets.add(t)

    if _has_inline_route(node):
        r = node.route
        if r.goto is not None:
            _add(r.goto)
        for case in r.cases:
            _add(case.goto)
        if r.else_ is not None:
            _add(r.else_.goto)
    return targets


def _make_inline_route_node(node: Node):
    """Build the async fn for an inline-route dispatcher (Node.route).

    Used in two configurations:
    1. **Standalone** — node has `route:` and no `execute:`. Registered
       under ``node.id``; runs as the node itself.
    2. **Synthetic post-execute** — node has both `execute:` and
       `route:`. The synthetic `_route_<id>` runs after the execute
       node (and after eval, if present) and resolves the route block.

    Returns Command(update={...}, goto=resolved). Update includes:

    - ``_route_sender``: source node id (always).
    - ``_route_include_eval``: bool flag from the matched case.
    - ``_route_eval_preamble``: pre-built preamble string. Populated
      only when ``include_eval=True`` on the matched case AND the
      source node has ``evaluation:`` AND a result is recorded. Empty
      string otherwise (overwrites any stale value from a prior
      Command emission). ``_dispatch_prompt`` auto-prepends a
      non-empty preamble before the rendered prompt body.
    """
    assert _has_inline_route(node)
    route = node.route
    sender_id = node.id

    def _emit(target: Any, include_eval: bool) -> Command:
        preamble = ""
        if include_eval and node.evaluation is not None:
            # Built lazily here (not at compile) because the eval
            # result is in state, set by `_eval_<id>` before this
            # dispatcher fires. Fan-out children would be in
            # `state.evaluations[child_id]` — but inline route on
            # fan-out children is forbidden by the dynamic.py
            # composition rules, so this only ever sees top-level
            # evals.
            pass  # filled below — _emit is closed over state in node_fn

        return Command(
            update={
                "_route_sender": sender_id,
                "_route_include_eval": include_eval,
                "_route_eval_preamble": preamble,
            },
            goto=_resolve_goto(target),
        )

    async def node_fn(state: WorkflowState) -> Command:
        from abe_froman.compile.route import build_safe_funcs
        ns = build_route_namespace(state, node.depends_on)
        funcs = build_safe_funcs(state)

        def _build_preamble(include_eval: bool) -> str:
            if not include_eval or node.evaluation is None:
                return ""
            records = (state.get("evaluations") or {}).get(sender_id) or []
            if not records:
                return ""
            last_result = records[-1].get("result", {}) or {}
            return build_eval_preamble(last_result, node.evaluation)

        def _command(target: Any, include_eval: bool) -> Command:
            return Command(
                update={
                    "_route_sender": sender_id,
                    "_route_include_eval": include_eval,
                    "_route_eval_preamble": _build_preamble(include_eval),
                },
                goto=_resolve_goto(target),
            )

        if route.goto is not None:
            return _command(route.goto, route.include_eval)

        for case in route.cases:
            try:
                matched = evaluate_case(case.when, ns, functions=funcs)
            except Exception as e:
                raise ValueError(
                    f"Route '{sender_id}' case {case.when!r}: {e}"
                ) from e
            if matched:
                return _command(case.goto, case.include_eval)

        # else (always present when cases is non-empty per validation)
        else_ = route.else_
        return _command(else_.goto, else_.include_eval)

    node_fn.__name__ = f"route_{sender_id}"
    return node_fn


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
    executor: NodeExecutor | None,
    exec_node_id: str | None = None,
    *,
    effective_settings: Settings | None = None,
) -> str:
    """Register `_eval_{exec_node_id}` and return its id."""
    eval_id = f"_eval_{exec_node_id or node.id}"
    builder.add_node(
        eval_id,
        _make_evaluation_node(
            node, config, executor, effective_settings=effective_settings,
        ),
    )
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
    executor: NodeExecutor | None = None,
    checkpointer: Any = None,
    *,
    logger: Any | None = None,
    effective_settings: Settings | None = None,
    _depth: int = 0,
    _base_dir: Any = None,
) -> Any:
    """Build a compiled LangGraph StateGraph from workflow config.

    If `checkpointer` is provided, the compiled graph will persist state
    after each node via LangGraph's checkpointer protocol.

    If `logger` (a JsonlLogger) is provided, subgraph wrappers will
    stream their internal `astream(stream_mode="values")` output through
    a SubgraphLogger that prefixes node ids with the parent node's id,
    so subgraph-internal completions surface in the parent JSONL keyed
    as `parent_id::child_id` (and nested as `parent::child::grandchild`).

    ``effective_settings`` (Phase 3 / scope-aware): pre-merged settings
    for this scope. ``None`` at top-level (use ``config.settings``);
    subgraph wrappers compute the merge and pass it in. Used for
    ``effective_timeout``, ``effective_max_retries``, ``retry_backoff``,
    LLM-gate ``default_model``, and the executor's ``settings_override``.

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

    settings = effective_settings or config.settings

    _detect_cycles(config)

    if _depth > settings.max_subgraph_depth:
        raise SubgraphDepthError(
            f"Subgraph nesting exceeded max_subgraph_depth="
            f"{settings.max_subgraph_depth}"
        )

    base_dir = Path(_base_dir) if _base_dir is not None else Path(".")

    builder = StateGraph(WorkflowState)
    terminal_ids = _find_terminal_nodes(config)
    node_map = {p.id: p for p in config.nodes}

    gated_node_ids: set[str] = set()
    dynamic_fan_out_ids: set[str] = set()
    gated_fan_out_template_ids: set[str] = set()
    subgraph_node_ids: set[str] = set()
    route_node_ids: set[str] = set()
    # Stage 5c: nodes carrying `Node.route` block. `synthetic_route_ids`
    # is the subset that ALSO has `execute:` — they get a synthetic
    # `_route_<id>` dispatcher post-execute. `inline_route_ids` includes
    # both standalone and synthetic forms (used to skip terminal-end
    # wiring; their exit is via Command(goto=)).
    inline_route_ids: set[str] = set()
    synthetic_route_ids: set[str] = set()

    for node in config.nodes:
        if node.evaluation:
            gated_node_ids.add(node.id)
        if node.fan_out and node.fan_out.enabled:
            dynamic_fan_out_ids.add(node.id)
            if node.fan_out.template.evaluation:
                gated_fan_out_template_ids.add(node.id)
        if _is_subgraph_ref(node):
            subgraph_node_ids.add(node.id)
            # Cycle detection happens once at top-level — nested calls
            # see _depth>0 so they skip this and rely on the depth cap.
            if _depth == 0:
                detect_config_cycle(node_subgraph_path(node), base_dir=base_dir)
        if _is_route(node):
            route_node_ids.add(node.id)
        if _has_inline_route(node):
            inline_route_ids.add(node.id)
            if _has_synthetic_route(node):
                synthetic_route_ids.add(node.id)

    # ----- Node registration -----

    # compile_fn is shared by recursive subgraph machinery and per-child
    # fan-out subgraphs: both compile a referenced YAML at parent build
    # time and invoke it later. Defined once so call sites don't drift.
    # `logger` propagates so nested subgraphs keep emitting prefixed events.
    # ``effective_settings`` (Phase 3) is the pre-merged scope view from
    # the subgraph wrapper; threaded into the recursive call so subgraph
    # nodes receive their own scope's settings.
    def compile_fn(c, executor=None, _depth=0, effective_settings=None):
        return build_workflow_graph(
            c, executor=executor, _depth=_depth,
            _base_dir=base_dir, logger=logger,
            effective_settings=effective_settings,
        )

    # Execution nodes for every configured node.
    for node in config.nodes:
        if node.id in subgraph_node_ids:
            sub_config = load_graph(node_subgraph_path(node), base_dir=base_dir)
            wrapper = make_subgraph_node(
                node, sub_config,
                compile_fn=compile_fn,
                executor=executor,
                depth=_depth,
                logger=logger,
                parent_settings=settings,  # subgraph wrapper merges with sub's settings
            )
            builder.add_node(node.id, wrapper)
        elif node.id in route_node_ids:
            # Standalone inline `Node.route` with no execute. Dispatched
            # via Command from the node fn itself.
            builder.add_node(node.id, _make_inline_route_node(node))
        else:
            builder.add_node(
                node.id,
                _make_execution_node(
                    node, config, executor,
                    effective_settings=settings,
                ),
            )

    # Synthetic _route_<id> dispatchers for execute+route nodes.
    # They fire after the execute body (and after eval, if present)
    # settles, resolving the route and emitting Command(goto=...) with
    # _route_sender / _route_include_eval state updates.
    for node_id in synthetic_route_ids:
        node = node_map[node_id]
        builder.add_node(f"_route_{node_id}", _make_inline_route_node(node))

    # Evaluation nodes for every gated node (top-level, dynamic parents,
    # and gated final nodes). Dynamic parents' eval runs before the fan-
    # out router so it sees completed/retries/failed state.
    for node in config.nodes:
        if node.evaluation:
            _register_evaluation_node(
                builder, node, config, executor,
                effective_settings=settings,
            )

    # Dynamic node child template + final nodes.
    final_node_ids: dict[tuple[str, str], str] = {}
    gated_final_ids: set[str] = set()
    for node_id in dynamic_fan_out_ids:
        node = node_map[node_id]
        builder.add_node(
            f"_sub_{node.id}",
            _make_fan_out_node(
                node, config, executor,
                compile_fn=compile_fn, base_dir=base_dir, depth=_depth,
                logger=logger,
                effective_settings=settings,
            ),
        )

        for idx, final_node in enumerate(node.fan_out.final_nodes):
            fid = f"_final_{node.id}_{final_node.id}"
            final_node_ids[(node.id, final_node.id)] = fid
            builder.add_node(
                fid, _make_final_fan_out_node(
                    node, final_node, config, executor, is_first=(idx == 0),
                    effective_settings=settings,
                ),
            )
            if final_node.evaluation:
                gated_final_ids.add(fid)
                synthetic = Node(
                    id=fid, name=final_node.name, evaluation=final_node.evaluation,
                    model=node.model,
                )
                _register_evaluation_node(
                    builder, synthetic, config, executor, exec_node_id=fid,
                    effective_settings=settings,
                )

    # ----- exit_node: what downstream deps plain-edge from -----
    # For gated nodes, downstream waits on the eval node, not execution.

    exit_node: dict[str, str] = {}
    needs_conditional: set[str] = set()

    for node in config.nodes:
        if node.id in dynamic_fan_out_ids:
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
        elif node.id in gated_node_ids:
            exit_node[node.id] = f"_eval_{node.id}"
            needs_conditional.add(node.id)
        else:
            exit_node[node.id] = node.id

    # ----- Plain edges: start + dep-to-node -----

    # Goto targets of routes: a node reached only by Command(goto=) must
    # not get a START → node fallback edge (would fire it unconditionally
    # regardless of routing). Covers standalone `Node.route` and
    # `_route_<id>` synthetic dispatchers (their goto targets are also
    # Command-driven).
    route_goto_targets: set[str] = set()
    for node in config.nodes:
        route_goto_targets |= _collect_route_targets(node)

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
        if node.id not in has_incoming and node.id not in route_goto_targets:
            builder.add_edge(START, node.id)

    # ----- Top-level gated node wiring (non-dynamic) -----

    for node in config.nodes:
        if node.id not in gated_node_ids or node.id in dynamic_fan_out_ids:
            continue
        if node.id in synthetic_route_ids:
            # execute + eval + route: pass target is the synthetic
            # `_route_<id>` dispatcher, which then emits Command(goto=)
            # to the actual route case target. Dependents-via-depends_on
            # is forbidden for inline-route nodes (validated in schema),
            # so deps_of is guaranteed empty.
            _wire_evaluation_pair(builder, node.id, [f"_route_{node.id}"])
        else:
            deps_of = [p.id for p in config.nodes if node.id in p.depends_on]
            _wire_evaluation_pair(builder, node.id, deps_of or [END])

    # ----- Inline-route synthetic node wiring (ungated execute+route) -----
    # For nodes with execute + route but NO eval, plain edge from the
    # execute node to the synthetic dispatcher. Gated case is already
    # wired above via the eval pair's pass target.

    for node_id in synthetic_route_ids:
        if node_id in gated_node_ids:
            continue
        builder.add_edge(node_id, f"_route_{node_id}")

    # ----- Dynamic node wiring -----

    for node_id in dynamic_fan_out_ids:
        node = node_map[node_id]
        dsc = node.fan_out
        template_id = f"_sub_{node.id}"

        # Gated parent: plain edge node → _eval_node so the dynamic router
        # attaches to the eval node (and sees completed/retries/failed).
        if node.id in gated_node_ids:
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
    # Inline-route nodes (standalone or execute+route) drive their exit
    # via Command(goto=...), so they must not get a static node→END
    # edge — that would create a parallel path to END alongside the
    # Command-driven path. Excluded by route_node_ids and
    # inline_route_ids.

    for node in config.nodes:
        if (
            node.id in terminal_ids
            and node.id not in gated_node_ids
            and node.id not in dynamic_fan_out_ids
            and node.id not in route_node_ids
            and node.id not in inline_route_ids
        ):
            builder.add_edge(node.id, END)

    return builder.compile(checkpointer=checkpointer)
