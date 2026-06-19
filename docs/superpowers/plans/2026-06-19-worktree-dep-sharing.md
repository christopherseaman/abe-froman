# Worktree Dependency Sharing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let parallel worktree branches see gitignored base deps (e.g. `node_modules`) for in-branch gates, without leaking them into the promote footprint — via a universal `.git/info/exclude` write plus two sharing paths (read-only symlink + per-worktree rehydrate), and a `promote_exclude` safety net.

**Architecture:** All sharing hooks the single `foreman._create_worktree` chokepoint (and the `_acquire_worktree` resume/retry early-return). The load-bearing piece is a `.git/info/exclude` write so shared artifacts never enter `discover_changes`' `git status`. Read-only sharing is a whole-dir symlink (`worktree_share`); generality is a sentinel-gated, fatal-per-branch command runner (`worktree_setup`). Independently, `promote_exclude` filters every promoting node's footprint at the promote layer.

**Tech Stack:** Python 3.14, Pydantic v2, `asyncio`, real `git worktree` + real subprocess in tests (no pnpm/Prisma needed — tests use generic dirs/commands; the pnpm/Prisma validation is a separate consumer-side pre-merge step in the design doc).

**Spec:** `docs/superpowers/specs/2026-06-19-worktree-dep-sharing-design.md` (operator-reviewed; decisions resolved).

**Phasing:** Tasks 1–3 = **Phase A** (universal exclude-write + read-only `worktree_share` + `promote_exclude`) — the builder's actual need + the request-#1 defense; each is independently shippable. Tasks 4–6 = **Phase B** (`worktree_setup` rehydrate). Task 7 = lint. Task 8 = docs.

**Decisions baked in (from review):** sentinel hashes base state (commands + base HEAD), not the worktree's own schema; `worktree_setup_exclude` is explicit (no auto-derive); setup failure is **fatal for the branch**; no dedicated setup semaphore; `promote_exclude` ships as a general field.

---

### Task 1: `promote_exclude` — exclude pathspecs from the promote footprint

**Files:**
- Modify: `src/sqrlly/runtime/promote.py` (`discover_changes`, `reconcile_promotions`)
- Modify: `src/sqrlly/schema/models.py` (`Settings`, after `on_promote_conflict`)
- Modify: `src/sqrlly/cli/main.py` (the `reconcile_promotions(...)` call site)
- Test: `tests/unit/runtime/test_promote.py`, `tests/unit/schema/test_schema.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/runtime/test_promote.py`:

```python
def test_discover_changes_excludes_pathspec(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "wex")
    (wt / "real.txt").write_text("keep")
    (wt / "node_modules").mkdir()
    (wt / "node_modules" / "dep.js").write_text("x")
    # Without excludes, node_modules is in the footprint:
    assert "node_modules/dep.js" in discover_changes(str(wt))
    # With an exclude pathspec, it is filtered out; real.txt survives:
    changes = discover_changes(str(wt), excludes=["node_modules"])
    assert "real.txt" in changes
    assert not any(p.startswith("node_modules") for p in changes)


def test_reconcile_threads_excludes(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "wex2")
    (wt / "real.txt").write_text("keep")
    (wt / "node_modules").mkdir()
    (wt / "node_modules" / "dep.js").write_text("x")
    reconcile_promotions(
        [("n", str(wt), None)], str(tmp_path), "warn",
        excludes=["node_modules"],
    )
    assert (tmp_path / "real.txt").read_text() == "keep"
    assert not (tmp_path / "node_modules").exists()  # never promoted into base
```

Append to `tests/unit/schema/test_schema.py`:

```python
class TestPromoteExclude:
    def test_defaults_empty(self):
        from sqrlly.schema.models import Settings
        assert Settings().promote_exclude == []

    def test_accepts_list(self):
        from sqrlly.schema.models import Settings
        assert Settings(promote_exclude=["node_modules", ".next/cache"]).promote_exclude \
            == ["node_modules", ".next/cache"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/runtime/test_promote.py -k "excludes or threads_excludes" tests/unit/schema/test_schema.py::TestPromoteExclude -v`
Expected: FAIL — `discover_changes()`/`reconcile_promotions()` reject the `excludes=` kwarg (`TypeError`); `Settings` has no `promote_exclude`.

- [ ] **Step 3: Extend `discover_changes` + `reconcile_promotions`**

In `src/sqrlly/runtime/promote.py`, change the `discover_changes` signature and pathspec assembly:

