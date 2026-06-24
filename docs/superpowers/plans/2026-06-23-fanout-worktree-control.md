# Per-fan-out worktree control + stable fan-out item id (#8) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. TRANSCRIBE the code verbatim — every block is complete and the line context was traced against the live tree on branch `feat/fanout-worktree-id`.

**Goal:** Two independent asks on the fan-out machinery.
(a) **Per-fan-out worktree control** — a `FanOutTemplate.worktree` override so one workflow can run an isolated build fan-out AND a shared-base planner fan-out under a single top-level `settings.worktree`. (b) **Stable branch id / fail-loud** — a WARN when a dict manifest item lacks `id` (it silently collapses every branch to one `…::unknown` child), and a `ManifestError` raised on DUPLICATE child ids before Send dispatch.

**Architecture:** A fan-out branch resolves isolation in TWO code paths, both of which must honor the new template override:
  - **Subgraph template** (`execute.url` = `.yaml`/`.yml`): isolation is gated in `compile/subgraph.py::make_fan_out_subgraph_invoker` by `_isolate = (parent_settings or Settings()).worktree != "off"`. Thread a `template_worktree` argument in and let it override the parent-settings value (None = inherit). `dynamic.py::_make_fan_out_node` already constructs the invoker and passes `parent_settings=settings`; it adds `template_worktree=template.worktree`.
  - **Non-subgraph template** (`.md`/`.py`/script): `dynamic.py::_make_fan_out_node` builds a `synthetic_node = Node(id=child_id, ...)` and runs it via `execute_with_timeout(executor, synthetic_node, ...)`. The `ForemanExecutor.execute` then calls `node.effective_worktree(s)` — so setting `synthetic_node.worktree = template.worktree` makes `effective_worktree` resolve the per-fan-out override (None = inherit settings, identical to `Node.worktree` semantics).

  For the fail-loud asks: `compile/_manifest.py::_normalize_items` is the single normalization funnel for BOTH the node-output manifest and the on-disk manifest, so the missing-`id` WARN lives there (logging, matching the existing zero-items router warning). The duplicate-child-id guard lives in the Send router (`compile/graph.py::_make_dynamic_router`), which computes child ids and dispatches `Send(...)` per item — it raises `ManifestError` before returning the Send list. `ManifestError` already exists in `runtime/result.py`.

**Tech Stack:** Python 3.11+, Pydantic v2, LangGraph, git worktrees, pytest, pytest-asyncio.

## Global Constraints

- Python `>=3.11`; use `sys.executable` for subprocess where a Python interpreter is needed in tests; the `python` binary is unavailable in this env. (The e2e here uses `/bin/sh` script nodes, so no Python interpreter is invoked.)
- Layer rules (`tests/architecture/test_layers.py`): `schema/` must not import `langgraph`; `compile/` must not import `sqrlly.cli`. `compile/_manifest.py` may import `logging` (stdlib) and already imports `runtime.result.ManifestError` (an allowed `runtime` module). The new `FanOutTemplate.worktree` field stays pure schema (no langgraph).
- No mocks of external systems; tests use real git + real `ForemanExecutor` + real `DispatchExecutor`. `MockExecutor` is the only sanctioned `NodeExecutor` double (not used here).
- `extra="forbid"` on all schema models — a typo'd field is a hard `ValidationError`, so the new field name must be exact.
- Conventional-commit messages; **no attribution trailers** (no `Co-Authored-By`, no `via Happy`).
- Do not change the signatures of `effective_worktree`, `ForemanExecutor.execute`, or `_BranchScopedExecutor` — the override flows through the existing `node.effective_worktree(settings)` resolution.

---

### Task 1: Schema — `FanOutTemplate.worktree`

**Files:**
- Modify: `src/sqrlly/schema/models.py` (the `FanOutTemplate` model)
- Test: `tests/unit/schema/test_worktree_control.py` (extend)

**Interfaces:**
- Produces: `FanOutTemplate.worktree: Literal["auto", "isolated", "off"] | None = None` (default `None` = inherit `settings.worktree`; same optional-override semantics as `Node.worktree`). The YAML-bool / `"none"` normalization is shared with `Node.worktree`/`Settings.worktree` via the existing `_normalize_worktree` validator.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/schema/test_worktree_control.py — add at end of file
def test_fan_out_template_worktree_defaults_to_none():
    from sqrlly.schema.models import Execute, FanOutTemplate
    t = FanOutTemplate(execute=Execute(url="w.yaml"))
    assert t.worktree is None


@pytest.mark.parametrize(
    "value,normalized",
    [("auto", "auto"), ("isolated", "isolated"), ("off", "off"), ("none", "off")],
)
def test_fan_out_template_worktree_accepts_modes_and_normalizes_none(value, normalized):
    from sqrlly.schema.models import Execute, FanOutTemplate
    t = FanOutTemplate(execute=Execute(url="w.yaml"), worktree=value)
    assert t.worktree == normalized


def test_fan_out_template_worktree_rejects_group_token():
    from sqrlly.schema.models import Execute, FanOutTemplate
    with pytest.raises(ValidationError) as exc:
        FanOutTemplate(execute=Execute(url="w.yaml"), worktree="team-a")
    msg = str(exc.value)
    assert "auto" in msg and "isolated" in msg and "off" in msg
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/schema/test_worktree_control.py -q -k fan_out_template_worktree`
Expected: FAIL — `FanOutTemplate` has no field `worktree`; under `extra="forbid"` Pydantic raises `ValidationError` ("Extra inputs are not permitted") for the construction in `test_fan_out_template_worktree_accepts_modes_and_normalizes_none`, and `test_fan_out_template_worktree_defaults_to_none` fails with `AttributeError` on `.worktree`.

- [ ] **Step 3: Implement** — add the field + the shared normalizer to `FanOutTemplate` in `src/sqrlly/schema/models.py`. The current model is:

```python
class FanOutTemplate(BaseModel):
    """Template for nodes spawned during fan-out over a manifest."""
    model_config = ConfigDict(extra="forbid")
    execute: Execute
    evaluation: Evaluation | None = None
