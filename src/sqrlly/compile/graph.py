"""Top-level graph builder: YAML config → compiled LangGraph StateGraph."""

from __future__ import annotations

import json
import logging
from graphlib import CycleError, TopologicalSorter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send

from sqrlly.compile._manifest import _read_manifest, find_terminal_nodes
from sqrlly.compile.dynamic import _make_final_fan_out_node, _make_fan_out_node
from sqrlly.compile.nodes import (
    _make_combined_eval_decide_node,
    _make_evaluation_node,
    _make_execution_node,
)
from sqrlly.compile.route import build_route_namespace, evaluate_case
from sqrlly.compile.subgraph import node_subgraph_path
from sqrlly.runtime.gates import build_eval_preamble
from sqrlly.runtime.result import ManifestError, RouteError
from sqrlly.runtime.state import WorkflowState
from sqrlly.schema.models import Graph, Node, Settings

if TYPE_CHECKING:
    from sqrlly.runtime.result import NodeExecutor


def _resolve_goto(target: str | list[str]) -> str | list[str]:
    """Normalize ``__end__`` → ``END`` for str or list-valued goto.

    Supports list-valued goto (Stage 5c) for static fan-out via
    `Command(goto=[...])` — LangGraph 1.x dispatches each target as
    its own concurrent edge in the next super-step.
    """
    if isinstance(target, list):
        return [END if t == "__end__" else t for t in target]
    return END if target == "__end__" else target


