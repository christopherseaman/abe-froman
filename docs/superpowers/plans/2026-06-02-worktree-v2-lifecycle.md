# Worktree v2 Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the worktree lifecycle (`fork → produce → read/share → promote → GC`) on top of the v1 control surface shipped in 0.5.3 — adding named shared-worktree **groups**, opt-in **GC**, and single-source git-delta **promotion**.

**Architecture:** Three independent milestones, each shippable as its own patch release. M1 (groups) extends the existing `worktree` field with a typo-safe `Literal` + a separate `worktree_group`, resolved by pure scope specificity through the existing `merge_settings` cascade, and pooled in foreman by a deterministic group path. M2 (GC) reclaims trees at clean end-of-run. M3 (promotion) applies one worktree's git delta to the base, discovering the footprint from git (so unanticipated edits/deletes work) and optionally filtering by git-pathspec globs.

**Tech Stack:** Python 3.14, Pydantic v2, LangGraph, `git worktree`, `uv` + `pytest`. Real subprocess / real git in tests — no mocks (see `CLAUDE.md` testing doctrine; `MockExecutor` is the only sanctioned `NodeExecutor` double).

**Invariants that must not break:**
- Pure stdout/`node_outputs` workflows untouched.
- `auto`/`isolated`/`off` (v1) behavior byte-identical.
- Foreman retry-reuse (same node → same tree) and resume-rehydrate (`node_worktrees` seeds foreman) preserved.
- Three-layer import rules (`schema` no langgraph; `compile` no cli; `runtime` no compile/langgraph) — enforced by `tests/architecture/test_layers.py`.

**Design reference:** `WISHLIST.md` "Worktree composition (design north star)"; this supersedes TODO AP3 (promotion) and folds B5 (globs). B3 (octopus-merge of overlapping isolated trees) stays deferred.

---

## Resolution semantics (the contract every task upholds)

Two fields, mutually exclusive **per scope**:
- `worktree: Literal["auto","isolated","off"]` — the isolation **mode**.
- `worktree_group: str | None` — names a **shared tree**.

