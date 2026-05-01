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

from abe_froman.compile.nodes import build_context
from abe_froman.runtime.executor.prompt import render_template
from abe_froman.runtime.state import WorkflowState, make_initial_state
from abe_froman.schema.models import Graph, Node

if TYPE_CHECKING:
    from abe_froman.runtime.result import NodeExecutor


class SubgraphCycleError(ValueError):
    """Raised when the config-reference DAG contains a cycle."""


class SubgraphDepthError(ValueError):
    """Raised when subgraph nesting exceeds settings.max_subgraph_depth."""


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
    depended_on: set[str] = set()
    for n in sub_config.nodes:
        depended_on.update(n.depends_on)
    terminals = [n.id for n in sub_config.nodes if n.id not in depended_on]
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
):
    """Create the wrapper async function added as the parent graph's node.

    The wrapper compiles the subgraph (passing depth+1 so cycles bottom
    out at max_subgraph_depth), then on each invocation:
      1. Renders `inputs:` templates against parent context.
      2. Builds a fresh subgraph initial state with rendered inputs.
      3. Invokes the compiled subgraph.
      4. Projects subgraph outputs back into parent state per `outputs:`.
    """
    sub_graph = compile_fn(sub_config, executor=executor, _depth=depth + 1)

    parent_id = parent_node.id
    inputs_decl = dict(parent_node.inputs)
    outputs_decl = dict(parent_node.outputs)

    async def wrapper(parent_state: WorkflowState) -> dict[str, Any]:
        # Skip if parent node already terminal (re-entry on dep updates).
        if parent_id in parent_state.get("completed_nodes", []):
            return {}
        if parent_id in parent_state.get("failed_nodes", []):
            return {}
        # Wait for dependencies — same join semantics as a regular node.
        completed = set(parent_state.get("completed_nodes", []))
        failed = set(parent_state.get("failed_nodes", []))
        for dep in parent_node.depends_on:
            if dep in failed:
                return {
                    "failed_nodes": [parent_id],
                    "errors": [{
                        "node": parent_id,
                        "error": f"dependency '{dep}' failed",
                    }],
                }
            if dep not in completed:
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

        sub_result = await sub_graph.ainvoke(sub_state)

        update: dict[str, Any] = {"completed_nodes": [parent_id]}
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
            update["failed_nodes"] = [parent_id]
            update["errors"] = [{
                "node": parent_id,
                "error": (
                    f"subgraph '{sub_config.name}' had failed nodes: "
                    f"{sub_result['failed_nodes']}"
                ),
            }]
            update["completed_nodes"] = []

        return update

    wrapper.__name__ = f"subgraph_{parent_id}"
    return wrapper


def _node_subgraph_path(n: Node) -> str | None:
    """Return the subgraph YAML path for either Stage-4 or Stage-5b shape."""
    if n.config:
        return n.config
    if n.execute and n.execute.url:
        suffix = Path(n.execute.url).suffix.lower()
        if suffix in {".yaml", ".yml"}:
            return n.execute.url
    return None


def detect_config_cycle(
    config_path: str,
    visited: list[str] | None = None,
    base_dir: str | Path = ".",
) -> None:
    """Walk the config-reference DAG; raise on cycle.

    Called at compile time for any subgraph reference (Stage 4
    ``node.config:`` OR Stage 5b ``node.execute.url`` ending in
    ``.yaml``). Visited paths are accumulated as the walker descends;
    revisiting a path means the chain refers back to an ancestor.
    """
    visited = list(visited or [])
    abs_path = str(Path(base_dir) / config_path)
    if abs_path in visited:
        chain = " -> ".join(visited + [abs_path])
        raise SubgraphCycleError(f"Subgraph reference cycle: {chain}")
    visited.append(abs_path)
    sub = load_graph(config_path, base_dir=base_dir)
    for n in sub.nodes:
        sub_path = _node_subgraph_path(n)
        if sub_path is not None:
            detect_config_cycle(sub_path, visited=visited, base_dir=base_dir)