```

Replace it with (the `field_validator` is already imported at the top of `models.py`, and `_normalize_worktree` is the module-level helper used by `Node`/`Settings`):

```python
class FanOutTemplate(BaseModel):
    """Template for nodes spawned during fan-out over a manifest."""
    model_config = ConfigDict(extra="forbid")
    execute: Execute
    evaluation: Evaluation | None = None
    # Per-fan-out worktree-isolation override. None (default) inherits
    # settings.worktree — same optional-override semantics as Node.worktree.
    # Lets one workflow run an isolated build fan-out AND a shared-base
    # planner fan-out under a single top-level settings.worktree: the value
    # here overrides settings.worktree at the per-branch isolation gate
    # (subgraph templates: make_fan_out_subgraph_invoker; non-subgraph
    # templates: the synthetic child node's effective_worktree).
    worktree: Literal["auto", "isolated", "off"] | None = None

    @field_validator("worktree", mode="before")
    @classmethod
    def _normalize_worktree_override(cls, v: Any) -> Any:
        return _normalize_worktree(v)
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/schema/test_worktree_control.py -q -k fan_out_template_worktree`
Expected: PASS (3 tests: the `none`/`off` parametrize case and the bare-modes cases all pass; the group-token case raises `ValidationError`).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/schema/models.py tests/unit/schema/test_worktree_control.py
git commit -m "feat: FanOutTemplate.worktree — per-fan-out isolation override"
```

---

### Task 2: Thread the template worktree into the subgraph isolation gate

**Files:**
- Modify: `src/sqrlly/compile/subgraph.py` (`make_fan_out_subgraph_invoker` signature + `_isolate` gate)
- Modify: `src/sqrlly/compile/dynamic.py` (`_make_fan_out_node` — pass `template.worktree` into the invoker)
- Test: `tests/unit/compile/test_subgraph.py` (extend — BEHAVIORAL: assert whether a branch worktree is acquired)

**Interfaces:**
- Consumes: `FanOutTemplate.worktree` (Task 1).
- Produces: `make_fan_out_subgraph_invoker(..., template_worktree: str | None = None)` — a new keyword-only-friendly parameter (default `None` = inherit `parent_settings.worktree`). The `_isolate` gate becomes: when `template_worktree` is not None, isolate iff `template_worktree != "off"`; else fall back to `(parent_settings or Settings()).worktree != "off"`.

**Context:** In `subgraph.py`, the factory currently computes:
```python
    _isolate = (parent_settings or Settings()).worktree != "off"
```
The gate is *observable*: when `_isolate` is true (and `prefix` is set and the executor has `acquire_branch_worktree`), `invoke` calls `await executor.acquire_branch_worktree(prefix)`; when false it does not. The test below pins THAT behavior — whether a branch worktree was acquired — using a recording `NodeExecutor` double (sanctioned instrumentation, NOT a mock of an external system) and a stub `compile_fn` returning a graph object whose `ainvoke` yields a trivial sub-state. No closure / freevar introspection.

`dynamic.py::_make_fan_out_node` builds the invoker (~line 86) with `parent_settings=settings`. `template = parent_node.fan_out.template` is already bound at the top of `_make_fan_out_node`.

- [ ] **Step 1: Write the failing test** — a behavioral unit test. Add to `tests/unit/compile/test_subgraph.py`:

```python
# tests/unit/compile/test_subgraph.py — add
import pytest
import yaml as _yaml

from sqrlly.compile.subgraph import make_fan_out_subgraph_invoker
from sqrlly.runtime.result import ExecutionResult
from sqrlly.schema.models import Node, Settings as _Settings


def _write_trivial_sub(tmp_path) -> str:
    sub = {
        "name": "sub", "version": "1.0",
        "nodes": [{"id": "n", "name": "n",
                   "execute": {"url": "/bin/sh", "params": {"args": ["-c", "true"]}}}],
    }
    (tmp_path / "sub.yaml").write_text(_yaml.safe_dump(sub))
    return "sub.yaml"


class _RecordingExecutor:
    """Records whether the per-branch worktree was acquired. Implements the
    NodeExecutor Protocol bits the invoker touches (execute + the optional
    acquire_branch_worktree); duck-typed, not a unittest.mock. The compiled
    subgraph is stubbed below, so execute() here is never actually reached —
    the observable signal is whether acquire_branch_worktree was called."""

    def __init__(self) -> None:
        self.acquired: list[str] = []

    async def acquire_branch_worktree(self, branch_id: str) -> str:
        self.acquired.append(branch_id)
        return f"/wt/{branch_id}"

    async def execute(self, node, context, workdir=None, settings_override=None):
        return ExecutionResult(success=True, output="ok")


class _StubCompiled:
    """Stands in for a compiled LangGraph — only ainvoke is exercised."""
    async def ainvoke(self, state):
        # Minimal terminal state: the subgraph's single node 'n' produced
        # output. _terminal_node_output reads node_outputs[terminals[-1]].
        return {"node_outputs": {"n": "done"}, "failed_nodes": set()}


def _stub_compile_fn(c, executor=None, _depth=0, effective_settings=None):
    return _StubCompiled()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "parent_worktree,template_worktree,expect_acquire",
    [
        # No override → inherit parent settings.
        ("isolated", None, True),
        ("off", None, False),
        # Template override wins over parent settings.
        ("isolated", "off", False),     # the builder's exact use-case
        ("off", "isolated", True),
    ],
)
async def test_fanout_invoker_branch_acquire_follows_effective_worktree(
    tmp_path, parent_worktree, template_worktree, expect_acquire,
):
    sub_yaml = _write_trivial_sub(tmp_path)
    executor = _RecordingExecutor()
    invoker = make_fan_out_subgraph_invoker(
        sub_yaml, {}, compile_fn=_stub_compile_fn, base_dir=tmp_path, depth=0,
        executor=executor, parent_settings=_Settings(worktree=parent_worktree),
        template_worktree=template_worktree,
    )

    result = await invoker({}, str(tmp_path), False, prefix="build::alpha")

    assert result.success is True
    if expect_acquire:
        assert executor.acquired == ["build::alpha"], (
            "expected a branch worktree to be acquired (isolation on)"
        )
        # The acquired tree is surfaced for node_worktrees recording.
        assert result.worktree == "/wt/build::alpha"
    else:
        assert executor.acquired == [], (
            "expected NO branch worktree (worktree:off → run in shared workdir)"
        )
        assert result.worktree is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/compile/test_subgraph.py -q -k "fanout_invoker_branch_acquire"`
