# Design: promote conflict modes, `base_directory` promote glob, `LlmPreset.env`

**Date:** 2026-06-17
**Status:** Approved (design); pending implementation plan
**Origin:** `../samus-ai/builder-sqrlly/SQRLLY_REQUEST.md` — feature requests
from the builder-sqrlly port. This spec covers the "Steps 1 & 2" subset that
was approved for implementation now. Requests #2 (skip-completed resume) and #3
(share gitignored deps into worktrees) are **deferred** pending a separate
design review of opinionated defaults for resume + worktree behavior.

## Motivation

Three independently-verified findings against current sqrlly (`b2207a7`, 0.5.10):

- **#1 (Critical):** the promote loop applies each promoting node's git delta in
  sequence with no cross-node awareness. Two same-wave branches that both edit
  the same path silently clobber each other; `failed_nodes` is computed before
  the loop and a clobber is a *successful copy*, so the run reports green while
  losing work.
- **#4 (Medium):** `output_contract` has two consumers that disagree. The
  existence check prepends `base_directory`; the promote glob uses
  `required_files` raw. A contract with a non-`.` `base_directory` **passes the
  check but promotes nothing**.
- **#5a (Minor):** there is no way to set per-node environment variables for LLM
  nodes (e.g. `CLAUDE_CODE_EFFORT_LEVEL`). `cli_args` is the only escape hatch
  and is dropped under `transport: acp`. Script/exec nodes already have
  `SubprocessParams.env`; LLM nodes have no equivalent.

## Scope

In scope: #1, #4, #5a. Out of scope: #2, #3 (deferred), #5b (provider lock-in →
existing TODO 36), #5c (realized-graph view → separate enhancement; also a stale
CLAUDE.md "Known limitations" bullet to correct later).

---

## A. Promote conflict modes (#1)

### Decision

- Default behavior is **`warn`**: detect overlaps, emit a loud report naming the
  conflicting paths and their owning nodes, then proceed last-write-wins. The
  run stays green but the loss is visible, not silent.
- Behavior is **configurable at the workflow level** via a new `Settings` field
  across four modes: `fail | warn | overwrite | skip`.

| Mode | Behavior |
|---|---|
| `fail` | Abort the run with an error naming conflicting paths + owners. Raised **before any write** (discover-first), so nothing is half-promoted. Non-zero exit. |
| `warn` (default) | Log the conflict report, then proceed last-write-wins (later node in `config.nodes` order wins). Run stays green. |
| `overwrite` | Proceed last-write-wins **silently** — today's exact behavior, now opt-in. |
| `skip` | **First-writer-wins**: the first owning node (in `config.nodes` order) keeps the path; later owners drop that path. Each later node's *non-conflicting* paths still promote. |

A path appearing in ≥2 nodes' footprints is a conflict. Deletions are footprints
too (`discover_changes` returns them), so a delete-vs-edit on the same path is a
conflict.

### Schema

`schema/models.py`, `Settings`:

```python
on_promote_conflict: Literal["fail", "warn", "overwrite", "skip"] = "warn"
```

### Runtime refactor — `runtime/promote.py`

Split `promote()` into composable seams and add a pure planner:

- **`discover_changes(worktree, globs)`** — unchanged (already exists).
- **`apply_changes(worktree, base, changes) -> list[str]`** — extracted from the
  back half of `promote()` (the `copy2`/`unlink` loop). Returns applied paths.
- **`promote(worktree, base, globs=None)`** — kept as
  `apply_changes(worktree, base, discover_changes(worktree, globs))` for
  single-source callers / back-compat.
- **`plan_promotions(footprints, mode) -> PromotionPlan`** — **pure**, langgraph-
  free. Input: `{node_id: {path: kind}}` (discovery order preserved) + mode.
  Output: a plan giving each node its allowed paths plus a conflict report;
  raises `PromoteConflictError` when `mode == "fail"` and conflicts exist.
  Unit-testable in isolation (no git, no fs).

`PromotionPlan` / `PromoteConflictError` are small dataclasses/exceptions defined
in `promote.py`.

### CLI loop — `cli/main.py:432–446`

Restructure from apply-as-you-go into **discover → plan → apply**:

