# Native promotion of fan-out branch worktrees (#7) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A worktree-isolated fan-out can merge its branch worktree deltas back to base natively via `fan_out.promote: true`, routed through the existing `reconcile_promotions` machinery.

**Architecture:** Branch worktrees are already recorded in `result["node_worktrees"]` keyed `<parent>::<item>` (`compile/dynamic.py:217`, `runtime/state.py:64`). Add `promote: bool` to the `FanOut` schema; a Graph-free helper `fanout_branch_specs()` in `runtime/promote.py` turns the promote-true parents' branch worktrees into promote specs; the `cli/main.py` promote loop extends its existing `specs` list with them, so they flow through the same `reconcile_promotions` + `on_promote_conflict` + `promote_exclude` path (inheriting #1's conflict detection). Promotion already runs BEFORE `reclaim()`, so there is no GC race for the in-CLI path.

**Tech Stack:** Python 3.11+, Pydantic, Click, git worktrees, pytest.

## Global Constraints

- Python `>=3.11`; use `sys.executable` for subprocess where a Python interpreter is needed in tests; the `python` binary is unavailable.
- Layer rules (`tests/architecture/test_layers.py`): `runtime/` must not import `compile/` or `langgraph`; `runtime/promote.py` may import `schema/` but the new helper must stay Graph-free (take primitives, not a `Graph`).
- No mocks of external systems; tests use real git + real `ForemanExecutor` + `DispatchExecutor`. `MockExecutor` is the only sanctioned `NodeExecutor` double.
- Conventional-commit messages; no attribution trailers.
- Reuse `reconcile_promotions` / `discover_changes` / `apply_changes` as-is — no changes to their signatures.

---

### Task 1: Schema — `FanOut.promote`

**Files:**
- Modify: `src/sqrlly/schema/models.py` (the `FanOut` model)
- Test: `tests/unit/schema/test_schema.py` (extend)

