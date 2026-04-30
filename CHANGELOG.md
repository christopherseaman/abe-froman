# Changelog

All notable changes to abe-froman are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased] — Stage 4: Phase → Node + Recursive Subgraphs + Join Nodes

### ⚠️ Breaking changes

#### YAML schema (hard cutover; no aliases)
- `phases:` → `nodes:`
- `dynamic_subphases:` → `fan_out:` (with structural flattening — see below)
- `quality_gate:` → `evaluation:`
- `dynamic_subphases.template.prompt_file` is now lifted onto the parent
  node (`prompt_file:`) since fan-out spawns instances of the parent
  itself.
- `dynamic_subphases.final_phases:` items are now top-level sibling
  nodes with explicit `depends_on: [<parent_id>]` chains. The first
  former-final-phase depends on the fan-out parent; subsequent ones
  chain depends on the previous.

A migration tool ships with this release: `abe-froman migrate <file>
[--dry-run | --in-place]` rewrites pre-Stage-4 YAML to the new shape
using `ruamel.yaml` (preserves comments, anchors, references, and
`{{templated}}` strings).

#### State channels
- `phase_outputs` → `node_outputs`
- `phase_structured_outputs` → `node_structured_outputs`
- `completed_phases` → `completed_nodes`
- `failed_phases` → `failed_nodes`
- `phase_worktrees` → `node_worktrees`
- `subphase_outputs` → `child_outputs`
- `_subphase_item` (transient field) → `_fan_out_item`

#### JSONL event log
- `phase_started` → `node_started`
- `phase_completed` → `node_completed`
- `phase_failed` → `node_failed`
- `phase_retried` → `node_retried`
- `gate_evaluated` is unchanged.

#### Schema/runtime symbols
- `WorkflowConfig` → `Graph`
- `Phase` (Pydantic model) → `Node`
- `PhaseExecutor` (Protocol) → `NodeExecutor`
- `_make_phase_node` → `_make_execution_node`
- `_make_subphase_node` → `_make_fan_out_node`
- `_make_final_phase_node` → `_make_final_fan_out_node`
- Many internal-only renames in `compile/graph.py` and `runtime/gates.py`
  (`phase_map`, `gated_phase_ids`, `dynamic_phase_ids`, `phase_output`
  parameter, etc.). User-facing template variables — `{{dep_subphases}}`,
  `{{<parent>_subphases}}`, `{{<parent>_subphase_worktrees}}` — are
  intentionally retained: subphase IDs follow the documented
  `{parent_id}::{item_id}` form, which is the term for fan-out
  children.

### Added

#### `JoinExecution` — explicit topology marker
- New `execution: { type: join }` body. No-op execution that exists
  purely to name an explicit synchronization point at fan-in. Multi-
  predecessor nodes implicit-join automatically (LangGraph default);
  the join type is for author readability. Composes with `evaluation:`
  like any other node.

#### Recursive subgraph composition
- A `Node` may reference another graph YAML via `config:
  path/to/sub.yaml`. The subgraph compiles recursively via
  `add_node(name, compiled_subgraph)`. Graphs and subgraphs are
  definitionally identical — the same YAML is invokable both
  standalone (via `abe-froman run`) and as a subgraph reference.
- `inputs:` projects parent state into the subgraph's `node_inputs`
  channel; subgraph nodes see them as plain template variables alongside
  their own dep outputs. Subgraph never sees parent's full state.
- `outputs:` exposes named subgraph node outputs as `node_outputs[
  parent_id.key]` in the parent. Default (empty `outputs:`) projects
  the subgraph's terminal-node output as `node_outputs[parent_id]`.
- Compile-time guards: cycle detection over the config-reference DAG
  (`SubgraphCycleError`) and `settings.max_subgraph_depth` cap (default
  10; `SubgraphDepthError`).
- Subgraph internal failures surface as `failed_nodes[parent_id]` only;
  internals are not flattened into parent state.
- Demo: `examples/absurd-paper/subgraphs/compose_and_validate.yaml`
  carves the reconcile → persist → submission_check chain into a
  standalone-runnable subgraph; the parent workflow's `paper` node
  references it via `config:` + `inputs:`.

#### `abe-froman migrate` CLI
- Rewrites pre-Stage-4 YAML to the new shape losslessly. Round-trip
  YAML mode preserves comments, anchors, and `{{templated}}` strings.
- `--dry-run` prints the rewrite to stdout without modifying the file;
  `--in-place` writes back; default writes to stdout.
- Idempotent: running on already-migrated YAML is a no-op.

#### Multi-dep template aggregates
- `build_context` now synthesizes `_deps` (JSON map of dep_id → output)
  and `_dep_worktrees` (JSON map of dep_id → worktree path) when a node
  has 2+ dependencies. Lets multi-dep templates iterate inputs
  generically without hardcoding dep names.

### Changed
- The `evaluation:` block on a Node is the only way to attach gate logic
  (the alias `quality_gate:` was dropped in Stage 3b).
- `Node.config:` is mutually exclusive with `prompt_file:` / `execution:`
  on the same node (a node defines either an atom or a subgraph
  reference, not both).

### Documentation
- New: `docs/plans/stage-5a-route-node.md` — the next-stage plan for the
  `route` node primitive (simpleeval predicates, Command(goto), zero
  baked-in retry/halt semantics).
- New: `docs/plans/stage-5b-execute-url.md` — proposed redesign that
  collapses today's seven execution shapes (`prompt_file:` shorthand,
  `execution: { type: prompt | command | gate_only | join }`, top-level
  `config: + inputs: + outputs:`, `fan_out.template.prompt_file`) into a
  single `execute: { url, params }` block dispatched by URL extension.

### Notes on Stage 4c demo

The original Stage-4 plan called for `reviewer_pool` (in `examples/
absurd-paper/workflow.yaml`) to be carved into a per-child subgraph as
the Stage 4c demo. After landing the carve, two things became clear:

1. `reviewer_pool` is N-way parallelism over a manifest of single-prompt
   reviewers — that's exactly what the existing fan-out already does;
   wrapping each child in a subgraph reference adds nothing.
2. `paper` (the multi-step reconcile → persist → check chain) is the
   right shape for subgraph composition — multiple sequential nodes
   that compose into one logical unit.

Stage 4c therefore ships with `paper` as the canonical demo (top-level
`Node.config:`). The fan-out path stays inline-prompt-only by design;
when a real consumer needs multi-step per-child templates, that's the
right time to extend `FanOutTemplate` (queued under Stage 5b's broader
unified-execution-shape redesign).

### Removed
- Token usage tracking. The `tokens_used` field on `ExecutionResult`,
  the `token_usage` channel on `WorkflowState`, the per-phase token
  capture in `_ACPCallbacks`, the JSONL `tokens` field on
  `node_completed` events, and the CLI `Tokens:` summary line are all
  gone. Cost visibility was a documented backlog item that wasn't
  pulling its weight; if it returns later, the dispatch path needs
  redesign anyway under Stage 5b.
