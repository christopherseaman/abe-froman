# Wave-driven research planner

Demonstrates the **wave-driven dynamic-task pattern** — discover new
tasks mid-run without restarting upstream work. Each "wave" is one
trip around the dispatcher → workers → reconcile → gate loop. A
wave that finds new pending tasks routes back to the dispatcher;
a wave that finds none routes to `__end__`.

## Topology

```
planner ─→ dispatcher (fan_out) ─→ workers (Send×N)
               ▲                       │
               │                       ▼
               └── gate (route) ←── reconcile
```

- **planner** seeds initial tasks into `state.json`.
- **dispatcher** reads `state.json`, emits a JSON manifest of
  pending items; abe-froman's fan_out machinery dispatches one
  worker child per item via LangGraph `Send`.
- **workers** run in parallel, each marking its own task `done`
  in `state.json`.
- **reconcile** inspects post-wave state and may add follow-up
  tasks. Stdout signals the gate: `reconcile_added:<id>` to loop,
  `reconcile_clean` to exit.
- **gate** is a route-only node. Its `cases:` predicate inspects
  reconcile's stdout and emits `Command(goto=dispatcher)` (loop)
  or `Command(goto=__end__)` (exit).

## Why this pattern

Static fan-out (the standard `fan_out:` block on a dependency-
graph node) requires the manifest of work-items to be known when
the parent node runs. The wave pattern lets a workflow *discover*
work-items as it goes — typical use cases:

- Crawl a research question; sub-questions emerge from initial
  findings.
- Process a queue where workers occasionally enqueue new items
  via reconcile.
- Iterative refinement loops where an evaluator decides whether
  another pass is needed.

Two abe-froman primitives make it work:

1. `dispatcher` has BOTH `depends_on: [planner]` AND is the
   `goto:` target of the gate. The compile layer wires both
   edges; the planner→dispatcher edge fires once at startup,
   the gate→dispatcher goto fires on subsequent waves.

2. `Command(goto=dispatcher)` always re-executes dispatcher's
   body. The fan_out router then reads the freshly-written
   `node_outputs[dispatcher]` rather than a cached value from
   the first wave.

## Running

This example shares state across waves via a single `state.json`
file in the workdir. abe-froman normally isolates each node in
its own git worktree (the Foreman) when the workdir is a git
repo — sibling worktrees can't see each other's state mutations,
which would break the wave pattern.

So run with a non-git workdir:

```bash
mkdir -p /tmp/wave-demo
ln -s "$(pwd)/examples/wave_planner/scripts" /tmp/wave-demo/scripts
uv run abe-froman validate examples/wave_planner/workflow.yaml
uv run abe-froman run examples/wave_planner/workflow.yaml --workdir /tmp/wave-demo
```

You'll see two waves run:

```
Note: workdir is not a git repo — running without worktree isolation (foreman disabled).
Completed: 8 nodes
  Nodes: planner, dispatcher, dispatcher::q_market_size, dispatcher::q_growth_rate,
         reconcile, dispatcher, dispatcher::q_competitor_share, reconcile
```

The `dispatcher` entry appears twice (one fire per wave), and
the dynamically-added `q_competitor_share` shows up as a
fan-out subphase only on the second wave — the manifest from
wave 2 included it because reconcile added it after wave 1.

`/tmp/wave-demo/state.json` has the final state with all three
questions marked `done`.

## Real workflows

The deterministic Python scripts in `scripts/` exist so the
example runs without any backend keys and the e2e test stays
stable. Real workflows replace them with LLM-driven nodes:

- A "research" worker that invokes Claude/DeepSeek to answer
  the question, emitting both the answer and any follow-up
  questions in its output.
- A reconcile node that runs an LLM gate over partial results
  and decides whether to enqueue follow-ups.

When combining the wave pattern with worktree isolation in a
real workflow, thread state through abe-froman's `node_outputs`
mechanism (each node emits state JSON on stdout, downstream
nodes template-read `{{upstream_id}}`) instead of a shared
file. abe-froman's state is automatically visible across
worktrees because it's tracked in the LangGraph WorkflowState,
not on disk.

## Recursion limit

LangGraph caps super-step count at `recursion_limit=25` by
default. Each wave is ~4 super-steps (dispatcher, workers,
reconcile, gate), so the default supports up to ~6 waves before
hitting the limit. Workflows whose reconcile chains can exceed
that should raise the limit explicitly via
`compiled.invoke(..., config={"recursion_limit": N})` or the
equivalent for `astream`.