# Route detection helpers — local since route semantics are
# compile-time-only. Subgraph detection uses
# `compile/subgraph.py::node_subgraph_path` directly.

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

    async def node_fn(state: WorkflowState) -> Command:
        from sqrlly.compile.route import build_safe_funcs
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
                raise RouteError(
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
    """Raise ``ValueError`` if ``depends_on`` edges form a cycle.

    Delegates to ``graphlib.TopologicalSorter`` — its ``prepare()``
    raises ``CycleError`` (carrying the offending cycle) on a cyclic
    graph. Edges to undeclared node ids are dropped; ``_validate_
    depends_on`` already rejects those at schema time.
    """
    node_ids = {n.id for n in config.nodes}
    adj = {
        n.id: {dep for dep in n.depends_on if dep in node_ids}
        for n in config.nodes
    }
    try:
        TopologicalSorter(adj).prepare()
    except CycleError as e:
        cycle = " → ".join(e.args[1])
        raise ValueError(f"Circular dependency detected: {cycle}") from e


def _wire_evaluation_pair(
    builder: StateGraph,
    node: Node,
    exec_id: str,
    pass_targets: list[str],
    config: Graph,
    *,
    effective_settings: Settings | None = None,
) -> None:
    """Wire a gated execution node's evaluation + decision pair.

    Plain edges only — ``exec → _eval_<id> → _decide_<id>``. The
    Decision node returns ``Command(update=..., goto=...)`` instead
    of a downstream conditional-edge router reading written state.
    Replaces the pre-Stage-5d ``add_conditional_edges`` + ``_make_
    evaluation_router`` pattern; topology is equivalent at the goto
    level (retry → exec_id, fail → END, pass → pass_targets).
    """
    from sqrlly.compile.nodes import _make_decision_node

    eval_id = f"_eval_{exec_id}"
    decide_id = f"_decide_{exec_id}"

    builder.add_edge(exec_id, eval_id)
    builder.add_node(
        decide_id,
        _make_decision_node(
            node, config,
            exec_id=exec_id,
            pass_targets=pass_targets,
            effective_settings=effective_settings,
        ),
    )
    builder.add_edge(eval_id, decide_id)


def _make_dynamic_router(node: Node, no_items_targets: list[str]):
    """Conditional-edge router from the dynamic source.

    Returns *concrete* target node-ids (or a list of them) rather than
    abstract keys: ``END`` on fail, the parent id on retry,
    ``no_items_targets`` when the manifest is empty, and the per-item
    ``Send`` array otherwise. Returning the dependent list directly is
    what lets the empty-manifest case fan to *every* dependent — a
    ``route_map`` value could only ever be a single node.

    Reads ``state.failed_nodes`` / ``state.completed_nodes`` (written
    by the combined eval+decide factory upstream — see
    ``_make_combined_eval_decide_node``). Dynamic gated parents
    intentionally use the pre-Stage-5d combined eval body so this
    router can decide off pre-written state without an intervening
    Decision node fragmenting the manifest-dispatch path.
    """
    template_node_id = f"_sub_{node.id}"

    def router(state: WorkflowState):
        if node.id in state.get("failed_nodes", set()):
            return END
        if node.evaluation and node.id not in state.get("completed_nodes", set()):
            return node.id

        items = _read_manifest(state, node)
        if not items:
            logging.getLogger(__name__).warning(
                "fan-out %r: manifest resolved to zero items — routing to "
                "no-items target(s) %s instead of fanning out.",
                node.id, no_items_targets,
            )
            return no_items_targets

        # Fail loud on colliding child ids before dispatch. Each branch's
        # child id is `<parent>::<item_id>` (matching dynamic._make_fan_out_node).
        # Two items mapping to the same id (literal duplicate, or ≥2 id-less
        # dict items collapsing onto `::unknown`) would silently merge into one
        # branch — historically a multi-minute timeout, not an error.
        seen: set[str] = set()
        duplicates: set[str] = set()
        for item in items:
            child_id = f"{node.id}::{item.get('id', 'unknown')}"
            if child_id in seen:
                duplicates.add(child_id)
            seen.add(child_id)
        if duplicates:
            raise ManifestError(
                f"fan-out {node.id!r}: manifest produces duplicate child id(s) "
                f"{sorted(duplicates)!r} — each item needs a unique 'id' "
                f"(id-less items collapse onto '{node.id}::unknown')."
            )

        return [
            Send(template_node_id, {**state, "_fan_out_item": item})
            for item in items
        ]

    return router


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
    `_depth+1` to enforce MAX_SUBGRAPH_DEPTH and propagate
    the base directory so nested config: paths resolve correctly.
    """
    # Deferred import to break the compile/subgraph ↔ compile/graph
    # circularity (subgraph imports build_workflow_graph via compile_fn).
    from sqrlly.compile.subgraph import (
        MAX_SUBGRAPH_DEPTH,
        SubgraphDepthError,
        detect_config_cycle,
        load_graph,
        make_subgraph_node,
    )

    settings = effective_settings or config.settings

    _detect_cycles(config)

    if _depth > MAX_SUBGRAPH_DEPTH:
        raise SubgraphDepthError(
            f"Subgraph nesting exceeded MAX_SUBGRAPH_DEPTH={MAX_SUBGRAPH_DEPTH}"
        )

    base_dir = Path(_base_dir) if _base_dir is not None else Path(".")

    builder = StateGraph(WorkflowState)
    terminal_ids = set(find_terminal_nodes(config))
    node_map = {p.id: p for p in config.nodes}

    # Reverse dependency map: dependents[x] = ids declaring `x` in their
    # depends_on, in declaration order. Built once instead of an O(N)
    # rescan of config.nodes per gated/dynamic node below.
    dependents: dict[str, list[str]] = {n.id: [] for n in config.nodes}
    for p in config.nodes:
        for dep in p.depends_on:
            if dep in dependents:
                dependents[dep].append(p.id)

    gated_node_ids: set[str] = set()
    dynamic_fan_out_ids: set[str] = set()
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
        if node.fan_out is not None:
            dynamic_fan_out_ids.add(node.id)
        if node_subgraph_path(node) is not None:
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
    # _route_sender / _route_eval_preamble state updates.
    for node_id in synthetic_route_ids:
        node = node_map[node_id]
        builder.add_node(f"_route_{node_id}", _make_inline_route_node(node))

    # Evaluation nodes for every gated node. Top-level non-dynamic
    # gated nodes use the Stage-5d eval/decision split (eval writes
    # the record only; a separate _decide_<id> node returns Command
    # for routing). Dynamic gated parents use the pre-Stage-5d
    # combined factory (eval node writes record + outcome state) so
    # the dynamic_router conditional edge downstream can read the
    # outcome from state.
    for node in config.nodes:
        if node.evaluation:
            factory = (
                _make_combined_eval_decide_node
                if node.id in dynamic_fan_out_ids
                else _make_evaluation_node
            )
            builder.add_node(
                f"_eval_{node.id}",
                factory(node, config, executor, effective_settings=settings),
            )

    # Dynamic node child template + final nodes.
    final_node_ids: dict[tuple[str, str], str] = {}
    gated_final_ids: set[str] = set()
    # Lookup the per-id Node object used for eval/decision construction.
    # Populated for gated finals (synthetic Node mirrors the FanOutFinalNode
    # config); top-level gated nodes look up directly via node_map.
    final_synthetic_nodes: dict[str, Node] = {}
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
                    id=fid, name=final_node.name,
                    evaluation=final_node.evaluation,
                )
                final_synthetic_nodes[fid] = synthetic
                builder.add_node(
                    f"_eval_{fid}",
                    _make_evaluation_node(
                        synthetic, config, executor,
                        effective_settings=settings,
                    ),
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
            _wire_evaluation_pair(
                builder, node, node.id, [f"_route_{node.id}"], config,
                effective_settings=settings,
            )
        else:
            deps_of = dependents[node.id]
            _wire_evaluation_pair(
                builder, node, node.id, deps_of or [END], config,
                effective_settings=settings,
            )

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
                    _wire_evaluation_pair(
                        builder, final_synthetic_nodes[cur], cur, [nxt],
                        config, effective_settings=settings,
                    )
                else:
                    builder.add_edge(cur, nxt)

        # Branch exit → parent's dependents.
        deps_of = dependents[node.id]
        last_final = (
            final_node_ids[(node.id, dsc.final_nodes[-1].id)]
            if dsc.final_nodes else None
        )
        if last_final and last_final in gated_final_ids:
            _wire_evaluation_pair(
                builder, final_synthetic_nodes[last_final], last_final,
                deps_of or [END], config, effective_settings=settings,
            )
        else:
            for tgt in (deps_of or [END]):
                builder.add_edge(exit_node[node.id], tgt)

        # Fan-out router at the dynamic source. An empty manifest
        # ("no_items") routes to the first final node when the fan-out
        # declares final_nodes, otherwise to *every* dependent.
        if dsc.final_nodes:
            no_items_targets = [f"_final_{node.id}_{dsc.final_nodes[0].id}"]
        else:
            no_items_targets = deps_of or [END]
        router = _make_dynamic_router(node, no_items_targets)
        path_map = list(dict.fromkeys([END, node.id, *no_items_targets]))
        builder.add_conditional_edges(dynamic_source, router, path_map)

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
