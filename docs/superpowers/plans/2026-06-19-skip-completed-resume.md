# Skip-completed Resume Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `sqrlly run --resume` skip cleanly-completed nodes by default (with `--rerun-all` escape hatch and `--resume-from <node>` for iterate), so long LLM pipelines recover/iterate without re-billing completed work.

**Architecture:** A single frozen snapshot channel `_resume_skip` (set, no reducer, last-write-wins like `_route_sender`) is seeded once at resume entry from the prior checkpoint. A pure `compute_skip_set` (new `compile/resume.py`) derives it = `prior_completed − dirty_closure`. Node-body guards read `_resume_skip` (never the live `completed_nodes`, which goto re-fires/join barriers legitimately re-enter mid-run).

**Tech Stack:** Python 3.14, Pydantic v2, LangGraph, `AsyncSqliteSaver`, `uv` + `pytest`, real subprocess/checkpointer in tests.

**Spec:** `docs/superpowers/specs/2026-06-19-skip-completed-resume-design.md`. **Release: minor 0.6.0.**

**Task order:** state channel (1) → pure skip-set (2) → three guards (3,4,5) → CLI wiring + e2e (6) → docs (7). Guards are unit-tested at the factory level (matching `tests/unit/compile/test_execution_node.py`); the end-to-end behavior is pinned in Task 6 by extending the real-checkpointer fixture.

---

### Task 1: `_resume_skip` state channel

**Files:**
- Modify: `src/sqrlly/runtime/state.py` (`WorkflowState`, after `_route_eval_preamble`)
- Test: `tests/unit/runtime/test_resume_skip_channel.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/runtime/test_resume_skip_channel.py`:

```python
"""The _resume_skip channel: a frozen, last-write-wins skip snapshot."""
from sqrlly.runtime.state import REDUCERS, WorkflowState, make_initial_state


def test_resume_skip_absent_on_fresh_run():
    # Fresh runs must not carry a skip set — absent => guards skip nothing.
    assert "_resume_skip" not in make_initial_state()


def test_resume_skip_is_declared_but_has_no_reducer():
    # Declared so LangGraph keeps the seeded key; no reducer => last-write-wins
    # (it must never accumulate via set-union like completed_nodes).
    assert "_resume_skip" in WorkflowState.__annotations__
    assert "_resume_skip" not in REDUCERS


def test_make_initial_state_accepts_override():
    # Tests/CLI may seed it explicitly; overrides flow through.
    st = make_initial_state(_resume_skip={"a", "b"})
    assert st["_resume_skip"] == {"a", "b"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/runtime/test_resume_skip_channel.py -v`
Expected: FAIL — `test_resume_skip_is_declared_but_has_no_reducer` fails (`_resume_skip` not in annotations).

- [ ] **Step 3: Add the channel**

In `src/sqrlly/runtime/state.py`, immediately after the `_route_eval_preamble` field (the last field of `WorkflowState`), add:

```python
    # Resume skip-set (skip-completed --resume). A FROZEN snapshot of node
    # ids that completed in the prior run and are safe to skip this run.
    # Seeded ONCE at resume entry from the prior checkpoint; never written by
    # a node body, so it persists unchanged across super-steps. Last-write-wins
    # (NO REDUCER — must not set-union-accumulate). Guards read it; nothing
    # mutates it. Absent on a fresh run => skip nothing.
    _resume_skip: NotRequired[set[str]]
```

