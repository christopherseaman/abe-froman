"""Resume skip-set computation (pure, langgraph-free, compile layer).

`compute_skip_set` decides which prior-run-completed nodes are safe to skip on
``--resume``: prior_completed minus the "dirty" closure (prior failures +
``--resume-from`` targets, expanded by transitive ``depends_on`` dependents,
route-target reachability, and ``worktree_group`` siblings). Topological only —
no content hashing (LLM nondeterminism + fan-out/promote side-effects make a
content hash neither sufficient nor safe; see the design spec)."""
from __future__ import annotations

from sqrlly.schema.models import Graph, Node


def _flatten_goto(goto: "str | list[str] | None") -> list[str]:
    if goto is None:
        return []
    return [goto] if isinstance(goto, str) else list(goto)


def _route_targets(node: Node) -> list[str]:
    """Every static goto target declared on a node's route (goto/cases/else)."""
    r = node.route
    if r is None:
        return []
    out: list[str] = _flatten_goto(r.goto)
    for case in r.cases:
        out += _flatten_goto(case.goto)
    if r.else_ is not None:
        out += _flatten_goto(r.else_.goto)
    return out


def compute_skip_set(
    config: Graph,
    prior_completed: set[str],
    prior_failed: set[str],
    rerun_targets: set[str],
) -> set[str]:
    """Return the node ids safe to skip on resume.

    ``skip = prior_completed - dirty`` where ``dirty`` starts at
    ``prior_failed | rerun_targets`` and is closed over: transitive
    ``depends_on`` dependents, route-target reachability, and
    ``worktree_group`` siblings (a shared mutable tree means any dirty member
    dirties the whole group). Failed nodes are never in ``prior_completed``,
    so the difference can't accidentally skip a failure."""
    ids = {n.id for n in config.nodes}
    dependents: dict[str, list[str]] = {nid: [] for nid in ids}
    for n in config.nodes:
        for dep in n.depends_on:
            if dep in dependents:
                dependents[dep].append(n.id)

    # Fan-out final/aggregation nodes get synthetic ids (_final_<parent>_<f>)
    # that aren't in config.nodes but ARE written to completed_nodes. Wire them
    # as dependents of their parent so a dirty fan-out parent dirties its finals
    # (else the planner can't reach them and they'd be wrongly skipped).
    for n in config.nodes:
        if n.fan_out and n.fan_out.final_nodes:
            for f in n.fan_out.final_nodes:
                dependents.setdefault(n.id, []).append(f"_final_{n.id}_{f.id}")

    route_adj = {n.id: _route_targets(n) for n in config.nodes}
    groups: dict[str, list[str]] = {}
    group_of: dict[str, str] = {}
    for n in config.nodes:
        kind, group = n.effective_worktree(config.settings)
        if kind == "group":
            groups.setdefault(group, []).append(n.id)
            group_of[n.id] = group

    # A failed fan-out child has a synthetic id `<parent>::<item>` that is
    # NOT in config.nodes, so the BFS below can't reach its parent. Seed the
    # parent into the dirty set so it re-fans on resume; the dirty closure
    # then dirties its `_final_<parent>_<f>` aggregation (wired as a synthetic
    # dependent above). Completed siblings are not config.nodes ids and aren't
    # reachable from the parent, so they stay in prior_completed - dirty
    # (frozen — not re-billed). The per-child re-run gate in
    # compile/dynamic.py freezes a child on `child_id in skip`.
    fan_out_parents = {n.id for n in config.nodes if n.fan_out}
    failed_child_parents = {
        fid.split("::", 1)[0]
        for fid in prior_failed
        if "::" in fid and fid.split("::", 1)[0] in fan_out_parents
    }

    dirty: set[str] = set(prior_failed) | set(rerun_targets) | failed_child_parents
    frontier = list(dirty)
    while frontier:
        cur = frontier.pop()
        neighbors = dependents.get(cur, []) + route_adj.get(cur, [])
        g = group_of.get(cur)
        if g:
            neighbors += groups.get(g, [])
        for nxt in neighbors:
            if nxt not in dirty:
                dirty.add(nxt)
                frontier.append(nxt)
    return set(prior_completed) - dirty
