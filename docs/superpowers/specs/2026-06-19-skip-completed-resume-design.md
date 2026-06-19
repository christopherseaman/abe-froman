# Design: Skip-completed resume (`--resume` reworked)

**Date:** 2026-06-19
**Status:** Approved-in-direction (design workflow + operator decisions); pending spec sign-off
**Origin:** builder-sqrlly request #2; TODO 26 / 31 / B2. Design produced by a
propose→critique→synthesize workflow and hardened against the codebase invariants.
**Release:** **minor 0.6.0** (user-facing `--resume` default change — operator-approved).

## Problem

`sqrlly run --resume` currently **re-runs every node**. The resume block in
`cli/main.py` reseeds the prior checkpoint's `channel_values`, clears
`failed_nodes`/`retries`/`errors`, deletes the thread, and re-streams — with no
completed-node short-circuit. For a multi-hour, ~20-phase LLM pipeline this
re-bills every node that already succeeded. Two needs: **recover** a long run
that died mid-way without re-paying for completed work, and **iterate** on one
node without re-running everything upstream.

## Operator decisions (this design implements these)

1. **Flip the default.** Bare `--resume` skips cleanly-completed nodes.
   `--rerun-all` restores today's full replay. → **minor 0.6.0**.
2. **Subgraphs: ref-node granularity for v1.** A whole subgraph is skipped when
   its reference node completed cleanly; a subgraph that failed/ran-partially
   re-runs in full. The re-run count is **printed** so it's never silently
   believed-complete.
3. `--resume-from <node>` **implies** `--resume`.

---

## Core mechanism — one frozen snapshot channel

The load-bearing insight (and the trap any naive design dies on): **the
skip-guard must NOT read the live `completed_nodes` set.** Goto-driven re-fires,
wave dispatchers, and the `all_deps_completed` join barrier all *legitimately
re-enter completed nodes within a single run* — guarding on the live set would
break them.

Instead, add ONE channel to `WorkflowState` (`runtime/state.py`):

```python
_resume_skip: NotRequired[set[str]]
```

- **No `REDUCERS` entry** — last-write-wins, exactly like `_route_sender`.
- A **plain `set`**, not `frozenset` (the `AsyncSqliteSaver` msgpack path does not
  round-trip `frozenset`; `completed_nodes` already round-trips as `set` — match it).
- **Seeded exactly once** at resume entry from the *prior* checkpoint, never
  written by any node body — so LangGraph carries it forward untouched across
  every super-step. Guards **read** it; nothing mutates it.
- `make_initial_state` does **not** seed it → absent on a fresh run → guards skip
  nothing → fresh-run invariant preserved.

### Skip-set math

```
dirty = prior_failed
      ∪ resume_from_targets
      ∪ transitive depends_on-dependents of (prior_failed ∪ resume_from_targets)
      ∪ route-target closure of dirty            # goto/case/else targets
      ∪ worktree_group siblings of any dirty member
skip  = prior_completed − dirty
```

Failed nodes are structurally never in `completed_nodes` (the body writes
`failed_nodes` on failure and `completed_nodes` only on success / when ungated),
so the set-difference can never accidentally skip a failed node.

A new **pure, langgraph-free** helper lives in a new module `compile/resume.py`:

```python
def compute_skip_set(
    config: Graph, prior_completed: set[str], prior_failed: set[str],
    rerun_targets: set[str],
) -> set[str]
```

It walks the static `depends_on` DAG (already validated acyclic + complete), the
route-target adjacency (`route.goto` / `case.goto` / `else_.goto`), and
`worktree_group` membership. Returns `prior_completed − dirty`.

---

## The three guard sites (and the one that's easy to miss)

1. **Execution node** (`compile/nodes.py`, the exec `node_fn`): as the FIRST
   guard, `if (skip := state.get("_resume_skip")) and node.id in skip: return {}`.
   For an ungated skipped node this suffices — `completed_nodes[node.id]` and
   `node_outputs[node.id]` are already in the reseeded state, so the join barrier
   and downstream `build_context` see them.
