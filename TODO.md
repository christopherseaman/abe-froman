# TODO

Review-surfaced fixes deferred for focused work. Distinct from
`WISHLIST.md` (feature wants) — everything here is a known defect or
cleanup with a diagnosis attached, deferred because the fix is
non-trivial or a judgment call.

Source: full-repo review, 2026-05-20 (four-layer agent review).

---

## Bugs (real defects, non-trivial fix)

### C1 — `no_items` route drops dependents on a fan-out node with no `final_nodes`

`compile/graph.py:618`. The dynamic-router `route_map` wires
`"no_items"` to `deps_of[0] if deps_of else END` — only the *first*
dependent. A fan-out node that has no `final_nodes` and ≥2 dependents,
when its manifest comes back empty, triggers only dependent #1; the
rest stall (never run, graph finishes incomplete — not a crash).

The non-empty-manifest path (`graph.py:611`) already fans to all
dependents via a loop of `add_edge`, so multiple dependents is an
otherwise-supported topology — the bug is specific to the empty-
manifest branch.

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

## Framework-fit / consistency

### E5 — ACP backend has its own `_is_overload_error`

`runtime/executor/backends/acp.py:17`. Anthropic + OpenAI backends
share `_overload.py::maybe_raise_overload`; ACP keeps a private
`_is_overload_error`. NOT a clean swap: ACP's version does
string-message matching (`"529" in msg`, `"overload" in msg`) because
ACP subprocess errors are message-shaped, not status-attribute-shaped
(generic exceptions, no `.status_code`). `maybe_raise_overload` only
checks numeric status + class name — migrating ACP to it as-is would
*weaken* detection. Unifying needs `_overload.py` to grow an optional
message-pattern check, or ACP keeps its own with a comment explaining
why. Decide the shape first.

### S6 — `extra="forbid"` missing on several schema models

`schema/models.py`. `Node`, `FanOut`, `LlmPreset`, `CommandPreset`
set `extra="forbid"`; `Evaluation`, `DimensionCheck`, `OutputContract`,
`RouteCase`, `RouteElse`, `FanOutTemplate`, `FanOutFinalNode` do not —
so a typo'd YAML key in those blocks silently drops instead of raising
`ValidationError`. The project's stated intent (per the `Node`
docstring) is `extra="forbid"` everywhere. Deferred because the sweep
could surface latent unknown keys in existing example/test YAML —
needs a run-the-suite-and-see pass, not a blind add.

---

## DRY / simplification (no correctness impact)

### C5 — terminal-node logic duplicated

`compile/graph.py:25` (`_find_terminal_nodes`) and
`compile/subgraph.py:59` (`_terminal_node_output`) both compute "nodes
not in any `depends_on`" identically. Extract `_find_terminal_nodes`
to a shared langgraph-free location (e.g. `_manifest.py`) and call it
from both.

### C6 — `_register_evaluation_node` is a thin flag-dispatch wrapper

`compile/graph.py:182`. Exists only to pick `_make_combined_eval_
decide_node` vs `_make_evaluation_node` via a `combined=` flag and
call `add_node`. Two call sites. Inlining the factory selection at
each call site removes the flag-parameter smell. Judgment call.

### C7 — `deps_of` reverse-lookup scan duplicated

`compile/graph.py:552,600`. The O(N) scan
`[p.id for p in config.nodes if node.id in p.depends_on]` runs in two
loops. Precompute a `dependents: dict[str, list[str]]` reverse map
once at the top of `build_workflow_graph`.

### U4 — `_yaml()` duplicated between migrate.py and the migrator script

`cli/migrate.py:60` and `scripts/migrate_legacy_executor_to_presets.py:44`
define the identical ruamel `YAML()` setup. The script is a PEP-723
standalone (can't import the package), so the duplication is
structurally justified — only worth resolving if `migrate.py` grows
more shared YAML utilities.

---

## Low-priority / judgment calls

### S3 — `params_for_url` raises `KeyError` on an unknown `mode`

`schema/params.py:84`. `_MODE_TO_PARAMS[mode]` with no guard. Low risk
in practice — `mode` originates from `Execute.mode`, a `Literal`-typed
field Pydantic validates. Only reachable if schema validation is
bypassed. A `.get()` + explicit `ValueError` would give a better
message; marginal.

### S4 — `DimensionCheck.min` shadows the `min` builtin

`schema/models.py`. Field named `min` shadows the builtin and reads
awkwardly (`d.min`). Renaming to `threshold` (matching
`Evaluation.threshold`) is cleaner but a breaking YAML change —
needs a `Field(alias="min")` shim or a migrator pass.

### U1 — `file://` URLs bypass the remote-fetch gates

`runtime/url.py:158`. `file://` URLs skip `max_remote_fetch_bytes`,
`allow_remote_scripts`, and have no path-within-workdir confinement —
`url: /etc/passwd` reads unconfined. Low severity under the current
trust model (a workflow author already controls what executes), but a
real robustness gap if workflow YAML ever comes from a less-trusted
source. Worth: apply the size cap to file reads + optional workdir
confinement.

### U6 — `_select_headers` prefix-match footgun

`runtime/url.py:128`. `url_headers` keys are documented as URL
prefixes but look like hostnames; `https://api.example.com` matches
`https://api.example.com/v1/...` but not `:8080/...`. Consider host-
pattern matching (consistent with `_matches_allowlist`) or a schema
doc note.