Expected: FAIL — the two override rows (`("isolated", "off", ...)` and `("off", "isolated", ...)`) raise `TypeError: make_fan_out_subgraph_invoker() got an unexpected keyword argument 'template_worktree'`. The two no-override rows ALSO error on the unknown kwarg until Step 3 adds the parameter (the test always passes `template_worktree=`), so all four parametrize cases fail at collection/call time. That is expected — the inheritance behavior is re-pinned by the no-override rows once the param exists.

- [ ] **Step 3: Implement** —

(3a) In `src/sqrlly/compile/subgraph.py`, change the `make_fan_out_subgraph_invoker` signature. Current:

```python
def make_fan_out_subgraph_invoker(
    template_url: str,
    template_params: dict[str, Any],
    compile_fn: Any,
    base_dir: str | Path,
    depth: int,
    executor: "NodeExecutor | None",
    logger: Any | None = None,
    parent_settings: Settings | None = None,
) -> Any:
```

becomes:

```python
def make_fan_out_subgraph_invoker(
    template_url: str,
    template_params: dict[str, Any],
    compile_fn: Any,
    base_dir: str | Path,
    depth: int,
    executor: "NodeExecutor | None",
    logger: Any | None = None,
    parent_settings: Settings | None = None,
    template_worktree: str | None = None,
) -> Any:
```

(3b) In the same function, replace the `_isolate` gate. Current:

```python
    # Gate: isolate iff the FAN-OUT scope's effective worktree mode is not
    # "off" (matches how inline fan-out children decide isolation).
    _isolate = (parent_settings or Settings()).worktree != "off"
```

becomes:

```python
    # Gate: isolate iff the effective worktree mode for THIS fan-out is not
    # "off". A per-fan-out override (FanOutTemplate.worktree) wins over the
    # parent scope's settings.worktree; None inherits the parent scope.
    _effective_worktree = (
        template_worktree
        if template_worktree is not None
        else (parent_settings or Settings()).worktree
    )
    _isolate = _effective_worktree != "off"
```

(3c) In `src/sqrlly/compile/dynamic.py`, pass the template worktree into the invoker. Current call (~line 86):

```python
        sub_invoker = make_fan_out_subgraph_invoker(
            template_subgraph_path,
            template.execute.params,
            compile_fn=compile_fn,
            base_dir=base_dir,
            depth=depth,
            executor=executor,
            logger=logger,
            parent_settings=settings,
        )
```

becomes:

```python
        sub_invoker = make_fan_out_subgraph_invoker(
            template_subgraph_path,
            template.execute.params,
            compile_fn=compile_fn,
            base_dir=base_dir,
            depth=depth,
            executor=executor,
            logger=logger,
            parent_settings=settings,
            template_worktree=template.worktree,
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/compile/test_subgraph.py -q -k "fanout_invoker_branch_acquire"`
Expected: PASS (4 parametrize cases: inherit-isolated→acquire, inherit-off→no-acquire, override-off-wins→no-acquire, override-isolated-wins→acquire).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/compile/subgraph.py src/sqrlly/compile/dynamic.py tests/unit/compile/test_subgraph.py
git commit -m "feat: thread FanOutTemplate.worktree into the subgraph fan-out isolation gate"
```

---

### Task 3: Thread the template worktree into the non-subgraph synthetic child node

**Files:**
- Modify: `src/sqrlly/compile/dynamic.py` (`_make_fan_out_node` — set `synthetic_node.worktree` from the template)
- Test: `tests/unit/compile/test_execution_node.py` OR a focused new test — see Step 1; this plan adds a focused unit test to `tests/unit/compile/test_fan_out_resume_skip.py`'s sibling file. **Create** `tests/unit/compile/test_fanout_synthetic_worktree.py`.

**Interfaces:**
- Consumes: `FanOutTemplate.worktree` (Task 1).
- Produces: the `synthetic_node` built in `_make_fan_out_node` carries `worktree=template.worktree`, so `ForemanExecutor.execute` resolves `synthetic_node.effective_worktree(settings)` with the per-fan-out override for `.md`/`.py`/script templates.

**Context:** In `dynamic.py::_make_fan_out_node`, the non-subgraph path constructs (~line 129):

```python
        synthetic_node = Node(
            id=child_id,
            name=f"{parent_node.name} - {item.get('name', item_id)}",
            evaluation=template.evaluation,
            execute=template.execute,
        )
```

This node is run via `execute_with_timeout(executor, synthetic_node, context, timeout, settings_override=effective_settings)`, and `ForemanExecutor.execute` calls `node.effective_worktree(s)` where `s = settings_override or self._settings`. Setting `worktree=template.worktree` on the synthetic node makes `effective_worktree` (Node-mode-wins-over-settings) apply the per-fan-out override exactly as a top-level `Node.worktree` would. `None` inherits `settings.worktree` (unchanged behavior).

- [ ] **Step 1: Write the failing test** — verify the synthetic node carries the template worktree by introspecting what the executor receives. Use `MockExecutor` (the sanctioned `NodeExecutor` double) to capture the node handed to `execute()`. **Create** `tests/unit/compile/test_fanout_synthetic_worktree.py`:

```python
"""The non-subgraph fan-out synthetic child node inherits the template's
worktree override, so ForemanExecutor.effective_worktree resolves it.

Uses MockExecutor (the sanctioned NodeExecutor double) to capture the
Node object handed to execute(); no LLM, no git.
"""
from __future__ import annotations

