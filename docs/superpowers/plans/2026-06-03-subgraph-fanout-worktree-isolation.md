# Subgraph Fan-Out Worktree Isolation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Give each subgraph fan-out branch its own isolated worktree (matching inline-template behavior), fixing the gap where all branches shared one inner-node worktree and `{{<parent>_branch_map}}.worktree` was null.

**Architecture (CHILD-IS-THE-UNIT, chosen via design workflow 2026-06-03):** The fan-out *child* is the isolation boundary; the subgraph is its internals. Each Send branch acquires ONE worktree keyed by `child_id`, and the subgraph runs *inside* it with every inner node pinned to that tree (forced `worktree: off` + `workdir=branch_tree`). One tree per branch, keyed `child_id`, surfaced as `ExecutionResult.worktree`, recorded by the already-correct `dynamic.py:210-211`. This reproduces inline-fan-out semantics exactly (1:1 resume/GC parity) and gives free intra-branch read-across.

**Why not the alternatives:** namespace-inner-keys / per-branch-executor-wrapper produce N trees per branch (one per inner node) — over-isolation that contradicts the intent, breaks intra-branch read-across, makes `branch_map.worktree` a partial pointer, and has a confirmed resume/GC defect (non-terminal inner trees never persist to `node_worktrees` → orphan on disk, invisible to `reclaim()`), plus reintroduces the cross-branch race for grouped inner nodes. compile-per-branch adds fragile edge-rewriting for the same wrong semantics.

**Tech Stack:** Python 3.14, Pydantic v2, LangGraph, git worktrees, `uv`+`pytest`. Real-subprocess git-worktree tests (CLAUDE.md doctrine); `MockExecutor` only for the non-foreman no-isolation fallback path.

**Resolved design decisions:**
- `worktree`/`worktree_group` are on **Node** (verified), not `Execute`. `_strip_worktree(node)` = `node.model_copy(update={"worktree":"off","worktree_group":None})` → `effective_worktree` returns `("off", None)` for both explicit-directive and inherit inner nodes. No `settings_override` manipulation needed (node fields win, `models.py:537-547`).
- Template-level `worktree: off` keeps inline parity: branch runs in the passed `workdir`, `ExecutionResult.worktree=None`, `branch_map.worktree` null for those branches.
- Per-branch lazy recompile (moving `compile_fn` into `invoke`): accepted; `compile_fn` is pure in-process LangGraph wiring (config load / cycle detection / `merge_settings` stay at factory build time). Profile only if deep-nesting latency shows up.

**Invariants that must not break:** inline fan-out isolation (`dynamic.py:192-211`, untouched); non-fan-out subgraph (`make_subgraph_node`, untouched); top-level `worktree:off` resolving to base when `workdir=None`; resume-rehydrate; GC `reclaim()`; retry-reuse; three-layer import rules.

Design source: workflow `wf_8e0ab9b3-6ca` (grounding + 4 approaches + critiques).

---

### Task 1: Foreman off-path honors an explicit `workdir`

**Files:** Modify `src/sqrlly/runtime/foreman.py` (the `kind == "off"` branch in `execute`); Test `tests/unit/runtime/test_foreman.py`.

This is the seam that lets the branch tree flow into inner nodes. Today the `off` branch sets `run_dir = self._base`, ignoring `workdir`. Generalize to "off = run where told, default base." Verified behavior-preserving: every current production caller passes `workdir=None` for execution nodes, so top-level `off` nodes still get `self._base`.

- [ ] **Step 1: Failing tests**

```python
    @pytest.mark.asyncio
    async def test_off_node_defaults_to_base_workdir(self, tmp_path):
        _init_git_repo(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(inner=inner, base_workdir=str(tmp_path))
        try:
            node = Node(id="p", name="p",
                        execute=Execute(url=_PWD, params={"args": []}), worktree="off")
            result = await foreman.execute(node, {})            # workdir=None
            assert Path(result.output.strip()).resolve() == Path(tmp_path).resolve()
        finally:
            await foreman.close()

    @pytest.mark.asyncio
    async def test_off_node_honors_explicit_workdir(self, tmp_path):
        _init_git_repo(tmp_path)
        sub = tmp_path / "branch_tree"; sub.mkdir()
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(inner=inner, base_workdir=str(tmp_path))
        try:
            node = Node(id="p", name="p",
                        execute=Execute(url=_PWD, params={"args": []}), worktree="off")
            result = await foreman.execute(node, {}, workdir=str(sub))
            assert Path(result.output.strip()).resolve() == sub.resolve()
        finally:
            await foreman.close()
```