2. **Evaluation nodes — THE FIX the critique caught.** The eval node guards only
   on `failed_nodes`/`dry_run`, **not** `completed_nodes` — so a reseeded *gated*
   node would re-run its validator (often the expensive LLM judge). Add the same
   `_resume_skip` guard to **BOTH** `_make_evaluation_node` AND
   `_make_combined_eval_decide_node` (the dynamic gated parent). The Decision node
   already short-circuits on `completed_nodes` (reseeded), so routing to
   `pass_targets` happens with zero eval bill.
3. **Fan-out child** (`compile/dynamic.py`): after the `failed_nodes` check,
   `if skip and parent_node.id in skip and child_id in skip: return {}`. Gated on
   the **parent** being frozen — a child skips only when the parent's manifest
   isn't re-derived (so `child_id` is stable). A dirty parent re-fans and all
   children re-run.

**Route nodes need no extra guard.** An execute+route node exits via a separate
synthetic `_route_<id>` dispatcher on a static edge that is never in
`completed_nodes` (so never in the snapshot) — it always fires and re-resolves
`Command(goto=...)` from the reseeded outputs. Freezing the execute body still
routes correctly. `compute_skip_set` adds the route-target closure to `dirty` so
a node reached *only* via a goto from a dirty node still re-runs.

---

## CLI surface (`cli/main.py` `run`)

```
--resume                 # NEW default: skip cleanly-completed nodes
--resume-from <node>     # repeatable; re-run node + downstream; implies --resume
--rerun-all              # with --resume: force skip-set empty (pre-0.6 full replay)
```

- `--resume-from X` implies `--resume`; adds `X` + closure to `dirty`.
- `--rerun-all` + `--resume-from` together → `ClickException` (contradictory).
- Any `--resume-from` id that is unknown or contains `::` → `ClickException` with
  the valid-id list (user input at a system boundary; Send children have no stable
  cross-run id).
- Echo replaces "N nodes already completed" with:
  `Resuming: skipping {len(skip)} completed; re-running {len(dirty_present)} (from: {targets or 'failed nodes'}).`
- At `-v`: print the skip-set list **and**
  `Note: {k} subgraph/partially-run nodes are not skippable in v1 and will re-run.`
  (the honesty requirement — partial coverage must be visible).

Thread `resume_from: tuple[str,...]` and `rerun_all: bool` through
`run` → `_run_async` → `_execute_workflow` alongside the existing `resume` bool.

---

## Seed (resume block, `cli/main.py`)

```python
old = dict(prev.checkpoint["channel_values"])
prior_completed = set(old.get("completed_nodes", set()))
prior_failed    = set(old.get("failed_nodes", set()))
rerun_targets   = set(resume_from)                       # CLI-validated
skip = set() if rerun_all else compute_skip_set(config, prior_completed, prior_failed, rerun_targets)
state = {**old, "failed_nodes": set(), "retries": {}, "errors": [],
         "workdir": workdir, "dry_run": False, "_resume_skip": skip}
```

The rest of the resume block (`adelete_thread`, re-stream, foreman rehydrate from
`node_worktrees`) is unchanged. `node_outputs` / `node_structured_outputs` /
`node_worktrees` / `child_outputs` / `evaluations` are already reseeded, so a
skipped node's frozen output is already visible to downstream `build_context` —
**no separate output cache is needed.**

## Promotion is already safe

The promote loop does `if tree is None: continue`. A skipped node never ran in
the new process, so `get_worktree(node.id)` returns `None` and it's excluded —
no stale-tree promote.

---

## Explicitly out of scope (v1)

- **Subgraph inner-node skip** — inner ids never reach the top-level checkpoint;
  v1 skips at ref-node granularity and prints the limitation. (Follow-up; should
  build on this same `_resume_skip` substrate, alongside TODO 226 / item 391.)
- **Non-cascading `--rerun-this-only`** — requires the operator to certify output-
  shape stability, unverifiable under LLM nondeterminism.
- **Content-addressed invalidation** — rejected: a hash proves same-inputs not
  same-result (LLM nondeterminism), Send children have no stable cross-run id, and
  worktree/promote filesystem side-effects aren't captured by an output hash.
- **GC'd-worktree pre-flight** (refuse to freeze a node whose reseeded worktree
  path is gone) — default `worktree_gc: never` makes this rare; a doc note for v1.

## Edge cases handled

