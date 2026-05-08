"""Render a workflow as a single self-contained HTML viewer.

Two modes:
  - Authoring: ``abe-froman view <yaml>`` — topology + per-node
    config panel. No runtime overlay.
  - Debug: ``abe-froman view <yaml> --log <jsonl>`` — same as above,
    plus per-node status overlay (passed/failed/retried/untouched)
    and per-node log slices on click.

The Mermaid output is generated directly from the ``Graph`` model
rather than from ``compiled.get_graph().draw_mermaid()``. This gives
the viewer two properties LangGraph's emission does not:

  1. **Author's perspective.** Synthetic ``_eval_<id>`` and
     ``_route_<id>`` nodes that the compile layer adds are not
     surfaced — authors see the workflow as written, not as compiled.
  2. **Layout control.** All workflow nodes are wrapped in a Mermaid
     ``subgraph`` block. Invisible spine edges ``START ~~~ workflow
     ~~~ END`` are emitted first to anchor layout direction; real
     entry/terminal edges follow.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from abe_froman.schema.models import Graph, Node


# ----- Mermaid emission -----------------------------------------------------


_END_TARGET = "__end__"


def _flatten_goto(target: str | list[str] | None) -> list[str]:
    if target is None:
        return []
    if isinstance(target, str):
        return [target]
    return list(target)


def _node_shape(node: Node) -> tuple[str, str]:
    """Return (open, close) Mermaid shape delimiters for a node.

    - Plain execute → rectangle ``[...]``
    - Fan-out parent → hexagon ``{{...}}``
    - Subgraph reference (``execute.url`` ends in .yaml/.yml) →
      subroutine ``[[...]]``
    - Route-only (no execute) → diamond ``{...}``
    """
    if node.fan_out is not None:
        return "{{", "}}"
    if node.execute is None:
        return "{", "}"
    url = node.execute.url or ""
    if url.endswith(".yaml") or url.endswith(".yml"):
        return "[[", "]]"
    return "[", "]"


def _node_label(node: Node) -> str:
    """Human-readable label used inside the shape brackets.

    Escapes double quotes and trims the rendered name. The full
    config (timeouts, model, eval thresholds, etc.) is rendered in
    the side panel, not on the diagram.
    """
    label = node.name or node.id
    return label.replace('"', '&quot;')


def _route_targets(graph: Graph) -> dict[str, list[tuple[str, str | None]]]:
    """For each node, list its outgoing route targets and their labels.

    Returns ``{node_id: [(target_id, label_or_None), ...]}``. Label is
    ``None`` for unconditional gotos, ``"else"`` for else branches,
    and the ``when`` predicate string for case branches.
    """
    out: dict[str, list[tuple[str, str | None]]] = {}
    for node in graph.nodes:
        if not node.route:
            continue
        edges: list[tuple[str, str | None]] = []
        if node.route.goto is not None:
            for t in _flatten_goto(node.route.goto):
                edges.append((t, None))
        else:
            for case in node.route.cases:
                for t in _flatten_goto(case.goto):
                    edges.append((t, case.when))
            if node.route.else_:
                for t in _flatten_goto(node.route.else_.goto):
                    edges.append((t, "else"))
        if edges:
            out[node.id] = edges
    return out


def _classify_endpoints(graph: Graph) -> tuple[set[str], set[str]]:
    """Return (entry_ids, terminal_ids).

    Entry: nodes with no incoming dep edge AND no incoming route
    edge from another node.
    Terminal: nodes with no outgoing dep edge AND no outgoing route
    edge to another in-graph node (route to ``__end__`` counts as
    outgoing-to-END, not in-graph).
    """
    all_ids = {n.id for n in graph.nodes}
    has_incoming: set[str] = set()
    has_outgoing: set[str] = set()

    # depends_on contributes incoming for the node itself, outgoing
    # for the dep.
    for node in graph.nodes:
        if node.depends_on:
            has_incoming.add(node.id)
            for dep in node.depends_on:
                has_outgoing.add(dep)

    # Routes contribute outgoing for the source, incoming for the
    # target (unless target is __end__).
    routes = _route_targets(graph)
    for src, edges in routes.items():
        has_outgoing.add(src)
        for tgt, _label in edges:
            if tgt != _END_TARGET and tgt in all_ids:
                has_incoming.add(tgt)

    entry_ids = all_ids - has_incoming
    terminal_ids = all_ids - has_outgoing
    return entry_ids, terminal_ids


def _routes_to_end(graph: Graph, node_id: str) -> bool:
    """True iff the node has at least one route target == ``__end__``."""
    routes = _route_targets(graph)
    edges = routes.get(node_id, [])
    return any(tgt == _END_TARGET for tgt, _label in edges)


def render_mermaid(graph: Graph, direction: str = "TB") -> str:
    """Emit a Mermaid flowchart for the workflow.

    Layout strategy:
      - ``START`` and ``END`` declared at top.
      - Invisible spine ``START ~~~ workflow ~~~ END`` defined FIRST
        (anchors dagre's layout direction).
      - All workflow nodes wrapped in a single ``subgraph workflow``
        block.
      - Real entry edges (``START --> entry_node``) and terminal
        edges (``terminal --> END``) emitted last.
    """
    if direction not in ("TB", "LR", "BT", "RL"):
        raise ValueError(
            f"direction must be one of TB/LR/BT/RL, got {direction!r}"
        )

    entry_ids, terminal_ids = _classify_endpoints(graph)
    routes = _route_targets(graph)

    lines: list[str] = []
    lines.append(f"flowchart {direction}")
    lines.append("    START([START])")
    lines.append("    END([END])")
    lines.append("")
    # Invisible spine — first edges in the file so dagre uses them
    # to anchor the workflow box between START and END.
    lines.append("    START ~~~ workflow")
    lines.append("    workflow ~~~ END")
    lines.append("")
    lines.append('    subgraph workflow [" "]')

    # Node declarations, sorted by id for stable output.
    nodes_sorted = sorted(graph.nodes, key=lambda n: n.id)
    for node in nodes_sorted:
        open_b, close_b = _node_shape(node)
        label = _node_label(node)
        suffix = ":::gated" if node.evaluation else ""
        lines.append(f'        {node.id}{open_b}"{label}"{close_b}{suffix}')

    # Internal edges: depends_on + routes between in-graph nodes.
    all_ids = {n.id for n in graph.nodes}
    for node in nodes_sorted:
        for dep in node.depends_on:
            lines.append(f"        {dep} --> {node.id}")
    for src in sorted(routes.keys()):
        for tgt, label in routes[src]:
            if tgt == _END_TARGET or tgt not in all_ids:
                continue
            if label is None:
                lines.append(f"        {src} -.-> {tgt}")
            else:
                safe = label.replace("|", "/").replace('"', "'")
                lines.append(f'        {src} -.->|"{safe}"| {tgt}')

    lines.append("    end")
    lines.append("")

    # Spine endpoints: START → entries, terminals → END.
    for entry in sorted(entry_ids):
        lines.append(f"    START --> {entry}")
    for term in sorted(terminal_ids):
        if _routes_to_end(graph, term):
            continue  # already routes to __end__ explicitly
        lines.append(f"    {term} --> END")

    # Routes that go directly to __end__: emit as edge to the END
    # node so it's visible in the diagram.
    for src in sorted(routes.keys()):
        for tgt, label in routes[src]:
            if tgt != _END_TARGET:
                continue
            if label is None:
                lines.append(f"    {src} -.-> END")
            else:
                safe = label.replace("|", "/").replace('"', "'")
                lines.append(f'    {src} -.->|"{safe}"| END')

    # Style for gated nodes — colored stroke marks "this node has an
    # evaluation gate." Subtle so it doesn't clash with status overlay.
    lines.append("")
    lines.append("    classDef gated stroke:#9333ea,stroke-width:2px")

    return "\n".join(lines)


# ----- Per-node config extraction -------------------------------------------


def extract_node_config(node: Node) -> dict[str, Any]:
    """Distill the YAML-level fields a viewer should show in the
    side panel.

    Keeps only fields with non-default values so the panel doesn't
    drown in defaults. Returned dict is JSON-serializable.
    """
    cfg: dict[str, Any] = {"id": node.id, "name": node.name}
    if node.description:
        cfg["description"] = node.description
    if node.depends_on:
        cfg["depends_on"] = list(node.depends_on)
    if node.model:
        cfg["model"] = node.model
    if node.timeout is not None:
        cfg["timeout"] = node.timeout
    if node.execute:
        ex: dict[str, Any] = {}
        if node.execute.url:
            ex["url"] = node.execute.url
        if node.execute.type:
            ex["type"] = node.execute.type
        if node.execute.mode:
            ex["mode"] = node.execute.mode
        if node.execute.params:
            ex["params"] = (
                node.execute.params.model_dump(exclude_none=True)
                if hasattr(node.execute.params, "model_dump")
                else node.execute.params
            )
        cfg["execute"] = ex
    if node.evaluation:
        ev: dict[str, Any] = {}
        if node.evaluation.validator:
            ev["validator"] = node.evaluation.validator
        if node.evaluation.threshold != 1.0:
            ev["threshold"] = node.evaluation.threshold
        if node.evaluation.dimensions:
            ev["dimensions"] = [
                {"field": d.field, "min": d.min}
                for d in node.evaluation.dimensions
            ]
        if node.evaluation.max_retries is not None:
            ev["max_retries"] = node.evaluation.max_retries
        ev["blocking"] = node.evaluation.blocking
        cfg["evaluation"] = ev
    if node.fan_out:
        fo: dict[str, Any] = {}
        if node.fan_out.manifest_path:
            fo["manifest_path"] = node.fan_out.manifest_path
        if node.fan_out.template:
            fo["template"] = "(per-child execute block)"
        if node.fan_out.final_nodes:
            fo["final_nodes"] = [n.id for n in node.fan_out.final_nodes]
        cfg["fan_out"] = fo
    if node.route:
        rt: dict[str, Any] = {}
        if node.route.goto is not None:
            rt["goto"] = node.route.goto
        if node.route.cases:
            rt["cases"] = [
                {"when": c.when, "goto": c.goto, "include_eval": c.include_eval}
                for c in node.route.cases
            ]
        if node.route.else_:
            rt["else"] = {
                "goto": node.route.else_.goto,
                "include_eval": node.route.else_.include_eval,
            }
        cfg["route"] = rt
    if node.output_contract:
        oc: dict[str, Any] = {}
        if node.output_contract.base_directory:
            oc["base_directory"] = node.output_contract.base_directory
        if node.output_contract.required_files:
            oc["required_files"] = list(node.output_contract.required_files)
        cfg["output_contract"] = oc
    return cfg


# ----- Status overlay from JSONL events -------------------------------------


@dataclass
class NodeStatus:
    status: str  # "passed" | "failed" | "retried" | "untouched"
    fired_count: int
    retry_count: int
    last_error: str | None
    events: list[dict[str, Any]]

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "fired_count": self.fired_count,
            "retry_count": self.retry_count,
            "last_error": self.last_error,
            "events": self.events,
        }


def compute_node_status(
    graph: Graph,
    events: list[dict[str, Any]] | None,
) -> dict[str, NodeStatus]:
    """Reduce a JSONL event list to a per-node status snapshot.

    Rules:
      - ``status="untouched"`` if no terminal event for the node.
      - ``status="passed"`` if the LAST terminal event is
        ``node_completed``.
      - ``status="failed"`` if the LAST terminal event is
        ``node_failed``.
      - ``retry_count`` = number of ``node_retried`` events.
      - ``fired_count`` = number of ``node_completed`` events
        (a goto-driven re-fire that completes successfully bumps
        this; useful for spotting wave-pattern dispatcher loops).
      - ``last_error`` is the most recent ``error`` field on a
        ``node_failed`` event, else ``None``.
      - All events with matching ``node`` field are kept under
        ``events`` for the side-panel drill-down.

    Untouched nodes (defined in topology, no events) get a
    ``NodeStatus`` row with empty events. Nodes that appear in events
    but are NOT in the topology (e.g., fan-out children
    ``parent::child``) are also returned so the viewer can render
    chip-level stats for them; the topology renderer simply ignores
    those keys.
    """
    statuses: dict[str, NodeStatus] = {}

    # Seed from topology so untouched nodes still appear.
    for node in graph.nodes:
        statuses[node.id] = NodeStatus(
            status="untouched",
            fired_count=0,
            retry_count=0,
            last_error=None,
            events=[],
        )

    if not events:
        return statuses

    for ev in events:
        node_id = ev.get("node")
        if not node_id:
            continue
        if node_id not in statuses:
            statuses[node_id] = NodeStatus(
                status="untouched",
                fired_count=0,
                retry_count=0,
                last_error=None,
                events=[],
            )
        s = statuses[node_id]
        s.events.append(ev)
        et = ev.get("event")
        if et == "node_completed":
            s.status = "passed"
            s.fired_count += 1
        elif et == "node_failed":
            s.status = "failed"
            s.last_error = ev.get("error")
        elif et == "node_retried":
            s.retry_count += 1

    return statuses


# ----- HTML rendering -------------------------------------------------------


_TEMPLATE_PATH = Path(__file__).parent / "templates" / "view.html.j2"


def render_view(
    graph: Graph,
    events: list[dict[str, Any]] | None,
    direction: str = "TB",
) -> str:
    """Render the full HTML page as a string.

    Imports Jinja2 lazily so unrelated CLI commands don't pay the
    import cost.
    """
    from jinja2 import Template

    mermaid = render_mermaid(graph, direction=direction)
    statuses = compute_node_status(graph, events)
    node_configs = {n.id: extract_node_config(n) for n in graph.nodes}

    payload = {
        "workflow": {
            "name": graph.name,
            "version": graph.version,
        },
        "node_configs": node_configs,
        "statuses": {nid: s.to_json() for nid, s in statuses.items()},
        "has_log": events is not None,
    }

    template = Template(
        _TEMPLATE_PATH.read_text(),
        keep_trailing_newline=True,
    )
    return template.render(
        mermaid=mermaid,
        payload_json=json.dumps(payload, indent=2),
        workflow_name=graph.name,
        workflow_version=graph.version,
        has_log=events is not None,
    )


def read_jsonl_log(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL log file into a list of event dicts.

    Skips blank lines and lines that don't parse as JSON (resilient
    to trailing whitespace, partial writes).
    """
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out