- [ ] **Step 2: Run, verify the explicit-workdir test FAILS** (`off` ignores workdir → runs in base).
  Run: `uv run pytest tests/unit/runtime/test_foreman.py -q -k off_node`

- [ ] **Step 3: Implement** — in `execute`, the `off` branch:

```python
                if kind == "off":
                    run_dir = workdir or self._base
```

- [ ] **Step 4: Run, both pass** + full foreman suite `uv run pytest tests/unit/runtime/test_foreman.py -q`.

- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: foreman off-path honors explicit workdir (default base)"`

---

### Task 2: `_strip_worktree` + `_BranchScopedExecutor`

**Files:** Modify `src/sqrlly/compile/subgraph.py`; Test `tests/unit/compile/test_branch_scoped_executor.py` (new).

A compile-layer wrapper (duck-types the `NodeExecutor` Protocol; compile→runtime dependency direction is already legal). It pins every inner node to the branch tree by forcing the node `off` and passing `workdir=branch_tree`.

- [ ] **Step 1: Failing test** (new file). Use a recording stub implementing the Protocol to capture what the wrapper forwards:

```python
from sqrlly.compile.subgraph import _BranchScopedExecutor
from sqrlly.schema.models import Node, Settings, Execute


class _Recorder:
    def __init__(self): self.calls = []
    async def execute(self, node, context, workdir=None, settings_override=None):
        from sqrlly.runtime.result import ExecutionResult
        self.calls.append((node, workdir))
        return ExecutionResult(success=True, output="ok")


def test_strip_worktree_forces_off_for_explicit_and_inherit():
    from sqrlly.compile.subgraph import _strip_worktree
    explicit = Node(id="a", name="a", worktree="isolated")
    inherit = Node(id="b", name="b")
    assert _strip_worktree(explicit).effective_worktree(Settings(worktree="isolated")) == ("off", None)
    assert _strip_worktree(inherit).effective_worktree(Settings(worktree="auto")) == ("off", None)


import pytest
@pytest.mark.asyncio
async def test_wrapper_pins_workdir_and_neutralizes_node():
    rec = _Recorder()
    wrapped = _BranchScopedExecutor(rec, "/branch/tree")
    node = Node(id="inner", name="inner", worktree="isolated",
                execute=Execute(url="/bin/true", params={"args": []}))
    await wrapped.execute(node, {})
    seen_node, seen_workdir = rec.calls[0]
    assert seen_workdir == "/branch/tree"
    assert seen_node.effective_worktree(Settings(worktree="isolated")) == ("off", None)
```