```python
def discover_changes(
    worktree: str, globs: list[str] | None = None,
    excludes: list[str] | None = None,
) -> dict[str, str]:
    """Return ``{path_relative_to_worktree: change_kind}`` for everything the
    worktree changed vs HEAD, including untracked adds and deletions.

    ``globs`` (git pathspec) filters the set to matches; ``excludes`` removes
    matches (a git ``:(exclude)`` pathspec — prefix match, so ``"node_modules"``
    drops the dir/symlink and everything under it). Change kinds are
    ``"added"``, ``"modified"``, or ``"deleted"``.
    """
    cmd = ["git", "-C", worktree, "status", "--porcelain=v1", "-z",
           "--untracked-files=all"]
    pathspecs: list[str] = []
    if globs:
        # :(glob) so ** matches across separators (root-level files too).
        pathspecs += [f":(glob){g}" for g in globs]
    if excludes:
        # :(exclude) is prefix-matching: "node_modules" excludes it and its
        # contents. git treats an exclude-only pathspec as "everything minus".
        pathspecs += [f":(exclude){e}" for e in excludes]
    if pathspecs:
        cmd += ["--", *pathspecs]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    # ... (rest of the function body unchanged) ...
```

(Leave the token-parsing loop below unchanged.)

Change `reconcile_promotions` to thread `excludes` into each discovery:

```python
def reconcile_promotions(
    specs: list[tuple[str, str, list[str] | None]], base: str, mode: str,
    excludes: list[str] | None = None,
) -> PromotionPlan:
    """Discover each node's footprint, plan under ``mode``, then apply.

    ``specs`` is ``[(node_id, worktree, globs), ...]`` in promote order.
    ``excludes`` (git ``:(exclude)`` pathspecs) are filtered from EVERY node's
    footprint — the promote-layer defense beneath the worktree exclude write.
    Returns the ``PromotionPlan``. Raises ``PromoteConflictError`` (``mode='fail'``)
    before any file is written."""
    footprints = {
        node_id: discover_changes(worktree, globs, excludes=excludes)
        for node_id, worktree, globs in specs
    }
    plan = plan_promotions(footprints, mode)
    for node_id, worktree, _ in specs:
        apply_changes(worktree, base, plan.allowed[node_id])
    return plan
```

- [ ] **Step 4: Add the `Settings` field**

In `src/sqrlly/schema/models.py`, immediately after the `on_promote_conflict` field, add:

```python
    # Git pathspecs filtered out of EVERY promoting node's footprint (promote
    # layer). Defense-in-depth beneath worktree-level excludes: keeps generated
    # artifacts (node_modules, build caches) from being promoted into base even
    # if they slip past a worktree's .git/info/exclude. Prefix-match, e.g.
    # ["node_modules", ".next/cache"].
    promote_exclude: list[str] = []
```

- [ ] **Step 5: Wire it in the CLI promote loop**

In `src/sqrlly/cli/main.py`, find the `reconcile_promotions(specs, workdir, config.settings.on_promote_conflict)` call and add the `excludes` argument:

```python
                        plan = reconcile_promotions(
                            specs, workdir,
                            config.settings.on_promote_conflict,
                            excludes=config.settings.promote_exclude or None,
                        )
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/unit/runtime/test_promote.py tests/unit/schema/test_schema.py tests/unit/cli/test_cli.py -q`
Expected: PASS (new tests + all pre-existing promote/conflict/CLI tests — `excludes=None` is the default, so existing behavior is unchanged).

- [ ] **Step 7: Commit**

```bash
git add src/sqrlly/runtime/promote.py src/sqrlly/schema/models.py src/sqrlly/cli/main.py tests/unit/runtime/test_promote.py tests/unit/schema/test_schema.py
git commit -m "feat: settings.promote_exclude filters pathspecs from the promote footprint"
```

---

### Task 2: `.git/info/exclude` write helper (the universal footprint fix)

**Files:**
- Create: `src/sqrlly/runtime/worktree_share.py`
- Test: `tests/unit/runtime/test_worktree_share.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/runtime/test_worktree_share.py`:

```python
"""Worktree dep-sharing helpers (runtime, langgraph-free)."""
import subprocess
from pathlib import Path

from sqrlly.runtime.promote import discover_changes
from sqrlly.runtime.worktree_share import write_worktree_excludes


def _repo(tmp):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp)], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "t"], check=True)
    (tmp / "a.txt").write_text("orig")
    subprocess.run(["git", "-C", str(tmp), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-qm", "init"], check=True)


def _wt(tmp, name):
    dest = tmp / ".sqrlly" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(tmp), "worktree", "add", "-q", str(dest), "HEAD"], check=True)
    return dest


def test_write_excludes_hides_dir_from_git_status(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "wa")
    (wt / "node_modules").mkdir()
    (wt / "node_modules" / "dep.js").write_text("x")
    # Before the exclude write, node_modules is untracked -> in discover_changes:
    assert any(p.startswith("node_modules") for p in discover_changes(str(wt)))
    write_worktree_excludes(str(wt), ["node_modules"])
    # After: hidden from git status, so it never enters the promote footprint.
    assert not any(p.startswith("node_modules") for p in discover_changes(str(wt)))


def test_write_excludes_is_idempotent(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "wb")
    write_worktree_excludes(str(wt), ["node_modules"])
    write_worktree_excludes(str(wt), ["node_modules"])
    exclude_path = subprocess.run(
        ["git", "-C", str(wt), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    lines = [l for l in Path(wt, exclude_path).read_text().splitlines()
             if l.strip() == "node_modules"] if not Path(exclude_path).is_absolute() \
        else [l for l in Path(exclude_path).read_text().splitlines() if l.strip() == "node_modules"]
    assert len(lines) == 1  # appended once, not twice
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/runtime/test_worktree_share.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sqrlly.runtime.worktree_share'`.