import pytest

from sqrlly.compile.dynamic import _make_fan_out_node
from sqrlly.runtime.result import ExecutionResult
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import (
    Execute, FanOut, FanOutTemplate, Graph, Node, Settings,
)

from mock_executor import MockExecutor


def _parent(template_worktree):
    return Node(
        id="build", name="build",
        fan_out=FanOut(
            template=FanOutTemplate(
                execute=Execute(url="/bin/sh", params={"args": ["-c", "true"]}),
                worktree=template_worktree,
            ),
        ),
    )


class _CapturingExecutor(MockExecutor):
    """Captures the Node passed to execute(); returns a trivial success."""
    def __init__(self):
        super().__init__()
        self.captured: list[Node] = []

    async def execute(self, node, context, workdir=None, settings_override=None):
        self.captured.append(node)
        return ExecutionResult(success=True, output="ok")


@pytest.mark.asyncio
@pytest.mark.parametrize("template_worktree", ["off", "isolated", None])
async def test_synthetic_child_inherits_template_worktree(template_worktree):
    parent = _parent(template_worktree)
    config = Graph(name="t", version="1.0", nodes=[parent],
                   settings=Settings(worktree="isolated"))
    executor = _CapturingExecutor()
    node_fn = _make_fan_out_node(
        parent, config, executor,
        compile_fn=lambda *a, **k: None, base_dir=".", depth=0,
        effective_settings=config.settings,
    )
    state = make_initial_state(workdir=".")
    state["_fan_out_item"] = {"id": "x"}
    await node_fn(state)

    assert len(executor.captured) == 1
    synthetic = executor.captured[0]
    assert synthetic.id == "build::x"
    assert synthetic.worktree == template_worktree
    # effective_worktree resolves the per-fan-out override over settings.
    expected = (template_worktree or "isolated", None)
    assert synthetic.effective_worktree(config.settings) == expected
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/compile/test_fanout_synthetic_worktree.py -q`
Expected: FAIL — the synthetic node currently has `worktree=None` (the constructor never sets it), so `synthetic.worktree == template_worktree` fails for the `"off"` and `"isolated"` cases, and `effective_worktree` returns `("isolated", None)` (inherited) rather than the override. (The `None` case passes — it already inherits.) Report which parametrize cases fail.

- [ ] **Step 3: Implement** — in `src/sqrlly/compile/dynamic.py`, add `worktree=template.worktree` to the synthetic node. Replace:

```python
        synthetic_node = Node(
            id=child_id,
            name=f"{parent_node.name} - {item.get('name', item_id)}",
            evaluation=template.evaluation,
            execute=template.execute,
        )
```

with:

```python
        synthetic_node = Node(
            id=child_id,
            name=f"{parent_node.name} - {item.get('name', item_id)}",
            evaluation=template.evaluation,
            execute=template.execute,
            # Per-fan-out worktree override (FanOutTemplate.worktree). None
            # inherits settings.worktree — identical to a top-level node with
            # no worktree set. Node-mode-wins-over-settings in
            # effective_worktree applies the override at the foreman gate.
            worktree=template.worktree,
        )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/compile/test_fanout_synthetic_worktree.py -q`
Expected: PASS (3 parametrize cases).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/compile/dynamic.py tests/unit/compile/test_fanout_synthetic_worktree.py
git commit -m "feat: non-subgraph fan-out child inherits FanOutTemplate.worktree override"
```

---

### Task 4: Missing-`id` WARN in manifest normalization

**Files:**
- Modify: `src/sqrlly/compile/_manifest.py` (`_normalize_items` — warn on a dict item lacking `id`)
- Test: `tests/unit/compile/test_manifest.py` (extend the `TestNormalizeItems` class)

**Interfaces:**
- Produces: `_normalize_items` logs a WARNING (via `logging.getLogger(__name__)`) once per dict item that has no `id` key. Items are NOT mutated — they pass through unchanged (the existing `…::unknown` collapse still occurs; the WARN is the diagnostic the builder lost 40 minutes to). Scalar items (already coerced to `{"id": ...}`) and dict items WITH an `id` are silent.

**Context:** `_normalize_items(items)` (in `compile/_manifest.py`) is the single funnel for both the node-output manifest and the on-disk manifest (`_read_manifest` calls it in all four branches). `compile/_manifest.py` currently has no `logging` import; add it. The existing zero-items warning lives in `graph.py::_make_dynamic_router` and uses `logging.getLogger(__name__).warning(...)` — mirror that.

- [ ] **Step 1: Write the failing test** — add to the `TestNormalizeItems` class in `tests/unit/compile/test_manifest.py`:

```python
    def test_dict_item_missing_id_warns(self, caplog):
        """A dict manifest item with no 'id' collapses every branch to one
        ::unknown child — _normalize_items must WARN so the author sees it."""
        import logging

        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"topic": "x"}, {"topic": "y"}])}
        )
        with caplog.at_level(logging.WARNING):
            result = _read_manifest(state, _phase_with_dynamic())
        # Items pass through unchanged (no synthetic id injected).
        assert result == [{"topic": "x"}, {"topic": "y"}]
        # One warning per id-less dict item.
        missing_id_warnings = [
            r for r in caplog.records if "missing 'id'" in r.message
        ]
        assert len(missing_id_warnings) == 2

    def test_dict_item_with_id_is_silent(self, caplog):
        import logging

        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"id": "a"}, {"id": "b"}])}
        )
        with caplog.at_level(logging.WARNING):
            _read_manifest(state, _phase_with_dynamic())
        assert not any("missing 'id'" in r.message for r in caplog.records)

    def test_scalar_items_do_not_warn(self, caplog):
        """Scalars are coerced to {'id': str(item)} → they have an id → silent."""
        import logging

        state = make_initial_state(node_outputs={"p1": json.dumps(["alpha", "beta"])})
        with caplog.at_level(logging.WARNING):
            _read_manifest(state, _phase_with_dynamic())
        assert not any("missing 'id'" in r.message for r in caplog.records)
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/compile/test_manifest.py -q -k "missing_id or do_not_warn or with_id_is_silent"`
Expected: FAIL — `test_dict_item_missing_id_warns` asserts 2 warnings but `_normalize_items` emits none (`len(missing_id_warnings) == 0`). (The two silent-case tests already pass.)

