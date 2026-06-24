# `settings.promote_include` — re-include after exclude (N3) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Let a workflow exclude a directory from the promote footprint **except** a subpath — e.g. drop `log/` but keep `log/phases/*` — which raw git `:(exclude)` pathspecs cannot express (no in-list negation).

**Architecture:** Add `Settings.promote_include: list[str]` (git pathspecs). `discover_changes` gains an `includes` param: after the existing glob+exclude pass, it runs a SECOND pass (`globs=includes`, no excludes) and unions those changed paths back in — so an excluded path that matches the allow-list is re-added. Threaded through `reconcile_promotions` and the `cli/main.py` promote loop alongside the existing `promote_exclude`.

**Tech Stack:** Python 3.11+, Pydantic, git pathspecs, pytest.

## Global Constraints

- Python `>=3.11` (use `sys.executable` for python subprocess; bare `python` unavailable).
- Layer rules (`tests/architecture/test_layers.py`): `runtime/promote.py` stays Graph-free / stdlib-only; `schema/` no langgraph; `cli` may import both.
- No mocks of external systems — tests use real `git` worktrees (model the helpers on `tests/unit/runtime/test_promote.py`'s `_repo`/`_wt`).
- Conventional commits, NO attribution trailers. `extra=forbid` on all models.
- `promote_include` is a footprint-level override: it re-adds matching changed paths to EVERY promoting node's footprint, regardless of `promote_exclude` (and regardless of a node's `output_contract` globs). Document this.

---

### Task 1: Schema — `Settings.promote_include`

**Files:**
- Modify: `src/sqrlly/schema/models.py` (the `Settings` model, next to `promote_exclude`)
- Test: `tests/unit/schema/test_schema.py` (extend)

**Interfaces:**
- Produces: `Settings.promote_include: list[str]` (default `[]`).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/schema/test_schema.py — add
def test_settings_promote_include_field():
    from sqrlly.schema.models import Settings
    s = Settings(promote_include=["log/phases/*"])
    assert s.promote_include == ["log/phases/*"]
    assert Settings().promote_include == []   # default empty
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/schema/test_schema.py::test_settings_promote_include_field -q`
Expected: FAIL — `Settings` has no field `promote_include` (`extra=forbid` → ValidationError).

- [ ] **Step 3: Implement** — add the field to `Settings` in `src/sqrlly/schema/models.py` immediately after the `promote_exclude` field:

```python
    # Git pathspecs RE-INCLUDED into the promote footprint after
    # `promote_exclude` removes them — the allow-list half of exclude. Lets
    # you drop a directory but keep a subpath (e.g. promote_exclude=["log/"]
    # + promote_include=["log/phases/*"] keeps log/phases/* and drops the
    # rest of log/). Git `:(exclude)` pathspecs have no in-list negation, so
    # this is a second pass unioned back in. Applies to every promoting node.
    promote_include: list[str] = []
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/schema/test_schema.py::test_settings_promote_include_field -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/schema/models.py tests/unit/schema/test_schema.py
git commit -m "feat: Settings.promote_include — re-include allow-list for promote"
```

---

### Task 2: `discover_changes` — second-pass re-include union

**Files:**
- Modify: `src/sqrlly/runtime/promote.py` (`discover_changes`)
- Test: `tests/unit/runtime/test_promote.py` (extend)

**Interfaces:**
- Consumes: nothing new at runtime.
- Produces: `discover_changes(worktree, globs=None, excludes=None, includes=None) -> dict[str, str]` — after the glob+exclude pass, paths matching `includes` (git pathspecs) are unioned back in even if `excludes` dropped them.

**Context:** `discover_changes` currently builds a `git status --porcelain=v1 -z --untracked-files=all -- :(glob)<g> :(exclude)<e>` command and parses the NUL-separated output into a `{path: kind}` dict named `changes`. You add an `includes` param and, at the END (just before `return changes`), union in a second pass.

- [ ] **Step 1: Write the failing test**

Model the git helpers on the existing `tests/unit/runtime/test_promote.py` (`_repo`, `_wt`). Add:

```python
# tests/unit/runtime/test_promote.py — add
def test_discover_changes_reinclude_keeps_subpath(tmp_path):
    _repo(tmp_path)
    wt = _wt(tmp_path, "wt-ri")
    # Create changes: a kept subpath under an excluded dir, a dropped sibling,
    # and an unrelated file.
    (wt / "log" / "phases").mkdir(parents=True)
    (wt / "log" / "phases" / "keep.txt").write_text("keep")
    (wt / "log" / "noise.txt").write_text("noise")
    (wt / "src").mkdir()
    (wt / "src" / "main.py").write_text("x")

    out = discover_changes(
        str(wt), excludes=["log/"], includes=["log/phases/**"],
    )
    assert "src/main.py" in out                 # unrelated change kept
    assert "log/phases/keep.txt" in out         # re-included by allow-list
    assert "log/noise.txt" not in out           # excluded, not re-included