- [ ] **Step 3: Implement the helper**

Create `src/sqrlly/runtime/worktree_share.py`:

```python
"""Worktree dependency-sharing helpers (runtime layer, langgraph-free).

The load-bearing piece is ``write_worktree_excludes``: it appends paths to a
worktree's ``info/exclude`` so shared/generated artifacts (node_modules, build
caches, in-tree generated clients) never appear in ``git status
--untracked-files=all`` — i.e. never enter ``promote.discover_changes``'
footprint, so ``apply_changes`` can't follow them into base. A real
``.gitignore`` rule cannot be trusted (a ``node_modules/`` dir-slash rule does
NOT hide a symlink named ``node_modules``); writing the bare path here does.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _info_exclude_path(worktree: str) -> Path:
    """Resolve the worktree's git ``info/exclude`` file (absolute)."""
    rel = subprocess.run(
        ["git", "-C", worktree, "rev-parse", "--git-path", "info/exclude"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    p = Path(rel)
    return p if p.is_absolute() else Path(worktree) / p


def write_worktree_excludes(worktree: str, paths: list[str]) -> None:
    """Append each path (trailing slash stripped) to the worktree's
    ``info/exclude`` if not already a line. Bare (slash-less) entries match a
    symlink/dir of that name, which a ``foo/`` .gitignore rule does not.
    Idempotent — safe to call on every worktree hand-back."""
    if not paths:
        return
    exclude_file = _info_exclude_path(worktree)
    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        set(exclude_file.read_text().splitlines())
        if exclude_file.exists() else set()
    )
    to_add = [p.rstrip("/") for p in paths if p.rstrip("/") not in existing]
    if not to_add:
        return
    with exclude_file.open("a") as fh:
        if exclude_file.stat().st_size and not _ends_with_newline(exclude_file):
            fh.write("\n")
        for p in to_add:
            fh.write(p + "\n")


def _ends_with_newline(p: Path) -> bool:
    with p.open("rb") as fh:
        try:
            fh.seek(-1, 2)
        except OSError:
            return True  # empty file
        return fh.read(1) == b"\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/runtime/test_worktree_share.py -v`
Expected: PASS (2 tests). Then layer test: `uv run pytest tests/architecture/test_layers.py -q` (the new module imports only stdlib + sibling runtime — langgraph-free).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/runtime/worktree_share.py tests/unit/runtime/test_worktree_share.py
git commit -m "feat: write_worktree_excludes — keep shared artifacts out of the promote footprint"
```

---

### Task 3: `worktree_share` — read-only whole-dir symlinks, wired at the chokepoint

**Files:**
- Modify: `src/sqrlly/runtime/worktree_share.py` (add `materialize_shares`)
- Modify: `src/sqrlly/schema/models.py` (`Settings.worktree_share`)
- Modify: `src/sqrlly/runtime/foreman.py` (`_create_worktree`)
- Test: `tests/unit/runtime/test_worktree_share.py`, `tests/unit/runtime/test_foreman.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/runtime/test_worktree_share.py`:

```python
import os
from sqrlly.runtime.worktree_share import materialize_shares


def test_materialize_shares_symlinks_and_excludes(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "wc")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("lib")
    materialize_shares(str(tmp_path), str(wt), ["node_modules"])
    link = wt / "node_modules"
    assert link.is_symlink()                          # relative symlink to base
    assert (link / "dep.js").read_text() == "lib"     # resolves to base content
    assert not os.path.isabs(os.readlink(link))       # relative target
    # Hidden from the promote footprint (exclude write ran):
    assert not any(p.startswith("node_modules") for p in discover_changes(str(wt)))


def test_materialize_shares_missing_base_path_raises(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "wd")
    import pytest
    with pytest.raises(RuntimeError, match="worktree_share"):
        materialize_shares(str(tmp_path), str(wt), ["node_modules"])  # base has none