- [ ] **Step 3: Implement** — in `src/sqrlly/compile/_manifest.py`:

(3a) Add the `logging` import near the top (after `import json`):

```python
import json
import logging
import re
```

(3b) Add a module-level logger after the `_FENCE_RE` definition:

```python
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)\n?```", re.DOTALL)

logger = logging.getLogger(__name__)
```

(3c) In `_normalize_items`, warn on a dict item with no `id`. Current dict branch:

```python
    out: list[dict] = []
    for i, item in enumerate(items):
        if isinstance(item, dict):
            out.append(item)
        elif isinstance(item, (str, int, float, bool)):
            out.append({"id": str(item)})
        else:
            raise ValueError(
                f"fan-out manifest item {i} must be an object or scalar, "
                f"got {type(item).__name__}: {item!r}"
            )
    return out
```

becomes:

```python
    out: list[dict] = []
    for i, item in enumerate(items):
        if isinstance(item, dict):
            if "id" not in item:
                # An id-less dict item collapses every branch onto one
                # `<parent>::unknown` child (the fan-out body keys child_id
                # off item.get("id", "unknown")). Warn loudly — this is a
                # silent N→1 fan-out collapse the author rarely intends.
                logger.warning(
                    "fan-out manifest item %d is missing 'id' (%r) — every "
                    "such item collapses onto a single '::unknown' branch; "
                    "give each manifest item a unique 'id'.",
                    i, item,
                )
            out.append(item)
        elif isinstance(item, (str, int, float, bool)):
            out.append({"id": str(item)})
        else:
            raise ValueError(
                f"fan-out manifest item {i} must be an object or scalar, "
                f"got {type(item).__name__}: {item!r}"
            )
    return out
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/compile/test_manifest.py -q`
Expected: PASS (the full manifest suite, including the new 3 cases and all pre-existing tests).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/compile/_manifest.py tests/unit/compile/test_manifest.py
git commit -m "feat: warn when a fan-out manifest dict item is missing 'id'"
```

---

### Task 5: Fail-loud on DUPLICATE child ids before Send dispatch

**Files:**
- Modify: `src/sqrlly/compile/graph.py` (`_make_dynamic_router` — raise `ManifestError` on duplicate child ids)
- Test: `tests/unit/compile/test_manifest.py` (extend the `TestEmptyManifestRouting` class, or add a `TestDuplicateChildIds` class)

**Interfaces:**
- Consumes: nothing new.
- Produces: `_make_dynamic_router`'s inner `router(state)` raises `ManifestError` when two manifest items resolve to the SAME child id (`<parent>::<item_id>`), naming the parent and the colliding id, before returning the `Send(...)` list. This catches both literal duplicate `id`s AND the `::unknown` collapse (≥2 id-less dict items, which all map to `<parent>::unknown`).

**Context:** `compile/graph.py::_make_dynamic_router` returns the Send list:

```python
        items = _read_manifest(state, node)
        if not items:
            logging.getLogger(__name__).warning(...)
            return no_items_targets

        return [
            Send(template_node_id, {**state, "_fan_out_item": item})
            for item in items
        ]
```

The child id formula matches `dynamic.py::_make_fan_out_node`: `f"{node.id}::{item.get('id', 'unknown')}"`. `graph.py` already imports `from sqrlly.runtime.result import RouteError` (line 24) — add `ManifestError` to that import.

- [ ] **Step 1: Write the failing test** — add to `tests/unit/compile/test_manifest.py`:

```python
class TestDuplicateChildIds:
    def test_duplicate_literal_ids_raise(self):
        """Two manifest items with the same 'id' would dispatch two Send
        branches onto one child id — raise before dispatch."""
        from sqrlly.compile.graph import _make_dynamic_router
        from sqrlly.runtime.result import ManifestError

        node = _phase_with_dynamic()  # parent id 'p1'
        router = _make_dynamic_router(node, no_items_targets=["after"])
        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"id": "dup"}, {"id": "dup"}])}
        )
        with pytest.raises(ManifestError, match="duplicate"):
            router(state)

    def test_unknown_collapse_raises(self):
        """≥2 id-less dict items all map to p1::unknown — a duplicate."""
        from sqrlly.compile.graph import _make_dynamic_router
        from sqrlly.runtime.result import ManifestError

        node = _phase_with_dynamic()
        router = _make_dynamic_router(node, no_items_targets=["after"])
        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"topic": "a"}, {"topic": "b"}])}
        )
        with pytest.raises(ManifestError, match="unknown"):
            router(state)

    def test_unique_ids_dispatch_one_send_each(self):
        """Distinct ids → no raise; one Send per item."""
        from langgraph.types import Send

        from sqrlly.compile.graph import _make_dynamic_router

        node = _phase_with_dynamic()
        router = _make_dynamic_router(node, no_items_targets=["after"])
        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"id": "a"}, {"id": "b"}])}
        )
        result = router(state)
        assert isinstance(result, list) and len(result) == 2
        assert all(isinstance(s, Send) for s in result)
        sent_ids = sorted(s.arg["_fan_out_item"]["id"] for s in result)
        assert sent_ids == ["a", "b"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/compile/test_manifest.py -q -k "TestDuplicateChildIds"`
Expected: FAIL — `test_duplicate_literal_ids_raise` and `test_unknown_collapse_raises` do not raise (the router returns a 2-element Send list); `test_unique_ids_dispatch_one_send_each` already passes (it pins the happy path so the guard does not regress normal fan-out).

- [ ] **Step 3: Implement** —

(3a) In `src/sqrlly/compile/graph.py`, extend the import on line 24. Current:

```python
from sqrlly.runtime.result import RouteError
```

