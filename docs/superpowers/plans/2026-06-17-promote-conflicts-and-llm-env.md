# Promote Conflict Modes, base_directory Promote Glob, LlmPreset.env — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Detect/handle cross-node promote conflicts (configurable `fail|warn|overwrite|skip`, default `warn`), make the promote glob honor `output_contract.base_directory`, and add a per-preset `LlmPreset.env` map for LLM nodes.

**Architecture:** A pure planner (`plan_promotions`) reconciles per-node promote footprints; a thin orchestrator (`reconcile_promotions`) discovers→plans→applies; the CLI loop calls it. The `output_contract` path derivation moves onto the model (`OutputContract.required_paths()`) so the existence check and the promote glob share one source. `env` threads from `LlmPreset` through the factory into each backend's subprocess spawn, overlaid on `os.environ`.

**Tech Stack:** Python 3.14, Pydantic v2, LangGraph, `uv` + `pytest`, real `git worktree` / real subprocess in tests (no mocks).

**Spec:** `docs/superpowers/specs/2026-06-17-promote-conflicts-and-llm-env-design.md`

**Refinements from the spec (decided during planning):**
- The `#4` helper is realized as a **method** `OutputContract.required_paths()` (single home on the model; both consumers call it) rather than a free function in `gates.py` — strictly more DRY, no cross-module import.
- The promote loop is made testable by extracting `reconcile_promotions` into `promote.py` (runtime), keeping `cli/main.py` thin glue.
- `#5a` env tests use the repo's existing fake-`#!/bin/sh`-script pattern (echo the env var) — real subprocess, consistent with `tests/unit/runtime/test_cli_backend.py`.