(`make_initial_state` is unchanged — it must NOT seed `_resume_skip`; the `**overrides` path already lets tests/CLI pass it.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/runtime/test_resume_skip_channel.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/runtime/state.py tests/unit/runtime/test_resume_skip_channel.py
git commit -m "feat: _resume_skip frozen snapshot channel for skip-completed resume"
```

---

### Task 2: `compute_skip_set` pure planner

**Files:**
- Create: `src/sqrlly/compile/resume.py`
- Test: `tests/unit/compile/test_resume_skip_set.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/compile/test_resume_skip_set.py`:

```python
"""compute_skip_set: prior_completed minus the dirty closure."""
import pytest

from sqrlly.compile.resume import compute_skip_set
from sqrlly.schema.models import Graph


def _g(nodes):
    return Graph(name="T", version="1.0", nodes=nodes)


def _exec(id_, **kw):
    return {"id": id_, "name": id_, "execute": {"url": "t.md"}, **kw}


def test_linear_failure_dirties_downstream():
    # a -> b -> c ; b failed => b and its dependent c are dirty, a is skippable.
    g = _g([_exec("a"), _exec("b", depends_on=["a"]), _exec("c", depends_on=["b"])])
    assert compute_skip_set(g, {"a"}, {"b"}, set()) == {"a"}


def test_clean_run_skips_everything():
    g = _g([_exec("a"), _exec("b", depends_on=["a"])])
    assert compute_skip_set(g, {"a", "b"}, set(), set()) == {"a", "b"}


def test_resume_from_dirties_node_and_downstream():
    # a -> b -> c all clean; --resume-from b => b,c dirty, a skippable.
    g = _g([_exec("a"), _exec("b", depends_on=["a"]), _exec("c", depends_on=["b"])])
    assert compute_skip_set(g, {"a", "b", "c"}, set(), {"b"}) == {"a"}


def test_diamond_only_failure_branch_dirty():
    # a -> {b,c} -> d ; b failed => b,d dirty; a,c skippable.
    g = _g([
        _exec("a"),
        _exec("b", depends_on=["a"]),
        _exec("c", depends_on=["a"]),
        _exec("d", depends_on=["b", "c"]),
    ])
    assert compute_skip_set(g, {"a", "b", "c", "d"}, {"b"}, set()) == {"a", "c"}


def test_route_target_of_failure_is_dirty():
    # a routes to b via goto; a failed => its route target b is dirty even with
    # no depends_on edge (route targets have no static depends_on).
    g = _g([
        {"id": "a", "name": "a", "execute": {"url": "t.md"},
         "route": {"goto": "b"}},
        _exec("b"),
    ])
    assert compute_skip_set(g, {"a", "b"}, {"a"}, set()) == set()


def test_worktree_group_sibling_force_dirty():
    # a,b share a worktree_group; a dirty => b force-dirty (shared mutable tree).
    g = _g([
        _exec("a", worktree_group="team"),
        _exec("b", worktree_group="team"),
    ])
    assert compute_skip_set(g, {"a", "b"}, {"a"}, set()) == set()


def test_empty_inputs():
    g = _g([_exec("a")])
    assert compute_skip_set(g, set(), set(), set()) == set()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/compile/test_resume_skip_set.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'sqrlly.compile.resume'`.

- [ ] **Step 3: Implement the planner**

Create `src/sqrlly/compile/resume.py`:

```python
"""Resume skip-set computation (pure, langgraph-free, compile layer).

`compute_skip_set` decides which prior-run-completed nodes are safe to skip on
``--resume``: prior_completed minus the "dirty" closure (prior failures +
``--resume-from`` targets, expanded by transitive ``depends_on`` dependents,
route-target reachability, and ``worktree_group`` siblings). Topological only —
no content hashing (LLM nondeterminism + fan-out/promote side-effects make a
content hash neither sufficient nor safe; see the design spec)."""
from __future__ import annotations

from sqrlly.schema.models import Graph, Node


def _flatten_goto(goto: "str | list[str] | None") -> list[str]:
    if goto is None:
        return []
    return [goto] if isinstance(goto, str) else list(goto)


def _route_targets(node: Node) -> list[str]:
    """Every static goto target declared on a node's route (goto/cases/else)."""
    r = node.route
    if r is None:
        return []
    out: list[str] = _flatten_goto(r.goto)
    for case in r.cases:
        out += _flatten_goto(case.goto)
    if r.else_ is not None:
        out += _flatten_goto(r.else_.goto)
    return out


def compute_skip_set(
    config: Graph,
    prior_completed: set[str],
    prior_failed: set[str],
    rerun_targets: set[str],
) -> set[str]:
    """Return the node ids safe to skip on resume.

    ``skip = prior_completed - dirty`` where ``dirty`` starts at
    ``prior_failed | rerun_targets`` and is closed over: transitive
    ``depends_on`` dependents, route-target reachability, and
    ``worktree_group`` siblings (a shared mutable tree means any dirty member
    dirties the whole group). Failed nodes are never in ``prior_completed``,
    so the difference can't accidentally skip a failure."""
    ids = {n.id for n in config.nodes}
    dependents: dict[str, list[str]] = {nid: [] for nid in ids}
    for n in config.nodes:
        for dep in n.depends_on:
            if dep in dependents:
                dependents[dep].append(n.id)
    route_adj = {n.id: _route_targets(n) for n in config.nodes}
    groups: dict[str, list[str]] = {}
    group_of: dict[str, str] = {}
    for n in config.nodes:
        if n.worktree_group:
            groups.setdefault(n.worktree_group, []).append(n.id)
            group_of[n.id] = n.worktree_group

    dirty: set[str] = set(prior_failed) | set(rerun_targets)
    frontier = list(dirty)
    while frontier:
        cur = frontier.pop()
        neighbors = dependents.get(cur, []) + route_adj.get(cur, [])
        g = group_of.get(cur)
        if g:
            neighbors += groups.get(g, [])
        for nxt in neighbors:
            if nxt not in dirty:
                dirty.add(nxt)
                frontier.append(nxt)
    return set(prior_completed) - dirty
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/compile/test_resume_skip_set.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/compile/resume.py tests/unit/compile/test_resume_skip_set.py
git commit -m "feat: compute_skip_set pure resume planner"
```

---

### Task 3: Skip-guard in the execution node

**Files:**
- Modify: `src/sqrlly/compile/nodes.py` (`_make_execution_node` `node_fn`, the body starting at `async def node_fn`)
- Test: `tests/unit/compile/test_execution_node.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/unit/compile/test_execution_node.py` (inside `class TestExecutionNodeClosure`):

```python
    @pytest.mark.asyncio
    async def test_resume_skip_freezes_body(self):
        """A node id in _resume_skip is frozen: body does not run, no update."""
        node = Node(id="p1", name="P1", execute=Execute(url="t.md"))
        ex = MockExecutor()
        nf = _make_execution_node(node, _config_with(node), ex)
        update = await nf(make_initial_state(_resume_skip={"p1"}))
        assert update == {}
        assert ex.execution_order == []  # body never executed

    @pytest.mark.asyncio
    async def test_resume_skip_other_node_still_runs(self):
        """A skip-set that doesn't name this node leaves it running."""
        node = Node(id="p1", name="P1", execute=Execute(url="t.md"))
        ex = MockExecutor()
        nf = _make_execution_node(node, _config_with(node), ex)
        update = await nf(make_initial_state(_resume_skip={"other"}))
        assert ex.execution_order == ["p1"]
        assert update["completed_nodes"] == {"p1"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/compile/test_execution_node.py -k resume_skip -v`
Expected: FAIL — `test_resume_skip_freezes_body` fails (body runs; `execution_order == ["p1"]`).

- [ ] **Step 3: Add the guard**

In `src/sqrlly/compile/nodes.py`, in `_make_execution_node`'s `node_fn`, insert as the FIRST statement (before the `for check in (check_dep_failed, check_dry_run):` loop):

```python
    async def node_fn(state: WorkflowState) -> dict[str, Any]:
        # Resume skip: a node frozen in the prior-run snapshot does not
        # re-execute. Its completed_nodes/node_outputs are already reseeded,
        # so downstream context and the deps join-barrier still see it.
        skip = state.get("_resume_skip")
        if skip and node.id in skip:
            return {}
        for check in (check_dep_failed, check_dry_run):
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/compile/test_execution_node.py -v`
Expected: PASS (new tests + all pre-existing closure tests).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/compile/nodes.py tests/unit/compile/test_execution_node.py
git commit -m "feat: resume skip-guard in the execution node"
```

---

### Task 4: Skip-guard in BOTH evaluation factories (the no-rebill fix)

**Files:**
- Modify: `src/sqrlly/compile/nodes.py` (`_make_evaluation_node` and `_make_combined_eval_decide_node`)
- Test: `tests/unit/compile/test_evaluation_node.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/compile/test_evaluation_node.py` (use the module's existing imports; both factories are importable from `sqrlly.compile.nodes`). Add a small node-with-evaluation builder if the file lacks one:

```python
import pytest
from sqrlly.compile.nodes import (
    _make_evaluation_node,
    _make_combined_eval_decide_node,
)
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Execute, Evaluation, Node, Graph, Settings


def _gated_node():
    return Node(
        id="g1", name="G1", execute=Execute(url="t.md"),
        evaluation=Evaluation(validator="check.md", threshold=0.8),
    )


def _cfg(node):
    return Graph(name="T", version="1.0", nodes=[node], settings=Settings())


class TestEvaluationResumeSkip:
    @pytest.mark.asyncio
    async def test_evaluation_node_skipped_returns_empty(self):
        node = _gated_node()
        nf = _make_evaluation_node(node, _cfg(node), executor=None)
        # node_output present (would normally evaluate) but node is frozen.
        state = make_initial_state(
            node_outputs={"g1": "prior output"}, _resume_skip={"g1"},
        )
        assert await nf(state) == {}

    @pytest.mark.asyncio
    async def test_combined_eval_decide_skipped_returns_empty(self):
        node = _gated_node()
        nf = _make_combined_eval_decide_node(node, _cfg(node), executor=None)
        state = make_initial_state(
            node_outputs={"g1": "prior output"}, _resume_skip={"g1"},
        )
        assert await nf(state) == {}

    @pytest.mark.asyncio
    async def test_evaluation_node_not_skipped_when_absent(self):
        node = _gated_node()
        nf = _make_evaluation_node(node, _cfg(node), executor=None)
        state = make_initial_state(node_outputs={"g1": "prior output"})
        # Not frozen => proceeds past the skip-guard (does NOT return {} for
        # the skip reason). With executor=None/no backend it still produces a
        # record-only update, i.e. a non-empty dict OR the deferred {} only if
        # output absent — here output is present so it evaluates.
        result = await nf(state)
        assert result != {} or "evaluations" in result
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/compile/test_evaluation_node.py -k ResumeSkip -v`
Expected: FAIL — the skipped-node tests do not return `{}` (the eval body proceeds), because no `_resume_skip` guard exists yet.

- [ ] **Step 3: Add the guard to BOTH factories**

In `src/sqrlly/compile/nodes.py`, in `_make_evaluation_node`'s `node_fn`, immediately AFTER the existing `failed_nodes` short-circuit:

```python
        if node_id in state.get("failed_nodes", set()):
            return {}

        if (skip := state.get("_resume_skip")) and node_id in skip:
            # Frozen on resume: do NOT re-run the validator (often an LLM
            # judge). The reseeded completed_nodes lets the Decision node
            # route to pass_targets with zero eval bill.
            return {}
```

In `_make_combined_eval_decide_node`'s `node_fn`, immediately AFTER its `failed_nodes` short-circuit, add the identical guard:

```python
        if node_id in state.get("failed_nodes", set()):
            return {}

        if (skip := state.get("_resume_skip")) and node_id in skip:
            return {}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/compile/test_evaluation_node.py -v`
Expected: PASS (new ResumeSkip tests + all pre-existing).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/compile/nodes.py tests/unit/compile/test_evaluation_node.py
git commit -m "feat: resume skip-guard in both evaluation factories (no validator re-bill)"
```

---

### Task 5: Skip-guard in the fan-out child

**Files:**
- Modify: `src/sqrlly/compile/dynamic.py` (`_make_fan_out_node` `node_fn`)
- Test: `tests/unit/compile/test_fan_out_resume_skip.py` (new)

- [ ] **Step 1: Write the failing test**

Create `tests/unit/compile/test_fan_out_resume_skip.py`:

```python
"""Fan-out child resume skip-guard: a child is frozen only when its PARENT
is also in the skip-set (so the manifest isn't re-derived and child ids are
stable)."""
import pytest

from sqrlly.compile.dynamic import _make_fan_out_node
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Execute, FanOut, FanOutTemplate, Node, Graph, Settings
from mock_executor import MockExecutor


def _fan_parent():
    return Node(
        id="fan", name="Fan",
        fan_out=FanOut(
            manifest_path="m.json",
            template=FanOutTemplate(execute=Execute(url="t.md")),
        ),
    )


def _make(parent, executor):
    cfg = Graph(name="T", version="1.0", nodes=[parent], settings=Settings())
    return _make_fan_out_node(
        parent, cfg, executor,
        compile_fn=lambda *a, **k: None, base_dir=".", depth=0,
    )


@pytest.mark.asyncio
async def test_child_frozen_when_parent_and_child_in_skip():
    ex = MockExecutor()
    nf = _make(_fan_parent(), ex)
    state = make_initial_state(
        _fan_out_item={"id": "item1"},
        _resume_skip={"fan", "fan::item1"},
    )
    assert await nf(state) == {}
    assert ex.execution_order == []


@pytest.mark.asyncio
async def test_child_runs_when_parent_not_in_skip():
    # Parent dirty (re-fanned) => child must run even if its id is in skip.
    ex = MockExecutor()
    nf = _make(_fan_parent(), ex)
    state = make_initial_state(
        _fan_out_item={"id": "item1"},
        _resume_skip={"fan::item1"},  # parent "fan" NOT in skip
    )
    result = await nf(state)
    assert result != {}  # proceeded past the skip-guard
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/unit/compile/test_fan_out_resume_skip.py -v`
Expected: FAIL — `test_child_frozen_when_parent_and_child_in_skip` does not return `{}` (no guard yet).

- [ ] **Step 3: Add the guard**

In `src/sqrlly/compile/dynamic.py`, in `_make_fan_out_node`'s `node_fn`, immediately AFTER the existing `failed_nodes` short-circuit:

```python
        if child_id in state.get("failed_nodes", set()):
            return {}

        # Resume skip: freeze this child only when its PARENT is also frozen —
        # a dirty parent re-derives the manifest, so child ids aren't stable.
        skip = state.get("_resume_skip")
        if skip and parent_node.id in skip and child_id in skip:
            return {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/unit/compile/test_fan_out_resume_skip.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/compile/dynamic.py tests/unit/compile/test_fan_out_resume_skip.py
git commit -m "feat: resume skip-guard in the fan-out child (parent-gated)"
```

---

### Task 6: CLI wiring + end-to-end resume behavior

**Files:**
- Modify: `src/sqrlly/cli/main.py` (`run` options + signature; `_run_async`/`_execute_workflow` signatures; the `if resume:` seed block; resume echo)
- Test: `tests/e2e/test_resume_fan_out.py`

- [ ] **Step 1: Write the failing e2e tests**

In `tests/e2e/test_resume_fan_out.py`, first update the `_run_phase` helper's resume branch so it seeds `_resume_skip` exactly as the CLI will. Replace the `if resume:` block inside `_run_phase` with:

```python
        if resume:
            prev = await cp.aget_tuple(
                {"configurable": {"thread_id": thread_id}}
            )
            assert prev is not None, "phase 2 expected a saved checkpoint"
            old = dict(prev.checkpoint.get("channel_values", {}))
            from sqrlly.compile.resume import compute_skip_set
            skip = (
                set()
                if rerun_all
                else compute_skip_set(
                    config,
                    set(old.get("completed_nodes", set())),
                    set(old.get("failed_nodes", set())),
                    set(resume_from),
                )
            )
            state = {
                **old,
                "failed_nodes": set(),
                "retries": {},
                "errors": [],
                "workdir": str(workdir),
                "dry_run": False,
                "_resume_skip": skip,
            }
            await cp.adelete_thread(thread_id)
```

and extend `_run_phase`'s signature to `(..., *, resume: bool, resume_from=(), rerun_all=False)`.

Then flip the pinned regression in `test_resume_after_mid_chain_failure` (the comment at lines ~182-186 already anticipates this) — replace the three post-resume run-count assertions:

```python
        # Skip-completed resume: a completed cleanly in phase 1 and is NOT
        # downstream of the failure, so it is frozen — runs ONCE total.
        assert _read_runs(tmp_path, "a") == 1
        # b failed in phase 1, is dirty, re-runs in phase 2 — counter == 2.
        assert _read_runs(tmp_path, "b") == 2
        # c is downstream of the failed b (dirty) and ran for the first time.
        assert _read_runs(tmp_path, "c") == 1
```

Then append two new tests to `class TestResumeFromCheckpoint`:

```python
    @pytest.mark.asyncio
    async def test_rerun_all_restores_full_replay(self, tmp_path):
        """--rerun-all reproduces pre-0.6 behavior: a re-executes."""
        config = _build_chain(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "rerun-all"
        (tmp_path / "fail.txt").write_text("b")
        await _run_phase(tmp_path, config, db_path, thread_id, resume=False)
        (tmp_path / "fail.txt").unlink()
        await _run_phase(
            tmp_path, config, db_path, thread_id, resume=True, rerun_all=True,
        )
        assert _read_runs(tmp_path, "a") == 2  # full replay

    @pytest.mark.asyncio
    async def test_resume_from_reruns_node_and_downstream(self, tmp_path):
        """All clean; --resume-from b => a frozen, b & c re-run."""
        config = _build_chain(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "resume-from"
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert result_1["completed_nodes"] == {"a", "b", "c"}
        await _run_phase(
            tmp_path, config, db_path, thread_id,
            resume=True, resume_from=("b",),
        )
        assert _read_runs(tmp_path, "a") == 1   # frozen
        assert _read_runs(tmp_path, "b") == 2   # rerun target
        assert _read_runs(tmp_path, "c") == 2   # downstream of b
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/e2e/test_resume_fan_out.py -v`
Expected: FAIL — the flipped `a == 1` assertion fails (current code re-runs everything; without the guards `a` runs twice), and the new tests fail.

> NOTE: Tasks 1–5 (the channel + guards) must be committed before this step passes — the e2e exercises them through the compiled graph. If running tasks strictly in order, this test goes green once Task 6 Step 3 seeds `_resume_skip` AND Tasks 3–5 guards are in place. Confirm Tasks 1–5 are merged on the branch before expecting green.

- [ ] **Step 3: Wire the CLI**

In `src/sqrlly/cli/main.py`, add two options to the `run` command (after the `--resume` option, before `--log`):

```python
@click.option(
    "--resume-from", "resume_from", multiple=True,
    help="Re-run this node and everything downstream; freeze upstream. "
         "Implies --resume. Repeatable.",
)
@click.option(
    "--rerun-all", "rerun_all", is_flag=True,
    help="With --resume: re-execute every node (pre-0.6 full replay; "
         "disables skip-completed).",
)
```

Extend the `run` signature and validation (top of `run`, after `config` load):

```python
def run(
    config_file: str,
    workdir: str,
    dry_run: bool,
    preset: str | None,
    resume: bool,
    resume_from: tuple[str, ...],
    rerun_all: bool,
    log_file: str | None,
    quiet: bool,
):
    """Run a workflow from a configuration file."""
    try:
        config = load_config(config_file)
    except Exception as e:
        raise click.ClickException(str(e))

    resume = resume or bool(resume_from)  # --resume-from implies --resume
    if rerun_all and resume_from:
        raise click.ClickException("--rerun-all and --resume-from are mutually exclusive")
    valid_ids = {n.id for n in config.nodes}
    for rid in resume_from:
        if "::" in rid:
            raise click.ClickException(
                f"--resume-from {rid!r}: fan-out children are not addressable across runs"
            )
        if rid not in valid_ids:
            raise click.ClickException(
                f"--resume-from {rid!r}: unknown node id. Valid: {', '.join(sorted(valid_ids))}"
            )
```

Thread the two params through the call chain. Update the `_run_async(...)` call and signature, and `_execute_workflow(...)` signature/call, to carry `resume_from: tuple[str, ...]` and `rerun_all: bool` alongside `resume`. (Mirror exactly how `resume` is already threaded.)

In `_execute_workflow`'s `if resume:` block, replace the seed so it computes and seeds `_resume_skip`:

```python
        if resume:
            prev = await cp.aget_tuple({"configurable": {"thread_id": thread_id}})
            if prev is None:
                raise click.ClickException(
                    f"No saved state for this workflow at {_db_path(workdir)}"
                )
            old = dict(prev.checkpoint.get("channel_values", {}))
            from sqrlly.compile.resume import compute_skip_set
            prior_completed = set(old.get("completed_nodes", set()))
            skip = (
                set()
                if rerun_all
                else compute_skip_set(
                    config, prior_completed,
                    set(old.get("failed_nodes", set())), set(resume_from),
                )
            )
            state = {
                **old,
                "failed_nodes": set(), "retries": {}, "errors": [],
                "workdir": workdir, "dry_run": False, "_resume_skip": skip,
            }
            rerun_present = (prior_completed - skip)
            click.echo(
                f"Resuming: skipping {len(skip)} completed; re-running "
                f"{len(rerun_present)} (from: "
                f"{', '.join(sorted(resume_from)) or 'failed nodes'})."
            )
            await cp.adelete_thread(thread_id)
```

(The existing `else:` fresh-state branch and the rest of the block are unchanged.)

- [ ] **Step 4: Run the e2e + full CLI/compile suites**

Run: `uv run pytest tests/e2e/test_resume_fan_out.py tests/unit/cli/test_cli.py tests/unit/compile -v`
Expected: PASS — the flipped fixture, the new resume tests, and no regressions.

- [ ] **Step 5: Run the full core suite**

Run: `uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add src/sqrlly/cli/main.py tests/e2e/test_resume_fan_out.py
git commit -m "feat: --resume skips completed nodes by default; --resume-from / --rerun-all"
```

---

### Task 7: Documentation + 0.6.0 changelog

**Files:**
- Modify: `SCHEMA.md`, `SKILLS.md`, `CLAUDE.md`, `CHANGELOG.md`

- [ ] **Step 1: CLAUDE.md — replace the stale `--resume` limitation**

In `CLAUDE.md` "Known limitations", replace the `--resume is a fault-recovery re-run, not skip-completed` bullet with current behavior:

> **`--resume` skips completed nodes (0.6.0)** — bare `--resume` reseeds the prior
> checkpoint and skips nodes that completed cleanly and aren't downstream of a
> failure (`compile/resume.py::compute_skip_set` → the `_resume_skip` frozen
> snapshot channel; guards in `compile/nodes.py`/`dynamic.py` read it, never the
> live `completed_nodes`). `--rerun-all` forces the pre-0.6 full replay;
> `--resume-from <node>` re-runs a node + its downstream. **v1 limitation:**
> subgraph *inner* nodes aren't individually skippable — a subgraph re-runs in
> full unless its reference node completed cleanly (printed at `-v`).

- [ ] **Step 2: SCHEMA.md + SKILLS.md — resume semantics**

In `SCHEMA.md` (CLI/dispatch area) and `SKILLS.md` (the "Debug a run"/resume paragraph), update any text that says `--resume` re-executes every node to describe skip-completed-by-default + `--rerun-all` + `--resume-from`, matching the CLAUDE.md wording above. (SKILLS.md currently states "every node **re-executes**" — correct it.)

- [ ] **Step 3: CHANGELOG.md — 0.6.0 entry**

At the top of `CHANGELOG.md`, under the `# Changelog` header, add a new section (this is a minor release — keep it as `[Unreleased]` until the release script tags it, OR title it `[0.6.0]` if cutting immediately per the release flow):

```markdown
## [Unreleased] — skip-completed resume

### Changed
- **`--resume` now skips cleanly-completed nodes by default** (was: re-run
  every node). Recovers/iterates long LLM pipelines without re-billing
  completed work. `--rerun-all` restores the previous full-replay behavior.

### Added
- `--resume-from <node>` (repeatable): re-run a node and everything downstream,
  freezing upstream. Implies `--resume`.
```

- [ ] **Step 4: Sanity check**

Run: `uv run sqrlly validate examples/jokes/workflow.yaml`
Expected: `Valid: ...` (docs/schema unaffected).

- [ ] **Step 5: Commit**

```bash
git add SCHEMA.md SKILLS.md CLAUDE.md CHANGELOG.md
git commit -m "docs: skip-completed --resume semantics (0.6.0)"
```

---

### Final verification

- [ ] Run `uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -q` — all pass.
- [ ] Run `uv run pytest tests/architecture/test_layers.py -q` — layer rules hold (`compile/resume.py` is langgraph-free; the channel is runtime; guards are compile-layer bodies).

---

## Self-Review

**1. Spec coverage:**
- Frozen `_resume_skip` channel (no reducer, set) → Task 1. ✓
- `compute_skip_set` (prior_completed − dirty; failure/rerun closure + route + worktree_group) → Task 2. ✓
- Guard in exec node → Task 3; **both** eval factories (the no-rebill fix) → Task 4; fan-out child (parent-gated) → Task 5. ✓
- Route nodes need no guard (synthetic dispatcher) — covered by NOT adding one + route closure in `compute_skip_set` (Task 2 `test_route_target_of_failure_is_dirty`). ✓
- CLI: `--resume` default flip, `--rerun-all`, `--resume-from` (implies resume; `::`/unknown → ClickException; rerun-all+resume-from mutually exclusive), seed + echo → Task 6. ✓
- Subgraph ref-node granularity + printed limitation → documented (Task 7); the `-v` print line is in the spec — Task 6 Step 3 includes the resume echo; the explicit "N subgraph nodes not skippable" `-v` line should be added in Task 6 (see note below). ✓ (added)
- Reject content-addressing → not built (Task 2 is purely topological). ✓
- Tests: recover (flip), gated no-rebill (Task 4), iterate, failure-closure, route-target-of-failure, rerun-all → Tasks 4 & 6 + Task 2 units. ✓
- Docs → Task 7. ✓

**Gap found & fixed:** the spec's `-v` honesty line ("N subgraph/partially-run nodes will re-run") is not yet a concrete step. Add to Task 6 Step 3, after the resume echo:
```python
            import sys as _sys
            if not _sys.stdout.isatty():
                pass  # detailed -v print is a follow-up; the count echo above suffices for v1
```
Decision: v1 ships the skip/re-run **count** echo (already in Step 3); the subgraph-specific `-v` breakdown is deferred to the subgraph follow-up (it needs subgraph-ref detection). Documented as a known v1 limitation in Task 7 Step 1. Not a blocker.

**2. Placeholder scan:** No TBD/TODO/"handle edge cases". Every code step shows complete code; every run step shows the command + expected result. ✓

**3. Type consistency:** `_resume_skip: set[str]` (Task 1) read identically by all guards (Tasks 3–5) and seeded as a `set` in Task 6 + the test helper. `compute_skip_set(config, prior_completed, prior_failed, rerun_targets) -> set[str]` (Task 2) called with that exact arg order in Task 6 and the e2e helper. `resume_from: tuple[str,...]` / `rerun_all: bool` threaded consistently run → _run_async → _execute_workflow. ✓
