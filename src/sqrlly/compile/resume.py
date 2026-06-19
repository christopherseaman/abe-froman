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
    route_adj = {n.id: _route_targets(n) for n in config.nodes}
    groups: dict[str, list[str]] = {}
    group_of: dict[str, str] = {}
    for n in config.nodes:
        kind, group = n.effective_worktree(config.settings)
        if kind == "group":
            groups.setdefault(group, []).append(n.id)
            group_of[n.id] = group

    dirty: set[str] = set(prior_failed) | set(rerun_targets)
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