```

Append to `tests/unit/runtime/test_foreman.py` (it already constructs `ForemanExecutor` against a real git repo; mirror the existing setup):

```python
@pytest.mark.asyncio
async def test_foreman_worktree_share_symlinks_into_new_tree(tmp_path):
    """A worktree_share path is symlinked into each created worktree."""
    import subprocess
    from sqrlly.runtime.foreman import ForemanExecutor
    from sqrlly.schema.models import Settings
    from mock_executor import MockExecutor
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "i"], check=True)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "d.js").write_text("lib")
    fm = ForemanExecutor(
        MockExecutor(), str(tmp_path),
        settings=Settings(worktree=
"isolated", worktree_share=["node_modules"]),
    )
    wt = await fm.acquire_branch_worktree("b1")
    assert (Path(wt) / "node_modules").is_symlink()
    assert (Path(wt) / "node_modules" / "d.js").read_text() == "lib"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/runtime/test_worktree_share.py tests/unit/runtime/test_foreman.py -k "share" -v`
Expected: FAIL — `materialize_shares` undefined; `Settings` has no `worktree_share`.

- [ ] **Step 3: Add `materialize_shares`**

In `src/sqrlly/runtime/worktree_share.py`, add:

```python
import os


def materialize_shares(base: str, dest: str, shares: list[str]) -> None:
    """Symlink each read-only share path from ``base`` into the worktree
    ``dest`` (relative symlink), then write the worktree exclude so the link
    stays out of the promote footprint. Raises if a configured share path is
    absent in ``base`` (fail fast — a dangling link makes in-branch tooling
    fail confusingly). Idempotent: a correct existing symlink is left as-is."""
    for share in shares:
        src = Path(base) / share
        if not src.exists():
            raise RuntimeError(
                f"worktree_share path {share!r} does not exist in base "
                f"{base!r} — install/build it before the run."
            )
        link = Path(dest) / share
        target = os.path.relpath(src, link.parent)
        if link.is_symlink():
            if os.readlink(link) == target:
                pass  # already correct
            else:
                link.unlink()
                link.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(target, link)
        elif link.exists():
            # A real path collides with the share name — leave it; the worktree
            # checked out a tracked file/dir here, which the author owns.
            continue
        else:
            link.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.symlink(target, link)
            except FileExistsError:
                pass  # race with a sibling acquiring the same group tree
        write_worktree_excludes(dest, [share])
```

- [ ] **Step 4: Add the `Settings` field**

In `src/sqrlly/schema/models.py`, after `promote_exclude` (from Task 1), add:

```python
    # Read-only sharing: whole-dir symlinks of these base paths into each
    # worktree (no in-worktree install). For read-only in-branch gates
    # (tsc --noEmit, scoped tests). Each shared path also gets the
    # info/exclude write so it stays out of the promote footprint.
    worktree_share: list[str] = []
```

- [ ] **Step 5: Wire into `_create_worktree`**

In `src/sqrlly/runtime/foreman.py`, import the helper at the top (with the other runtime imports):

```python
from sqrlly.runtime.worktree_share import materialize_shares
```

In `_create_worktree`, call `materialize_shares` at BOTH return points — the group early-return and after a successful `git worktree add`. Replace the group early-return:

```python
            if dest.is_dir() and (dest / ".git").exists():
                # Shared live worktree already exists (sibling created it, or prior run).
                materialize_shares(self._base, str(dest), self._settings.worktree_share)
                return str(dest)
```

and replace the tail (the final `return str(dest)`):

```python
        _out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"foreman: 'git worktree add' failed for {pool_key}: "
                f"{err.decode().strip()}"
            )
        materialize_shares(self._base, str(dest), self._settings.worktree_share)
        return str(dest)
```

- [ ] **Step 6: Run tests**

Run: `uv run pytest tests/unit/runtime/test_worktree_share.py tests/unit/runtime/test_foreman.py -v`
Expected: PASS (share tests + all pre-existing foreman tests — `worktree_share` defaults `[]`, so `materialize_shares` is a no-op for existing tests).

- [ ] **Step 7: Commit**

```bash
git add src/sqrlly/runtime/worktree_share.py src/sqrlly/schema/models.py src/sqrlly/runtime/foreman.py tests/unit/runtime/test_worktree_share.py tests/unit/runtime/test_foreman.py
git commit -m "feat: settings.worktree_share — read-only base-dep symlinks into each worktree"
```

---

### Task 4: Rehydrate `Settings` fields

**Files:**
- Modify: `src/sqrlly/schema/models.py` (`Settings`)
- Test: `tests/unit/schema/test_schema.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/schema/test_schema.py`:

```python
class TestWorktreeSetupFields:
    def test_defaults(self):
        from sqrlly.schema.models import Settings
        s = Settings()
        assert s.worktree_setup == []
        assert s.worktree_setup_exclude == []
        assert s.worktree_setup_store_dir is None

    def test_accepts_values(self):
        from sqrlly.schema.models import Settings
        s = Settings(
            worktree_setup=["pnpm install --prefer-offline"],
            worktree_setup_exclude=["node_modules", "src/generated/prisma"],
            worktree_setup_store_dir=".sqrlly/.pnpm-store",
        )
        assert s.worktree_setup == ["pnpm install --prefer-offline"]
        assert s.worktree_setup_exclude == ["node_modules", "src/generated/prisma"]
        assert s.worktree_setup_store_dir == ".sqrlly/.pnpm-store"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/schema/test_schema.py::TestWorktreeSetupFields -v`
Expected: FAIL — `AttributeError`/`ValidationError` (fields absent).

- [ ] **Step 3: Add the fields**

In `src/sqrlly/schema/models.py`, after `worktree_share` (from Task 3), add:

```python
    # Rehydrate path: ordered shell commands run in each fresh worktree so the
    # package manager builds its own real node_modules there (e.g.
    # ["pnpm install --prefer-offline", "pnpm exec prisma generate"]). PM-agnostic.
    worktree_setup: list[str] = []
    # Paths written to each worktree's info/exclude before promote can run, so
    # generated artifacts (node_modules, in-tree prisma output=) stay out of the
    # footprint. Explicit — sqrlly does not infer generator output paths.
    worktree_setup_exclude: list[str] = []
    # When set, exported into the setup commands' environment as the package
    # store dir (e.g. PNPM_HOME) so the store sits on the worktree device
    # (hardlinks work instead of EXDEV full-copy). Resolved relative to base.
    worktree_setup_store_dir: str | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/schema/test_schema.py::TestWorktreeSetupFields -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/schema/models.py tests/unit/schema/test_schema.py