**Interfaces:**
- Produces: `FanOut.promote: bool` (default `False`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/schema/test_schema.py — add
def test_fan_out_promote_field():
    from sqrlly.schema.models import FanOut, FanOutTemplate, Execute
    fo = FanOut(template=FanOutTemplate(execute=Execute(url="x.md")), promote=True)
    assert fo.promote is True
    # default is False
    assert FanOut(template=FanOutTemplate(execute=Execute(url="x.md"))).promote is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/schema/test_schema.py::test_fan_out_promote_field -q`
Expected: FAIL — `FanOut` has no field `promote` (`extra=forbid` → ValidationError).

- [ ] **Step 3: Implement** — add the field to `FanOut` in `src/sqrlly/schema/models.py` (the model has `manifest_path`, `template`, `final_nodes`):

```python
    final_nodes: list[FanOutFinalNode] = []
    # When true, each Send branch's worktree delta is promoted back to base
    # at the end of a clean run (top-level nodes only, before GC), routed
    # through reconcile_promotions + on_promote_conflict + promote_exclude.
    # Distinct from Node.promote (which promotes a node's OWN worktree —
    # for a fan-out parent that is the manifest-only tree; see the lint).
    promote: bool = False
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/schema/test_schema.py::test_fan_out_promote_field -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/schema/models.py tests/unit/schema/test_schema.py
git commit -m "feat: FanOut.promote — opt-in promotion of fan-out branch worktrees"
```

---

### Task 2: Helper — `fanout_branch_specs`

**Files:**
- Modify: `src/sqrlly/runtime/promote.py` (add the helper)
- Test: `tests/unit/runtime/test_promote.py` (extend)

**Interfaces:**
- Consumes: nothing from earlier tasks at runtime (takes primitives).
- Produces: `fanout_branch_specs(promote_parents: set[str], node_worktrees: dict[str, str]) -> list[tuple[str, str, list[str] | None]]` — one `(child_id, worktree_path, None)` spec per branch worktree (`node_worktrees` key `<parent>::<item>`) whose parent is in `promote_parents`. `None` globs = full delta (no per-template `output_contract`); `promote_exclude` still applies downstream. Stable order (sorted by child_id) for deterministic conflict reporting.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/runtime/test_promote.py — add
from sqrlly.runtime.promote import fanout_branch_specs

def test_fanout_branch_specs_selects_promote_parents():
    node_worktrees = {
        "build::feat_a": "/wt/build__feat_a",
        "build::feat_b": "/wt/build__feat_b",
        "plan::v1": "/wt/plan__v1",     # parent not promoting
        "toplevel_node": "/wt/toplevel", # not a branch (no ::)
    }
    specs = fanout_branch_specs({"build"}, node_worktrees)
    assert specs == [
        ("build::feat_a", "/wt/build__feat_a", None),
        ("build::feat_b", "/wt/build__feat_b", None),
    ]

def test_fanout_branch_specs_empty_when_no_promote_parents():
    assert fanout_branch_specs(set(), {"build::a": "/wt/a"}) == []
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/runtime/test_promote.py::test_fanout_branch_specs_selects_promote_parents -q`
Expected: FAIL — `ImportError: cannot import name 'fanout_branch_specs'`.

- [ ] **Step 3: Implement** — add to `src/sqrlly/runtime/promote.py`:

```python
def fanout_branch_specs(
    promote_parents: set[str], node_worktrees: dict[str, str],
) -> list[tuple[str, str, list[str] | None]]:
    """Promote specs for the branch worktrees of the given fan-out parents.

    ``node_worktrees`` (workflow state) records each Send branch's worktree
    keyed ``<parent_id>::<item_id>``. Each branch promotes its FULL delta
    (``None`` globs — a fan-out template has no ``output_contract``);
    ``promote_exclude`` still filters downstream. Returned sorted by child
    id for deterministic conflict ordering. Empty when no parent opts in.
    """
    specs: list[tuple[str, str, list[str] | None]] = []
    for child_id in sorted(node_worktrees):
        parent = child_id.split("::", 1)[0]
        if "::" in child_id and parent in promote_parents:
            specs.append((child_id, node_worktrees[child_id], None))
    return specs
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/runtime/test_promote.py -q -k fanout_branch_specs`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/runtime/promote.py tests/unit/runtime/test_promote.py
git commit -m "feat: fanout_branch_specs — promote specs for fan-out branch worktrees"
```

---

### Task 3: Wire into the CLI promote loop + end-to-end test

**Files:**
- Modify: `src/sqrlly/cli/main.py` (the promote loop — currently builds `specs` from `config.nodes`, then calls `reconcile_promotions`)
- Test: `tests/e2e/test_fanout_promote.py` (create)

**Interfaces:**
- Consumes: `FanOut.promote` (Task 1), `fanout_branch_specs` (Task 2).

**Context:** The promote loop already exists in `cli/main.py` (inside `_execute_workflow`, after the run): it builds `specs: list[tuple[str, str, list[str] | None]]` from top-level promoting nodes, then `reconcile_promotions(specs, workdir, config.settings.on_promote_conflict, excludes=...)`. You add the fan-out branch specs to that same `specs` list before the `if specs:` reconcile call. `result` (the final workflow state) is in scope and carries `node_worktrees`.

- [ ] **Step 1: Write the failing e2e test**

Model the fixture on `tests/e2e/test_subgraph_fanout_worktree.py` (git repo + a subgraph fan-out with `/bin/sh` script nodes — no LLM, deterministic) and `tests/unit/runtime/test_promote.py` (git assertions). Create `tests/e2e/test_fanout_promote.py`:

```python
"""fan_out.promote merges branch worktree deltas back to base.

Git workdir + a subgraph fan-out (2 items) where each branch writes a
distinct tracked file into its branch worktree; with fan_out.promote:true
the deltas land in the base workdir after a clean run. Pure /bin/sh nodes
— no LLM, deterministic. Models tests/e2e/test_subgraph_fanout_worktree.py.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
import yaml

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.foreman import ForemanExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph
from sqrlly.runtime.promote import fanout_branch_specs, reconcile_promotions


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.mark.asyncio
async def test_fan_out_promote_merges_branch_deltas(tmp_path):
    base = tmp_path
    _git("init", "-b", "main", ".", cwd=base)
    _git("config", "user.email", "t@t", cwd=base)
    _git("config", "user.name", "t", cwd=base)
    (base / "seed.txt").write_text("seed")
    _git("add", ".", cwd=base); _git("commit", "-qm", "init", cwd=base)

    # A branch worker subgraph: writes out/<item>.txt in its worktree.
    sub = base / "worker.yaml"
    sub.write_text(yaml.safe_dump({
        "name": "worker", "version": "0.1.0",
        "nodes": [{
            "id": "write", "name": "write",
            "execute": {
                "url": "/bin/sh",
                "params": {"args": ["-c", "mkdir -p out && echo {{item}} > out/{{item}}.txt"]},
            },
        }],
        "settings": {},
    }))
    manifest = base / "items.json"
    manifest.write_text(json.dumps([{"id": "a", "item": "a"}, {"id": "b", "item": "b"}]))

    cfg = Graph(**{
        "name": "fp", "version": "0.1.0",
        "nodes": [{
            "id": "build", "name": "build",
            "fan_out": {
                "manifest_path": "items.json",
                "template": {"execute": {"url": "worker.yaml"}},
                "promote": True,
            },
        }],
        "settings": {"worktree": "isolated"},
    })

    executor = ForemanExecutor(DispatchExecutor(workdir=str(base)), base_workdir=str(base))
    graph = build_workflow_graph(cfg, executor)
    result = await graph.ainvoke(make_initial_state(workflow_name="fp", workdir=str(base)))

    # Replicate the CLI promote step the test is proving end-to-end.
    promote_parents = {n.id for n in cfg.nodes if n.fan_out and n.fan_out.promote}
    specs = fanout_branch_specs(promote_parents, result.get("node_worktrees", {}))
    assert len(specs) == 2, f"expected 2 branch specs, got {specs}"
    reconcile_promotions(specs, str(base), cfg.settings.on_promote_conflict, excludes=None)
    await executor.close()

    # Branch deltas are now in the base workdir.
    assert (base / "out" / "a.txt").read_text().strip() == "a"
    assert (base / "out" / "b.txt").read_text().strip() == "b"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/e2e/test_fanout_promote.py -q`
Expected: FAIL — the assertion `len(specs) == 2` or the base-file reads fail until branch worktrees promote. If it errors instead (fixture wiring), fix the fixture against `tests/e2e/test_subgraph_fanout_worktree.py` until the failure is the promote assertion, not a setup error. Report the failure reason.

- [ ] **Step 3: Wire the CLI promote loop** — in `src/sqrlly/cli/main.py`, immediately after the `for node in config.nodes:` loop that appends top-level `specs` and BEFORE the `if specs:` block, add:

```python
                # Fan-out branch worktrees opt in via fan_out.promote — route
                # them through the same reconcile path (inherits conflict
                # detection + promote_exclude). They are recorded in
                # node_worktrees keyed <parent>::<item>.
                promote_parents = {
                    n.id for n in config.nodes
                    if n.fan_out is not None and n.fan_out.promote
                }
                specs.extend(fanout_branch_specs(
                    promote_parents, result.get("node_worktrees", {}),
                ))
```

Add `fanout_branch_specs` to the existing `from sqrlly.runtime.promote import ...` line in `cli/main.py` (which already imports `PromoteConflictError, reconcile_promotions`).

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/e2e/test_fanout_promote.py -q`
Expected: PASS. (The test proves the helper+reconcile path; the inline `specs`/reconcile in the test mirrors the CLI wiring, and the CLI edit makes the real `sqrlly run` do the same.)

- [ ] **Step 5: Run the architecture + promote suites**

Run: `uv run pytest tests/architecture/test_layers.py tests/unit/runtime/test_promote.py -q`
Expected: PASS (no layer violation; `cli/` importing `runtime/promote` is allowed).

- [ ] **Step 6: Commit**

```bash
git add src/sqrlly/cli/main.py tests/e2e/test_fanout_promote.py
git commit -m "feat: promote fan-out branch worktrees via fan_out.promote in the CLI run loop"
```

---

### Task 4: Lint warning — `node.promote` on a fan-out parent

**Files:**
- Modify: `src/sqrlly/compile/lint.py` (add a warning function + register it in `collect_warnings`)
- Test: `tests/unit/compile/test_lint.py` (extend)

**Interfaces:**
- Consumes: `FanOut.promote` (Task 1).

**Context:** `compile/lint.py::collect_warnings(config)` aggregates sub-functions (`_advisory_gate_warnings`, `_worktree_setup_exclude_warnings`, `_hyphenated_id_warnings`). A node with a `fan_out` block AND `node.promote: true` is a footgun — `Node.promote` promotes the node's OWN worktree, which for a fan-out parent is the manifest-only tree (no branch deltas). Steer the author to `fan_out.promote`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/compile/test_lint.py — add
def test_fanout_parent_promote_warns():
    from sqrlly.compile.lint import collect_warnings
    from sqrlly.schema.models import Graph
    cfg = Graph(**{
        "name": "t", "version": "0.0.0",
        "nodes": [{
            "id": "build", "name": "build", "promote": True,
            "fan_out": {"manifest_path": "m.json",
                        "template": {"execute": {"url": "w.yaml"}}},
        }],
        "settings": {},
    })
    warns = collect_warnings(cfg)
    assert any("fan_out.promote" in w and "build" in w for w in warns)

def test_fanout_promote_no_warn():
    from sqrlly.compile.lint import collect_warnings
    from sqrlly.schema.models import Graph
    cfg = Graph(**{
        "name": "t", "version": "0.0.0",
        "nodes": [{
            "id": "build", "name": "build",
            "fan_out": {"manifest_path": "m.json",
                        "template": {"execute": {"url": "w.yaml"}},
                        "promote": True},
        }],
        "settings": {},
    })
    assert not any("fan_out.promote" in w for w in collect_warnings(cfg))
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/compile/test_lint.py -q -k fanout`
Expected: FAIL — no such warning yet.

- [ ] **Step 3: Implement** — add to `src/sqrlly/compile/lint.py` and register in `collect_warnings`:

```python
def _fanout_parent_promote_warnings(config: Graph) -> list[str]:
    out: list[str] = []
    for node in config.nodes:
        if node.fan_out is not None and node.promote:
            out.append(
                f"node {node.id!r}: promote on a fan-out parent promotes only "
                f"its (manifest-only) worktree, not the branch worktrees — set "
                f"fan_out.promote to merge the branch deltas back to base."
            )
    return out
```

In `collect_warnings`, add `out.extend(_fanout_parent_promote_warnings(config))` alongside the existing sub-function calls.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/compile/test_lint.py -q -k fanout`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/compile/lint.py tests/unit/compile/test_lint.py
git commit -m "feat: lint — steer node.promote on a fan-out parent to fan_out.promote"
```

---

### Task 5: Docs + CHANGELOG + TODO

**Files:**
- Modify: `SCHEMA.md`, `CLAUDE.md`, `SKILLS.md`, `CHANGELOG.md`, `TODO.md`

**Interfaces:** none.

- [ ] **Step 1: SCHEMA.md** — in the `FanOut` field table (search the `fan_out` / `FanOut` section), add a row:

```markdown
| `promote` | `bool` | `false` | When true, each Send branch's worktree delta is promoted back to base at the end of a clean run (top-level nodes only, before GC), through `reconcile_promotions` + `on_promote_conflict` + `promote_exclude` — the native merge-back for an isolated parallel build. Distinct from `Node.promote` (which promotes the parent's manifest-only tree; a `validate` lint steers you here). |
```

- [ ] **Step 2: CLAUDE.md** — in "Known limitations", update/add the fan-out-promote note (long line, matching the file):

```markdown
- **Fan-out branch promotion is `fan_out.promote`, and external promote needs `worktree_gc: never`** — `fan_out.promote: true` merges each Send branch's worktree delta back to base at end-of-run via the shared `reconcile_promotions` path (before GC, so no race for the in-CLI path). A promote done OUTSIDE the run (a runner script after `sqrlly run` returns) must set `settings.worktree_gc: never`: with `on_success`, `reclaim()` `git worktree remove`s the branch trees before the process exits, so an external promote finds nothing and silently no-ops (silent data loss).
```

- [ ] **Step 3: SKILLS.md** — near the worktree/promote authoring guidance, add a line (hard-wrapped to match the file):

```markdown
A worktree-isolated **fan-out** merges its branch deltas back to base with
`fan_out.promote: true` (routed through `on_promote_conflict` /
`promote_exclude`, same as a top-level `promote: true`). Put `promote` on
the `fan_out:` block, not the parent node — `node.promote` on a fan-out
parent only promotes its manifest-only worktree (a `validate` lint warns).
```

- [ ] **Step 4: CHANGELOG.md** — add a new `## [Unreleased]` → `### Added` block above the top released section:

```markdown
## [Unreleased]

### Added

- `fan_out.promote: true` natively promotes each Send branch's worktree delta back to base at end-of-run, through the existing `reconcile_promotions` + `on_promote_conflict` + `promote_exclude` machinery — the native merge-back path for a worktree-isolated parallel build (previously only top-level `promote: true` nodes were promoted; branch worktrees had no native merge path). A `validate` lint steers `node.promote` on a fan-out parent toward `fan_out.promote`.
```

- [ ] **Step 5: TODO.md** — rewrite the `### B7 …` heading + body to the SHIPPED style (matching `### ✅ B6 … — SHIPPED 0.7.0`): `### ✅ B7 — Native fan-out branch promotion — SHIPPED 0.7.4`, body trimmed to current state (names `fan_out.promote`, the reconcile reuse, and the `worktree_gc: never` external-promote note).

- [ ] **Step 6: Sanity + commit**

```bash
uv run sqrlly validate examples/jokes/workflow.yaml   # still Valid
git add SCHEMA.md CLAUDE.md SKILLS.md CHANGELOG.md TODO.md
git commit -m "docs: document fan_out.promote; mark B7 shipped"
```

---

## Final verification (after all tasks)

```bash
uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -q   # full core suite green
uv run pytest tests/e2e/test_fanout_promote.py -q               # the feature e2e
```
Expected: full suite passes; fan-out branch promotion proven end-to-end.