**Task order rationale:** Task 1 lands the `base_directory` path derivation (#4). Tasks 2–4 build the promote machinery bottom-up (refactor → pure planner → orchestrator). Task 5 adds the setting. Task 6 wires the CLI (lands #1 + the #4 glob fix together — they share `main.py`'s promote loop). Tasks 7–8 are the independent `env` feature. Task 9 is docs.

---

### Task 1: `OutputContract.required_paths()` + route the existence check through it (#4 part 1)

**Files:**
- Modify: `src/sqrlly/schema/models.py` (add `from pathlib import Path`; method on `OutputContract`, ~L277-280)
- Modify: `src/sqrlly/runtime/gates.py` (`validate_output_contract`, ~L472-482)
- Test: `tests/unit/runtime/test_contracts.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/runtime/test_contracts.py`:

```python
class TestRequiredPaths:
    def test_prepends_base_directory(self):
        contract = OutputContract(
            base_directory="reference",
            required_files=["prd-context-map.json", "sub/x.txt"],
        )
        assert contract.required_paths() == [
            "reference/prd-context-map.json",
            "reference/sub/x.txt",
        ]

    def test_dot_base_collapses_to_bare_name(self):
        contract = OutputContract(base_directory=".", required_files=["x.json"])
        assert contract.required_paths() == ["x.json"]

    def test_empty_required_files(self):
        contract = OutputContract(base_directory="reference", required_files=[])
        assert contract.required_paths() == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/runtime/test_contracts.py::TestRequiredPaths -v`
Expected: FAIL — `AttributeError: 'OutputContract' object has no attribute 'required_paths'`

- [ ] **Step 3: Add the method to `OutputContract`**

In `src/sqrlly/schema/models.py`, add the import near the other top-level imports (after `import re`):

```python
from pathlib import Path
```

Replace the `OutputContract` class body:

```python
class OutputContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_directory: str
    required_files: list[str] = []
```

with:

```python
class OutputContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_directory: str
    required_files: list[str] = []

    def required_paths(self) -> list[str]:
        """Required files as workdir-relative POSIX paths, with
        ``base_directory`` prepended. Shared by the existence check
        (`gates.validate_output_contract`) and the promote glob filter
        (`cli/main.py`) so the two consumers cannot drift. A
        ``base_directory`` of ``"."`` collapses to the bare filename."""
        return [(Path(self.base_directory) / f).as_posix()
                for f in self.required_files]
```

- [ ] **Step 4: Route `validate_output_contract` through the method**

In `src/sqrlly/runtime/gates.py`, replace:

```python
def validate_output_contract(
    contract: OutputContract,
    workdir: str,
) -> list[str]:
    """Check that all required files exist. Returns list of missing files."""
    base = Path(workdir) / contract.base_directory
    missing = []
    for f in contract.required_files:
        if not (base / f).exists():
            missing.append(str(Path(contract.base_directory) / f))
    return missing
```

with:

```python
def validate_output_contract(
    contract: OutputContract,
    workdir: str,
) -> list[str]:
    """Check that all required files exist. Returns list of missing files
    (workdir-relative, ``base_directory`` prepended)."""
    missing = []
    for rel in contract.required_paths():
        if not (Path(workdir) / rel).exists():
            missing.append(rel)
    return missing
```

- [ ] **Step 5: Run the contract tests (new + existing) to verify they pass**

Run: `uv run pytest tests/unit/runtime/test_contracts.py -v`
Expected: PASS — both `TestRequiredPaths` and the pre-existing `TestValidateOutputContract` (the `"output/missing.txt"` assertions are unchanged because `required_paths()` produces exactly those strings).

- [ ] **Step 6: Commit**

```bash
git add src/sqrlly/schema/models.py src/sqrlly/runtime/gates.py tests/unit/runtime/test_contracts.py
git commit -m "feat: OutputContract.required_paths(); route existence check through it"
```

---

### Task 2: Extract `apply_changes` from `promote()` (refactor, no behavior change)

**Files:**
- Modify: `src/sqrlly/runtime/promote.py` (`promote`, L78-91)
- Test: `tests/unit/runtime/test_promote.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/runtime/test_promote.py` (the import line at top will be expanded in a later step; for now add the symbol):

```python
def test_apply_changes_applies_a_discovered_delta(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path)
    (wt / "a.txt").write_text("changed"); (wt / "new.md").write_text("hi")
    from sqrlly.runtime.promote import apply_changes, discover_changes
    changes = discover_changes(str(wt))
    applied = apply_changes(str(wt), str(tmp_path), changes)
    assert (tmp_path / "a.txt").read_text() == "changed"
    assert (tmp_path / "new.md").read_text() == "hi"
    assert set(applied) == {"a.txt", "new.md"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/runtime/test_promote.py::test_apply_changes_applies_a_discovered_delta -v`
Expected: FAIL — `ImportError: cannot import name 'apply_changes'`

- [ ] **Step 3: Extract `apply_changes`; keep `promote` as discover+apply**

In `src/sqrlly/runtime/promote.py`, replace the `promote` function (L78-91):

```python
def promote(worktree: str, base: str, globs: list[str] | None = None) -> list[str]:
    """Apply the worktree's discovered delta onto ``base``. Adds/edits are
    copied; deletions are removed. Single-source: assumes ``base`` has not
    diverged for these paths (it is the fork point). Returns applied paths."""
    changes = discover_changes(worktree, globs)
    for rel, kind in changes.items():
        dst = Path(base) / rel
        if kind == "deleted":
            dst.unlink(missing_ok=True)
        else:
            src = Path(worktree) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return list(changes)
```

with:

```python
def apply_changes(worktree: str, base: str, changes: dict[str, str]) -> list[str]:
    """Apply a discovered ``{path: kind}`` delta onto ``base``. Adds/edits
    are copied; deletions are removed. Returns the applied paths. The
    ``changes`` map is what ``discover_changes`` returns (optionally
    filtered, e.g. by conflict reconciliation)."""
    for rel, kind in changes.items():
        dst = Path(base) / rel
        if kind == "deleted":
            dst.unlink(missing_ok=True)
        else:
            src = Path(worktree) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return list(changes)


def promote(worktree: str, base: str, globs: list[str] | None = None) -> list[str]:
    """Apply the worktree's discovered delta onto ``base`` (single-source).
    Assumes ``base`` has not diverged for these paths (it is the fork
    point). Returns applied paths."""
    return apply_changes(worktree, base, discover_changes(worktree, globs))
```

- [ ] **Step 4: Run promote tests to verify they pass**

Run: `uv run pytest tests/unit/runtime/test_promote.py -v`
Expected: PASS — the new test plus all pre-existing `test_promote_*` (behavior of `promote` is unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/runtime/promote.py tests/unit/runtime/test_promote.py
git commit -m "refactor: extract apply_changes from promote()"
```

---

### Task 3: `plan_promotions` + `PromotionPlan` + `PromoteConflictError` (pure planner)

**Files:**
- Modify: `src/sqrlly/runtime/promote.py` (add `from dataclasses import dataclass`; new symbols)
- Test: `tests/unit/runtime/test_promote.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/runtime/test_promote.py`:

```python
from sqrlly.runtime.promote import (  # noqa: E402
    PromoteConflictError,
    PromotionPlan,
    plan_promotions,
)


def _footprints():
    # writer_a and writer_b both touch shared.txt; each also touches a unique file.
    return {
        "writer_a": {"shared.txt": "added", "a.txt": "added"},
        "writer_b": {"shared.txt": "added", "b.txt": "added"},
    }


def test_plan_disjoint_has_no_conflict():
    fp = {"writer_a": {"a.txt": "added"}, "writer_b": {"b.txt": "added"}}
    plan = plan_promotions(fp, "warn")
    assert plan.conflicts == {}
    assert plan.allowed == fp


def test_plan_warn_keeps_full_footprints_and_reports_conflict():
    plan = plan_promotions(_footprints(), "warn")
    assert plan.conflicts == {"shared.txt": ["writer_a", "writer_b"]}
    assert plan.allowed["writer_a"] == {"shared.txt": "added", "a.txt": "added"}
    assert plan.allowed["writer_b"] == {"shared.txt": "added", "b.txt": "added"}


def test_plan_overwrite_matches_warn_allowance():
    assert (plan_promotions(_footprints(), "overwrite").allowed
            == plan_promotions(_footprints(), "warn").allowed)


def test_plan_skip_first_writer_keeps_conflicting_path():
    plan = plan_promotions(_footprints(), "skip")
    # writer_a (first in order) keeps shared.txt; writer_b drops it but
    # keeps its non-conflicting b.txt.
    assert plan.allowed["writer_a"] == {"shared.txt": "added", "a.txt": "added"}
    assert plan.allowed["writer_b"] == {"b.txt": "added"}
    assert plan.conflicts == {"shared.txt": ["writer_a", "writer_b"]}


def test_plan_fail_raises_before_any_decision():
    with pytest.raises(PromoteConflictError) as ei:
        plan_promotions(_footprints(), "fail")
    assert "shared.txt" in str(ei.value)
    assert ei.value.conflicts == {"shared.txt": ["writer_a", "writer_b"]}


def test_plan_fail_no_conflict_returns_plan():
    fp = {"writer_a": {"a.txt": "added"}, "writer_b": {"b.txt": "added"}}
    plan = plan_promotions(fp, "fail")
    assert isinstance(plan, PromotionPlan)
    assert plan.conflicts == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/runtime/test_promote.py -k "plan_" -v`
Expected: FAIL — `ImportError: cannot import name 'plan_promotions'`

- [ ] **Step 3: Implement the planner**

In `src/sqrlly/runtime/promote.py`, add to the imports at the top (after `import shutil`):

```python
from dataclasses import dataclass
```

Add the following after `apply_changes` / `promote`:

```python
@dataclass
class PromotionPlan:
    """Reconciliation result for multiple nodes' promote footprints.

    ``allowed`` maps ``node_id`` → the ``{path: kind}`` subset that node is
    cleared to apply. ``conflicts`` maps ``path`` → the owning ``node_id``s
    for any path claimed by more than one node (the report the caller
    surfaces to the user)."""
    allowed: dict[str, dict[str, str]]
    conflicts: dict[str, list[str]]


class PromoteConflictError(Exception):
    """Raised by ``plan_promotions`` under ``on_promote_conflict='fail'``
    when two or more nodes promote the same path. Carries the conflict map
    so the CLI can render an actionable message."""

    def __init__(self, conflicts: dict[str, list[str]]):
        self.conflicts = conflicts
        detail = "; ".join(
            f"{path} <- {', '.join(nodes)}"
            for path, nodes in sorted(conflicts.items())
        )
        super().__init__(
            f"Promote conflict ({len(conflicts)} path(s)) — the same path is "
            f"promoted by multiple nodes: {detail}"
        )


def plan_promotions(
    footprints: dict[str, dict[str, str]], mode: str,
) -> PromotionPlan:
    """Reconcile per-node promote footprints under ``mode``.

    ``footprints`` is ``{node_id: {path: kind}}`` in promote order (the
    iteration order of ``config.nodes``). A path appearing in two or more
    footprints is a conflict (deletions count — a delete-vs-edit on the
    same path collides). Modes:

    - ``fail``      → raise ``PromoteConflictError`` (before any apply).
    - ``warn``      → every node keeps its full footprint (last-write-wins);
                      ``conflicts`` rides along for the caller to log.
    - ``overwrite`` → same allowance as ``warn``; the caller stays silent.
    - ``skip``      → the first owner (promote order) keeps a conflicting
                      path; later owners drop it (other paths still apply).
    """
    owners: dict[str, list[str]] = {}
    for node_id, changes in footprints.items():
        for path in changes:
            owners.setdefault(path, []).append(node_id)
    conflicts = {p: ns for p, ns in owners.items() if len(ns) > 1}

    if conflicts and mode == "fail":
        raise PromoteConflictError(conflicts)

    if mode == "skip":
        allowed: dict[str, dict[str, str]] = {}
        claimed: set[str] = set()
        for node_id, changes in footprints.items():
            allowed[node_id] = {
                p: k for p, k in changes.items() if p not in claimed
            }
            claimed.update(changes)
        return PromotionPlan(allowed=allowed, conflicts=conflicts)

    return PromotionPlan(
        allowed={nid: dict(ch) for nid, ch in footprints.items()},
        conflicts=conflicts,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/runtime/test_promote.py -k "plan_" -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/runtime/promote.py tests/unit/runtime/test_promote.py
git commit -m "feat: plan_promotions pure planner for promote conflict modes"
```

---

### Task 4: `reconcile_promotions` orchestrator + real-worktree integration tests (#1 modes + #4 glob)

**Files:**
- Modify: `src/sqrlly/runtime/promote.py` (new `reconcile_promotions`)
- Test: `tests/unit/runtime/test_promote.py` (parametrize `_wt` for a 2nd worktree)

- [ ] **Step 1: Make `_wt` create distinctly-named worktrees**

In `tests/unit/runtime/test_promote.py`, replace the `_wt` helper:

```python
def _wt(tmp):
    dest = tmp/".sqrlly"/"wt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git","-C",str(tmp),"worktree","add","-q",str(dest),"HEAD"],check=True)
    return dest
```

with (default keeps existing single-arg callers working):

```python
def _wt(tmp, name="wt"):
    dest = tmp/".sqrlly"/name
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git","-C",str(tmp),"worktree","add","-q",str(dest),"HEAD"],check=True)
    return dest
```

- [ ] **Step 2: Write the failing integration tests**

Append to `tests/unit/runtime/test_promote.py`:

```python
from sqrlly.runtime.promote import reconcile_promotions  # noqa: E402
from sqrlly.schema.models import OutputContract  # noqa: E402


def _two_writers(tmp_path):
    """Two worktrees: both write shared.txt (different content) + a unique file."""
    _repo(tmp_path)
    wa = _wt(tmp_path, "wa"); wb = _wt(tmp_path, "wb")
    (wa/"shared.txt").write_text("AAA"); (wa/"a.txt").write_text("a")
    (wb/"shared.txt").write_text("BBB"); (wb/"b.txt").write_text("b")
    return [("writer_a", str(wa), None), ("writer_b", str(wb), None)]


def test_reconcile_warn_last_write_wins_and_reports(tmp_path):
    specs = _two_writers(tmp_path)
    plan = reconcile_promotions(specs, str(tmp_path), "warn")
    assert (tmp_path/"shared.txt").read_text() == "BBB"   # later writer wins
    assert (tmp_path/"a.txt").read_text() == "a"
    assert (tmp_path/"b.txt").read_text() == "b"
    assert plan.conflicts == {"shared.txt": ["writer_a", "writer_b"]}


def test_reconcile_skip_first_writer_wins(tmp_path):
    specs = _two_writers(tmp_path)
    reconcile_promotions(specs, str(tmp_path), "skip")
    assert (tmp_path/"shared.txt").read_text() == "AAA"   # first writer kept
    assert (tmp_path/"a.txt").read_text() == "a"
    assert (tmp_path/"b.txt").read_text() == "b"          # non-conflicting still lands


def test_reconcile_fail_aborts_before_any_write(tmp_path):
    specs = _two_writers(tmp_path)
    with pytest.raises(PromoteConflictError):
        reconcile_promotions(specs, str(tmp_path), "fail")
    # Nothing applied — discover-first means the raise precedes all copies.
    assert not (tmp_path/"shared.txt").exists()
    assert not (tmp_path/"a.txt").exists()
    assert not (tmp_path/"b.txt").exists()


def test_reconcile_disjoint_promotes_both(tmp_path):
    _repo(tmp_path)
    wa = _wt(tmp_path, "wa"); wb = _wt(tmp_path, "wb")
    (wa/"a.txt").write_text("a"); (wb/"b.txt").write_text("b")
    specs = [("writer_a", str(wa), None), ("writer_b", str(wb), None)]
    plan = reconcile_promotions(specs, str(tmp_path), "warn")
    assert plan.conflicts == {}
    assert (tmp_path/"a.txt").read_text() == "a"
    assert (tmp_path/"b.txt").read_text() == "b"


def test_reconcile_glob_honors_base_directory(tmp_path):
    """#4: globs from required_paths() (base_directory prepended) promote the
    subdir file; the raw-required_files glob would promote nothing."""
    _repo(tmp_path)
    wt = _wt(tmp_path, "wc")
    (wt/"reference").mkdir()
    (wt/"reference"/"prd-context-map.json").write_text("{}")
    contract = OutputContract(
        base_directory="reference", required_files=["prd-context-map.json"],
    )
    # Correct (fixed) behavior: base_directory-prepended glob → file promoted.
    reconcile_promotions(
        [("ref", str(wt), contract.required_paths())], str(tmp_path), "warn",
    )
    assert (tmp_path/"reference"/"prd-context-map.json").read_text() == "{}"


def test_reconcile_raw_required_files_glob_promotes_nothing(tmp_path):
    """Documents the bug #4 fixes: the raw (un-prepended) pathspec matches
    only a root-level file, so the subdir file is excluded."""
    _repo(tmp_path)
    wt = _wt(tmp_path, "wd")
    (wt/"reference").mkdir()
    (wt/"reference"/"prd-context-map.json").write_text("{}")
    reconcile_promotions(
        [("ref", str(wt), ["prd-context-map.json"])], str(tmp_path), "warn",
    )
    assert not (tmp_path/"reference"/"prd-context-map.json").exists()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/unit/runtime/test_promote.py -k "reconcile" -v`
Expected: FAIL — `ImportError: cannot import name 'reconcile_promotions'`

- [ ] **Step 4: Implement `reconcile_promotions`**

In `src/sqrlly/runtime/promote.py`, add after `plan_promotions`:

```python
def reconcile_promotions(
    specs: list[tuple[str, str, list[str] | None]], base: str, mode: str,
) -> PromotionPlan:
    """Discover each node's footprint, plan under ``mode``, then apply.

    ``specs`` is ``[(node_id, worktree, globs), ...]`` in promote order.
    Returns the ``PromotionPlan`` (so the caller can log ``plan.conflicts``).
    Raises ``PromoteConflictError`` (``mode='fail'``) **before any file is
    written** — discovery and planning precede every ``apply_changes``."""
    footprints = {
        node_id: discover_changes(worktree, globs)
        for node_id, worktree, globs in specs
    }
    plan = plan_promotions(footprints, mode)
    trees = {node_id: worktree for node_id, worktree, _ in specs}
    for node_id, changes in plan.allowed.items():
        apply_changes(trees[node_id], base, changes)
    return plan
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/runtime/test_promote.py -v`
Expected: PASS — all reconcile tests plus everything from Tasks 2-3 and the original suite.

- [ ] **Step 6: Commit**

```bash
git add src/sqrlly/runtime/promote.py tests/unit/runtime/test_promote.py
git commit -m "feat: reconcile_promotions orchestrator (discover/plan/apply)"
```

---

### Task 5: `Settings.on_promote_conflict` field

**Files:**
- Modify: `src/sqrlly/schema/models.py` (`Settings`, after `worktree_gc` ~L451)
- Test: `tests/unit/schema/test_schema.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/schema/test_schema.py`:

```python
class TestOnPromoteConflict:
    def test_default_is_warn(self):
        from sqrlly.schema.models import Settings
        assert Settings().on_promote_conflict == "warn"

    def test_accepts_all_four_modes(self):
        from sqrlly.schema.models import Settings
        for mode in ("fail", "warn", "overwrite", "skip"):
            assert Settings(on_promote_conflict=mode).on_promote_conflict == mode

    def test_rejects_unknown_mode(self):
        import pytest
        from pydantic import ValidationError
        from sqrlly.schema.models import Settings
        with pytest.raises(ValidationError):
            Settings(on_promote_conflict="merge")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/schema/test_schema.py::TestOnPromoteConflict -v`
Expected: FAIL — `test_default_is_warn` raises `AttributeError` (no such field); `test_rejects_unknown_mode` fails because `extra` keys are silently accepted-or the field doesn't exist.

- [ ] **Step 3: Add the field**

In `src/sqrlly/schema/models.py`, in `Settings`, immediately after the `worktree_gc` field (`worktree_gc: Literal["never", "on_success"] = "never"`), add:

```python
    # Cross-node promote reconciliation when two same-wave promoting nodes
    # touch the same path. `warn` (default): log the overlap, last-write-wins
    # (run stays green). `fail`: abort before any write. `overwrite`: silent
    # last-write-wins. `skip`: the first promoting node (in `nodes` order)
    # keeps the path; later nodes drop it (their other paths still promote).
    on_promote_conflict: Literal["fail", "warn", "overwrite", "skip"] = "warn"
```

(`Literal` is already imported in this module.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/schema/test_schema.py::TestOnPromoteConflict -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/schema/models.py tests/unit/schema/test_schema.py
git commit -m "feat: Settings.on_promote_conflict (fail|warn|overwrite|skip, default warn)"
```

---

### Task 6: Wire the CLI promote loop to `reconcile_promotions` + e2e conflict tests (#1 + #4 wiring)

**Files:**
- Modify: `src/sqrlly/cli/main.py` (import L16; promote loop ~L432-448)
- Test: `tests/unit/cli/test_cli.py` (new class `TestPromoteConflict`)

- [ ] **Step 1: Write the failing e2e tests**

Append to `tests/unit/cli/test_cli.py` (the file already imports `cli` and `Path` and defines the `runner` fixture):

```python
class TestPromoteConflict:
    def _init_repo(self, path):
        import subprocess
        subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
        subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
        (path / "README").write_text("init")
        subprocess.run(["git", "-C", str(path), "add", "README"], check=True)
        subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)

    def _scripts(self, tmp_path):
        a = tmp_path / "wa.sh"
        a.write_text("#!/bin/bash\nset -euo pipefail\necho AAA > shared.txt\necho a > a.txt\n")
        a.chmod(0o755)
        b = tmp_path / "wb.sh"
        b.write_text("#!/bin/bash\nset -euo pipefail\necho BBB > shared.txt\necho b > b.txt\n")
        b.chmod(0o755)
        return a, b

    def _cfg(self, repo, a, b, mode):
        cfg = repo / "workflow.yaml"
        cfg.write_text(
            "name: ConflictTest\nversion: '1.0'\n"
            "settings:\n"
            f"  on_promote_conflict: {mode}\n"
            "nodes:\n"
            "  - id: writer_a\n    name: A\n    worktree: isolated\n    promote: true\n"
            f"    execute:\n      url: {a}\n"
            "  - id: writer_b\n    name: B\n    worktree: isolated\n    promote: true\n"
            f"    execute:\n      url: {b}\n"
        )
        return cfg

    def test_warn_last_write_wins_and_warns(self, runner, tmp_path):
        repo = tmp_path / "warn"; repo.mkdir(); self._init_repo(repo)
        a, b = self._scripts(tmp_path)
        cfg = self._cfg(repo, a, b, "warn")
        result = runner.invoke(cli, ["run", str(cfg), "--workdir", str(repo)])
        assert result.exit_code == 0, result.output + result.stderr
        assert (repo / "shared.txt").read_text().strip() == "BBB"
        assert (repo / "a.txt").exists() and (repo / "b.txt").exists()
        assert "promote conflict" in result.stderr
        assert "shared.txt" in result.stderr

    def test_skip_first_writer_wins(self, runner, tmp_path):
        repo = tmp_path / "skip"; repo.mkdir(); self._init_repo(repo)
        a, b = self._scripts(tmp_path)
        cfg = self._cfg(repo, a, b, "skip")
        result = runner.invoke(cli, ["run", str(cfg), "--workdir", str(repo)])
        assert result.exit_code == 0, result.output + result.stderr
        assert (repo / "shared.txt").read_text().strip() == "AAA"
        assert (repo / "a.txt").exists() and (repo / "b.txt").exists()

    def test_fail_aborts_nonzero_and_nothing_promoted(self, runner, tmp_path):
        repo = tmp_path / "fail"; repo.mkdir(); self._init_repo(repo)
        a, b = self._scripts(tmp_path)
        cfg = self._cfg(repo, a, b, "fail")
        result = runner.invoke(cli, ["run", str(cfg), "--workdir", str(repo)])
        assert result.exit_code != 0
        assert "Promote conflict" in result.stderr
        assert not (repo / "shared.txt").exists()

    def test_overwrite_silent_last_write_wins(self, runner, tmp_path):
        repo = tmp_path / "ow"; repo.mkdir(); self._init_repo(repo)
        a, b = self._scripts(tmp_path)
        cfg = self._cfg(repo, a, b, "overwrite")
        result = runner.invoke(cli, ["run", str(cfg), "--workdir", str(repo)])
        assert result.exit_code == 0, result.output + result.stderr
        assert (repo / "shared.txt").read_text().strip() == "BBB"
        assert "promote conflict" not in result.stderr
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/cli/test_cli.py::TestPromoteConflict -v`
Expected: FAIL — current loop silently last-write-wins with no warning/abort: `test_warn_*` fails on the missing `"promote conflict"` text; `test_skip_*` fails (gets `BBB`); `test_fail_*` fails (exit 0, file present).

- [ ] **Step 3: Update the import**

In `src/sqrlly/cli/main.py`, replace line 16:

```python
from sqrlly.runtime.promote import promote
```

with:

```python
from sqrlly.runtime.promote import PromoteConflictError, reconcile_promotions
```

- [ ] **Step 4: Rewrite the promote loop**

In `src/sqrlly/cli/main.py`, replace the promote loop:

```python
            if clean and isinstance(executor_obj, ForemanExecutor):
                for node in config.nodes:
                    if not node.promote:
                        continue
                    tree = executor_obj.get_worktree(node.id)
                    if tree is None:
                        continue  # `off` node already wrote to the base workdir
                    # output_contract.required_files doubles as the promote
                    # pathspec filter when present; otherwise the full delta
                    # is promoted (discover mode).
                    globs = (
                        node.output_contract.required_files
                        if node.output_contract else None
                    )
                    promote(tree, workdir, globs=globs)
                if config.settings.worktree_gc == "on_success":
                    await executor_obj.reclaim()
```

with:

```python
            if clean and isinstance(executor_obj, ForemanExecutor):
                specs: list[tuple[str, str, list[str] | None]] = []
                for node in config.nodes:
                    if not node.promote:
                        continue
                    tree = executor_obj.get_worktree(node.id)
                    if tree is None:
                        continue  # `off` node already wrote to the base workdir
                    # output_contract.required_files (base_directory-prepended)
                    # doubles as the promote pathspec filter when present;
                    # otherwise the full delta is promoted (discover mode).
                    globs = (
                        node.output_contract.required_paths()
                        if node.output_contract else None
                    )
                    specs.append((node.id, tree, globs))
                if specs:
                    try:
                        plan = reconcile_promotions(
                            specs, workdir,
                            config.settings.on_promote_conflict,
                        )
                    except PromoteConflictError as e:
                        raise click.ClickException(str(e)) from e
                    if (plan.conflicts
                            and config.settings.on_promote_conflict == "warn"):
                        for path, nodes in sorted(plan.conflicts.items()):
                            click.echo(click.style(
                                f"warning: promote conflict on {path!r} — "
                                f"promoted by {', '.join(nodes)}; "
                                f"last-write-wins",
                                fg="yellow"), err=True)
                if config.settings.worktree_gc == "on_success":
                    await executor_obj.reclaim()
```

- [ ] **Step 5: Run the e2e tests to verify they pass**

Run: `uv run pytest tests/unit/cli/test_cli.py::TestPromoteConflict -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Run the full CLI + promote regression set**

Run: `uv run pytest tests/unit/cli/test_cli.py tests/unit/runtime/test_promote.py -v`
Expected: PASS — including the pre-existing `test_promote_copies_worktree_delta_to_base` (single-node promote unchanged) and `worktree_gc` tests.

- [ ] **Step 7: Commit**

```bash
git add src/sqrlly/cli/main.py tests/unit/cli/test_cli.py
git commit -m "feat: promote conflict detection wired into the CLI promote loop"
```

---

### Task 7: `LlmPreset.env` + CLIBackend env injection + factory wiring (#5a, CLI transport)

**Files:**
- Modify: `src/sqrlly/schema/models.py` (`LlmPreset`, after `cli_args` ~L362)
- Modify: `src/sqrlly/runtime/executor/backends/cli.py` (add `import os`; ctor + `send_prompt`)
- Modify: `src/sqrlly/runtime/executor/backends/factory.py` (`_build_cli`)
- Test: `tests/unit/runtime/test_cli_backend.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/runtime/test_cli_backend.py`:

```python
class TestCLIBackendEnv:
    @pytest.mark.asyncio
    async def test_env_reaches_subprocess(self, tmp_path):
        fake = _write_fake(tmp_path, "claude-fake", '#!/bin/sh\necho "FOO=$FOO"\n')
        backend = CLIBackend(argv_prefix=(str(fake),), env={"FOO": "bar"})
        result = await backend.send_prompt("x", "sonnet", str(tmp_path))
        assert result.output == "FOO=bar"

    @pytest.mark.asyncio
    async def test_empty_env_inherits_parent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INHERITED", "yes")
        fake = _write_fake(
            tmp_path, "claude-fake", '#!/bin/sh\necho "INHERITED=$INHERITED"\n',
        )
        backend = CLIBackend(argv_prefix=(str(fake),))  # no env → None → inherit
        result = await backend.send_prompt("x", "sonnet", str(tmp_path))
        assert result.output == "INHERITED=yes"

    @pytest.mark.asyncio
    async def test_env_overlays_without_wiping_inherited(self, tmp_path, monkeypatch):
        monkeypatch.setenv("INHERITED", "keepme")
        fake = _write_fake(
            tmp_path, "claude-fake",
            '#!/bin/sh\necho "FOO=$FOO INHERITED=$INHERITED"\n',
        )
        backend = CLIBackend(argv_prefix=(str(fake),), env={"FOO": "bar"})
        result = await backend.send_prompt("x", "sonnet", str(tmp_path))
        assert result.output == "FOO=bar INHERITED=keepme"


def test_build_cli_threads_env():
    from sqrlly.runtime.executor.backends.factory import create_backend_from_preset
    from sqrlly.schema.models import LlmPreset
    backend = create_backend_from_preset(LlmPreset(
        transport="cli", provider="anthropic", model="sonnet", env={"X": "1"},
    ))
    assert backend._env == {"X": "1"}


def test_llm_preset_env_defaults_empty():
    from sqrlly.schema.models import LlmPreset
    p = LlmPreset(transport="cli", provider="anthropic", model="sonnet")
    assert p.env == {}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/runtime/test_cli_backend.py -k "Env or env" -v`
Expected: FAIL — `LlmPreset(... env=...)` raises `ValidationError` (extra="forbid", no `env` field); `CLIBackend(... env=...)` raises `TypeError` (unexpected kwarg).

- [ ] **Step 3: Add `env` to `LlmPreset`**

In `src/sqrlly/schema/models.py`, in `LlmPreset`, immediately after the `cli_args` field, add:

```python
    # Per-preset environment overlay for the spawned backend process
    # (e.g. CLAUDE_CODE_EFFORT_LEVEL). Overlaid on os.environ at spawn —
    # never replaces it. Mirrors SubprocessParams.env for script nodes.
    # Applies to both transports (cli + acp).
    env: dict[str, str] = {}
```

- [ ] **Step 4: Inject env in `CLIBackend`**

In `src/sqrlly/runtime/executor/backends/cli.py`, add to the imports (after `import asyncio`):

```python
import os
```

Add the `env` parameter to `__init__` (after `cli_args`):

```python
    def __init__(
        self,
        argv_prefix: tuple[str, ...] = ("claude", "-p"),
        *,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        cli_args: list[str] | None = None,
        env: dict[str, str] | None = None,
    ):
        self._argv_prefix = argv_prefix
        self._permission_mode = permission_mode
        self._allowed_tools = allowed_tools
        self._disallowed_tools = disallowed_tools
        self._cli_args = cli_args
        self._env = env
```

In `send_prompt`, replace the `create_subprocess_exec` call:

```python
        argv = [*self._argv_prefix, "--model", model, *self._tool_argv()]
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workdir,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
```

with:

```python
        argv = [*self._argv_prefix, "--model", model, *self._tool_argv()]
        # Overlay the preset env on the inherited environment; empty → None
        # (inherit unchanged), preserving prior behavior exactly.
        proc_env = {**os.environ, **self._env} if self._env else None
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workdir,
            env=proc_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
```

- [ ] **Step 5: Thread env through the factory**

In `src/sqrlly/runtime/executor/backends/factory.py`, replace `_build_cli`:

```python
def _build_cli(preset: "LlmPreset") -> PromptBackend:
    return CLIBackend(
        argv_prefix=("claude", "-p"),
        permission_mode=preset.permission_mode,
        allowed_tools=preset.allowed_tools,
        disallowed_tools=preset.disallowed_tools,
        cli_args=preset.cli_args,
    )
```

with:

```python
def _build_cli(preset: "LlmPreset") -> PromptBackend:
    return CLIBackend(
        argv_prefix=("claude", "-p"),
        permission_mode=preset.permission_mode,
        allowed_tools=preset.allowed_tools,
        disallowed_tools=preset.disallowed_tools,
        cli_args=preset.cli_args,
        env=preset.env,
    )
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/unit/runtime/test_cli_backend.py -v`
Expected: PASS — new env tests plus all pre-existing CLIBackend tests (empty-env path leaves behavior unchanged).

- [ ] **Step 7: Commit**

```bash
git add src/sqrlly/schema/models.py src/sqrlly/runtime/executor/backends/cli.py src/sqrlly/runtime/executor/backends/factory.py tests/unit/runtime/test_cli_backend.py
git commit -m "feat: LlmPreset.env injected into the CLI backend subprocess"
```

---

### Task 8: ACPBackend env injection + factory wiring (#5a, ACP transport)

**Files:**
- Modify: `src/sqrlly/runtime/executor/backends/acp.py` (ctor + `spawn_agent_process`)
- Modify: `src/sqrlly/runtime/executor/backends/factory.py` (`_build_acp`)
- Test: `tests/unit/runtime/test_dispatch_presets.py` (offline construction)

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/runtime/test_dispatch_presets.py`:

```python
class TestFactoryThreadsAcpEnv:
    """ACPBackend construction is offline-safe (spawn deferred to first
    send_prompt), so we can assert env wiring without launching the adapter."""

    def test_build_acp_threads_env(self):
        from sqrlly.runtime.executor.backends.factory import (
            create_backend_from_preset,
        )
        backend = create_backend_from_preset(LlmPreset(
            transport="acp", provider="anthropic", model="opus",
            env={"CLAUDE_CODE_EFFORT_LEVEL": "max"},
        ))
        assert backend._env == {"CLAUDE_CODE_EFFORT_LEVEL": "max"}

    def test_build_acp_default_env_is_none(self):
        from sqrlly.runtime.executor.backends.factory import (
            create_backend_from_preset,
        )
        backend = create_backend_from_preset(LlmPreset(
            transport="acp", provider="anthropic", model="opus",
        ))
        # Empty preset env → backend stores {} (falsy → None at spawn).
        assert backend._env == {}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/runtime/test_dispatch_presets.py::TestFactoryThreadsAcpEnv -v`
Expected: FAIL — `ACPBackend(... env=...)` raises `TypeError` (unexpected kwarg) once the factory passes it; before the factory change, `backend._env` raises `AttributeError`.

- [ ] **Step 3: Add `env` to `ACPBackend`**

In `src/sqrlly/runtime/executor/backends/acp.py`, add the `env` parameter to `__init__` (after `disallowed_tools`) and store it:

```python
    def __init__(
        self,
        program: str = "npx",
        args: tuple[str, ...] = ("@zed-industries/claude-code-acp",),
        *,
        permission_mode: str | None = None,
        allowed_tools: list[str] | None = None,
        disallowed_tools: list[str] | None = None,
        env: dict[str, str] | None = None,
    ):
        self._program = program
        self._args = args
        self._env = env
        self._callbacks = _ACPCallbacks(
            permission_mode=permission_mode,
            allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools,
        )
        self._conn: Any = None
        self._proc: Any = None
        self._proc_pid: int | None = None
        self._session_id: str | None = None
        self._ctx_manager: Any = None
        self._initialized = False
        self._init_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
```

In `_ensure_initialized`, replace:

```python
            self._ctx_manager = spawn_agent_process(
                self._callbacks, self._program, *self._args
            )
```

with:

```python
            # Overlay the preset env on the inherited environment; empty →
            # None (inherit unchanged), matching the CLI backend.
            proc_env = {**os.environ, **self._env} if self._env else None
            self._ctx_manager = spawn_agent_process(
                self._callbacks, self._program, *self._args, env=proc_env,
            )
```

(`os` is already imported in `acp.py`.)

- [ ] **Step 4: Thread env through the factory**

In `src/sqrlly/runtime/executor/backends/factory.py`, replace the `ACPBackend(...)` call in `_build_acp`:

```python
    return ACPBackend(
        program="npx",
        args=("@zed-industries/claude-code-acp",),
        permission_mode=preset.permission_mode,
        allowed_tools=preset.allowed_tools,
        disallowed_tools=preset.disallowed_tools,
    )
```

with:

```python
    return ACPBackend(
        program="npx",
        args=("@zed-industries/claude-code-acp",),
        permission_mode=preset.permission_mode,
        allowed_tools=preset.allowed_tools,
        disallowed_tools=preset.disallowed_tools,
        env=preset.env,
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/runtime/test_dispatch_presets.py -v`
Expected: PASS — the two new env tests plus all pre-existing dispatch/preset resolution tests.

- [ ] **Step 6: Commit**

```bash
git add src/sqrlly/runtime/executor/backends/acp.py src/sqrlly/runtime/executor/backends/factory.py tests/unit/runtime/test_dispatch_presets.py
git commit -m "feat: LlmPreset.env injected into the ACP backend spawn"
```

---

### Task 9: Documentation (SCHEMA.md + CHANGELOG.md)

**Files:**
- Modify: `SCHEMA.md`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: SCHEMA.md — Settings**

In `SCHEMA.md`, in the `Settings` field reference (where `worktree_gc` is documented), add an entry for `on_promote_conflict`, matching the surrounding format:

> `on_promote_conflict` — `fail | warn | overwrite | skip` (default `warn`). When two same-wave promoting nodes touch the same path: `warn` logs the overlap and applies last-write-wins (run stays green); `fail` aborts before any write; `overwrite` is silent last-write-wins; `skip` keeps the first promoting node's version (by `nodes` order) and drops the path from later nodes (their other paths still promote). Detection runs discover-first, so `fail` never half-promotes.

- [ ] **Step 2: SCHEMA.md — LlmPreset.env and required_files promote semantics**

In the `LlmPreset` field reference, add:

> `env` — `dict[str, str]` (default `{}`). Environment overlay for the spawned backend process (cli + acp), e.g. `CLAUDE_CODE_EFFORT_LEVEL`. Overlaid on the inherited environment at spawn; never replaces it. Per-node tuning is done by selecting a preset variant via `params.preset`.

In the `output_contract` section, add a clarifying note:

> `required_files` are interpreted relative to `base_directory` for **both** the existence check and the promote glob filter. A node with `base_directory: reference` and `required_files: [x.json]` validates and promotes `reference/x.json`.

- [ ] **Step 3: CHANGELOG.md**

Add to the top unreleased section of `CHANGELOG.md` (create an `## [Unreleased]` heading if one is not present, matching the file's existing entry style):

```markdown
### Added
- `settings.on_promote_conflict` (`fail | warn | overwrite | skip`, default
  `warn`): cross-node promote conflict detection. Overlapping same-wave promote
  footprints are detected discover-first; `warn`/`fail`/`skip`/`overwrite` choose
  the resolution.
- `LlmPreset.env`: per-preset environment overlay for LLM backend processes
  (cli + acp), e.g. `CLAUDE_CODE_EFFORT_LEVEL`.

### Fixed
- `output_contract.base_directory` is now honored by the promote glob filter
  (previously the existence check prepended it but the promote glob used
  `required_files` raw, so a non-root `base_directory` passed validation but
  promoted nothing).
```

- [ ] **Step 4: Verify docs build/reference nothing broken**

Run: `uv run sqrlly validate examples/jokes/workflow.yaml`
Expected: `Valid: ...` (sanity check that the schema additions didn't break a known-good config).

- [ ] **Step 5: Commit**

```bash
git add SCHEMA.md CHANGELOG.md
git commit -m "docs: on_promote_conflict, LlmPreset.env, base_directory promote semantics"
```

---

### Final verification (run after all tasks)

- [ ] **Full core suite**

Run: `uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -q`
Expected: all pass (~1k tests). The ACP suite (`tests/acp`) additionally exercises the env path end-to-end if `@zed-industries/claude-code-acp` is installed: `uv run pytest tests/acp -v`.

- [ ] **Layer rules**

Run: `uv run pytest tests/architecture/test_layers.py -v`
Expected: PASS — new code stays within layers (planner/orchestrator in runtime; method on schema model; cli→runtime import only).

---

## Self-Review

**1. Spec coverage:**
- #1 promote conflict modes → Tasks 3 (planner), 4 (orchestrator), 5 (setting), 6 (CLI wiring + e2e). ✓ default `warn`, configurable `fail|warn|overwrite|skip`, `skip`=first-writer-wins, discover-first. ✓
- #4 base_directory promote glob → Task 1 (`required_paths()` + check), Task 4 (integration incl. the raw-glob-promotes-nothing guard), Task 6 (cli uses `required_paths()`). ✓
- #5a LlmPreset.env → Task 7 (field + CLI), Task 8 (ACP), both factory-wired, overlay semantics tested. ✓
- promote.py discover/plan/apply refactor → Tasks 2-4. ✓
- Docs (SCHEMA.md/CHANGELOG.md) → Task 9. ✓
- Testing principles (real subprocess/worktrees, known-good + known-bad, assert output) → every task. ✓
No spec requirement is left without a task.

**2. Placeholder scan:** No TBD/TODO/"add error handling"/"similar to". Every code step shows complete code; every run step shows the command + expected result. ✓

**3. Type consistency:**
- `PromotionPlan(allowed: dict[str, dict[str,str]], conflicts: dict[str, list[str]])` — defined Task 3, consumed identically in Task 4 (`plan.allowed`, `plan.conflicts`) and Task 6 (`plan.conflicts`). ✓
- `reconcile_promotions(specs: list[tuple[str,str,list[str]|None]], base, mode) -> PromotionPlan` — defined Task 4, called with exactly that shape in Task 6. ✓
- `OutputContract.required_paths()` — defined Task 1, called in Task 4 (test) and Task 6 (cli). ✓
- `PromoteConflictError` — raised in Task 3, caught in Task 6. ✓
- `CLIBackend(..., env=...)`/`ACPBackend(..., env=...)` and `_env` attribute — defined Tasks 7/8, asserted in the same tasks' tests. ✓
- `Settings.on_promote_conflict` — Task 5, read in Task 6. ✓
All names/signatures consistent across tasks.