git commit -m "feat: worktree_setup / _exclude / _store_dir settings (rehydrate path)"
```

---

### Task 5: `ensure_setup` — sentinel-gated, fatal-per-branch command runner

**Files:**
- Modify: `src/sqrlly/runtime/worktree_share.py` (add `ensure_setup`, `setup_fingerprint`)
- Test: `tests/unit/runtime/test_worktree_share.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/runtime/test_worktree_share.py`:

```python
import pytest as _pytest


@_pytest.mark.asyncio
async def test_ensure_setup_runs_commands_writes_excludes_and_sentinel(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "ws1")
    await ensure_setup(
        base=str(tmp_path), dest=str(wt),
        commands=["sh -c 'echo hi > marker.txt'"],
        excludes=["node_modules"], store_dir=None,
    )
    assert (wt / "marker.txt").read_text().strip() == "hi"      # command ran
    assert (wt / ".sqrlly" / "setup-ok").exists()               # sentinel written
    # exclude written before commands:
    assert not any(p.startswith("node_modules")
                   for p in __import__("sqrlly.runtime.promote", fromlist=["discover_changes"]).discover_changes(str(wt)))


@_pytest.mark.asyncio
async def test_ensure_setup_is_idempotent(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "ws2")
    cmds = ["sh -c 'echo x >> count.txt'"]
    await ensure_setup(base=str(tmp_path), dest=str(wt), commands=cmds, excludes=[], store_dir=None)
    await ensure_setup(base=str(tmp_path), dest=str(wt), commands=cmds, excludes=[], store_dir=None)
    # Sentinel matched on the 2nd call -> command ran exactly once.
    assert (wt / "count.txt").read_text().count("x") == 1


@_pytest.mark.asyncio
async def test_ensure_setup_failure_raises_branch_fatal(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "ws3")
    with _pytest.raises(RuntimeError, match="setup failed"):
        await ensure_setup(
            base=str(tmp_path), dest=str(wt),
            commands=["sh -c 'exit 3'"], excludes=[], store_dir=None, retries=0,
        )
    assert not (wt / ".sqrlly" / "setup-ok").exists()  # no sentinel on failure
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/runtime/test_worktree_share.py -k ensure_setup -v`
Expected: FAIL — `ensure_setup` undefined.

- [ ] **Step 3: Implement `ensure_setup`**

In `src/sqrlly/runtime/worktree_share.py`, add (`asyncio`, `hashlib`, `shlex`, `os`, `subprocess` imports as needed at top):

```python
import asyncio
import hashlib
import shlex