1. For each promoting top-level node with a worktree, `discover_changes(tree,
   globs)` (globs from the #4 helper). Collect `{node_id: changes}` in
   `config.nodes` order; remember each node's `tree`.
2. `plan = plan_promotions(footprints, settings.on_promote_conflict)`.
   - `fail` + conflicts → raises; propagates like the existing promotion-error
     path (data-loss path), nothing applied.
   - `warn` + conflicts → emit the report to stderr.
3. For each node, `apply_changes(tree, workdir, plan.allowed[node_id])`
   (for `skip`, `allowed` is the node's changes minus paths already claimed by an
   earlier node; for the other modes it is the node's full footprint).

Layer rules unaffected: `cli` already imports `runtime.promote`; the planner is
pure runtime.

---

## B. `base_directory` honored by the promote glob (#4)

### Decision — single shared derivation, no drift

Extract one helper and route **both** `output_contract` consumers through it.

`runtime/gates.py`:

```python
def contract_required_paths(contract: OutputContract) -> list[str]:
    """Required files as paths relative to the workdir (base_directory prepended)."""
    return [(Path(contract.base_directory) / f).as_posix()
            for f in contract.required_files]
```

- **`validate_output_contract`** (existence check) uses it: each returned path is
  tested with `(Path(workdir) / rel).exists()` and reported raw when missing.
- **Promote glob** (`cli/main.py`):
  `globs = contract_required_paths(node.output_contract) if node.output_contract
  else None`.

`Path("." ) / "x.json"` → `x.json`, so `base_directory: "."` (the default)
collapses to the bare filename — no regression for root-relative contracts.

### Ordering

B lands before A. The #1 detector computes footprints from the promote globs; if
those globs are still `base_directory`-blind, detection and promotion disagree
about what each node touches.

---

## C. `LlmPreset.env` (#5a)

### Decision — preset-level env, both transports

Backends are constructed once per preset, so env belongs on the preset. Per-node
tuning is achieved with preset variants selected via `params.preset` (the
idiomatic per-node knob in sqrlly). Confirmed feasible on both transports:
`asyncio.create_subprocess_exec` takes `env=`, and the ACP SDK's
`spawn_agent_process(..., env: Mapping[str,str] | None = None, ...)` does too.

### Schema

`schema/models.py`, `LlmPreset`:

```python
env: dict[str, str] = {}
```

### Plumbing

- `factory._build_cli(preset)` → `CLIBackend(..., env=preset.env)`.
- `factory._build_acp(preset)` → `ACPBackend(..., env=preset.env)`.
- `CLIBackend.__init__(..., env: dict[str,str] | None = None)`; in `send_prompt`,
  pass `env=` to `create_subprocess_exec`.
- `ACPBackend.__init__(..., env: dict[str,str] | None = None)`; in
  `_ensure_initialized`, pass `env=` to `spawn_agent_process`.
- The merged value is `{**os.environ, **self._env}` when `self._env` is non-empty,
  else `None`. **Overlay, never replace** — preserves `PATH`; empty env passes
  `None` so today's exact inherit behavior is unchanged. This one-line expression
  is **inlined at each spawn site**, not extracted into a shared helper —
  mirroring the codebase's deliberate choice to duplicate the equally-trivial
  `_await_with_timeout` across `cli.py`/`acp.py` rather than share a module
  (`cli.py:30`). `cli.py` gains an `import os` (`acp.py` already imports it).

---

## Testing

Per project testing principles (real subprocess / real fs, known-good AND
known-bad, assert specific output — never absence-of-error).

- **#1 — `plan_promotions` (pure, parametrized):**
  - disjoint footprints → no conflict, every path allowed (regression guard).
  - overlapping footprints × {`fail` raises; `warn` report + last-writer allowed;
    `overwrite` last-writer allowed, no report; `skip` first-writer keeps path,
    later owner's other paths still allowed}.
- **#1 — integration (two real worktrees editing the same path):**
  - `fail` → run exits non-zero, base path content unchanged (nothing applied).
  - `warn` → warning text captured naming path + nodes; base has the later
    node's content; run green.
  - `skip` → base has the first node's content; the later node's non-conflicting
    file is present.
  - disjoint footprints → both nodes' files land (regression guard).
- **#4:** a node with `base_directory: reference`, `required_files: [x.json]`,
  `promote: true` promotes `reference/x.json` into base (would have promoted
  nothing before). Unit test on `contract_required_paths` (known-good non-`.`
  base; known-good `.` base collapses to bare name).
- **#5a:** real subprocess — `CLIBackend(argv_prefix=("printenv","FOO"),
  env={"FOO":"bar"})` → `send_prompt` output contains `bar`; empty-env known-bad
  (printenv exits non-zero / value absent). ACP env verified by construction
  wiring (backend `_env` carries `preset.env`); end-to-end exercised by the acp
  suite when run.

## Files touched

| File | Change |
|---|---|
| `schema/models.py` | `Settings.on_promote_conflict`; `LlmPreset.env` |
| `runtime/promote.py` | extract `apply_changes`; add `plan_promotions`, `PromotionPlan`, `PromoteConflictError` |
| `runtime/gates.py` | `contract_required_paths`; route `validate_output_contract` through it |
| `cli/main.py` | promote loop → discover/plan/apply; globs via helper |
| `runtime/executor/backends/factory.py` | pass `preset.env` to both builders |
| `runtime/executor/backends/cli.py` | `env` ctor param + merged `env=` in `send_prompt` |
| `runtime/executor/backends/acp.py` | `env` ctor param + `env=` in `spawn_agent_process` |
| `SCHEMA.md` | document `on_promote_conflict`, `LlmPreset.env`, base-relative `required_files` for promote |
| `CHANGELOG.md` | entries for the three changes |
| `tests/` | as above |

Layer boundaries preserved (`tests/architecture/test_layers.py`): cli→runtime
only; the planner and helpers are langgraph-free runtime/schema code.

## Out of scope / follow-ups

- #2 skip-completed resume, #3 worktree dep sharing — deferred pending a defaults
  design review.
- #5b provider routing — existing TODO 36.
- #5c realized-graph view + the stale CLAUDE.md "view doesn't draw route edges"
  bullet — separate enhancement.