becomes:

```python
from sqrlly.runtime.result import ManifestError, RouteError
```

(3b) In `_make_dynamic_router`, add the duplicate guard before building the Send list. Current tail of `router`:

```python
        items = _read_manifest(state, node)
        if not items:
            logging.getLogger(__name__).warning(
                "fan-out %r: manifest resolved to zero items — routing to "
                "no-items target(s) %s instead of fanning out.",
                node.id, no_items_targets,
            )
            return no_items_targets

        return [
            Send(template_node_id, {**state, "_fan_out_item": item})
            for item in items
        ]
```

becomes:

```python
        items = _read_manifest(state, node)
        if not items:
            logging.getLogger(__name__).warning(
                "fan-out %r: manifest resolved to zero items — routing to "
                "no-items target(s) %s instead of fanning out.",
                node.id, no_items_targets,
            )
            return no_items_targets

        # Fail loud on colliding child ids before dispatch. Each branch's
        # child id is `<parent>::<item_id>` (matching dynamic._make_fan_out_node).
        # Two items mapping to the same id (literal duplicate, or ≥2 id-less
        # dict items collapsing onto `::unknown`) would silently merge into one
        # branch — historically a multi-minute timeout, not an error.
        seen: set[str] = set()
        duplicates: set[str] = set()
        for item in items:
            child_id = f"{node.id}::{item.get('id', 'unknown')}"
            if child_id in seen:
                duplicates.add(child_id)
            seen.add(child_id)
        if duplicates:
            raise ManifestError(
                f"fan-out {node.id!r}: manifest produces duplicate child id(s) "
                f"{sorted(duplicates)!r} — each item needs a unique 'id' "
                f"(id-less items collapse onto '{node.id}::unknown')."
            )

        return [
            Send(template_node_id, {**state, "_fan_out_item": item})
            for item in items
        ]
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/compile/test_manifest.py -q -k "TestDuplicateChildIds"`
Expected: PASS (3 cases). Note the `match=` strings: `"duplicate"` matches the message body; `"unknown"` matches the trailing parenthetical (the `::unknown` collapse message includes the literal `unknown`).

- [ ] **Step 5: Run the architecture suite + the full manifest suite**

Run: `uv run pytest tests/architecture/test_layers.py tests/unit/compile/test_manifest.py -q`
Expected: PASS — `compile/graph.py` importing `ManifestError` from `runtime.result` is already allowed (it imports `RouteError` from the same module today; `runtime.result` is in `ALLOWED_RUNTIME_MODULES`).

- [ ] **Step 6: Commit**

```bash
git add src/sqrlly/compile/graph.py tests/unit/compile/test_manifest.py
git commit -m "fix: fail loud on duplicate fan-out child ids before Send dispatch"
```

---

### Task 6: Load-bearing e2e — `worktree: off` template override visible at the shared base

**Files:**
- Test: `tests/e2e/test_fanout_worktree_control.py` (create)

**Interfaces:**
- Consumes: Tasks 1–3 (template worktree → subgraph + synthetic isolation).

**Context:** This proves the builder's exact use-case end-to-end with pure `/bin/sh` script nodes (no LLM, deterministic). Model the git-repo + ForemanExecutor + DispatchExecutor + `build_workflow_graph` + `ainvoke` wiring on `tests/e2e/test_subgraph_fanout_worktree.py` (read it first). The workflow has global `settings.worktree: isolated`; the fan-out template sets `worktree: off`; each branch appends a line to a file in the SHARED base workdir; a downstream join node reads that file and proves the branch writes landed in base (which isolation would have hidden inside per-branch trees).

The e2e covers BOTH execution paths via two test methods sharing one fixture shape:
  - `test_subgraph_template_off_writes_to_base` — template `execute.url` is a `.yaml` subgraph; `worktree: off` makes the subgraph branch invoker fall through to `workdir` (the base), so branch writes hit base.
  - `test_script_template_off_writes_to_base` — template `execute.url` is `/bin/sh` (non-subgraph); `worktree: off` on the synthetic child makes `effective_worktree` resolve `off`, so the foreman runs it in `self._base`.

Plus a control method asserting the DEFAULT (no override) isolates: with `worktree: isolated` inherited and NO template override, branch writes do NOT appear in base (they land in per-branch trees). This is the contrast that makes the override meaningful.

- [ ] **Step 1: Write the failing e2e test** — create `tests/e2e/test_fanout_worktree_control.py`:

```python
"""Per-fan-out worktree override (FanOutTemplate.worktree) — the off-override
makes branch writes land in the SHARED base workdir where global
settings.worktree:isolated would have hidden them in per-branch trees.

ONE workflow, global settings.worktree:isolated. A fan-out whose template
sets worktree:off; each branch appends to a file in the base workdir; a
downstream join node reads it. Proves the override for BOTH execution
paths (subgraph .yaml template + non-subgraph /bin/sh template) and the
default-isolation contrast. Pure /bin/sh nodes — no LLM, deterministic.
Models tests/e2e/test_subgraph_fanout_worktree.py.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.foreman import ForemanExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph, Settings

_PARENT_ID = "build"
_ITEMS = [{"id": "alpha"}, {"id": "beta"}, {"id": "gamma"}]


def _init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "init"],
        check=True, capture_output=True,
        env={**os.environ,
             "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@test.com",
             "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@test.com"},
    )


def _write_manifest(tmp_path: Path) -> None:
    (tmp_path / "items.json").write_text(json.dumps({"items": _ITEMS}))


def _script_node(item_template: str) -> dict:
    """A /bin/sh node that appends '<item>\\n' to shared.txt in its CWD.

    `>>` append + a per-item line lets the join node count how many
    branches wrote to the SAME shared.txt. In a branch worktree these
    appends are invisible to base; with worktree:off they land in base.
    """
    return {
        "id": "appender", "name": "appender",
        "execute": {
            "url": "/bin/sh",
            "params": {"args": ["-c", f"echo {item_template} >> shared.txt"]},
        },
    }


def _build_config(
    *, template_worktree, subgraph: bool, tmp_path: Path,
) -> Graph:
    """Fan-out over 3 items; join node reads shared.txt from base.

    When `subgraph` is True the template is a one-node subgraph .yaml whose
    node appends to shared.txt; else the template is the /bin/sh appender
    directly. `template_worktree` is the per-fan-out override (None inherits).
    """
    template_execute: dict
    if subgraph:
        sub = {
            "name": "appender-sub", "version": "1.0",
            "nodes": [_script_node("{{item_id}}")],
        }
        (tmp_path / "appender_sub.yaml").write_text(yaml.safe_dump(sub))
        template_execute = {
            "url": "appender_sub.yaml",
            "params": {"inputs": {"item_id": "{{id}}"}},
        }
    else:
        template_execute = {
            "url": "/bin/sh",
            "params": {"args": ["-c", "echo {{id}} >> shared.txt"]},
        }

    template: dict = {"execute": template_execute}
    if template_worktree is not None:
        template["worktree"] = template_worktree

    return Graph(
        name="fanout-worktree-control", version="1.0",
        nodes=[
            {
                "id": _PARENT_ID, "name": "Build",
                "execute": {
                    "url": "/bin/sh",
                    "params": {"args": ["-c",
                                         f"printf '%s' '{json.dumps({'items': _ITEMS})}'"]},
                },
                "fan_out": {"template": template},
            },
            {
                "id": "join", "name": "Join",
                "depends_on": [_PARENT_ID],
                "execute": {
                    "url": "/bin/sh",
                    # Print the file if it exists, else nothing. Runs in base
                    # (join inherits isolated, but we only read what branches
                    # left in base — see assertions).
                    "params": {"args": ["-c",
                                         "cat shared.txt 2>/dev/null || true"]},
                },
            },
        ],
        settings=Settings(worktree="isolated"),
    )


async def _run(config: Graph, tmp_path: Path) -> dict:
    inner = DispatchExecutor(workdir=str(tmp_path))
    foreman = ForemanExecutor(inner, base_workdir=str(tmp_path), max_parallel_jobs=4)
    graph = build_workflow_graph(config, foreman, _base_dir=tmp_path)
    state = make_initial_state(workdir=str(tmp_path))
    return await graph.ainvoke(state)


class TestFanOutWorktreeControl:
    @pytest.mark.asyncio
    async def test_subgraph_template_off_writes_to_base(self, tmp_path):
        """Subgraph template + worktree:off → branch appends land in base."""
        _init_git_repo(tmp_path)
        config = _build_config(
            template_worktree="off", subgraph=True, tmp_path=tmp_path,
        )
        final = await _run(config, tmp_path)

        for item in _ITEMS:
            assert f"{_PARENT_ID}::{item['id']}" in final["completed_nodes"]

        # The off-override sent every branch's append to the shared base file.
        shared = tmp_path / "shared.txt"
        assert shared.exists(), "shared.txt missing from base — off-override failed"
        lines = sorted(shared.read_text().split())
        assert lines == ["alpha", "beta", "gamma"], (
            f"expected all 3 branch appends in base, got {lines!r}"
        )

    @pytest.mark.asyncio
    async def test_script_template_off_writes_to_base(self, tmp_path):
        """Non-subgraph /bin/sh template + worktree:off → appends land in base."""
        _init_git_repo(tmp_path)
        config = _build_config(
            template_worktree="off", subgraph=False, tmp_path=tmp_path,
        )
        final = await _run(config, tmp_path)

        for item in _ITEMS:
            assert f"{_PARENT_ID}::{item['id']}" in final["completed_nodes"]

        shared = tmp_path / "shared.txt"
        assert shared.exists(), "shared.txt missing from base — off-override failed"
        lines = sorted(shared.read_text().split())
        assert lines == ["alpha", "beta", "gamma"]

    @pytest.mark.asyncio
    async def test_default_isolated_hides_branch_writes_from_base(self, tmp_path):
        """Contrast: NO override → inherits settings.worktree:isolated → branch
        appends land in per-branch trees, NOT base. (subgraph template.)"""
        _init_git_repo(tmp_path)
        config = _build_config(
            template_worktree=None, subgraph=True, tmp_path=tmp_path,
        )
        final = await _run(config, tmp_path)

        for item in _ITEMS:
            assert f"{_PARENT_ID}::{item['id']}" in final["completed_nodes"]

        # Isolation hid the writes: nothing in base. The branch worktrees
        # under .sqrlly/ each hold their own shared.txt.
        assert not (tmp_path / "shared.txt").exists(), (
            "branch writes leaked into base under isolated — isolation broken"
        )
        sqrlly_dir = tmp_path / ".sqrlly"
        branch_trees = list(sqrlly_dir.glob(f"wt-{_PARENT_ID}__*"))
        assert len(branch_trees) == 3, (
            f"expected 3 branch worktrees, got {[d.name for d in branch_trees]}"
        )
        # Each branch tree holds its own one-line shared.txt.
        per_tree = sorted(
            (t / "shared.txt").read_text().strip()
            for t in branch_trees if (t / "shared.txt").exists()
        )
        assert per_tree == ["alpha", "beta", "gamma"]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/e2e/test_fanout_worktree_control.py -q`
Expected: BEFORE Tasks 1–3 are merged this file won't import the schema field at all — but you are implementing in order, so by the time you reach Task 6, Tasks 1–3 are done and the `off`-override cases (`test_subgraph_template_off_writes_to_base`, `test_script_template_off_writes_to_base`) should PASS. If running this task in isolation against an unbuilt tree, the `off` cases FAIL with `shared.txt missing from base` (isolation still active) and the construction raises `ValidationError` on the unknown `worktree` template key. The control case (`test_default_isolated_hides_branch_writes_from_base`) passes regardless (it asserts the pre-existing isolation behavior). Report the observed failure.