def setup_fingerprint(base: str, commands: list[str]) -> str:
    """Sentinel content: hash of the setup commands + the base HEAD commit.
    Re-runs setup when commands change OR the base advances (a lockfile/schema
    change is a base commit in a git workflow). PM-agnostic — no knowledge of
    pnpm/Prisma file paths (decision: hash base state, not the worktree schema)."""
    try:
        head = subprocess.run(
            ["git", "-C", base, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        head = ""
    blob = "\n".join(commands) + "\x00" + head
    return hashlib.sha256(blob.encode()).hexdigest()


async def ensure_setup(
    *, base: str, dest: str, commands: list[str], excludes: list[str],
    store_dir: str | None, retries: int = 1,
) -> None:
    """Idempotently run the worktree setup commands in ``dest``.

    Sentinel-gated: skips when ``dest/.sqrlly/setup-ok`` already matches the
    fingerprint. Writes ``excludes`` to info/exclude BEFORE running commands (so
    a crash mid-install can't leak). On non-zero exit after ``retries`` retries,
    raises ``RuntimeError`` (fatal for the branch — a clear diagnosable failure,
    not a misleading downstream gate failure). Writes the sentinel only on
    success. (GC registration of ``dest`` is the caller's responsibility and
    must happen before this is awaited.)"""
    if not commands and not excludes:
        return
    marker = Path(dest) / ".sqrlly" / "setup-ok"
    fp = setup_fingerprint(base, commands)
    if marker.exists() and marker.read_text().strip() == fp:
        return
    # Exclude write first — even a crashed install must not leak artifacts.
    write_worktree_excludes(dest, excludes)
    env = dict(os.environ)
    if store_dir is not None:
        store_abs = str((Path(base) / store_dir).resolve())
        env["PNPM_HOME"] = store_abs
        env["npm_config_store_dir"] = store_abs  # generic store override
    for cmd in commands:
        argv = shlex.split(cmd)
        attempt = 0
        while True:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=dest, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _out, errb = await proc.communicate()
            if proc.returncode == 0:
                break
            if attempt >= retries:
                raise RuntimeError(
                    f"worktree setup failed: {cmd!r} exit {proc.returncode} "
                    f"in {dest}: {errb.decode().strip()}"
                )
            attempt += 1
            await asyncio.sleep(0.5 * attempt)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(fp)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/runtime/test_worktree_share.py -v`
Expected: PASS (all share + ensure_setup tests).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/runtime/worktree_share.py tests/unit/runtime/test_worktree_share.py
git commit -m "feat: ensure_setup — sentinel-gated, fatal-per-branch worktree rehydrate runner"
```

---

### Task 6: Wire `ensure_setup` into the foreman (create + group + resume paths)

**Files:**
- Modify: `src/sqrlly/runtime/foreman.py` (`_create_worktree`, `_acquire_worktree`)
- Test: `tests/unit/runtime/test_foreman.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/runtime/test_foreman.py`:

```python
@pytest.mark.asyncio
async def test_foreman_runs_worktree_setup_once_and_resume_reuses(tmp_path):
    """worktree_setup runs in each new tree; a re-acquire (resume) reuses it
    without re-running (sentinel matches)."""
    import subprocess
    from sqrlly.runtime.foreman import ForemanExecutor
    from sqrlly.schema.models import Settings
    from mock_executor import MockExecutor
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-qm", "i"], check=True)
    settings = Settings(worktree="isolated",
                        worktree_setup=["sh -c 'echo x >> $PWD/ran.txt'"])
    fm = ForemanExecutor(MockExecutor(), str(tmp_path), settings=settings)
    wt = await fm.acquire_branch_worktree("b1")
    assert Path(wt, "ran.txt").read_text().count("x") == 1
    # Re-acquire the same branch (retry/resume) -> reuse, no re-run.
    wt2 = await fm.acquire_branch_worktree("b1")
    assert wt2 == wt
    assert Path(wt, "ran.txt").read_text().count("x") == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/runtime/test_foreman.py -k worktree_setup -v`
Expected: FAIL — setup never runs (`ran.txt` absent).

- [ ] **Step 3: Wire `ensure_setup` at the three hand-back points**

In `src/sqrlly/runtime/foreman.py`, import it:

```python
from sqrlly.runtime.worktree_share import ensure_setup, materialize_shares
```

Add a private helper on `ForemanExecutor` to centralize the call (DRY across the three sites):

```python
    async def _ensure_worktree_ready(self, dest: str) -> None:
        """Materialize read-only shares + run rehydrate setup for a worktree
        about to be handed back. Idempotent (share symlinks + setup sentinel).
        Called on every hand-back path so a resumed/group/fresh tree is ready."""
        materialize_shares(self._base, dest, self._settings.worktree_share)
        await ensure_setup(
            base=self._base, dest=dest,
            commands=self._settings.worktree_setup,
            excludes=self._settings.worktree_setup_exclude,
            store_dir=self._settings.worktree_setup_store_dir,
        )
```

Replace the Task-3 `materialize_shares(...)` calls in `_create_worktree` with `await self._ensure_worktree_ready(str(dest))` at BOTH points (group early-return and post-`git worktree add`). Because `_create_worktree` is `async`, awaiting here is fine. (The group early-return now reads `await self._ensure_worktree_ready(str(dest)); return str(dest)`.)

In `_acquire_worktree`, the retry/resume early-return currently returns an existing tree **without** going through `_create_worktree`. Add the readiness call there too. Replace:

```python
            existing = self._worktrees.get(node_id)
            if existing and Path(existing).is_dir():
                return existing
```

with (release the lock before the subprocess work, per the documented no-subprocess-under-lock rule — capture the path, then ensure-ready outside the lock):

```python
            existing = self._worktrees.get(node_id)
        if existing and Path(existing).is_dir():
            await self._ensure_worktree_ready(existing)
            return existing
        async with self._worktree_lock:
```

(Re-acquire the lock to continue into the dedup block. The `git worktree add` in `_create_worktree` already runs outside the lock; `_ensure_worktree_ready` for the fresh-create path runs inside `_create_worktree`, also outside the `_acquire_worktree` lock. Confirm no `self._worktree_lock` is held while awaiting `ensure_setup`.)

GC-before-setup: `_create_worktree` does not record `_worktrees` (that's `_acquire_worktree` after the task resolves). For the fresh-create path, register the tree for reclaim BEFORE setup runs so a setup failure still reclaims it. Simplest: in `_create_worktree`, before calling `_ensure_worktree_ready`, add the dest to a reclaim set the foreman already owns — use `self._worktrees` is keyed by node_id (not available here), so instead add a dedicated `self._created_paths: set[str]` populated in `__init__` (`self._created_paths = set()`), append `dest` to it right after `git worktree add` succeeds and before `_ensure_worktree_ready`, and include `self._created_paths` in `reclaim()`'s `distinct` set.

Update `__init__` to add `self._created_paths: set[str] = set()`, and `reclaim()` to union it:

```python
        distinct = sorted(set(self._worktrees.values()) | self._created_paths)
        ...
        self._worktrees.clear()
        self._created_paths.clear()
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/unit/runtime/test_foreman.py -v`
Expected: PASS (the new setup test + all pre-existing foreman tests — empty `worktree_setup` makes `_ensure_worktree_ready` a no-op).

- [ ] **Step 5: Run the worktree-touching e2e regressions**

Run: `uv run pytest tests/e2e/test_fanout_worktrees.py tests/e2e/test_subgraph_fanout_worktree.py tests/unit/cli/test_cli.py -q`
Expected: PASS — fan-out/subgraph worktree creation + promote/GC unaffected by the (default-empty) sharing hooks.

- [ ] **Step 6: Commit**

```bash
git add src/sqrlly/runtime/foreman.py tests/unit/runtime/test_foreman.py
git commit -m "feat: wire worktree share + rehydrate setup into foreman (create/group/resume)"
```

---

### Task 7: `validate` lint — Prisma-generate-without-exclude footgun

**Files:**
- Modify: `src/sqrlly/compile/lint.py` (`collect_warnings`)
- Test: `tests/unit/compile/test_lint.py` (or wherever `collect_warnings` is tested)

- [ ] **Step 1: Write the failing test**

Find the existing `collect_warnings` test file (`rg -l collect_warnings tests/`); append:

```python
def test_warns_prisma_generate_without_exclude():
    from sqrlly.compile.lint import collect_warnings
    from sqrlly.schema.models import Graph
    g = Graph(name="T", version="1.0",
              nodes=[{"id": "n", "name": "n", "execute": {"url": "t.md"}}],
              settings={"worktree_setup": ["pnpm exec prisma generate"]})
    warnings = collect_warnings(g)
    assert any("prisma generate" in w and "worktree_setup_exclude" in w for w in warnings)


def test_no_warn_when_exclude_present():
    from sqrlly.compile.lint import collect_warnings
    from sqrlly.schema.models import Graph
    g = Graph(name="T", version="1.0",
              nodes=[{"id": "n", "name": "n", "execute": {"url": "t.md"}}],
              settings={"worktree_setup": ["pnpm exec prisma generate"],
                        "worktree_setup_exclude": ["src/generated/prisma"]})
    assert not any("prisma generate" in w for w in collect_warnings(g))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/compile/test_lint.py -k prisma -v` (adjust path to the actual lint test file)
Expected: FAIL — no such warning emitted.

- [ ] **Step 3: Add the lint**

In `src/sqrlly/compile/lint.py`, add a check and include it in `collect_warnings`:

```python
def collect_warnings(config: Graph) -> list[str]:
    """Advisory warnings for a workflow config, in declaration order."""
    return (
        _hyphenated_id_warnings(config)
        + _advisory_gate_warnings(config)
        + _worktree_setup_exclude_warnings(config)
    )


def _worktree_setup_exclude_warnings(config: Graph) -> list[str]:
    """Flag a worktree_setup that generates an in-tree artifact (prisma
    generate) without a worktree_setup_exclude — the generated client would
    leak into the promote footprint."""
    s = config.settings
    runs_prisma_generate = any(
        "prisma generate" in cmd for cmd in s.worktree_setup
    )
    if runs_prisma_generate and not s.worktree_setup_exclude:
        return [
            "settings.worktree_setup runs 'prisma generate' but "
            "worktree_setup_exclude is empty — the generated client may leak "
            "into the promote footprint. Add its output path (e.g. "
            "'src/generated/prisma' or 'node_modules') to worktree_setup_exclude."
        ]
    return []
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/compile/test_lint.py -v`
Expected: PASS (new tests + pre-existing lint tests).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/compile/lint.py tests/unit/compile/test_lint.py
git commit -m "feat: lint — warn on prisma generate in worktree_setup without an exclude"
```

---

### Task 8: Documentation

**Files:**
- Modify: `SCHEMA.md`, `CLAUDE.md`, `CHANGELOG.md`

- [ ] **Step 1: SCHEMA.md — new Settings fields**

In the `Settings` field table, add rows (matching the existing `| field | type | default | description |` format with `\|` escapes) for: `promote_exclude` (`list[str]`, `[]` — pathspecs filtered from every promoting node's footprint); `worktree_share` (`list[str]`, `[]` — read-only whole-dir symlinks of base paths into each worktree); `worktree_setup` (`list[str]`, `[]` — ordered commands run in each fresh worktree); `worktree_setup_exclude` (`list[str]`, `[]` — paths written to each worktree's info/exclude); `worktree_setup_store_dir` (`str \| None`, `None` — package store dir for the setup commands).

- [ ] **Step 2: CLAUDE.md — Known limitations / layout**

Add a "Known limitations" bullet: `**Worktree dep sharing** — base gitignored deps reach branch worktrees two ways: `worktree_share` (read-only whole-dir symlink, for read-only gates) and `worktree_setup` (per-worktree rehydrate commands, sentinel-gated, fatal-per-branch). Both write the shared paths to the worktree's `info/exclude` so they stay out of the promote footprint; `promote_exclude` is the promote-layer backstop. Wired in `runtime/foreman.py` via `runtime/worktree_share.py`.` Add `worktree_share.py` to the runtime layout list.

- [ ] **Step 3: CHANGELOG.md — Unreleased**

Add a top `## [Unreleased] — worktree dependency sharing` section with `### Added` listing `settings.worktree_share`, `settings.worktree_setup` (+ `_exclude` / `_store_dir`), and `settings.promote_exclude`.

- [ ] **Step 4: Sanity check**

Run: `uv run sqrlly validate examples/jokes/workflow.yaml`
Expected: `Valid: ...`.

- [ ] **Step 5: Commit**

```bash
git add SCHEMA.md CLAUDE.md CHANGELOG.md
git commit -m "docs: worktree dependency sharing (worktree_share / worktree_setup / promote_exclude)"
```

---

### Final verification

- [ ] `uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -q` — all pass.
- [ ] `uv run pytest tests/architecture/test_layers.py -q` — `runtime/worktree_share.py` is langgraph-free; lint stays in compile.

> **Note (consumer-side, NOT in this plan's CI):** the design doc's 6-test pnpm/Prisma validation (hardlink-inode, anchored/monorepo footprint, per-branch Prisma isolation, etc.) is a pre-merge step against the real builder-sqrlly repo — it needs pnpm/Prisma installed and is out of scope for sqrlly's unit/e2e suite, which tests the mechanism with generic dirs/commands.

---

## Self-Review

**1. Spec coverage:**
- Universal `.git/info/exclude` write → Task 2 (`write_worktree_excludes`), used by Tasks 3 (share) + 5 (setup). ✓
- Read-only `worktree_share` symlink path → Task 3. ✓
- Rehydrate `worktree_setup` + `_exclude` + `_store_dir`, sentinel (base-hash), fatal-per-branch, GC-before-setup → Tasks 4–6. ✓
- `promote_exclude` (promote-layer, independent) → Task 1. ✓
- `validate` lint (prisma-generate-without-exclude) → Task 7. ✓
- Chokepoint wiring at create + group early-return + resume rehydrate → Task 6. ✓
- Decisions: base-hash sentinel (Task 5 `setup_fingerprint`), explicit exclude (Task 4 + lint Task 7), fatal-per-branch (Task 5 raises), no dedicated semaphore (none added — relies on `max_parallel_jobs`), ship `promote_exclude` (Task 1). ✓
- Per-Node `worktree_setup` override is intentionally **out of scope for v1** (the `_create_worktree` chokepoint has no Node; Settings-level reaches all paths incl. fan-out branch trees). Documented here as the deferred follow-up.

**2. Placeholder scan:** No TBD/"handle errors"/"similar to". Every code step has complete code; every run step has a command + expected result. (Task 7's test-file path says "adjust to the actual lint test file" — the implementer must `rg -l collect_warnings tests/` first; that's a locate instruction, not a placeholder, and the test code is complete.)

**3. Type consistency:** `write_worktree_excludes(worktree, paths)`, `materialize_shares(base, dest, shares)`, `ensure_setup(*, base, dest, commands, excludes, store_dir, retries=1)`, `setup_fingerprint(base, commands)` — all defined in Tasks 2/3/5 and called with those exact signatures in Task 6's `_ensure_worktree_ready`. `discover_changes(worktree, globs=None, excludes=None)` and `reconcile_promotions(specs, base, mode, excludes=None)` — extended in Task 1, called with `excludes=` in Task 1's cli wiring. `Settings` fields (`promote_exclude`, `worktree_share`, `worktree_setup`, `worktree_setup_exclude`, `worktree_setup_store_dir`) defined across Tasks 1/3/4, read in Tasks 1/6/7. Consistent.
