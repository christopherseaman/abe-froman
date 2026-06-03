"""Recursive subgraph composition.

A Node with `config: path/to/another.yaml` is a subgraph reference: its
config file is loaded as a `Graph` (identical schema to the top-level
workflow), recursively compiled, and added as a node in the parent
graph. State projection is explicit:

    - `inputs:` maps parent dep outputs / context vars onto the
      subgraph's `node_inputs` channel. Subgraph nodes see these as
      ordinary template variables alongside their own dep outputs.

    - `outputs:` maps subgraph terminal-node outputs back into the
      parent's `node_outputs`. The default (empty `outputs:`) exposes
      the subgraph's last terminal node's output as
      `node_outputs[parent_node.id]`.

Cycle detection is performed at compile time over the config-reference
DAG; depth is capped by `settings.max_subgraph_depth` (default 10).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from sqrlly.compile._manifest import find_terminal_nodes
from sqrlly.compile.nodes import (
    all_deps_completed,
    build_context,
    check_dep_failed,
)
from sqrlly.runtime.executor.prompt import render_template
from sqrlly.runtime.result import ExecutionResult
from sqrlly.runtime.settings_merge import merge_settings
from sqrlly.runtime.state import WorkflowState, make_initial_state
from sqrlly.schema.models import Graph, Node, Settings

if TYPE_CHECKING:
    from sqrlly.runtime.result import NodeExecutor


class SubgraphCycleError(ValueError):
    """Raised when the config-reference DAG contains a cycle."""


class SubgraphDepthError(ValueError):
    """Raised when subgraph nesting exceeds settings.max_subgraph_depth."""


def _strip_worktree(node: Node) -> Node:
    """Force a fan-out subgraph inner node onto the branch tree: clear its
    own worktree directives to `off` (node fields win in effective_worktree),
    so it runs in the branch worktree, not a per-inner-node tree."""
    return node.model_copy(update={"worktree": "off", "worktree_group": None})


class _BranchScopedExecutor:
    """Wraps the shared executor for one fan-out subgraph branch: every inner
    node is pinned to `branch_tree` (forced `off` + workdir). Duck-types the
    NodeExecutor Protocol; passes backend / get_worktree / worktree_map /
    reclaim / close through to the inner executor."""

    def __init__(self, inner: "NodeExecutor", branch_tree: str) -> None:
        self._inner = inner
        self._branch_tree = branch_tree

    async def execute(
        self, node: Node, context: dict, workdir: str | None = None,
        settings_override: "Settings | None" = None,
    ) -> "ExecutionResult":
        # A caller-supplied workdir is intentionally ignored — the branch
        # tree always wins (that is the point of pinning the branch).
        return await self._inner.execute(
            _strip_worktree(node), context,
            workdir=self._branch_tree, settings_override=settings_override,
        )

    def get_backend(self) -> object:
        return self._inner.get_backend() if hasattr(self._inner, "get_backend") else None

    def get_worktree(self, node_id: str) -> "str | None":
        return self._inner.get_worktree(node_id) if hasattr(self._inner, "get_worktree") else None

    def worktree_map(self) -> dict:
        return self._inner.worktree_map() if hasattr(self._inner, "worktree_map") else {}

    async def reclaim(self) -> list:
        if hasattr(self._inner, "reclaim"):
            return await self._inner.reclaim()
        return []

    async def close(self) -> None:
        if hasattr(self._inner, "close"):
            await self._inner.close()


def load_graph(config_path: str, base_dir: str | Path = ".") -> Graph:
    """Load and parse a Graph YAML file. Path is relative to base_dir."""
    path = Path(base_dir) / config_path
    with open(path) as f:
        raw = yaml.safe_load(f)
    return Graph(**raw)


def _terminal_node_output(sub_state: dict[str, Any], sub_config: Graph) -> str:
    """Pick the subgraph's terminal node and return its output.

    A terminal node is one not depended on by any other node. If multiple
    terminals exist, picks the last one defined. Returns "" if subgraph
    has no node_outputs.
    """
    terminals = find_terminal_nodes(sub_config)
    if not terminals:
        return ""
    outputs = sub_state.get("node_outputs", {})
    return outputs.get(terminals[-1], "")


def make_subgraph_node(
    parent_node: Node,
    sub_config: Graph,
    compile_fn: Any,
    executor: "NodeExecutor | None",
    depth: int,
    logger: Any | None = None,
    parent_settings: Settings | None = None,
):
    """Create the wrapper async function added as the parent graph's node.

    The wrapper compiles the subgraph (passing depth+1 so cycles bottom
    out at max_subgraph_depth), then on each invocation:
      1. Renders `inputs:` templates against parent context.
      2. Builds a fresh subgraph initial state with rendered inputs.
      3. Invokes the compiled subgraph.
      4. Projects subgraph outputs back into parent state per `outputs:`.

    When ``logger`` is supplied, the wrapper streams subgraph state
    snapshots through a ``SubgraphLogger`` that prefixes node ids with
    the parent's id, so subgraph-internal completions surface in the
    parent JSONL keyed as ``parent_id::child_id``.

    ``parent_settings`` (Phase 3 / scope-aware): the parent scope's
    *effective* ``Settings``. Merged with ``sub_config.settings``
    (child-explicit-fields-win) and threaded into the recursive
    ``compile_fn`` so subgraph-internal nodes see the merged view.
    """
    parent_eff = parent_settings or sub_config.settings
    sub_effective = merge_settings(parent_eff, sub_config.settings)
    sub_graph = compile_fn(
        sub_config, executor=executor, _depth=depth + 1,
        effective_settings=sub_effective,
    )

    parent_id = parent_node.id
    inputs_decl: dict[str, str] = {}
    outputs_decl: dict[str, str] = {}
    if parent_node.execute is not None and parent_node.execute.params:
        params = parent_node.execute.params
        if isinstance(params.get("inputs"), dict):
            inputs_decl = dict(params["inputs"])
        if isinstance(params.get("outputs"), dict):
            outputs_decl = dict(params["outputs"])

    async def wrapper(parent_state: WorkflowState) -> dict[str, Any]:
        if parent_id in parent_state.get("failed_nodes", set()):
            return {}
        # Dep-join: same semantics as a regular execution node. The
        # `completed_nodes` re-entry guard removed in audit fix #19
        # (Stage 5c) is intentionally absent here too — Command(goto=)
        # re-fires must re-execute the subgraph wrapper, not no-op.
        if (failure := check_dep_failed(parent_node, parent_state)) is not None:
            return failure
        if parent_node.depends_on and not all_deps_completed(
            parent_node, parent_state,
        ):
            return {}

        parent_context = build_context(parent_node, parent_state)
        rendered_inputs = {
            k: render_template(v, parent_context)
            for k, v in inputs_decl.items()
        }

        sub_state = make_initial_state(
            workflow_name=sub_config.name,
            workdir=parent_state.get("workdir", "."),
            dry_run=parent_state.get("dry_run", False),
        )
        sub_state["node_inputs"] = rendered_inputs

        if logger is not None:
            from sqrlly.runtime.logging import SubgraphLogger
            sub_logger = SubgraphLogger(logger, prefix=parent_id)
            sub_result = sub_state
            async for chunk_type, payload in sub_graph.astream(
                sub_state, stream_mode=["updates", "values"],
            ):
                if chunk_type == "values":
                    sub_result = payload
                elif chunk_type == "updates":
                    for _name, update in payload.items():
                        sub_logger.log_update(update)
        else:
            sub_result = await sub_graph.ainvoke(sub_state)

        update: dict[str, Any] = {"completed_nodes": {parent_id}}
        sub_outputs = sub_result.get("node_outputs", {}) or {}

        if outputs_decl:
            new_outputs: dict[str, str] = {}
            for parent_key, template in outputs_decl.items():
                rendered = render_template(template, sub_outputs)
                new_outputs[f"{parent_id}.{parent_key}"] = rendered
            new_outputs[parent_id] = _terminal_node_output(sub_result, sub_config)
            update["node_outputs"] = new_outputs
        else:
            update["node_outputs"] = {
                parent_id: _terminal_node_output(sub_result, sub_config),
            }

        if sub_result.get("failed_nodes"):
            update["failed_nodes"] = {parent_id}
            update["errors"] = [{
                "node": parent_id,
                "error": (
                    f"subgraph '{sub_config.name}' had failed nodes: "
                    f"{sub_result['failed_nodes']}"
                ),
            }]
            update["completed_nodes"] = set()

        return update

    wrapper.__name__ = f"subgraph_{parent_id}"
    return wrapper


def make_fan_out_subgraph_invoker(
    template_url: str,
    template_params: dict[str, Any],
    compile_fn: Any,
    base_dir: str | Path,
    depth: int,
    executor: "NodeExecutor | None",
    logger: Any | None = None,
    parent_settings: Settings | None = None,
) -> Any:
    """Per-Send-branch subgraph invoker for fan-out templates.

    Compiles the subgraph **once** at parent-graph build time (same one-
    shot model as `make_subgraph_node`). Returns an async callable
    ``invoke(context, workdir, dry_run) -> ExecutionResult`` that the
    fan-out node body calls in place of ``executor.execute(...)``.

    Inputs (``template_params['inputs']``) render against the per-item
    context; the rendered map seeds the sub-invocation's
    ``node_inputs``. The subgraph's terminal-node output flows back as
    ``ExecutionResult.output`` so downstream gate evaluation, retries,
    and aggregation paths in dynamic.py stay unchanged.
    """
    # Cycle detection mirrors the recursive-subgraph path: only the
    # outermost build runs it (depth=0 callers), so nested fan-out
    # subgraphs don't re-walk the same chains.
    if depth == 0:
        detect_config_cycle(template_url, base_dir=base_dir)
    sub_config = load_graph(template_url, base_dir=base_dir)
    parent_eff = parent_settings or sub_config.settings
    sub_effective = merge_settings(parent_eff, sub_config.settings)
    sub_compiled = compile_fn(
        sub_config, executor=executor, _depth=depth + 1,
        effective_settings=sub_effective,
    )
    inputs_decl: dict[str, str] = {}
    if isinstance(template_params.get("inputs"), dict):
        inputs_decl = dict(template_params["inputs"])

    async def invoke(
        context: dict[str, Any], workdir: str, dry_run: bool,
        prefix: str | None = None,
    ) -> ExecutionResult:
        """``prefix`` (typically the per-Send child_id like
        ``reviewer_pool::maverick``) names the per-branch subgraph in
        the parent JSONL when ``logger`` is set."""
        rendered_inputs = {
            k: render_template(v, context) for k, v in inputs_decl.items()
        }
        sub_state = make_initial_state(
            workflow_name=sub_config.name,
            workdir=workdir,
            dry_run=dry_run,
        )
        sub_state["node_inputs"] = rendered_inputs

        try:
            if logger is not None and prefix is not None:
                from sqrlly.runtime.logging import SubgraphLogger
                sub_logger = SubgraphLogger(logger, prefix=prefix)
                sub_result = sub_state
                async for chunk_type, payload in sub_compiled.astream(
                    sub_state, stream_mode=["updates", "values"],
                ):
                    if chunk_type == "values":
                        sub_result = payload
                    elif chunk_type == "updates":
                        for _name, update in payload.items():
                            sub_logger.log_update(update)
            else:
                sub_result = await sub_compiled.ainvoke(sub_state)
        except Exception as e:
            return ExecutionResult(
                success=False,
                error=f"subgraph '{sub_config.name}' raised: {e}",
            )

        if sub_result.get("failed_nodes"):
            return ExecutionResult(
                success=False,
                error=(
                    f"subgraph '{sub_config.name}' had failed nodes: "
                    f"{sub_result['failed_nodes']}"
                ),
            )
        return ExecutionResult(
            success=True,
            output=_terminal_node_output(sub_result, sub_config),
        )

    return invoke


def execute_subgraph_path(execute: Any) -> str | None:
    """Return the subgraph YAML path for an Execute block whose `url`
    ends in `.yaml` / `.yml` OR which carries `mode: subgraph` to
    force-route an extensionless URL through the subgraph compiler.
    Used by callers that already hold the Execute (fan-out templates)
    without a containing Node.
    """
    if execute is None or not execute.url:
        return None
    if getattr(execute, "mode", None) == "subgraph":
        return execute.url
    suffix = Path(execute.url).suffix.lower()
    if suffix in {".yaml", ".yml"}:
        return execute.url
    return None


def node_subgraph_path(n: Node) -> str | None:
    """Return the subgraph YAML path for a Stage-5b execute.url ending
    in `.yaml` or `.yml`. Single source of truth for "is this node a
    subgraph reference and where does it point?"
    """
    return execute_subgraph_path(n.execute)


def detect_config_cycle(
    config_path: str,
    visited: list[str] | None = None,
    base_dir: str | Path = ".",
) -> None:
    """Walk the config-reference DAG; raise on cycle.

    Called at compile time for any subgraph reference (Stage 5b
    ``node.execute.url`` ending in ``.yaml``). Visited paths are
    accumulated as the walker descends; revisiting a path means the
    chain refers back to an ancestor.
    """
    visited = list(visited or [])
    abs_path = str(Path(base_dir) / config_path)
    if abs_path in visited:
        chain = " -> ".join(visited + [abs_path])
        raise SubgraphCycleError(f"Subgraph reference cycle: {chain}")
    visited.append(abs_path)
    sub = load_graph(config_path, base_dir=base_dir)
    for n in sub.nodes:
        sub_path = node_subgraph_path(n)
        if sub_path is not None:
            detect_config_cycle(sub_path, visited=visited, base_dir=base_dir)