- [ ] **Step 3: Implement** — no source change; Tasks 1–3 already implement the override. This task is the integration proof. If a method fails, debug against `tests/e2e/test_subgraph_fanout_worktree.py` (fixture wiring) and `runtime/foreman.py::execute` (the `kind == "off"` → `run_dir = workdir or self._base` branch) until the failure is a real assertion, not a setup error.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/e2e/test_fanout_worktree_control.py -q`
Expected: PASS (3 methods). The two off-override cases prove branch writes reach base across both execution paths; the control proves default isolation still hides them.

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/test_fanout_worktree_control.py
git commit -m "test: e2e proving FanOutTemplate.worktree:off writes reach the shared base"
```

---

### Task 7: Docs + CHANGELOG + TODO

**Files:**
- Modify: `SCHEMA.md`, `CLAUDE.md`, `SKILLS.md`, `CHANGELOG.md`, `TODO.md`

**Interfaces:** none.

- [ ] **Step 1: SCHEMA.md** — in the `FanOutTemplate` / fan-out section (search for `FanOutTemplate` or the `template:` field table), add a `worktree` row. Match the surrounding table's column shape; representative form:

```markdown
| `worktree` | `"auto" \| "isolated" \| "off"` | `null` (inherit `settings.worktree`) | Per-fan-out isolation override. Lets one workflow run an isolated build fan-out AND a shared-base planner fan-out under one top-level `settings.worktree`: this value overrides `settings.worktree` for every branch of THIS fan-out. `off` runs branches in the shared base workdir (their writes are visible to a downstream join node); `isolated`/`auto` give each branch its own git worktree. Same optional-override semantics as `Node.worktree`. Applies to both subgraph (`.yaml`) and script (`.md`/`.py`) templates. |
```

- [ ] **Step 2: CLAUDE.md** — in "Known limitations", under the existing **Worktree dep sharing** / fan-out worktree notes, add a line (hard-wrapped to match the file):

```markdown
- **Per-fan-out worktree override** — `fan_out.template.worktree` (`auto`/`isolated`/`off`, default inherit) overrides `settings.worktree` for one fan-out's branches, so a single workflow can mix an isolated build fan-out with a shared-base planner fan-out. Threaded two ways to match the two fan-out execution paths: subgraph templates gate isolation in `compile/subgraph.py::make_fan_out_subgraph_invoker` (new `template_worktree` arg overrides the `_isolate` computation); non-subgraph (`.md`/`.py`/script) templates set the synthetic child node's `worktree` in `compile/dynamic.py::_make_fan_out_node` so `effective_worktree` resolves it at the foreman gate. `off` runs branches in the base workdir (writes visible to a join node).
```

Also add (near the fan-out / manifest notes):

```markdown
- **Fan-out child-id collisions fail loud** — a dict manifest item missing `id` WARNs in `compile/_manifest.py::_normalize_items` (every id-less item collapses onto `<parent>::unknown`), and the Send router (`compile/graph.py::_make_dynamic_router`) raises `ManifestError` on any DUPLICATE child id before dispatch — catching both literal duplicate `id`s and the `::unknown` collapse before it becomes a silent N→1 fan-out.
```

- [ ] **Step 3: SKILLS.md** — near the fan-out / worktree authoring guidance, add (hard-wrapped to match the file):

```markdown
A fan-out can override isolation per-block with `fan_out.template.worktree`
(`auto`/`isolated`/`off`, default = inherit `settings.worktree`). Use
`worktree: off` on the template when branches must write to the SHARED base
workdir (a join node reads what they wrote); use `isolated` for a parallel
build whose branch deltas you promote with `fan_out.promote`. Always give
each manifest item a unique `id` — an id-less item WARNs and collapses every
branch onto one `<parent>::unknown` child; a duplicate `id` is a hard
`ManifestError` before dispatch.
```

- [ ] **Step 4: CHANGELOG.md** — add to the existing `## [Unreleased]` → `### Added` block (it already exists from #7; append these bullets — do NOT create a second Unreleased section):

```markdown
- `fan_out.template.worktree` (`auto`/`isolated`/`off`, default inherit) — per-fan-out worktree-isolation override of `settings.worktree`, so one workflow can run an isolated build fan-out alongside a shared-base planner fan-out. `worktree: off` runs branches in the shared base workdir (their writes reach a downstream join node); threaded through both fan-out execution paths (subgraph `make_fan_out_subgraph_invoker` and the non-subgraph synthetic child node).
- Fail-loud fan-out child ids: a dict manifest item missing `id` now WARNs (it silently collapses every branch onto one `<parent>::unknown` child), and the Send router raises `ManifestError` on any duplicate child id before dispatch.
```

- [ ] **Step 5: TODO.md** — locate the `### B8` (or `#8`) heading. Rewrite it to the SHIPPED style matching the file's `### ✅ B7 … — SHIPPED …` convention: `### ✅ B8 — Per-fan-out worktree control + stable fan-out item id — SHIPPED 0.7.x`, body trimmed to current state (names `fan_out.template.worktree`, the two-path threading, the missing-id WARN, and the duplicate-id `ManifestError`). If no B8 entry exists, add the shipped entry in the appropriate section.

- [ ] **Step 6: Sanity + commit**

```bash
uv run sqrlly validate examples/jokes/workflow.yaml   # still Valid
git add SCHEMA.md CLAUDE.md SKILLS.md CHANGELOG.md TODO.md
git commit -m "docs: document fan_out.template.worktree + fail-loud child ids; mark B8 shipped"
```

---

## Final verification (after all tasks)

```bash
uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -q   # full core suite green
uv run pytest tests/e2e/test_fanout_worktree_control.py -q      # the load-bearing e2e
uv run pytest tests/architecture/test_layers.py -q              # layer rules intact
uv run pytest tests/unit/schema/test_worktree_control.py tests/unit/compile/test_manifest.py tests/unit/compile/test_subgraph.py -q
```
Expected: full suite passes; the per-fan-out `worktree: off` override is proven to make branch writes reach the shared base across both execution paths; missing-id WARNs and duplicate-id raises `ManifestError`.