- [ ] **Step 2: Run, verify FAIL** (symbols don't exist). `uv run pytest tests/unit/compile/test_branch_scoped_executor.py -q`

- [ ] **Step 3: Implement** in `compile/subgraph.py`:

```python
def _strip_worktree(node):
    """Force a fan-out subgraph inner node onto the branch tree: clear its
    own worktree directives to `off` (node fields win in effective_worktree),
    so it runs in the branch worktree, not a per-inner-node tree."""
    return node.model_copy(update={"worktree": "off", "worktree_group": None})


class _BranchScopedExecutor:
    """Wraps the shared executor for one fan-out subgraph branch: every inner
    node is pinned to `branch_tree` (forced `off` + workdir). Duck-types the
    NodeExecutor Protocol; passes backend / get_worktree / worktree_map /
    reclaim / close through to the inner executor."""
    def __init__(self, inner, branch_tree: str):
        self._inner = inner
        self._branch_tree = branch_tree

    async def execute(self, node, context, workdir=None, settings_override=None):
        return await self._inner.execute(
            _strip_worktree(node), context,
            workdir=self._branch_tree, settings_override=settings_override,
        )

    def get_backend(self):
        return self._inner.get_backend() if hasattr(self._inner, "get_backend") else None

    def get_worktree(self, node_id):
        return self._inner.get_worktree(node_id) if hasattr(self._inner, "get_worktree") else None

    def worktree_map(self):
        return self._inner.worktree_map() if hasattr(self._inner, "worktree_map") else {}

    async def reclaim(self):
        if hasattr(self._inner, "reclaim"):
            return await self._inner.reclaim()
        return []

    async def close(self):
        if hasattr(self._inner, "close"):
            await self._inner.close()
```

- [ ] **Step 4: Run, pass.** `uv run pytest tests/unit/compile/test_branch_scoped_executor.py -q`
- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: _BranchScopedExecutor pins subgraph inner nodes to the branch tree"`

---

### Task 3: `ForemanExecutor.acquire_branch_worktree`

**Files:** Modify `src/sqrlly/runtime/foreman.py`; Test `tests/unit/runtime/test_foreman.py`.

- [ ] **Step 1: Failing test**

```python
    @pytest.mark.asyncio
    async def test_acquire_branch_worktree_distinct_and_reused(self, tmp_path):
        _init_git_repo(tmp_path)
        inner = DispatchExecutor(workdir=str(tmp_path))
        foreman = ForemanExecutor(inner=inner, base_workdir=str(tmp_path))
        try:
            a = await foreman.acquire_branch_worktree("parent::0")
            b = await foreman.acquire_branch_worktree("parent::1")
            a2 = await foreman.acquire_branch_worktree("parent::0")
            assert a != b and a == a2
            assert Path(a).is_dir() and Path(b).is_dir()
            assert set(foreman.worktree_map().values()) >= {a, b}
            assert foreman.get_worktree("parent::0") == a
        finally:
            await foreman.close()
```

- [ ] **Step 2: Run, verify FAIL** (method undefined).
- [ ] **Step 3: Implement** (delegates to the existing private acquire; preserves the "_worktrees keyed by real node_id" invariant — `branch_id` is the unique `child_id`):

```python
    async def acquire_branch_worktree(self, branch_id: str) -> str:
        """Acquire (or reuse) one isolated worktree keyed by a fan-out branch
        id — mirrors inline-child keying so GC/resume/retry-reuse see it."""
        return await self._acquire_worktree(branch_id, branch_id, None)
```

(Confirm the exact `_acquire_worktree(node_id, pool_key, group)` signature/defaults from Task 4 of the v2 work; pass `pool_key=branch_id`, `group=None`.)

- [ ] **Step 4: Run, pass** + full foreman suite.
- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: foreman.acquire_branch_worktree for fan-out branch isolation"`

---

### Task 4: Rewrite `make_fan_out_subgraph_invoker.invoke` for per-branch isolation (headline)

**Files:** Modify `src/sqrlly/compile/subgraph.py` (`make_fan_out_subgraph_invoker`); Test `tests/e2e/test_subgraph_fanout_worktree.py` (new).

Move `compile_fn` into `invoke` (lazy per-branch compile against a `_BranchScopedExecutor`); keep config load / cycle detection / `merge_settings` at factory build time. Read the current invoker (subgraph.py:193-283) first.

- [ ] **Step 1: Failing E2E test.** Build a real-git-repo fixture: a parent fan-out over 3 manifest items whose template is a multi-node subgraph YAML (`generate` → `polish`, both writing a file). Run under `auto`/`isolated`. Assert:

```python
    # exactly 3 distinct branch worktrees, named for the child ids
    branch_dirs = sorted((repo/".sqrlly").glob("wt-parent__*"))
    assert len(branch_dirs) == 3
    # NO per-inner-node worktrees were created (inner nodes share the branch tree)
    assert not list((repo/".sqrlly").glob("wt-generate-*"))
    assert not list((repo/".sqrlly").glob("wt-polish-*"))
    # both inner nodes of one branch wrote into the SAME dir (intra-branch read-across)
    # (assert generate's file and polish's file coexist in one branch dir)
```

Model the git-repo + ForemanExecutor + run_workflow wiring on `tests/e2e/test_fanout_worktrees.py` and `tests/e2e/test_fan_out_subgraph.py`. Verify the on-disk worktree dir-name format from foreman's `_create_worktree` (`wt-<safe_id>-<uuid8>`, with `::`→`__`).

- [ ] **Step 2: Run, verify FAIL** (today: one shared `wt-generate-*`, no per-branch dirs).
- [ ] **Step 3: Implement the invoke rewrite:**

```python
    async def invoke(context, workdir, dry_run, prefix=None):
        # honor a template-level `worktree: off` opt-out (inline parity)
        tmpl_kind, _ = Node(
            id="_t", name="_t", execute=template_execute
        ).effective_worktree(sub_effective)
        if prefix and tmpl_kind != "off" and hasattr(executor, "acquire_branch_worktree"):
            branch_tree = await executor.acquire_branch_worktree(prefix)
        else:
            branch_tree = workdir  # off path / non-foreman executor → no isolation
        branch_exec = (
            _BranchScopedExecutor(executor, branch_tree) if executor is not None else None
        )
        sub_compiled = compile_fn(
            sub_config, executor=branch_exec, _depth=depth + 1,
            effective_settings=sub_effective,
        )
        rendered_inputs = {k: render_template(v, context) for k, v in inputs_decl.items()}
        sub_state = make_initial_state(
            workflow_name=sub_config.name, workdir=branch_tree, dry_run=dry_run,
        )
        sub_state["node_inputs"] = rendered_inputs
        # ... existing astream(SubgraphLogger)/ainvoke + failed_nodes handling ...
        return ExecutionResult(
            success=True,
            output=_terminal_node_output(sub_result, sub_config),
            worktree=(branch_tree if tmpl_kind != "off" else None),
        )
```

Keep the existing `template_params`/`inputs_decl`, logger/prefix wiring, exception → failure-result, and `failed_nodes` → failure-result paths. `template_execute` / `sub_config` / `sub_effective` / `inputs_decl` remain captured at factory build time. The `Node(id="_t", ...)` worktree probe needs the template's `execute` — capture it in the factory (it already has `template.execute` via the caller; thread it in as `template_execute`).

- [ ] **Step 4: Run the E2E (pass) + the fan-out + subgraph regression suites:**
  `uv run pytest tests/e2e/test_subgraph_fanout_worktree.py tests/e2e/test_fan_out_subgraph.py tests/e2e/test_dynamic.py tests/e2e/test_fanout_worktrees.py -q`
- [ ] **Step 5: Commit** `git add -A && git commit -m "feat: per-branch worktree isolation for subgraph fan-out (child-is-the-unit)"`

---

### Task 5: Inner node with an explicit `worktree` directive does not escape the branch tree

**Files:** add a fixture variant + assertion to `tests/e2e/test_subgraph_fanout_worktree.py`.

Pins the precedence-hole fix (regression guard for `_strip_worktree`).

- [ ] **Step 1: Failing-or-passing test** (passes once Task 2+4 land; it's the regression pin). A subgraph whose `generate` inner node declares `worktree: isolated`. Assert: still exactly N branch trees; **no** bare `wt-generate-*` dir exists (the directive was neutralized; it ran in the branch tree).
- [ ] **Step 2: Run, pass.** If it FAILS, `_strip_worktree` regressed.
- [ ] **Step 3: Commit** `git add -A && git commit -m "test: inner worktree directive stays pinned to the branch tree"`

---

### Task 6: `node_worktrees[child_id]` + `branch_map.worktree` populated

**Files:** assertion-only against the Task 4 fixture (extend `test_subgraph_fanout_worktree.py`).

- [ ] **Step 1: Test** — after the run, `state.node_worktrees` has exactly one entry per branch keyed `parent::<item>`, each an existing dir; and a fan-in node depending on the parent sees `{{parent_branch_map}}` with a non-null `worktree` per branch pointing at that dir. (Model branch_map rendering on the existing AP4 path; assert the parsed JSON.)
- [ ] **Step 2: Run, pass.**
- [ ] **Step 3: Commit** `git add -A && git commit -m "test: subgraph fan-out records node_worktrees + branch_map.worktree"`

---

### Task 7: Resume rehydrates branch trees

**Files:** assertion against the Task 4 fixture + `--resume`; model on `tests/e2e/test_resume_fan_out.py`.

- [ ] **Step 1: Test** — run, interrupt/complete-with-a-forced-failure, `--resume`; assert foreman `_worktrees` rehydrates the N `parent::<item>` keys from `node_worktrees`, and a resumed branch re-requests its `child_id` and lands in the rehydrated path (one key per branch — parity with inline). Confirm the resume seam in `cli/main.py` (`rehydrate=dict(node_worktrees)`).
- [ ] **Step 2: Run, pass.**
- [ ] **Step 3: Commit** `git add -A && git commit -m "test: subgraph fan-out branch worktrees rehydrate on resume"`

---

### Task 8: GC reclaims branch trees

**Files:** assertion against the Task 4 fixture with `settings.worktree_gc: on_success`.

- [ ] **Step 1: Test** — after a clean run with GC on, exactly the N branch trees are removed (`set(_worktrees.values())` had N entries); zero stray inner-node trees existed to leak. Assert removed-count == N and `.sqrlly` has no `wt-*` left.
- [ ] **Step 2: Run, pass.**
- [ ] **Step 3: Commit** `git add -A && git commit -m "test: GC reclaims subgraph fan-out branch trees (no inner-tree leak)"`

---

### Task 9: Regression — non-fan-out subgraph + inline fan-out + capability fallthrough

**Files:** existing `make_subgraph_node` fixture; existing inline-fanout isolation test; a `MockExecutor` no-isolation assertion.

- [ ] **Step 1: Tests**
  - Non-fan-out subgraph: inner nodes still key foreman by bare `inner_id` (unchanged) — run the existing `test_fan_out_subgraph.py` / subgraph tests, assert green (no new behavior).
  - Inline fan-out: still one tree per child keyed `child_id` (existing `test_fanout_worktrees.py`), green.
  - Capability fallthrough: a subgraph fan-out run with a `MockExecutor` (no `acquire_branch_worktree`) → `invoke` sets `branch_tree=workdir`, returns `worktree=None`; assert the documented no-isolation contract (not silent breakage).
- [ ] **Step 2: Run, all pass.**
- [ ] **Step 3: Commit** `git add -A && git commit -m "test: non-fanout subgraph + inline fanout + non-foreman fallthrough unaffected"`

---

### Task 10: Docs

**Files:** `SKILLS.md`, `TECHNICAL.md` (§8), `CHANGELOG.md`, `TODO.md` (close the deferred B item from the v2 handoff).

- [ ] **Step 1:** SKILLS — note subgraph fan-out branches are isolated per branch (the child is the unit); inner nodes share the branch tree (intra-branch write race if two inner nodes write the same path; a `worktree`/`worktree_group` on an inner node is neutralized — it can't escape the branch tree or join a cross-branch group). TECHNICAL §8 — add the child-is-the-unit model for subgraph fan-out. CHANGELOG — Fixed: subgraph fan-out branches now get isolated worktrees (`branch_map.worktree` populated; was shared/null). TODO — mark the subgraph-fan-out worktree gap resolved.
- [ ] **Step 2:** Full suite + layers green: `uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -q && uv run pytest tests/architecture/test_layers.py -q`.
- [ ] **Step 3: Commit** `git add -A && git commit -m "docs: subgraph fan-out per-branch worktree isolation"`

---

## Self-Review

**Spec coverage:** foreman off-path (T1) → wrapper+strip (T2) → branch acquire (T3) → invoker rewrite (T4) → precedence-hole guard (T5) → state/branch_map (T6) → resume (T7) → GC (T8) → regressions+fallthrough (T9) → docs (T10). All contracts from the design's §2.4 table mapped.

**Type consistency:** `_strip_worktree(node) -> Node`; `_BranchScopedExecutor(inner, branch_tree)` with Protocol passthroughs; `acquire_branch_worktree(branch_id) -> str` delegating to `_acquire_worktree(branch_id, branch_id, None)`; `invoke(...)` returns `ExecutionResult(worktree=branch_tree | None)`. `dynamic.py:210-211` consumes `exec_result.worktree` unchanged.

**Placeholder scan:** T4/T6/T7/T9 reference existing fixtures/harnesses (test_fan_out_subgraph.py, test_fanout_worktrees.py, test_resume_fan_out.py) the implementer must read for exact wiring (run_workflow + ForemanExecutor + git repo) — flagged, not reproduced. The invoke rewrite shows the load-bearing structure; the implementer preserves the existing logger/exception/failed_nodes wiring around it.

**Open (non-blocking) — accepted defaults:** template `worktree:off` → branch_map.worktree null (inline parity); N per-branch recompiles (profile only if deep nesting bites).
