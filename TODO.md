# TODO

Review-surfaced fixes deferred for focused work. Distinct from
`WISHLIST.md` (feature wants) — everything here is a known defect or
cleanup with a diagnosis attached, deferred because the fix is
non-trivial or a judgment call.

Source: full-repo review, 2026-05-20 (four-layer agent review).

---

## Bugs (real defects, non-trivial fix)

### C1 — `no_items` route drops dependents on a fan-out node with no `final_nodes`

`compile/graph.py`. The dynamic-router `route_map` wires `"no_items"`
to `deps_of[0] if deps_of else END` — only the *first* dependent. A
fan-out node that has no `final_nodes` and ≥2 dependents, when its
manifest comes back empty, triggers only dependent #1; the rest stall
(never run, graph finishes incomplete — not a crash).

The non-empty-manifest path already fans to all dependents via a loop
of `add_edge`, so multiple dependents is an otherwise-supported
topology — the bug is specific to the empty-manifest branch.

Why deferred: the clean fix is a `_make_dynamic_router` refactor —
the router currently returns abstract keys (`retry`/`fail`/`no_items`)
translated by a `route_map`; a list can't be a `route_map` *value*
(verified: `TypeError: unhashable`). The fix is to have the router
return concrete target node-ids (and a list for the multi-dep
no_items case), dropping the `route_map` translation layer. That
touches the central dynamic-dispatch path and deserves its own
focused change + tests, not a tail-end rush. A validation guard
("fan-out without final_nodes may not have multiple dependents") was
considered and rejected — it would forbid a topology that works fine
in the common non-empty case.

### E3 — foreman holds the worktree lock across `git worktree add`

`runtime/foreman.py:141-148`. `_acquire_worktree` holds
`self._worktree_lock` for the entire duration of `_create_worktree`,
which `await`s a `git worktree add` subprocess. Concurrent fan-out
(N child nodes entering `execute()` together) serializes worktree
creation — every node after the first queues behind the lock.

Correct, just slow — a perf issue, not a correctness bug.

Why deferred: the fix (reserve the node_id under the global lock,
create the worktree under a per-node-id lock released for the
subprocess) is a concurrency change to correctness-critical code
(the worktree pool). It needs dedicated concurrency tests — two
coroutines for the same node_id must not both create; two for
different ids must parallelize. Worth doing as its own change with
that test coverage.

---

## Low-priority / judgment calls

### U1 (residual) — `file://` URLs still bypass script + workdir gates

`runtime/url.py`. The `max_remote_fetch_bytes` size cap now applies to
`file://` reads, but two gaps remain: `file://` still skips
`allow_remote_scripts`, and there is no path-within-workdir
confinement — `url: /etc/passwd` reads unconfined. Low severity under
the current trust model (a workflow author already controls what
executes), but a real robustness gap if workflow YAML ever comes from
a less-trusted source. Workdir confinement is the larger of the two
and deserves its own design pass.