def test_discover_changes_no_includes_unchanged(tmp_path):
    _repo(tmp_path)
    wt = _wt(tmp_path, "wt-noinc")
    (wt / "log").mkdir()
    (wt / "log" / "a.txt").write_text("a")
    (wt / "src").mkdir(); (wt / "src" / "b.py").write_text("b")
    out = discover_changes(str(wt), excludes=["log/"])  # includes default None
    assert "src/b.py" in out
    assert "log/a.txt" not in out               # excluded, no re-include
```

(`discover_changes` is already imported at the top of `test_promote.py`.)

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/runtime/test_promote.py -q -k "reinclude or no_includes"`
Expected: `test_discover_changes_reinclude_keeps_subpath` FAILs — `discover_changes()` got an unexpected keyword argument `includes` (TypeError); the `no_includes` case passes (it doesn't use `includes`).

- [ ] **Step 3: Implement** — change the `discover_changes` signature and add the second pass. Update the signature line:

```python
def discover_changes(
    worktree: str, globs: list[str] | None = None,
    excludes: list[str] | None = None,
    includes: list[str] | None = None,
) -> dict[str, str]:
```

Extend the docstring with a sentence:

```
    ``includes`` (git pathspec) are RE-INCLUDED after ``excludes`` — a second
    pass unions changed paths matching ``includes`` back in, so you can drop a
    directory but keep a subpath. ``includes`` overrides ``excludes``.
```

At the very end of the function, immediately before `return changes`, add:

```python
    if includes:
        # Re-include pass: git pathspecs can't negate in-list, so anything the
        # allow-list matches is unioned back in even if `excludes` dropped it.
        changes.update(discover_changes(worktree, globs=includes))
    return changes
```

(The recursive call passes neither `excludes` nor `includes`, so it terminates — it just returns the changed paths matching the include globs, which `update` merges.)

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/runtime/test_promote.py -q -k "reinclude or no_includes"`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/runtime/promote.py tests/unit/runtime/test_promote.py
git commit -m "feat: discover_changes re-include pass (promote_include allow-list)"
```

---

### Task 3: Thread `includes` through `reconcile_promotions` + the CLI promote loop

**Files:**
- Modify: `src/sqrlly/runtime/promote.py` (`reconcile_promotions`)
- Modify: `src/sqrlly/cli/main.py` (the promote loop)
- Test: `tests/unit/runtime/test_promote.py` (extend)

**Interfaces:**
- Consumes: `discover_changes(..., includes=)` (Task 2), `Settings.promote_include` (Task 1).
- Produces: `reconcile_promotions(specs, base, mode, excludes=None, includes=None)` — `includes` threaded to every node's `discover_changes`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/runtime/test_promote.py — add
def test_reconcile_promotions_reinclude(tmp_path):
    _repo(tmp_path)
    wt = _wt(tmp_path, "wt-rec")
    (wt / "log" / "phases").mkdir(parents=True)
    (wt / "log" / "phases" / "keep.txt").write_text("keep")
    (wt / "log" / "noise.txt").write_text("noise")
    plan = reconcile_promotions(
        [("n", str(wt), None)], str(tmp_path), "warn",
        excludes=["log/"], includes=["log/phases/**"],
    )
    # The re-included path is applied to base; the excluded sibling is not.
    assert (tmp_path / "log" / "phases" / "keep.txt").exists()
    assert not (tmp_path / "log" / "noise.txt").exists()
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/runtime/test_promote.py::test_reconcile_promotions_reinclude -q`
Expected: FAIL — `reconcile_promotions()` got an unexpected keyword argument `includes` (TypeError).

- [ ] **Step 3: Implement**

In `src/sqrlly/runtime/promote.py`, update `reconcile_promotions`:

```python
def reconcile_promotions(
    specs: list[tuple[str, str, list[str] | None]], base: str, mode: str,
    excludes: list[str] | None = None,
    includes: list[str] | None = None,
) -> PromotionPlan:
```

In its body, pass `includes` to `discover_changes`:

```python
    footprints = {
        node_id: discover_changes(worktree, globs, excludes=excludes,
                                  includes=includes)
        for node_id, worktree, globs in specs
    }
```

(Extend the docstring's `excludes` line to mention `includes` are re-added after.)

In `src/sqrlly/cli/main.py`, the promote loop calls `reconcile_promotions(specs, workdir, config.settings.on_promote_conflict, excludes=config.settings.promote_exclude or None)`. Add the `includes` argument:

```python
                        plan = reconcile_promotions(
                            specs, workdir,
                            config.settings.on_promote_conflict,
                            excludes=config.settings.promote_exclude or None,
                            includes=config.settings.promote_include or None,
                        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/runtime/test_promote.py::test_reconcile_promotions_reinclude -q`
Expected: PASS.

- [ ] **Step 5: Run the promote + architecture suites**

Run: `uv run pytest tests/unit/runtime/test_promote.py tests/architecture/test_layers.py -q`
Expected: PASS (no regression; layer rules hold — `cli` reads two Settings fields).

- [ ] **Step 6: Commit**

```bash
git add src/sqrlly/runtime/promote.py src/sqrlly/cli/main.py tests/unit/runtime/test_promote.py
git commit -m "feat: thread promote_include through reconcile_promotions + the CLI promote loop"
```

---

### Task 4: Docs + CHANGELOG + TODO

**Files:**
- Modify: `SCHEMA.md`, `CLAUDE.md`, `SKILLS.md`, `CHANGELOG.md`, `TODO.md`

**Interfaces:** none.

- [ ] **Step 1: SCHEMA.md** — add a row immediately after the `promote_exclude` row in the Worktree-isolation settings table:

```markdown
| `promote_include` | `list[str]` | `[]` | Git pathspecs RE-INCLUDED into every promoting node's footprint after `promote_exclude` removes them — the allow-list half. `promote_exclude: ["log/"]` + `promote_include: ["log/phases/**"]` promotes `log/phases/**` while dropping the rest of `log/`. (Git `:(exclude)` has no in-list negation, so this is a second pass; `include` overrides `exclude`.) |
```

- [ ] **Step 2: CLAUDE.md** — in "Known limitations" or the promote note, add (long line, matching the file):

```markdown
- **`promote_include` re-includes after `promote_exclude`** — `settings.promote_include` (git pathspecs) is a second `discover_changes` pass unioned back into every promoting node's footprint, so you can exclude a directory but keep a subpath (`promote_exclude: ["log/"]` + `promote_include: ["log/phases/**"]`). It overrides `promote_exclude` and ignores a node's `output_contract` globs (footprint-level override).
```

- [ ] **Step 3: SKILLS.md** — near the promote/worktree authoring guidance, add (hard-wrap ~70 cols to match):

```markdown
To exclude a directory from a `promote` but keep one subpath, pair
`settings.promote_exclude: ["log/"]` with `settings.promote_include:
["log/phases/**"]` — the include is re-added after the exclude (git
pathspecs can't negate in-list).
```

- [ ] **Step 4: CHANGELOG.md** — add to the top `## [Unreleased]` block (create it above the latest released section if absent), under `### Added`:

```markdown
- `settings.promote_include` — git pathspecs re-included into the promote footprint after `promote_exclude`, so you can exclude a directory but keep a subpath (`promote_exclude: ["log/"]` + `promote_include: ["log/phases/**"]`). Implemented as a second `discover_changes` pass; the include overrides the exclude.
```

- [ ] **Step 5: TODO.md** — rewrite the `### N3` entry to the SHIPPED style (matching the `✅ … SHIPPED` entries): `### ✅ N3 — promote_exclude re-include allow-list — SHIPPED 0.7.8`, body trimmed to current state (names `settings.promote_include`, the second-pass union).

- [ ] **Step 6: Sanity + commit**

```bash
uv run sqrlly validate examples/jokes/workflow.yaml   # still Valid
git add SCHEMA.md CLAUDE.md SKILLS.md CHANGELOG.md TODO.md
git commit -m "docs: document settings.promote_include; mark N3 shipped"
```

---

## Final verification (after all tasks)

```bash
uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -q   # full core suite green
uv run pytest tests/unit/runtime/test_promote.py -q             # promote suite incl. re-include
```
Expected: full suite passes; `promote_include` re-include proven.