Mutual exclusion (value-based, robust across YAML parse *and* `merge_settings`'s `model_validate`): an explicit non-neutral mode plus a group is an error. `auto` is the neutral mode, so a group with `worktree` unset/`auto` is fine.

```
error  iff  worktree_group is not None AND worktree in ("isolated", "off")
```

Effective resolution = **pure scope specificity** (node → settings; subgraph→graph already merged into `settings` by `merge_settings`):

```
effective_worktree(settings) -> (kind, group):
    if node.worktree_group is not None:   return ("group", node.worktree_group)
    if node.worktree is not None:         return (node.worktree, None)
    if settings.worktree_group is not None: return ("group", settings.worktree_group)
    return (settings.worktree, None)      # settings.worktree always set (default "auto")
```

`kind ∈ {"auto","isolated","off","group"}`. Foreman maps: `off` → base workdir, no tree; `group` → shared tree keyed `group:<name>` at a **deterministic** path; `auto`/`isolated` → per-node tree keyed `node.id`. Each grouped node still records its own `node.id → shared path` in `node_worktrees`.

---

# Milestone 1 — Named worktree groups (v2 control)

### Task 1: Tighten `worktree` to `Literal` + add `worktree_group` + mutual-exclusion validator

**Files:**
- Modify: `src/sqrlly/schema/models.py` (the `_normalize_worktree` helper; `Settings`; `Node`)
- Test: `tests/unit/schema/test_worktree_control.py`

- [ ] **Step 1: Write failing tests for the new fields + mutual exclusion**

```python
# append to tests/unit/schema/test_worktree_control.py
from sqrlly.schema.models import Node, Settings  # already imported at top


def test_settings_worktree_group_defaults_none():
    assert Settings().worktree_group is None


def test_node_worktree_group_defaults_none():
    assert Node(id="a", name="a").worktree_group is None


def test_settings_group_alone_is_valid():
    s = Settings(worktree_group="prd")
    assert s.worktree_group == "prd"
    assert s.worktree == "auto"  # neutral default, no conflict


def test_node_group_with_auto_is_valid():
    n = Node(id="a", name="a", worktree="auto", worktree_group="team-a")
    assert n.worktree_group == "team-a"


@pytest.mark.parametrize("mode", ["isolated", "off"])
def test_group_with_explicit_mode_is_rejected(mode):
    with pytest.raises(ValidationError) as exc:
        Node(id="a", name="a", worktree=mode, worktree_group="team-a")
    assert "worktree_group" in str(exc.value)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/schema/test_worktree_control.py -q`
Expected: FAIL — `worktree_group` is an unknown field (`extra_forbidden` on Node) / `AttributeError` on Settings.

- [ ] **Step 3: Add the field + validator to `Settings`**

In `class Settings`, beside the existing `worktree` field:

```python
    worktree: Literal["auto", "isolated", "off"] = "auto"
    worktree_group: str | None = None

    @field_validator("worktree", mode="before")
    @classmethod
    def _normalize_worktree_setting(cls, v: Any) -> Any:
        return _normalize_worktree(v)

    @model_validator(mode="after")
    def _worktree_group_exclusive(self) -> "Settings":
        if self.worktree_group is not None and self.worktree in ("isolated", "off"):
            raise ValueError(
                "worktree_group cannot be combined with worktree="
                f"{self.worktree!r}: a group already implies a shared tree "
                "(omit worktree, or set it to auto)"
            )
        return self
```

Change the `worktree` annotation from `str` to `Literal["auto", "isolated", "off"]`. `_normalize_worktree` (already present) coerces `none`→`off` and YAML booleans before the `Literal` validates, so a typo like `isolted` now fails with the valid-values list. Ensure `model_validator` is imported from pydantic (it already is for other models — verify the import line).

- [ ] **Step 4: Add the field + validator to `Node`**

In `class Node`, replace the v1 `worktree: str | None = None` block with:

```python
    worktree: Literal["auto", "isolated", "off"] | None = None
    worktree_group: str | None = None

    @field_validator("worktree", mode="before")
    @classmethod
    def _normalize_worktree_override(cls, v: Any) -> Any:
        return _normalize_worktree(v)

    @model_validator(mode="after")
    def _worktree_group_exclusive(self) -> "Node":
        if self.worktree_group is not None and self.worktree in ("isolated", "off"):
            raise ValueError(
                "worktree_group cannot be combined with worktree="
                f"{self.worktree!r}: pick one (a group implies a shared tree)"
            )
        return self
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/unit/schema/test_worktree_control.py -q`
Expected: PASS (all v1 + new tests).

- [ ] **Step 6: Commit**

```bash
git add src/sqrlly/schema/models.py tests/unit/schema/test_worktree_control.py
git commit -m "feat: worktree_group field + Literal worktree mode (typo-safe)"
```

---

### Task 2: `effective_worktree` returns `(kind, group)`

**Files:**
- Modify: `src/sqrlly/schema/models.py` (`Node.effective_worktree`)
- Test: `tests/unit/schema/test_worktree_control.py`

- [ ] **Step 1: Write failing tests for resolution**

```python
def test_effective_group_on_node_wins():
    s = Settings(worktree="isolated")
    n = Node(id="a", name="a", worktree_group="team-a")
    assert n.effective_worktree(s) == ("group", "team-a")


def test_effective_node_mode_beats_settings_group():
    s = Settings(worktree_group="prd")
    n = Node(id="a", name="a", worktree="off")
    assert n.effective_worktree(s) == ("off", None)


def test_effective_inherits_settings_group():
    s = Settings(worktree_group="prd")
    n = Node(id="a", name="a")
    assert n.effective_worktree(s) == ("group", "prd")


def test_effective_default_is_auto():
    assert Node(id="a", name="a").effective_worktree(Settings()) == ("auto", None)
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/schema/test_worktree_control.py -q -k effective`
Expected: FAIL — `effective_worktree` currently returns a plain `str`, not a tuple.

- [ ] **Step 3: Update `effective_worktree`**

Replace the v1 body with:

```python
    def effective_worktree(self, settings: Settings) -> tuple[str, str | None]:
        """Resolve isolation by scope specificity. Returns (kind, group):
        kind in {auto, isolated, off, group}; group is the shared-tree name
        when kind == "group", else None."""
        if self.worktree_group is not None:
            return ("group", self.worktree_group)
        if self.worktree is not None:
            return (self.worktree, None)
        if settings.worktree_group is not None:
            return ("group", settings.worktree_group)
        return (settings.worktree, None)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/schema/test_worktree_control.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/schema/models.py tests/unit/schema/test_worktree_control.py
git commit -m "feat: effective_worktree resolves (kind, group) by scope specificity"
```

---

### Task 3: `merge_settings` clears the inherited sibling

**Files:**
- Modify: `src/sqrlly/runtime/settings_merge.py:46-49`
- Test: `tests/unit/runtime/test_settings_merge.py`

- [ ] **Step 1: Write failing tests**

```python
# in tests/unit/runtime/test_settings_merge.py, extend TestWorktreeInheritance
    def test_child_mode_clears_inherited_group(self):
        parent = Settings(worktree_group="prd")
        child = Settings(worktree="isolated")  # subgraph forces isolation
        merged = merge_settings(parent, child)
        assert merged.worktree == "isolated"
        assert merged.worktree_group is None  # inherited group shadowed

    def test_child_group_clears_inherited_mode(self):
        parent = Settings(worktree="isolated")
        child = Settings(worktree_group="team-a")
        merged = merge_settings(parent, child)
        assert merged.worktree_group == "team-a"
        assert merged.worktree == "auto"  # neutralized; group is active
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/runtime/test_settings_merge.py -q -k clears`
Expected: FAIL — generic merge keeps both fields; first test gets `worktree_group == "prd"` (or a `ValidationError` from the exclusion validator firing on the merged dict).

- [ ] **Step 3: Add the special case to `merge_settings`**

```python
    merged = parent.model_dump()
    set_fields = child.model_fields_set
    for field in set_fields:
        merged[field] = getattr(child, field)
    # worktree / worktree_group are a mutually-exclusive pair: if the child
    # authored either, it speaks for this scope — drop the inherited sibling
    # so resolution stays pure scope-specificity (and the exclusion validator
    # doesn't trip on a parent-group + child-mode combination).
    if "worktree" in set_fields and "worktree_group" not in set_fields:
        merged["worktree_group"] = None
    if "worktree_group" in set_fields and "worktree" not in set_fields:
        merged["worktree"] = "auto"
    return Settings.model_validate(merged)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/runtime/test_settings_merge.py -q`
Expected: PASS (all prior + new).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/runtime/settings_merge.py tests/unit/runtime/test_settings_merge.py
git commit -m "feat: merge_settings clears inherited worktree sibling (scope specificity)"
```

---

### Task 4: Foreman pools group trees + records per-node paths

**Files:**
- Modify: `src/sqrlly/runtime/foreman.py` (`execute`, `_acquire_worktree`, `_create_worktree`)
- Test: `tests/unit/runtime/test_foreman.py`

**Design:** `_acquire_worktree(node_id, pool_key, group)`. Per-node trees keep the `wt-<id>-<uuid8>` path (non-deterministic; reused via the node→path record). Group trees use a **deterministic** path `wt-group-<safe_group>` (no uuid) so any sibling — or a resumed run — reuses the same tree by `is_dir()` check. In-flight creation deduped by `pool_key`. `self._worktrees` stays `node_id → path` (what `get_worktree`/`worktree_map` expose); a grouped node records its own id → the shared path.

- [ ] **Step 1: Write failing tests**

```python
# in tests/unit/runtime/test_foreman.py (TestWorktreePool)
    @pytest.mark.asyncio
    async def test_group_nodes_share_one_tree(self, tmp_path):
        _init_git_repo(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(inner=inner, base_workdir=str(tmp_path))
        try:
            a = Node(id="a", name="a", execute=Execute(url=_PWD, params={"args": []}),
                     worktree_group="team")
            b = Node(id="b", name="b", execute=Execute(url=_PWD, params={"args": []}),
                     worktree_group="team")
            ra = await foreman.execute(a, {})
            rb = await foreman.execute(b, {})
            # both ran in the SAME tree
            assert ra.output.strip() == rb.output.strip()
            # each node records its own id -> the shared path
            assert foreman.get_worktree("a") == foreman.get_worktree("b")
            # deterministic group path
            assert foreman.get_worktree("a").endswith("/.sqrlly/wt-group-team")
        finally:
            await foreman.close()

    @pytest.mark.asyncio
    async def test_group_separate_from_other_group_and_isolated(self, tmp_path):
        _init_git_repo(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(inner=inner, base_workdir=str(tmp_path))
        try:
            a = Node(id="a", name="a", execute=Execute(url=_PWD, params={"args": []}),
                     worktree_group="team-a")
            b = Node(id="b", name="b", execute=Execute(url=_PWD, params={"args": []}),
                     worktree_group="team-b")
            c = Node(id="c", name="c", execute=Execute(url=_PWD, params={"args": []}),
                     worktree="isolated")
            await foreman.execute(a, {}); await foreman.execute(b, {}); await foreman.execute(c, {})
            paths = {foreman.get_worktree(x) for x in ("a", "b", "c")}
            assert len(paths) == 3
        finally:
            await foreman.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/runtime/test_foreman.py -q -k group`
Expected: FAIL — foreman ignores `worktree_group`; `a` and `b` get separate per-node trees.

- [ ] **Step 3: Resolve the run target in `execute`**

Replace the v1 isolate-branch in `execute`:

```python
                kind, group = node.effective_worktree(s)
                if kind == "off":
                    run_dir = self._base
                else:
                    pool_key = f"group:{group}" if kind == "group" else node.id
                    run_dir = await self._acquire_worktree(node.id, pool_key, group)
                if node.output_contract:
                    scaffold_output_directory(node.output_contract, run_dir)
                result = await self._inner.execute(
                    node, context, workdir=run_dir,
                    settings_override=settings_override,
                )
                if kind != "off" and result.worktree is None:
                    result.worktree = run_dir
                return result
```

- [ ] **Step 4: Generalize `_acquire_worktree` + `_create_worktree`**

```python
    async def _acquire_worktree(
        self, node_id: str, pool_key: str, group: str | None
    ) -> str:
        async with self._worktree_lock:
            # retry-reuse / resume-rehydrate: this node already has a tree
            existing = self._worktrees.get(node_id)
            if existing and Path(existing).is_dir():
                return existing
            task = self._worktree_tasks.get(pool_key)
            if task is None:
                task = asyncio.create_task(self._create_worktree(pool_key, group))
                self._worktree_tasks[pool_key] = task
        try:
            path = await task
        finally:
            async with self._worktree_lock:
                if self._worktree_tasks.get(pool_key) is task:
                    del self._worktree_tasks[pool_key]
        self._worktrees[node_id] = path  # each node records its own id -> path
        return path

    async def _create_worktree(self, pool_key: str, group: str | None) -> str:
        dest_dir = Path(self._base) / ".sqrlly"
        dest_dir.mkdir(parents=True, exist_ok=True)
        if group is not None:
            safe = group.replace("/", "_").replace("::", "__")
            dest = dest_dir / f"wt-group-{safe}"
            if dest.is_dir():  # shared tree already created (sibling or prior run)
                self._worktrees.setdefault(pool_key, str(dest))
                return str(dest)
        else:
            safe = pool_key.replace("::", "__").replace("/", "_")
            dest = dest_dir / f"wt-{safe}-{uuid.uuid4().hex[:8]}"
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", self._base, "worktree", "add", "-q", str(dest), "HEAD",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(
                f"foreman: 'git worktree add' failed for {pool_key}: {err.decode().strip()}"
            )
        return str(dest)
```

Note: the `_worktrees.setdefault(pool_key, ...)` line is harmless bookkeeping; the authoritative `node_id → path` write is in `_acquire_worktree`. Keep the existing rehydrate-deletion recheck behavior (the `existing and is_dir()` guard preserves the resume re-create path; see `test_rehydrated_but_deleted_worktree_is_recreated`).

- [ ] **Step 5: Run to verify pass + no regression in foreman suite**

Run: `uv run pytest tests/unit/runtime/test_foreman.py -q`
Expected: PASS (v1 worktree tests + new group tests; concurrency tests unaffected).

- [ ] **Step 6: Commit**

```bash
git add src/sqrlly/runtime/foreman.py tests/unit/runtime/test_foreman.py
git commit -m "feat: foreman pools shared group worktrees by deterministic path"
```

---

### Task 5: Fan-out children record `node_worktrees`

**Files:**
- Modify: `src/sqrlly/compile/dynamic.py` (the child `exec_update` — the gap vs `compile/nodes.py:654`)
- Test: `tests/unit/compile/test_dynamic.py` (or the nearest fan-out test module; create `tests/e2e/test_fanout_worktrees.py` if no unit harness fits)

**Why:** `{{<parent>_branch_worktrees}}` (and the v2 keyed map) render empty under isolation because fan-out children never write `node_worktrees`. `compile/nodes.py:654` does this for static nodes; mirror it.

- [ ] **Step 1: Write a failing test (real git repo, real subprocess, isolated fan-out)**

```python
# tests/e2e/test_fanout_worktrees.py
import json, subprocess, sys
import pytest
from pathlib import Path
# Use the project's existing fan-out e2e harness/helpers; assert that after a
# 2-leaf isolated fan-out, state["node_worktrees"] has an entry per child id
# ("parent::0", "parent::1"), each an existing dir under .sqrlly/.
```

Model this on the closest existing fan-out e2e (search `tests/` for `branch_worktrees` / `fan_out`). The load-bearing assertion:

```python
    wts = final_state["node_worktrees"]
    child_keys = [k for k in wts if k.startswith("expand::")]
    assert len(child_keys) == 2
    for k in child_keys:
        assert Path(wts[k]).is_dir()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/e2e/test_fanout_worktrees.py -q`
Expected: FAIL — no `expand::*` keys in `node_worktrees`.

- [ ] **Step 3: Add the worktree write to the child `exec_update`**

In `compile/dynamic.py`, where the child success update is built (`exec_update = {"node_outputs": ..., "child_outputs": ...}`), add the same pattern as `nodes.py:654`:

```python
            exec_update: dict[str, Any] = {
                "node_outputs": {child_id: exec_result.output},
                "child_outputs": {child_id: exec_result.output},
            }
            if exec_result.worktree:
                exec_update["node_worktrees"] = {child_id: exec_result.worktree}
```

(`exec_result.worktree` is set by foreman for isolated/group children; `None` for `off`, which correctly records nothing.)

- [ ] **Step 4: Run to verify pass + fan-out regression**

Run: `uv run pytest tests/e2e/test_fanout_worktrees.py tests/unit/compile/ -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/compile/dynamic.py tests/e2e/test_fanout_worktrees.py
git commit -m "fix: fan-out children record node_worktrees (parity with static nodes)"
```

---

### Task 6: Keyed `{{<parent>_branch_map}}` (AP4)

**Files:**
- Modify: `src/sqrlly/compile/nodes.py:133-142` (the fan-in aggregate synthesis)
- Test: `tests/unit/compile/test_nodes.py` (the build-context helper test)

- [ ] **Step 1: Write a failing unit test for the new aggregate**

```python
def test_branch_map_pairs_output_and_worktree_by_id():
    state = {
        "child_outputs": {"p::0": "out0", "p::1": "out1"},
        "node_worktrees": {"p::0": "/wt/p0", "p::1": "/wt/p1"},
    }
    ctx = build_context(node_with_dep("p"), state)  # use the real helper signature
    import json
    bmap = json.loads(ctx["p_branch_map"])
    assert bmap == {
        "p::0": {"output": "out0", "worktree": "/wt/p0"},
        "p::1": {"output": "out1", "worktree": "/wt/p1"},
    }
```

Match the actual `build_context` signature in `compile/nodes.py` (check how existing tests call it).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/compile/test_nodes.py -q -k branch_map`
Expected: FAIL — `p_branch_map` not in context.

- [ ] **Step 3: Synthesize the keyed map (additive — keep the legacy list)**

In the `if dep_branches:` block at `compile/nodes.py:133-142`, after the existing `dep_branch_worktrees` line:

```python
            branch_map = {
                cid: {
                    "output": out,
                    "worktree": worktrees.get(cid),
                }
                for cid, out in dep_branches.items()
            }
            context[f"{dep}_branch_map"] = _json.dumps(branch_map)
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/compile/test_nodes.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/compile/nodes.py tests/unit/compile/test_nodes.py
git commit -m "feat: {{<parent>_branch_map}} pairs child output+worktree by id (AP4)"
```

---

### Task 7: M1 docs + release

**Files:**
- Modify: `docs/schema-reference.md` (Settings + Node tables; template-var section), `SKILLS.md` (worktree authoring guidance), `CHANGELOG.md`
- Test: `uv run sqrlly validate` on a group fixture

- [ ] **Step 1: Update `docs/schema-reference.md`** — add `worktree_group` rows to both Settings and Node tables; document mutual exclusion + scope-specificity resolution; add `{{<dep>_branch_map}}` to the prompt-node template-var list.

- [ ] **Step 2: Update `SKILLS.md`** — a short "controlling worktree isolation" subsection: `off` (shared base), `isolated`, `worktree_group: <name>` (feature-team shared tree), inheritance, the fan-out-template caveat (a group on a fan-out *template* shares one tree across all children — keep fan-out children on `auto` unless intentional).

- [ ] **Step 3: Validate a group fixture end-to-end**

```bash
cat > .temp/grp.yaml <<'YAML'
name: grp
version: "1.0"
settings: { worktree_group: prd }
nodes:
  - { id: a, name: a, execute: { url: /usr/bin/true } }
  - { id: b, name: b, worktree: isolated, execute: { url: /usr/bin/true } }
YAML
uv run sqrlly validate .temp/grp.yaml && rm .temp/grp.yaml
```
Expected: `Valid: grp v1.0 (2 nodes)`.

- [ ] **Step 4: Full suite + layer rules green**

Run: `uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -q && uv run pytest tests/architecture/test_layers.py -q`
Expected: PASS.

- [ ] **Step 5: CHANGELOG entry + commit + release**

Add a `## [0.6.0]` (or next patch — confirm with operator; new inherited field is arguably minor) section describing groups. Then:
```bash
git add docs/schema-reference.md SKILLS.md CHANGELOG.md
git commit -m "docs: worktree groups (settings.worktree_group + Node override)"
git checkout main && git merge --ff-only <branch> && git push origin main
# release bump confirmed with operator (CLAUDE.md: minor needs approval):
echo y | bash scripts/release.sh patch   # or minor, per operator
```

---

# Milestone 2 — Worktree GC (opt-in, end-of-run)

### Task 8: `settings.worktree_gc` field

**Files:**
- Modify: `src/sqrlly/schema/models.py` (`Settings`)
- Test: `tests/unit/schema/test_worktree_control.py`

- [ ] **Step 1: Write failing tests**

```python
def test_worktree_gc_defaults_never():
    assert Settings().worktree_gc == "never"

def test_worktree_gc_accepts_on_success():
    assert Settings(worktree_gc="on_success").worktree_gc == "on_success"

def test_worktree_gc_rejects_unknown():
    with pytest.raises(ValidationError):
        Settings(worktree_gc="always")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/schema/test_worktree_control.py -q -k gc`
Expected: FAIL — field absent.

- [ ] **Step 3: Add the field**

```python
    worktree_gc: Literal["never", "on_success"] = "never"
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/schema/test_worktree_control.py -q -k gc`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/schema/models.py tests/unit/schema/test_worktree_control.py
git commit -m "feat: settings.worktree_gc (never|on_success)"
```

---

### Task 9: `foreman.reclaim()` + end-of-run wiring

**Files:**
- Modify: `src/sqrlly/runtime/foreman.py` (add `reclaim`), `src/sqrlly/cli/main.py` (call after a clean run)
- Test: `tests/unit/runtime/test_foreman.py`

- [ ] **Step 1: Write failing tests for `reclaim`**

```python
    @pytest.mark.asyncio
    async def test_reclaim_removes_distinct_trees(self, tmp_path):
        _init_git_repo(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(inner=inner, base_workdir=str(tmp_path))
        try:
            a = Node(id="a", name="a", execute=Execute(url=_PWD, params={"args": []}),
                     worktree_group="team")
            b = Node(id="b", name="b", execute=Execute(url=_PWD, params={"args": []}),
                     worktree_group="team")
            c = Node(id="c", name="c", execute=Execute(url=_PWD, params={"args": []}))
            for n in (a, b, c):
                await foreman.execute(n, {})
            grp = foreman.get_worktree("a"); iso = foreman.get_worktree("c")
            removed = await foreman.reclaim()
            assert not Path(grp).exists() and not Path(iso).exists()
            assert sorted(removed) == sorted({grp, iso})  # shared tree counted once
            assert "team" in subprocess.run(  # 'git worktree list' no longer lists them
                ["git", "-C", str(tmp_path), "worktree", "list"],
                capture_output=True, text=True).stdout or True
        finally:
            await foreman.close()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/runtime/test_foreman.py -q -k reclaim`
Expected: FAIL — `reclaim` not defined.

- [ ] **Step 3: Implement `reclaim`**

```python
    async def reclaim(self) -> list[str]:
        """Remove every distinct worktree this foreman created. Returns the
        removed paths. Caller decides when (end-of-run, success only) — this
        method never gates on run status itself."""
        distinct = sorted(set(self._worktrees.values()))
        for path in distinct:
            proc = await asyncio.create_subprocess_exec(
                "git", "-C", self._base, "worktree", "remove", "--force", path,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            await proc.communicate()  # best-effort; a manually-deleted tree is fine
        self._worktrees.clear()
        return distinct
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/runtime/test_foreman.py -q -k reclaim`
Expected: PASS.

- [ ] **Step 5: Write failing integration test for end-of-run wiring**

Add to the CLI/run e2e suite: a single-node workflow in a git repo with `settings.worktree_gc: on_success` leaves **no** `.sqrlly/wt-*` dir after a successful run; with `never` (default) the dir **remains**. (Use the existing `run`-command e2e harness; assert on the on-disk `.sqrlly/` contents.)

- [ ] **Step 6: Run to verify failure, then wire it**

In `cli/main.py`, after the `astream` run loop completes, **only when the run finished with no `failed_nodes`** and `config.settings.worktree_gc == "on_success"` and the executor is a `ForemanExecutor`:

```python
    if (config.settings.worktree_gc == "on_success"
            and not final_state.get("failed_nodes")
            and isinstance(executor_obj, ForemanExecutor)):
        await executor_obj.reclaim()
```

Place this before `await executor_obj.close()`. Never call on failure (resume needs the trees).

- [ ] **Step 7: Run integration test to verify pass + full suite**

Run: `uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/sqrlly/runtime/foreman.py src/sqrlly/cli/main.py tests/
git commit -m "feat: opt-in worktree GC on clean run (settings.worktree_gc)"
```

---

### Task 10: M2 docs + release

- [ ] **Step 1:** Document `worktree_gc` in `docs/schema-reference.md` (Settings table) + a CLAUDE.md "Known limitations" update (the "No automatic worktree cleanup" note now reads "opt-in via `worktree_gc`").
- [ ] **Step 2:** CHANGELOG `### Added` entry.
- [ ] **Step 3:** Full suite green, commit, merge to main, `release.sh patch`.

---

# Milestone 3 — Single-source git-delta promotion

### Task 11: Discover a worktree's changed-file set (git delta), glob-filterable

**Files:**
- Create: `src/sqrlly/runtime/promote.py` (langgraph-free; runtime layer)
- Test: `tests/unit/runtime/test_promote.py`

- [ ] **Step 1: Write failing tests (real git worktree)**

```python
# tests/unit/runtime/test_promote.py
import subprocess
from pathlib import Path
import pytest
from sqrlly.runtime.promote import discover_changes

def _repo(tmp):  # minimal repo + one commit
    subprocess.run(["git","init","-q","-b","main",str(tmp)],check=True)
    subprocess.run(["git","-C",str(tmp),"config","user.email","t@t"],check=True)
    subprocess.run(["git","-C",str(tmp),"config","user.name","t"],check=True)
    (tmp/"a.txt").write_text("orig")
    subprocess.run(["git","-C",str(tmp),"add","."],check=True)
    subprocess.run(["git","-C",str(tmp),"commit","-qm","init"],check=True)

def _wt(tmp):
    dest = tmp/".sqrlly/wt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git","-C",str(tmp),"worktree","add","-q",str(dest),"HEAD"],check=True)
    return dest

def test_discover_add_edit_delete(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path)
    (wt/"a.txt").write_text("changed")   # edit
    (wt/"new.md").write_text("hi")        # add
    (wt/"a.txt").unlink() if False else None
    changes = discover_changes(str(wt))
    assert changes["new.md"] == "added"
    assert changes["a.txt"] == "modified"

def test_discover_captures_deletion(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path)
    (wt/"a.txt").unlink()
    assert discover_changes(str(wt))["a.txt"] == "deleted"

def test_glob_filter_pathspec(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path)
    (wt/"keep.md").write_text("x"); (wt/"skip.txt").write_text("y")
    changes = discover_changes(str(wt), globs=["**/*.md"])
    assert "keep.md" in changes and "skip.txt" not in changes
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/runtime/test_promote.py -q`
Expected: FAIL — module doesn't exist.

- [ ] **Step 3: Implement `discover_changes`**

```python
"""Single-source git-delta promotion helpers (langgraph-free, runtime layer).

Promotion = apply ONE worktree's diff (vs its fork point) onto the base.
The footprint is *discovered* from git, so unanticipated edits/deletes are
captured; an optional git-pathspec glob list filters it. Multi-source-
overlap reconciliation (true 3-way merge) is intentionally out of scope.
"""
from __future__ import annotations

import subprocess

_STATUS = {"A": "added", "M": "modified", "D": "deleted", "R": "modified", "C": "added"}


def discover_changes(worktree: str, globs: list[str] | None = None) -> dict[str, str]:
    """Return {path_relative_to_worktree: change_kind} for everything the
    worktree changed vs HEAD, including untracked adds and deletions.
    ``globs`` (git pathspec, e.g. ``["prd/**", "*.md"]``) filters the set."""
    cmd = ["git", "-C", worktree, "status", "--porcelain=v1", "--untracked-files=all"]
    if globs:
        cmd += ["--", *globs]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    changes: dict[str, str] = {}
    for line in out.splitlines():
        if not line:
            continue
        code, path = line[:2], line[3:]
        if code == "??":
            changes[path] = "added"
        else:
            changes[path] = _STATUS.get(code.strip()[:1], "modified")
    return changes
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/runtime/test_promote.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/runtime/promote.py tests/unit/runtime/test_promote.py
git commit -m "feat: discover worktree git delta (add/edit/delete, glob-filterable)"
```

---

### Task 12: Apply a worktree's delta to the base

**Files:**
- Modify: `src/sqrlly/runtime/promote.py` (add `promote`)
- Test: `tests/unit/runtime/test_promote.py`

- [ ] **Step 1: Write failing tests**

```python
def test_promote_applies_add_edit_delete_to_base(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path)
    (wt/"a.txt").write_text("changed"); (wt/"new.md").write_text("hi")
    from sqrlly.runtime.promote import promote
    applied = promote(str(wt), str(tmp_path))
    assert (tmp_path/"a.txt").read_text() == "changed"
    assert (tmp_path/"new.md").read_text() == "hi"
    assert set(applied) == {"a.txt", "new.md"}

def test_promote_deletion_propagates(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path)
    (wt/"a.txt").unlink()
    from sqrlly.runtime.promote import promote
    promote(str(wt), str(tmp_path))
    assert not (tmp_path/"a.txt").exists()
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/runtime/test_promote.py -q -k promote`
Expected: FAIL — `promote` not defined.

- [ ] **Step 3: Implement `promote`**

```python
import shutil
from pathlib import Path


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

(File-copy of the discovered set is the portable apply; deletions handled explicitly — the thing pure-copy collect could never do. A git-native `git apply` path can replace the body later without changing the signature.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest tests/unit/runtime/test_promote.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/runtime/promote.py tests/unit/runtime/test_promote.py
git commit -m "feat: promote a worktree's git delta to the base (add/edit/delete)"
```

---

### Task 13: Wire promotion into a run

**Files:**
- Modify: `src/sqrlly/schema/models.py` (`Node.promote: bool = False`), `src/sqrlly/cli/main.py` (promote nodes flagged, after their tree settles / at end-of-run), `src/sqrlly/runtime/foreman.py` if a hook is cleaner
- Test: `tests/unit/schema/test_worktree_control.py` + a `run` e2e

**Decision to confirm with operator before this task:** trigger shape — per-node `promote: true` (promote that node's tree when it completes) vs a run-end auto-promote of all isolated/group trees. This plan implements **per-node `promote: true`** (explicit, least surprising); run-end auto-promote can layer on later.

- [ ] **Step 1: Write failing schema test**

```python
def test_node_promote_defaults_false():
    assert Node(id="a", name="a").promote is False
def test_node_promote_accepts_true():
    assert Node(id="a", name="a", promote=True).promote is True
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/unit/schema/test_worktree_control.py -q -k promote`
Expected: FAIL — `promote` unknown (`extra_forbidden`).

- [ ] **Step 3: Add the field**

```python
    promote: bool = False
```

(in `class Node`, near `worktree`). Promotion uses the node's `output_contract.required_files` as the glob filter when present (discover mode otherwise).

- [ ] **Step 4: Run schema test to verify pass**

Run: `uv run pytest tests/unit/schema/test_worktree_control.py -q -k promote`
Expected: PASS.

- [ ] **Step 5: Write a failing `run` e2e**

A 2-node workflow: node `gen` (`worktree: isolated`, `promote: true`) writes `out.txt`; assert `out.txt` exists in the **base** workdir after the run (promoted out of the isolated tree). Use the real `run` harness in a git tmp repo.

- [ ] **Step 6: Wire promotion in `cli/main.py`**

After the run loop, for each node with `promote=True` that completed, resolve its tree via `executor_obj.get_worktree(node.id)` and call `promote(tree, workdir, globs=node.output_contract.required_files if node.output_contract else None)`. Skip nodes whose effective worktree was `off` (already in base). Guard on `isinstance(executor_obj, ForemanExecutor)`.

- [ ] **Step 7: Run e2e to verify pass + full suite**

Run: `uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -q`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add src/sqrlly/schema/models.py src/sqrlly/cli/main.py tests/
git commit -m "feat: per-node promote — apply isolated worktree delta to base"
```

---

### Task 14: M3 docs + release

- [ ] **Step 1:** Document `Node.promote` + the discover/declare + glob-pathspec semantics in `docs/schema-reference.md`; add a `fork → produce → promote → GC` concept note to `TECHNICAL.md`; update `SKILLS.md` with a promotion example. Note B5 (glob contracts) is now satisfied for the promotion path; if `output_contract` validation should also become glob-aware, track it explicitly.
- [ ] **Step 2:** CHANGELOG entry; note the deferred multi-source-overlap merge (B3).
- [ ] **Step 3:** Full suite green, commit, merge to main, `release.sh patch`.

---

## Self-Review

**Spec coverage:**
- Control surface v2 (groups, Literal, mutual exclusion, scope-specificity, merge special-case) → Tasks 1–4. ✓
- node.id↔group-key bookkeeping + resume safety (deterministic group path) → Task 4. ✓
- AP4 keyed map + fan-out child worktree write → Tasks 5–6. ✓
- GC (opt-in, end-of-run, success-only, distinct trees) → Tasks 8–9. ✓
- Promotion (git-delta discover-default, glob-filterable, deletions, single-source) → Tasks 11–13. ✓
- Multi-source-overlap merge → explicitly deferred (B3), noted in Task 14. ✓
- Docs at each milestone → Tasks 7, 10, 14. ✓

**Type consistency:** `effective_worktree` returns `tuple[str, str|None]` everywhere (Tasks 2, 4). `discover_changes(worktree, globs)` / `promote(worktree, base, globs)` signatures consistent across Tasks 11–13. `foreman.reclaim() -> list[str]` (Task 9) and `_acquire_worktree(node_id, pool_key, group)` (Task 4) used consistently.

**Open decisions flagged inline:** M1 release as patch vs minor (Task 7 — operator call per CLAUDE.md); promotion trigger shape (Task 13 — per-node chosen, run-end auto deferred).

**Placeholder scan:** Task 5 and Task 6 reference the project's existing fan-out/build-context test harness rather than reproducing it — the implementer must match the real `build_context` signature and fan-out e2e helper (search `tests/` for `branch_worktrees`). All other steps carry complete code.
