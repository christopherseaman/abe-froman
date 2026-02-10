# Phase 6: Dynamic Subphase Fan-Out

## Goal

Enable phases to dynamically spawn N subphases at runtime based on a manifest, then aggregate results through final phases before continuing the workflow.

## Problem

Static workflow graphs can't handle cases where the number of work items isn't known until a previous phase runs. For example: a research phase discovers 5 topics, and each needs its own analysis subphase.

## Design

### Schema Additions (`schema/models.py`)

Three new models support dynamic configuration:

- **`SubphaseTemplate`** — defines the prompt file (and optional quality gate) applied to each manifest item
- **`FinalPhase`** — a phase that runs after all subphases complete, used for aggregation/summarization; supports `prompt_file` shorthand like regular phases
- **`DynamicPhaseConfig`** — container with `enabled`, `manifest_path`, `template`, and `final_phases`

These attach to `Phase` via an optional `dynamic_subphases` field.

### State Addition (`engine/state.py`)

One new field:

- **`subphase_outputs: Annotated[dict[str, Any], _merge_dicts]`** — tracks subphase results separately from `phase_outputs`, enabling final phases to aggregate them by parent prefix

### Graph Construction (`engine/builder.py`)

#### Manifest Reading

`_read_manifest(state, phase)` resolves manifest items at runtime:
1. Tries parsing the parent phase's output as JSON (looking for `{"items": [...]}` or a bare list)
2. Falls back to reading `manifest_path` from disk
3. Returns `[]` if neither source provides items

#### Node Types

| Node | ID Pattern | Purpose |
|------|-----------|---------|
| Parent phase | `{phase.id}` | Normal phase node; executes and produces manifest |
| Template subphase | `_sub_{phase.id}` | Single node dispatched N times via `Send` |
| Final phase | `_final_{phase.id}_{final.id}` | Aggregation node(s) after all subphases complete |

#### Fan-Out via LangGraph Send

`_make_dynamic_router()` creates a conditional edge function that:
1. Checks the parent's quality gate (if any) — routes to `retry` or `fail` as needed
2. On pass: reads the manifest and returns `[Send(template_node_id, {**state, "_subphase_item": item}) for item in items]`
3. On empty manifest: routes to `no_items` (first final phase or downstream/END)

Each `Send` invocation injects the manifest item into state as `_subphase_item`, giving the template node access to item fields as template variables.

#### Subphase Node

`_make_subphase_node()` creates the template node:
- Reads `_subphase_item` from state to get the manifest item
- Constructs a synthetic `Phase` with ID `{parent_id}::{item_id}`
- Builds context from parent output + all item fields as template variables
- Executes via the standard executor
- Records output in both `phase_outputs` and `subphase_outputs`
- Evaluates template quality gate if configured (non-retrying, score-only)

#### Final Phase Nodes

`_make_final_phase_node()` wraps `_make_phase_node`:
- Collects all `subphase_outputs` with prefix `{parent_id}::`
- Injects them as `{parent_id}_subphases` in `phase_outputs` (JSON-serialized dict)
- Delegates to the standard phase node for execution and gate logic

#### Edge Wiring

The `exit_node` map determines what downstream phases connect to:
- **Dynamic phase with finals**: downstream connects from last final node
- **Dynamic phase without finals**: downstream connects from template node
- **Non-dynamic phase**: downstream connects from the phase itself

Internal wiring for dynamic phases:
```
parent → [conditional: gate check + Send fan-out]
  ├─ retry → parent (re-execute)
  ├─ fail → END
  ├─ no_items → first final or downstream or END
  └─ Send × N → _sub_{id} → _final_{id}_{f1} → _final_{id}_{f2} → ... → downstream/END
```

## Test Coverage (`tests/test_dynamic.py`)

18 tests across 5 test classes, all using real subprocess execution:

### TestDynamicFanOut
- `test_basic_fan_out` — 3-item manifest produces 3 completed subphases
- `test_subphase_outputs_recorded` — outputs appear in both `phase_outputs` and `subphase_outputs`
- `test_single_item_manifest` — single-item fan-out works correctly

### TestFinalPhases
- `test_final_phase_runs_after_subphases` — final phase completes after subphases
- `test_chained_final_phases` — multiple finals execute sequentially

### TestDownstreamWiring
- `test_downstream_waits_for_dynamic_parent` — dependent phase runs after finals complete
- `test_downstream_without_finals` — dependent phase wires from template node when no finals

### TestDynamicGates
- `test_parent_gate_pass_fans_out` — passing gate triggers fan-out
- `test_parent_gate_fail_blocks_fanout` — blocking gate prevents subphase execution
- `test_template_gate_scores_recorded` — per-subphase gate scores stored

### TestDynamicEdgeCases
- `test_empty_manifest_skips_to_end` — empty manifest skips subphases
- `test_dry_run_traces_subphases` — dry run traces parent without fan-out
- `test_disabled_dynamic_builds_normally` — `enabled: false` builds as normal phase
- `test_manifest_from_disk` — falls back to `manifest_path` when output isn't JSON

## Key Design Decisions

1. **LangGraph `Send` for fan-out** — native mechanism for runtime-determined parallelism; avoids pre-building N nodes
2. **Manifest from output vs. disk** — flexible sourcing; phase output is tried first for pure-pipeline workflows, disk fallback for pre-computed manifests
3. **Template gates are non-retrying** — subphase gates record scores but don't retry; keeps fan-out simple and avoids per-subphase retry loops
4. **Subphase ID format `parent::item`** — clear provenance, easy filtering by prefix
5. **`subphase_outputs` separate from `phase_outputs`** — enables final phases to aggregate subphase results without polluting the main output namespace
6. **`exit_node` map pattern** — single abstraction for downstream wiring regardless of whether a phase is dynamic, gated, or plain

## Files Changed

| File | Change |
|------|--------|
| `src/abe_froman/engine/state.py` | Added `subphase_outputs` field + initializer |
| `src/abe_froman/engine/builder.py` | Added `_read_manifest`, `_make_subphase_node`, `_make_dynamic_router`, `_make_final_phase_node`; refactored `build_workflow_graph` for dynamic+gated edge wiring |
| `tests/test_dynamic.py` | New test file, 14 tests covering fan-out, finals, wiring, gates, edge cases |