Gated-node eval re-bill (guard #2); subgraph inner nodes (ref-node scope + print);
within-run goto/wave re-fire (frozen snapshot never suppresses live re-entry);
downstream-of-failure (failure closure in `dirty`); route-target-of-failure
(route closure in `dirty`); `worktree_group` mutable-share (force-dirty the group);
route-body skip doesn't orphan goto targets (synthetic dispatcher fires);
fan-out child id stability (gated on parent frozen); msgpack set round-trip;
unknown/`::` `--resume-from` id (ClickException); empty skip-set (degenerate =
full replay); resume-of-a-resume (monotonic `completed_nodes` composes).

## Testing (real subprocess / real AsyncSqliteSaver, no mocks)

Extend `tests/e2e/test_resume_fan_out.py` (real checkpointer + real
DispatchExecutor + per-node runs-counter files):

1. **Recover (flip the pinned regression):** the existing `a→b(fail)→c` test —
   change `_read_runs(a) == 2` to `== 1` (a skipped), keep `b == 2` (re-ran after
   failure) and `c == 1`; assert `completed_nodes == {a,b,c}`. The fixture comment
   already anticipates exactly this flip.
2. **Gated-node no-eval-rebill** (TDD-pins guard #2): a node whose evaluation
   passed in run 1; on resume assert via an eval-invocation counter that the
   validator did NOT run and the body did NOT run. Fails without guard #2.
3. **Iterate:** `a→b→c→d` clean; `--resume-from c` → a,b frozen (==1), c,d re-run
   (==2); assert d's rendered prompt contains both the frozen b output and the
   fresh c output.
4. **Failure-closure (known-bad):** diamond `a→{b,c}→d`, b fails → a skipped, b
   re-runs, c skipped, d re-runs.
5. **Route-target-of-failure:** a node reached only via goto from a failed node
   re-runs (proves route closure in `dirty`).
6. **Subgraph limitation:** clean subgraph ref node skipped; a mid-failed
   subgraph re-runs in full (assert + document).
7. **Goto non-interference:** a goto re-fire loop not in the snapshot still
   re-fires the expected count under resume.
8. **`--rerun-all` back-compat:** reproduces today's full replay (`a == 2`).

Unit tests for `compute_skip_set` (parametrized over linear/diamond/fan-out/route/
worktree_group DAGs): known-good (failure, no dependents → closure == {failed})
and known-bad (failure + transitive dependents + route target + group sibling →
all dirty). Plus a serialization smoke test round-tripping a set-valued
`_resume_skip` through `AsyncSqliteSaver`.

## Files touched

| File | Change |
|---|---|
| `runtime/state.py` | `_resume_skip: NotRequired[set[str]]` channel (no reducer) |
| `compile/resume.py` (new) | `compute_skip_set` + closure helpers (pure, langgraph-free) |
| `compile/nodes.py` | skip-guard in exec `node_fn`, `_make_evaluation_node`, `_make_combined_eval_decide_node` |
| `compile/dynamic.py` | skip-guard in the fan-out child |
| `cli/main.py` | `--resume-from` / `--rerun-all` options; seed `_resume_skip`; echo/`-v` print |
| `tests/e2e/test_resume_fan_out.py`, `tests/unit/compile/` | tests above |
| `SCHEMA.md` / `SKILLS.md` / `CLAUDE.md` / `CHANGELOG.md` | document the new resume semantics + flags; update the `--resume` "Known limitations" bullet |

Layer rules intact: `compute_skip_set` is langgraph-free compile-layer; the
channel is runtime; guards are compile-layer node bodies.

## Risks

Changing the default is the headline risk — a goto/wave-heavy workflow relying on
full re-execution must learn `--rerun-all`; mitigated because the snapshot is
frozen (within-run re-fires untouched) and the maintainer's own fixture
pre-commits to skip-as-default. The eval-guard touches two factories — miss one
and a class of gated nodes silently re-bills (test 2 guards this). Route closure
correctness depends on enumerating all goto sites — keep `compute_skip_set`
adjacent to the route validator so they evolve together. `worktree_group`
force-dirty can over-re-run a group — accepted (correctness over thrift).

## Open questions deferred to implementation

- Confirm route-target closure need not also walk a dirty gated node's
  `pass_targets` (they're static edges, so likely already in `depends_on`).
