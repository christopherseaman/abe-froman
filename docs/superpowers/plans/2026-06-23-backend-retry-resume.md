# Transient-backend-error resilience for fan-out builds (#9) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. TRANSCRIBE the code verbatim — every block is complete and the line context was traced against the live tree on branch `feat/backend-retry-resume` (after #7/#8 merged at 0.7.5).

**Goal:** Two independent asks on fan-out build resilience.
(a) **Retry transient backend errors** — a `Settings.backend_max_retries` (opt-in, default `0`) that retries the SAME dispatch on a non-`OverloadError` backend failure (e.g. `claude exited 1`, a transient blip) up to N times with bounded backoff, before surfacing terminal failure. Distinct from the gate/evaluation `max_retries` (which fires only on a low gate score). `backend_max_retries: 0` MUST be exactly today's behavior (one attempt, terminal). (b) **Re-run only failed fan-out children on `--resume`** — today a failed fan-out child is unreachable on resume: the child is NOT in `completed_nodes`, the parent IS (it emitted the manifest), so the parent is skipped (not dirty) and never re-fans. Make a fan-out parent with failed children DIRTY in `compute_skip_set` (so it re-fans) while completed children stay frozen, and gate the per-child re-run on the skip set so completed siblings are NOT re-billed.

**Architecture:**
- **Part A** wraps at `runtime/executor/prompt.py::PromptExecutor.execute_rendered`. That method already owns the `OverloadError`→model-downgrade `while True` loop. We wrap THAT loop in a sibling bounded transient-retry layer: an outer loop that re-enters the downgrade loop on a non-overload backend exception, up to `s.backend_max_retries` times, sleeping `s.retry_backoff`-derived seconds between attempts. Overload still flows through the inner downgrade loop and is NOT counted as a backend retry (no double-counting). The CLI backend (`backends/cli.py`) already raises a plain `RuntimeError` for non-overload non-zero exits; nothing there changes — the retry layer sits above the backend. `_dispatch_prompt` already passes `settings=settings` into `execute_rendered`, so the scope's `backend_max_retries` / `retry_backoff` are already in hand — no dispatch.py change.
- **Part B** changes TWO funnels, coordinated:
  - `compile/resume.py::compute_skip_set` — a failed child id `<parent>::<item>` is not a `config.nodes` id, so today the dirty BFS never reaches the parent. Add a pre-pass: for each `prior_failed` id of the form `<pid>::<...>` whose `<pid>` is a real fan-out parent in `config.nodes`, seed `<pid>` into the dirty frontier. The dirty closure then dirties the parent (and, via the existing `_final_<parent>_<f>` synthetic-dependent wiring, its finals). The parent leaves `skip` → its node body's `_resume_skip` guard (`compile/nodes.py` line ~616) no longer freezes it → it re-runs, re-emits the manifest, and the router re-fans.
  - `compile/dynamic.py::_make_fan_out_node` child-skip gate (line ~107-110) — currently `if skip and parent_node.id in skip and child_id in skip: return {}`. Because a re-fanning parent is dirty (NOT in skip), today EVERY child re-runs — re-billing completed siblings. Change the gate to freeze a child purely on `child_id in skip`: a child that completed cleanly last run (in the frozen skip snapshot) is frozen regardless of whether the parent re-fanned; the formerly-failed child (never in `prior_completed`, so never in skip) is NOT frozen and re-runs. This is stable-id-safe: only ids that genuinely completed last run are frozen, so a manifest that drifts on re-fan simply produces new ids that aren't in skip and run normally.

  On a fresh resume run `failed_nodes` is reseeded EMPTY (CLI `state["failed_nodes"] = set()`), so the `if child_id in state.get("failed_nodes")` guard at the top of `node_fn` (line ~103) does not block the formerly-failed child — it falls through to the skip gate, which lets it run.

**Tech Stack:** Python 3.11+, Pydantic v2, LangGraph, `AsyncSqliteSaver` checkpointer, pytest, pytest-asyncio.

## Global Constraints

- Python `>=3.11`; use `sys.executable` for the Python interpreter in subprocess-driven tests — the `python` binary is unavailable in this env. (`tests/e2e/test_resume_fan_out.py` already binds `_PYTHON = sys.executable`; the new resume test reuses that pattern.)
- Layer rules (`tests/architecture/test_layers.py`): `schema/` must not import `langgraph`; `runtime/` must not import `sqrlly.compile` or `langgraph`; `compile/` must not import `sqrlly.cli`. Part A lives entirely in `runtime/` + `schema/`; the backoff computation is inlined in `prompt.py` (do NOT import `compile/nodes.py::_get_retry_delay` — that would invert the layer). Part B lives in `compile/` + `schema/` (no change) and reads only `runtime.state` shapes already imported.
- No mocks of external systems; tests use real subprocess / real `AsyncSqliteSaver` / real `DispatchExecutor`. A duck-typed `PromptBackend` double (scripted exit / call counter — NOT `unittest.mock`, NOT a fake of the real Claude network behavior) is sanctioned instrumentation for transient-error simulation, identical in spirit to the existing `OverloadThenSucceedBackend` / `ErrorBackend` doubles in `tests/unit/runtime/test_prompt.py`. `MockExecutor` is the sanctioned `NodeExecutor` double.
- `extra="forbid"` on all schema models — a typo'd field is a hard `ValidationError`, so `backend_max_retries` must be spelled exactly.
- Conventional-commit messages; **no attribution trailers** (no `Co-Authored-By`, no `via Happy`).
- Do not change the signatures of `PromptBackend.send_prompt`, `DispatchExecutor._dispatch_prompt`, or `ForemanExecutor.execute`. Part A's behavior change is internal to `execute_rendered`; the setting is read from the already-threaded `settings`.

---

### Task 1: Schema — `Settings.backend_max_retries`

**Files:**
- Modify: `src/sqrlly/schema/models.py` (the `Settings` model)
- Test: `tests/unit/schema/test_settings.py` (extend if present; else create `tests/unit/schema/test_backend_retries.py`)

**Interfaces:**
- Produces: `Settings.backend_max_retries: int = 0` — opt-in count of transient-backend-error retries of the SAME dispatch, distinct from `max_retries` (the evaluation/gate budget). `0` = today's behavior (one attempt, terminal on a raised backend error).

- [ ] **Step 1: Write the failing test** — create `tests/unit/schema/test_backend_retries.py`:

```python
"""Settings.backend_max_retries — opt-in transient-backend retry count,
distinct from the gate/evaluation max_retries."""
import pytest
from pydantic import ValidationError

from sqrlly.schema.models import Settings


def test_backend_max_retries_defaults_to_zero():
    """Default 0 = today's behavior (one attempt, terminal)."""
    assert Settings().backend_max_retries == 0


def test_backend_max_retries_is_independent_of_max_retries():
    """The two budgets are separate fields — setting one leaves the other."""
    s = Settings(backend_max_retries=3)
    assert s.backend_max_retries == 3
    assert s.max_retries == 3  # gate default, untouched

    s2 = Settings(max_retries=7)
    assert s2.max_retries == 7
    assert s2.backend_max_retries == 0  # backend default, untouched


def test_backend_max_retries_accepts_positive_int():
    assert Settings(backend_max_retries=5).backend_max_retries == 5


def test_unknown_field_still_rejected_under_extra_forbid():
    """extra='forbid' invariant: a typo'd field is a hard error."""
    with pytest.raises(ValidationError):
        Settings(backend_max_retry=2)  # singular typo
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/schema/test_backend_retries.py -q`
Expected: FAIL — `test_backend_max_retries_defaults_to_zero` fails with `AttributeError: 'Settings' object has no attribute 'backend_max_retries'`; the `backend_max_retries=...` constructions raise `ValidationError` ("Extra inputs are not permitted") under `extra="forbid"`. (`test_unknown_field_still_rejected_under_extra_forbid` already passes — it pins the invariant survives.)

- [ ] **Step 3: Implement** — add the field to `Settings` in `src/sqrlly/schema/models.py`, immediately after `max_retries` (line ~439) so the two retry budgets read together. Current:

```python
class Settings(BaseModel):
    output_directory: str = "output"
    max_retries: int = 3
    default_timeout: float | None = None
```

becomes:

```python
class Settings(BaseModel):
    output_directory: str = "output"
    max_retries: int = 3
    # Transient-backend-error retry budget. DISTINCT from `max_retries`
    # (the evaluation/gate budget, which retries on a low gate score).
    # This retries the SAME backend dispatch when the backend raises a
    # non-OverloadError exception (e.g. `claude exited 1` — a transient
    # blip), up to this many times with `retry_backoff` between attempts.
    # OverloadError stays on the model-downgrade path and is not counted
    # here. 0 (default) = today's behavior: one attempt, terminal.
    backend_max_retries: int = 0
    default_timeout: float | None = None
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/schema/test_backend_retries.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/schema/models.py tests/unit/schema/test_backend_retries.py
git commit -m "feat: Settings.backend_max_retries — opt-in transient-backend retry budget"
```

---

### Task 2: Retry transient backend errors in `execute_rendered`

**Files:**
- Modify: `src/sqrlly/runtime/executor/prompt.py` (`PromptExecutor.execute_rendered` — wrap the downgrade loop in a bounded transient-retry loop; add a module-level `_backend_retry_delay` helper)
- Test: `tests/unit/runtime/test_prompt.py` (add a `TestBackendTransientRetry` class + a scripted backend double)

**Interfaces:**
- Consumes: `Settings.backend_max_retries` (Task 1), `Settings.retry_backoff` (existing).
- Produces: `execute_rendered` retries the whole send-with-downgrade attempt on a non-`OverloadError` exception up to `s.backend_max_retries` times. On the final failure (or with `backend_max_retries == 0`) it returns the SAME `ExecutionResult(success=False, error=f"Backend error: {e}")` as today. Overload exhaustion is unchanged. No signature change.

**Context:** The current `execute_rendered` body (lines ~118-147):

```python
        s = settings or self._settings
        current_model = model
        try:
            while True:
                try:
                    result = await self._backend.send_prompt(
                        rendered, current_model, workdir, timeout=timeout,
                    )
                    break
                except OverloadError:
                    next_model = downgrade_model(
                        current_model, s.model_downgrade_chain
                    )
                    if next_model is None:
                        return ExecutionResult(
                            success=False,
                            error=(
                                f"API overloaded, exhausted model chain "
                                f"(last: {current_model})"
                            ),
                        )
                    current_model = next_model
        except Exception as e:
            return ExecutionResult(success=False, error=f"Backend error: {e}")

        return ExecutionResult(
            success=True,
            output=result.output,
            structured_output=result.structured_output,
        )
```

The inner `while True` is the overload-downgrade loop. A non-overload exception escapes it and is caught by `except Exception as e` → failure. The change: re-enter the downgrade loop (from the ORIGINAL `model`, not the downgraded `current_model`) on a non-overload exception up to `backend_max_retries` times, sleeping between attempts. Overload exhaustion still `return`s immediately from inside the inner loop, so it never reaches the outer retry.

`runtime/executor/prompt.py` already imports `from pathlib import Path` and `from typing import Any`; add `import asyncio` for the backoff sleep. The backoff is computed inline (NOT imported from `compile/nodes.py` — that crosses the layer boundary the wrong way), mirroring `_get_retry_delay`'s clamp-to-last semantics so author intuition is uniform across gate and backend retries.

- [ ] **Step 1: Write the failing test** — add to `tests/unit/runtime/test_prompt.py`. First add a scripted backend double in the "In-test backend doubles" section (after `AlwaysOverloadBackend`, ~line 235):

```python
class TransientThenSucceedBackend:
    """Raises a non-OverloadError (a plain RuntimeError, like the CLI
    backend's `claude exited 1`) on the first ``fail_count`` calls, then
    succeeds. Records every call so the test can count attempts. This is
    a deterministic scripted-exit double for transient-error simulation —
    sanctioned instrumentation, not a fake of the real network behavior."""

    def __init__(self, fail_count: int = 1, response: str = "recovered"):
        self._fail_count = fail_count
        self._response = response
        self.calls: list[tuple[str, str, str, float | None]] = []

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        self.calls.append((prompt, model, workdir, timeout))
        if len(self.calls) <= self._fail_count:
            raise RuntimeError(f"claude exited 1: transient blip {len(self.calls)}")
        return ExecutionResult(output=self._response)

    async def close(self) -> None:
        pass


class AlwaysTransientErrorBackend:
    """Raises a non-OverloadError on every call. Counts calls."""

    def __init__(self):
        self.calls: list[tuple[str, str, str, float | None]] = []

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        self.calls.append((prompt, model, workdir, timeout))
        raise RuntimeError("claude exited 1: persistent failure")

    async def close(self) -> None:
        pass
```

Then add the test class at the end of the file (after `TestPreambleInjection`):

```python
# ---------------------------------------------------------------------------
# Backend transient-error retry (Settings.backend_max_retries)
# ---------------------------------------------------------------------------


class TestBackendTransientRetry:
    @pytest.mark.asyncio
    async def test_zero_retries_is_terminal_today_behavior(self, tmp_path):
        """backend_max_retries=0 (default): a transient error is terminal,
        one attempt — exactly today's behavior."""
        backend = AlwaysTransientErrorBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(backend_max_retries=0),
            workdir=str(tmp_path),
        )
        result = await executor.execute_rendered(
            "x", "sonnet", str(tmp_path), timeout=None,
        )
        assert result.success is False
        assert "Backend error" in result.error
        assert "claude exited 1" in result.error
        assert len(backend.calls) == 1  # no retry

    @pytest.mark.asyncio
    async def test_transient_error_retried_then_succeeds(self, tmp_path):
        """Two transient failures then success, with budget 3 → 3 calls,
        final success."""
        backend = TransientThenSucceedBackend(fail_count=2, response="recovered")
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(backend_max_retries=3),
            workdir=str(tmp_path),
        )
        result = await executor.execute_rendered(
            "x", "sonnet", str(tmp_path), timeout=None,
        )
        assert result.success is True
        assert result.output == "recovered"
        # 2 failed + 1 success = 3 calls.
        assert len(backend.calls) == 3
        # Each attempt re-sent the ORIGINAL model (no downgrade — transient
        # errors are not overload).
        assert [c[1] for c in backend.calls] == ["sonnet", "sonnet", "sonnet"]

    @pytest.mark.asyncio
    async def test_budget_exhausted_returns_failure(self, tmp_path):
        """Persistent transient error with budget 2 → 1 initial + 2 retries
        = 3 calls, then terminal failure."""
        backend = AlwaysTransientErrorBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(backend_max_retries=2),
            workdir=str(tmp_path),
        )
        result = await executor.execute_rendered(
            "x", "sonnet", str(tmp_path), timeout=None,
        )
        assert result.success is False
        assert "Backend error" in result.error
        assert len(backend.calls) == 3  # 1 + 2 retries

    @pytest.mark.asyncio
    async def test_overload_uses_downgrade_not_backend_retry(self, tmp_path):
        """Overload must take the model-downgrade path, NOT the backend-retry
        path — it is not double-counted. With backend_max_retries=5 but a
        2-step overload, the executor walks opus→sonnet (2 calls) and
        succeeds; the backend-retry budget is never consumed."""
        backend = OverloadThenSucceedBackend(fail_count=1, response="recovered")
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(
                backend_max_retries=5,
                model_downgrade_chain=["opus", "sonnet", "haiku"],
            ),
            workdir=str(tmp_path),
        )
        result = await executor.execute_rendered(
            "x", "opus", str(tmp_path), timeout=None,
        )
        assert result.success is True
        assert result.output == "recovered"
        # Exactly the downgrade walk — opus then sonnet. No extra
        # backend-retry attempts (which would re-send opus a 3rd time).
        assert [c[1] for c in backend.calls] == ["opus", "sonnet"]

    @pytest.mark.asyncio
    async def test_overload_exhaustion_unchanged_with_backend_budget(self, tmp_path):
        """Overload at every step still exhausts the model chain and returns
        the overload failure — the backend-retry budget does NOT re-attempt
        an exhausted-chain overload."""
        backend = AlwaysOverloadBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(
                backend_max_retries=4,
                model_downgrade_chain=["opus", "sonnet", "haiku"],
            ),
            workdir=str(tmp_path),
        )
        result = await executor.execute_rendered(
            "x", "opus", str(tmp_path), timeout=None,
        )
        assert result.success is False
        assert "exhausted model chain" in result.error
        # Three downgrade calls, no backend-retry re-walk.
        assert [c[1] for c in backend.calls] == ["opus", "sonnet", "haiku"]

    @pytest.mark.asyncio
    async def test_backend_retry_distinct_from_gate_max_retries(self, tmp_path):
        """The backend-retry budget is read from backend_max_retries, NOT
        max_retries. A high gate max_retries with backend_max_retries=0 does
        NOT retry a transient backend error."""
        backend = AlwaysTransientErrorBackend()
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(max_retries=9, backend_max_retries=0),
            workdir=str(tmp_path),
        )
        result = await executor.execute_rendered(
            "x", "sonnet", str(tmp_path), timeout=None,
        )
        assert result.success is False
        assert len(backend.calls) == 1  # gate budget irrelevant here
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/runtime/test_prompt.py -q -k "TestBackendTransientRetry"`
Expected: FAIL —
  - `test_transient_error_retried_then_succeeds` and `test_budget_exhausted_returns_failure` fail: today the FIRST transient `RuntimeError` is caught by `except Exception` and returned as failure with exactly 1 call, so `len(backend.calls) == 1` (not 3) and `result.success is False` (not True for the recover case).
  - `test_zero_retries_is_terminal_today_behavior`, `test_overload_uses_downgrade_not_backend_retry`, `test_overload_exhaustion_unchanged_with_backend_budget`, and `test_backend_retry_distinct_from_gate_max_retries` already PASS (they pin behavior the change must preserve). Report which of the two new-behavior cases failed.

- [ ] **Step 3: Implement** —

(3a) In `src/sqrlly/runtime/executor/prompt.py`, add `import asyncio` to the top imports. Current:

```python
from __future__ import annotations

from pathlib import Path
from typing import Any

from jinja2 import Template
```

becomes:

```python
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from jinja2 import Template
```

(3b) Add a module-level backoff helper after `downgrade_model` (after line ~42, before `render_template`):

```python
def _backend_retry_delay(attempt: int, backoff: list[float]) -> float:
    """Seconds to sleep before backend-retry ``attempt`` (1-indexed).

    Clamps to the last value past the list length; 0.0 when ``backoff``
    is empty. Mirrors ``compile/nodes._get_retry_delay`` so gate and
    backend retries share the same author intuition — duplicated rather
    than imported because ``runtime/`` must not import ``compile/`` (layer
    rule), and the function is a two-line clamp.
    """
    if not backoff:
        return 0.0
    idx = min(attempt - 1, len(backoff) - 1)
    return backoff[idx]
```

(3c) Replace the body of `execute_rendered` (the block from `s = settings or self._settings` through the final `return ExecutionResult(success=True, ...)`) with the bounded transient-retry wrapper. Current:

```python
        s = settings or self._settings
        current_model = model
        try:
            while True:
                try:
                    result = await self._backend.send_prompt(
                        rendered, current_model, workdir, timeout=timeout,
                    )
                    break
                except OverloadError:
                    next_model = downgrade_model(
                        current_model, s.model_downgrade_chain
                    )
                    if next_model is None:
                        return ExecutionResult(
                            success=False,
                            error=(
                                f"API overloaded, exhausted model chain "
                                f"(last: {current_model})"
                            ),
                        )
                    current_model = next_model
        except Exception as e:
            return ExecutionResult(success=False, error=f"Backend error: {e}")

        return ExecutionResult(
            success=True,
            output=result.output,
            structured_output=result.structured_output,
        )
```

becomes:

```python
        s = settings or self._settings
        # Bounded transient-retry layer wrapping the overload-downgrade loop.
        # A non-OverloadError backend exception (e.g. the CLI backend's
        # `claude exited 1`) re-enters the downgrade loop from the ORIGINAL
        # model up to `backend_max_retries` times. OverloadError stays inside
        # the inner loop (model downgrade) and never consumes a backend
        # retry — overload exhaustion returns from inside the inner loop, so
        # it never reaches `except Exception` here. attempt 0 = first try.
        attempt = 0
        while True:
            current_model = model
            try:
                while True:
                    try:
                        result = await self._backend.send_prompt(
                            rendered, current_model, workdir, timeout=timeout,
                        )
                        break
                    except OverloadError:
                        next_model = downgrade_model(
                            current_model, s.model_downgrade_chain
                        )
                        if next_model is None:
                            return ExecutionResult(
                                success=False,
                                error=(
                                    f"API overloaded, exhausted model chain "
                                    f"(last: {current_model})"
                                ),
                            )
                        current_model = next_model
                break
            except Exception as e:
                if attempt >= s.backend_max_retries:
                    return ExecutionResult(
                        success=False, error=f"Backend error: {e}"
                    )
                attempt += 1
                delay = _backend_retry_delay(attempt, s.retry_backoff)
                if delay > 0:
                    await asyncio.sleep(delay)

        return ExecutionResult(
            success=True,
            output=result.output,
            structured_output=result.structured_output,
        )
```

Note: the inner `break` exits the downgrade loop on a successful `send_prompt`; the outer `break` (right after the inner loop) exits the retry loop on success. On a non-overload exception, control reaches `except Exception` — if the budget is exhausted it returns failure (today's behavior at `attempt=0`, `backend_max_retries=0`), else it bumps `attempt`, sleeps, and the outer `while True` re-enters with a fresh `current_model = model`.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/runtime/test_prompt.py -q`
Expected: PASS — the full prompt suite, including the new `TestBackendTransientRetry` (6 cases) and the pre-existing `TestExecuteRendered` / `TestOverloadDowngrade` (which re-confirm `backend_max_retries=0` default preserves today's overload + error-return behavior; note `test_backend_error_returns_failure` uses default `Settings()` → `backend_max_retries=0` → still 1 call, terminal).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/runtime/executor/prompt.py tests/unit/runtime/test_prompt.py
git commit -m "feat: retry transient (non-overload) backend errors via backend_max_retries"
```

---

### Task 3: Load-bearing e2e — backend retry through the CLI backend (real subprocess)

**Files:**
- Test: `tests/unit/runtime/test_cli_backend.py` (extend) OR a new e2e — see Step 1. This plan extends `tests/unit/runtime/test_cli_backend.py` with a DispatchExecutor-level test driving a real `/bin/sh` "claude" stub that exits non-zero a fixed number of times then succeeds, proving the retry layer activates through the real dispatch path.

**Interfaces:**
- Consumes: Tasks 1–2.

**Context:** Task 2's unit tests use a scripted `PromptBackend` double. This task proves the same behavior through the REAL `CLIBackend` (real `asyncio.create_subprocess_exec`) wired into `DispatchExecutor`, so the `RuntimeError("claude exited 1: ...")` raised by `cli.py` is what the retry layer catches. The "claude" CLI is a `/bin/sh` stub that fails on the first K invocations (tracked by a counter file) then prints output and exits 0. Model on `tests/unit/runtime/test_cli_backend.py` (read it first for the CLIBackend construction + the `argv_prefix` stub pattern).

- [ ] **Step 1: Read the existing CLI-backend test for the stub pattern, then write the failing test.**

Read `tests/unit/runtime/test_cli_backend.py` to confirm how `CLIBackend(argv_prefix=...)` is constructed against a `/bin/sh` stub and how `send_prompt` is invoked. Then add to that file a `TestCLIBackendRetryViaDispatch` class:

```python
# tests/unit/runtime/test_cli_backend.py — add near the end


class TestCLIBackendRetryViaDispatch:
    """End-to-end: a real CLIBackend (real subprocess) whose `claude` stub
    exits non-zero the first K times then succeeds is retried by the
    execute_rendered backend-retry layer when backend_max_retries is set.
    No mock — a /bin/sh stub stands in for `claude`."""

    def _stub_claude(self, tmp_path, fail_times: int):
        """Write an executable /bin/sh stub that increments a counter file
        and exits 1 until it has been called `fail_times` times, then prints
        a fixed line and exits 0. Returns the stub path."""
        import os
        import stat

        counter = tmp_path / "claude_calls.txt"
        stub = tmp_path / "claude_stub.sh"
        # Drain stdin (the prompt) so the parent's write side closes cleanly.
        stub.write_text(
            "#!/bin/sh\n"
            "cat >/dev/null\n"
            f'COUNTER="{counter}"\n'
            'n=0; [ -f "$COUNTER" ] && n=$(cat "$COUNTER")\n'
            'n=$((n+1)); echo "$n" > "$COUNTER"\n'
            f'if [ "$n" -le "{fail_times}" ]; then\n'
            '  echo "claude exited transiently" >&2\n'
            '  exit 1\n'
            'fi\n'
            'echo "stub-output"\n'
        )
        st = os.stat(stub)
        os.chmod(stub, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return stub, counter

    @pytest.mark.asyncio
    async def test_real_cli_backend_retried_then_succeeds(self, tmp_path):
        from sqrlly.runtime.executor.backends.cli import CLIBackend
        from sqrlly.runtime.executor.prompt import PromptExecutor
        from sqrlly.schema.models import Settings

        stub, counter = self._stub_claude(tmp_path, fail_times=2)
        backend = CLIBackend(argv_prefix=(str(stub),))
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(backend_max_retries=3),
            workdir=str(tmp_path),
        )
        result = await executor.execute_rendered(
            "prompt body", "sonnet", str(tmp_path), timeout=30.0,
        )
        assert result.success is True
        assert result.output == "stub-output"
        # 2 transient exits + 1 success = 3 real subprocess invocations.
        assert int(counter.read_text()) == 3

    @pytest.mark.asyncio
    async def test_real_cli_backend_zero_budget_terminal(self, tmp_path):
        from sqrlly.runtime.executor.backends.cli import CLIBackend
        from sqrlly.runtime.executor.prompt import PromptExecutor
        from sqrlly.schema.models import Settings

        stub, counter = self._stub_claude(tmp_path, fail_times=2)
        backend = CLIBackend(argv_prefix=(str(stub),))
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(backend_max_retries=0),
            workdir=str(tmp_path),
        )
        result = await executor.execute_rendered(
            "prompt body", "sonnet", str(tmp_path), timeout=30.0,
        )
        assert result.success is False
        assert "Backend error" in result.error
        # One invocation, no retry.
        assert int(counter.read_text()) == 1
```

- [ ] **Step 2: Run to verify it fails / passes**

Run: `uv run pytest tests/unit/runtime/test_cli_backend.py -q -k "TestCLIBackendRetryViaDispatch"`
Expected: With Tasks 1–2 implemented, both PASS. If you run this task BEFORE Task 2 is merged, `test_real_cli_backend_retried_then_succeeds` FAILS (`result.success is False`, counter == 1 because the first non-zero exit is terminal). The zero-budget case passes regardless. Report observed behavior.

- [ ] **Step 3: Implement** — no source change; Tasks 1–2 implement the behavior. This is the real-subprocess integration proof.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/runtime/test_cli_backend.py -q`
Expected: PASS (full CLI-backend suite + the 2 new cases).

- [ ] **Step 5: Commit**

```bash
git add tests/unit/runtime/test_cli_backend.py
git commit -m "test: real CLIBackend transient-exit retried through execute_rendered"
```

---

### Task 4: `compute_skip_set` — a fan-out parent with failed children is DIRTY

**Files:**
- Modify: `src/sqrlly/compile/resume.py` (`compute_skip_set` — seed fan-out parents of failed children into the dirty frontier)
- Test: `tests/unit/compile/test_resume_skip_set.py` (extend)

**Interfaces:**
- Consumes: nothing new (`prior_failed` already carries child ids like `fan::beta`).
- Produces: `compute_skip_set` seeds `<pid>` into `dirty` for every `prior_failed` id of the form `<pid>::<...>` where `<pid>` is a fan-out parent in `config.nodes`. The dirty closure then dirties the parent and (via the existing `_final_<pid>_<f>` synthetic-dependent wiring) its finals; completed children — which are NOT in `config.nodes` and NOT reachable from the parent in the BFS — stay in `prior_completed - dirty` (skip).

**Context:** `compute_skip_set` (in `compile/resume.py`) computes `ids = {n.id for n in config.nodes}`, builds `dependents` / `route_adj` / worktree groups, then runs a dirty BFS seeded at `prior_failed | rerun_targets`. A failed child id `fan::beta` is not in `ids`, so it seeds the frontier but reaches no neighbors → its parent is never dirtied. We add a pre-pass that maps each failed child id back to its parent id and seeds the parent. The set of fan-out parent ids is `{n.id for n in config.nodes if n.fan_out}`.

- [ ] **Step 1: Write the failing test** — add to `tests/unit/compile/test_resume_skip_set.py` (the `_fan_graph` helper at the bottom already builds `up -> fan(fan_out, final=agg)`):

```python
def test_fan_out_parent_dirty_when_child_failed():
    """A failed fan-out child (id 'fan::beta') dirties its PARENT 'fan' so it
    re-fans on resume — even though 'fan::beta' is not a config.nodes id.
    Completed siblings stay skippable; the parent and its final leave skip."""
    g = _fan_graph()
    prior_completed = {"up", "fan", "fan::alpha", "fan::gamma", "_final_fan_agg"}
    prior_failed = {"fan::beta"}
    skip = compute_skip_set(g, prior_completed, prior_failed, set())
    # Parent re-fans (dirty), so it must NOT be frozen.
    assert "fan" not in skip
    # Its final aggregation must re-run too (dirty via the parent).
    assert "_final_fan_agg" not in skip
    # Completed siblings stay frozen — NOT re-billed.
    assert "fan::alpha" in skip
    assert "fan::gamma" in skip
    # 'up' is unaffected (clean, upstream of the dirty parent... wait: fan
    # depends_on up, not the reverse — up is NOT downstream of fan, stays
    # skippable).
    assert "up" in skip


def test_failed_child_of_nonexistent_parent_is_ignored():
    """A '::'-shaped failed id whose prefix is not a real fan-out parent
    does not crash and dirties nothing extra (defensive: stale checkpoint)."""
    g = _fan_graph()
    skip = compute_skip_set(
        g, {"up", "fan"}, {"ghost::x"}, set(),
    )
    # 'ghost' is not a node; nothing it could dirty. up + fan stay skippable.
    assert skip == {"up", "fan"}


def test_clean_fan_out_resume_freezes_parent_and_children():
    """No failures: the parent and completed children all stay frozen."""
    g = _fan_graph()
    prior_completed = {"up", "fan", "fan::alpha", "fan::beta", "_final_fan_agg"}
    skip = compute_skip_set(g, prior_completed, set(), set())
    assert skip == prior_completed
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/compile/test_resume_skip_set.py -q -k "fan_out_parent_dirty or failed_child_of_nonexistent or clean_fan_out_resume"`
Expected: FAIL — `test_fan_out_parent_dirty_when_child_failed` fails: today `"fan" in skip` (the parent is NOT dirtied by the orphan `fan::beta` seed), so `assert "fan" not in skip` and `assert "_final_fan_agg" not in skip` both fail. `test_failed_child_of_nonexistent_parent_is_ignored` and `test_clean_fan_out_resume_freezes_parent_and_children` already PASS (the pre-pass is a no-op for them).

- [ ] **Step 3: Implement** — in `src/sqrlly/compile/resume.py`, add the parent-seeding pre-pass to `compute_skip_set`. Current tail (lines ~63-84):

```python
    route_adj = {n.id: _route_targets(n) for n in config.nodes}
    groups: dict[str, list[str]] = {}
    group_of: dict[str, str] = {}
    for n in config.nodes:
        kind, group = n.effective_worktree(config.settings)
        if kind == "group":
            groups.setdefault(group, []).append(n.id)
            group_of[n.id] = group

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

becomes:

```python
    route_adj = {n.id: _route_targets(n) for n in config.nodes}
    groups: dict[str, list[str]] = {}
    group_of: dict[str, str] = {}
    for n in config.nodes:
        kind, group = n.effective_worktree(config.settings)
        if kind == "group":
            groups.setdefault(group, []).append(n.id)
            group_of[n.id] = group

    # A failed fan-out child has a synthetic id `<parent>::<item>` that is
    # NOT in config.nodes, so the BFS below can't reach its parent. Seed the
    # parent into the dirty set so it re-fans on resume; the dirty closure
    # then dirties its `_final_<parent>_<f>` aggregation (wired as a synthetic
    # dependent above). Completed siblings are not config.nodes ids and aren't
    # reachable from the parent, so they stay in prior_completed - dirty
    # (frozen — not re-billed). The per-child re-run gate in
    # compile/dynamic.py freezes a child on `child_id in skip`.
    fan_out_parents = {n.id for n in config.nodes if n.fan_out}
    failed_child_parents = {
        fid.split("::", 1)[0]
        for fid in prior_failed
        if "::" in fid and fid.split("::", 1)[0] in fan_out_parents
    }

    dirty: set[str] = set(prior_failed) | set(rerun_targets) | failed_child_parents
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

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/compile/test_resume_skip_set.py -q`
Expected: PASS — the full skip-set suite (the new 3 cases plus all pre-existing rows, which the pre-pass leaves untouched: none of them have a `::`-shaped failed id whose prefix is a fan-out parent).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/compile/resume.py tests/unit/compile/test_resume_skip_set.py
git commit -m "fix: dirty a fan-out parent on resume when one of its children failed"
```

---

### Task 5: `dynamic.py` child-skip gate — freeze a completed child even when the parent re-fans

**Files:**
- Modify: `src/sqrlly/compile/dynamic.py` (`_make_fan_out_node` — the per-child `_resume_skip` gate)
- Test: `tests/unit/compile/test_fan_out_resume_skip.py` (rewrite the contract-inverting case + add coverage)

**Interfaces:**
- Consumes: the `_resume_skip` frozen snapshot (Task 4 keeps completed children in it; the re-fanned parent is NOT in it).
- Produces: the child gate becomes `if skip and child_id in skip: return {}` — a child that completed cleanly last run (its id in the frozen skip snapshot) is frozen regardless of whether the parent re-fanned; the formerly-failed child (never in `prior_completed`, so never in skip) runs.

**Context:** `_make_fan_out_node`'s `node_fn` body currently gates (lines ~103-110):

```python
        if child_id in state.get("failed_nodes", set()):
            return {}

        # Resume skip: freeze this child only when its PARENT is also frozen —
        # a dirty parent re-derives the manifest, so child ids aren't stable.
        skip = state.get("_resume_skip")
        if skip and parent_node.id in skip and child_id in skip:
            return {}
```

The `parent_node.id in skip` condition is what makes a re-fanning (dirty) parent re-run EVERY child. We drop it: gate on `child_id in skip` alone. This is stable-id-safe — only ids that genuinely completed last run are in the frozen snapshot, so a manifest that drifts on re-fan yields new ids absent from skip (they run) while a re-emitted completed id is frozen (not re-billed). The `failed_nodes` guard above is unchanged: on a fresh resume run `failed_nodes` is reseeded empty, so it does not block the formerly-failed child.

- [ ] **Step 1: Rewrite the failing test** — in `tests/unit/compile/test_fan_out_resume_skip.py`, the existing `test_child_runs_when_parent_not_in_skip` pins the OLD contract (child runs when parent dirty). Its contract inverts. Replace that test and the module docstring; keep `test_child_frozen_when_parent_and_child_in_skip` (still valid). New file body — replace the docstring and the two test functions:

```python
"""Fan-out child resume skip-guard: a child is frozen when its OWN id is in
the frozen skip snapshot — regardless of whether the parent re-fanned. Only
ids that completed cleanly last run are in the snapshot, so a completed child
is never re-billed even when a sibling failed and the parent re-fans; the
formerly-failed child (never in prior_completed → never in skip) runs."""
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
async def test_child_frozen_when_child_in_skip():
    """Child id in the frozen snapshot → frozen (not re-executed)."""
    ex = MockExecutor()
    nf = _make(_fan_parent(), ex)
    state = make_initial_state(
        _fan_out_item={"id": "item1"},
        _resume_skip={"fan", "fan::item1"},
    )
    assert await nf(state) == {}
    assert ex.execution_order == []


@pytest.mark.asyncio
async def test_completed_child_frozen_even_when_parent_refans():
    """Parent dirty (re-fanned, NOT in skip), but this child completed last
    run and IS in skip → it must stay frozen (no re-bill of a clean sibling)."""
    ex = MockExecutor()
    nf = _make(_fan_parent(), ex)
    state = make_initial_state(
        _fan_out_item={"id": "alpha"},
        _resume_skip={"fan::alpha", "fan::gamma"},  # parent 'fan' NOT in skip
    )
    assert await nf(state) == {}
    assert ex.execution_order == []


@pytest.mark.asyncio
async def test_failed_child_runs_when_not_in_skip():
    """The formerly-failed child id is NOT in the snapshot (never completed),
    so it runs on resume even though its siblings are frozen."""
    ex = MockExecutor()
    nf = _make(_fan_parent(), ex)
    state = make_initial_state(
        _fan_out_item={"id": "beta"},
        _resume_skip={"fan::alpha", "fan::gamma"},  # beta absent → runs
    )
    result = await nf(state)
    assert result != {}  # proceeded past the skip-guard


@pytest.mark.asyncio
async def test_no_skip_set_runs_normally():
    """Absent _resume_skip (fresh run) → child runs."""
    ex = MockExecutor()
    nf = _make(_fan_parent(), ex)
    state = make_initial_state(_fan_out_item={"id": "x"})
    result = await nf(state)
    assert result != {}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/compile/test_fan_out_resume_skip.py -q`
Expected: FAIL — `test_completed_child_frozen_even_when_parent_refans` fails: today, with `parent_node.id` ("fan") NOT in skip, the `parent_node.id in skip and child_id in skip` gate is False → the child PROCEEDS (`ex.execution_order != []` / `await nf(state) != {}`), so `assert await nf(state) == {}` fails. `test_child_frozen_when_child_in_skip`, `test_failed_child_runs_when_not_in_skip`, `test_no_skip_set_runs_normally` already pass under the old gate.

- [ ] **Step 3: Implement** — in `src/sqrlly/compile/dynamic.py`, change the child-skip gate. Current (lines ~103-110):

```python
        if child_id in state.get("failed_nodes", set()):
            return {}

        # Resume skip: freeze this child only when its PARENT is also frozen —
        # a dirty parent re-derives the manifest, so child ids aren't stable.
        skip = state.get("_resume_skip")
        if skip and parent_node.id in skip and child_id in skip:
            return {}
```

becomes:

```python
        if child_id in state.get("failed_nodes", set()):
            return {}

        # Resume skip: freeze a child purely on its OWN id being in the frozen
        # snapshot. Only ids that completed cleanly last run are in the
        # snapshot, so a completed child stays frozen (not re-billed) even when
        # a sibling failed and the parent re-fans. The formerly-failed child is
        # never in prior_completed → never in skip → it runs. Stable-id-safe: a
        # re-fan that drifts the manifest yields new ids absent from skip.
        skip = state.get("_resume_skip")
        if skip and child_id in skip:
            return {}
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/compile/test_fan_out_resume_skip.py -q`
Expected: PASS (4 cases).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/compile/dynamic.py tests/unit/compile/test_fan_out_resume_skip.py
git commit -m "fix: freeze a completed fan-out child on resume even when the parent re-fans"
```

---

### Task 6: Load-bearing e2e — failed fan-out child re-runs on resume, siblings frozen

**Files:**
- Test: `tests/e2e/test_resume_fan_out.py` (extend — add a `TestResumeFailedChild` class + a flippable-failure fan-out fixture)

**Interfaces:**
- Consumes: Tasks 4–5 (skip-set parent dirtying + child-skip gate) and the existing CLI resume wiring.

**Context:** Model on the existing `_run_phase` helper in `tests/e2e/test_resume_fan_out.py` (real `AsyncSqliteSaver`, `DispatchExecutor` — NO foreman, so children run in the shared workdir, no worktrees). The `_WORKER_SRC` worker already writes a per-id run-counter file (`runs_<qid>.txt`) and fails iff `fail.txt` holds its qid. We add a fan-out fixture where the parent emits a 3-item manifest (`alpha`/`beta`/`gamma`), the template runs the SAME worker keyed by the per-item `{{id}}`, and the child id passed to the worker is the manifest item id. Phase 1 fails `beta` (via `fail.txt` holding `beta`); phase 2 clears the marker and resumes. The per-child run-counter files (`runs_alpha.txt`, etc.) pin that ONLY `beta` re-runs.

Note the worker's `qid` is `sys.argv[1]`; the template binds it via `{{id}}`. The child node ids are `cfan::alpha` / `cfan::beta` / `cfan::gamma`. `fail.txt` holds the bare item id (`beta`), and the worker reads `argv[1]` (the item id), so the marker matches the worker's qid — independent of the `cfan::` child-id prefix.

- [ ] **Step 1: Write the failing test** — add to `tests/e2e/test_resume_fan_out.py`. Append a fixture builder and a test class:

```python
def _build_failable_fanout(workdir: Path) -> Graph:
    """up -> cfan (fans over alpha/beta/gamma) ; each child runs worker.py
    keyed by the per-item id, so a per-child run-counter (runs_<id>.txt) and
    the fail.txt marker (holding a bare item id) control exactly one child's
    failure. No final_nodes — the assertion is purely on per-child re-run."""
    worker = _worker_path(workdir)
    manifest = json.dumps(
        {"items": [{"id": "alpha"}, {"id": "beta"}, {"id": "gamma"}]}
    )
    return Graph(
        name="resume-failable-fanout",
        version="0.1.0",
        nodes=[
            {
                "id": "up", "name": "up",
                "execute": {
                    "url": _PYTHON,
                    "params": {"args": [str(worker), "up"]},
                },
            },
            {
                "id": "cfan", "name": "cfan", "depends_on": ["up"],
                "execute": {
                    "url": _ECHO,
                    "params": {"args": ["-n", manifest]},
                },
                "fan_out": {
                    "template": {
                        "execute": {
                            "url": _PYTHON,
                            "params": {"args": [str(worker), "{{id}}"]},
                        },
                    },
                },
            },
        ],
    )


class TestResumeFailedChild:
    @pytest.mark.asyncio
    async def test_only_failed_child_reruns_on_resume(self, tmp_path):
        """Fan-out over alpha/beta/gamma; beta fails in phase 1. On resume
        (bare --resume, no resume-from) ONLY beta re-runs; alpha and gamma
        are frozen — their run counters stay at 1. The parent re-fans (dirty
        via the failed child), beta succeeds, the run completes clean."""
        config = _build_failable_fanout(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "failed-child-resume"

        # Phase 1: beta fails.
        (tmp_path / "fail.txt").write_text("beta")
        result_1 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=False,
        )
        assert "cfan" in result_1["completed_nodes"]
        assert "cfan::alpha" in result_1["completed_nodes"]
        assert "cfan::gamma" in result_1["completed_nodes"]
        assert "cfan::beta" in result_1["failed_nodes"]
        assert _read_runs(tmp_path, "alpha") == 1
        assert _read_runs(tmp_path, "beta") == 1
        assert _read_runs(tmp_path, "gamma") == 1

        # Phase 2: clear the marker, bare --resume (no resume-from).
        (tmp_path / "fail.txt").unlink()
        result_2 = await _run_phase(
            tmp_path, config, db_path, thread_id, resume=True,
        )
        # The formerly-failed child now succeeds.
        assert "cfan::beta" in result_2["completed_nodes"]
        assert result_2["failed_nodes"] == set()

        # ONLY beta re-ran. alpha and gamma frozen — counters stay at 1.
        assert _read_runs(tmp_path, "alpha") == 1, "completed sibling re-billed"
        assert _read_runs(tmp_path, "gamma") == 1, "completed sibling re-billed"
        # beta ran once per phase = 2.
        assert _read_runs(tmp_path, "beta") == 2

        # 'up' completed cleanly and is upstream of the dirty parent (not
        # downstream), so it is frozen — runs once total.
        assert _read_runs(tmp_path, "up") == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/e2e/test_resume_fan_out.py -q -k "TestResumeFailedChild"`
Expected: BEFORE Tasks 4–5, FAIL at phase 2: `compute_skip_set` leaves `cfan` in skip (the orphan `cfan::beta` seed dirties nothing), so the parent is frozen, never re-fans, and `cfan::beta` is unreachable — `assert "cfan::beta" in result_2["completed_nodes"]` fails (`_read_runs(beta) == 1`, not 2). Implementing in order, by Task 6 Tasks 4–5 are done, so it should PASS. If you find phase-2 re-runs alpha/gamma too (counters == 2), Task 5's gate change is missing or wrong — debug there.

- [ ] **Step 3: Implement** — no source change; Tasks 4–5 implement the behavior. This is the integration proof tying skip-set dirtying to the per-child gate.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/e2e/test_resume_fan_out.py -q`
Expected: PASS — the full resume-fan-out suite, including the new `TestResumeFailedChild` and the pre-existing `TestResumeFromCheckpoint` / `TestResumeDirtyGuards` (Task 5's gate change must not regress `test_fan_out_final_reruns_on_resume_from_ancestor`, which asserts the agg final re-runs on `resume_from=up` — that test's fan child `fan::x` is not asserted on, and the final is dirtied by Task 4's existing `_final` wiring, so it remains green).

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/test_resume_fan_out.py
git commit -m "test: e2e proving only the failed fan-out child re-runs on resume"
```

---

### Task 7: Full-suite regression + architecture gate

**Files:** none (verification only).

- [ ] **Step 1: Architecture layer rules** — Part A added no `compile`/`langgraph` import to `runtime/` (only stdlib `asyncio`); Part B added no `cli`/`langgraph` import to `compile/`. Confirm:

Run: `uv run pytest tests/architecture/test_layers.py -q`
Expected: PASS — `runtime/executor/prompt.py` imports only `asyncio` (stdlib) + existing modules; `compile/resume.py` and `compile/dynamic.py` import only `sqrlly.schema` / `sqrlly.runtime.state` (already allowed).

- [ ] **Step 2: Targeted suites**

Run:
```bash
uv run pytest tests/unit/schema/test_backend_retries.py tests/unit/runtime/test_prompt.py tests/unit/runtime/test_cli_backend.py tests/unit/compile/test_resume_skip_set.py tests/unit/compile/test_fan_out_resume_skip.py tests/e2e/test_resume_fan_out.py -q
```
Expected: PASS (all of Tasks 1–6).

- [ ] **Step 3: Full core suite**

Run: `uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -q`
Expected: PASS. The two behavior-changing edits are guarded by opt-in (`backend_max_retries: 0` default = unchanged) and a contract that only fires on `--resume` with a failed child, so existing tests are unaffected except the intentionally-rewritten `test_fan_out_resume_skip.py` (Task 5).

- [ ] **Step 4: No commit** (verification task — no file changes). If a pre-existing test fails, STOP and diagnose; do not paper over.

---

### Task 8: Docs + CHANGELOG + TODO

**Files:**
- Modify: `SCHEMA.md`, `CLAUDE.md`, `SKILLS.md`, `CHANGELOG.md`, `TODO.md`

**Interfaces:** none.

- [ ] **Step 1: SCHEMA.md** — in the Settings "Retries, timeout, preamble" table (the rows at lines ~37-41), add a `backend_max_retries` row directly under `max_retries`:

```markdown
| `backend_max_retries` | `int` | `0` | Retries of the SAME backend dispatch on a non-overload backend error (e.g. `claude exited 1` — a transient blip), with `retry_backoff` between attempts. Distinct from `max_retries` (the gate/evaluation budget). `0` = one attempt, terminal. Overload stays on the `model_downgrade_chain` path and is not counted here. |
```

- [ ] **Step 2: CLAUDE.md** — in "Known limitations", under the retry/backend notes, add (hard-wrapped to match the file):

```markdown
- **Backend transient-error retry is opt-in** — `settings.backend_max_retries`
  (default `0`) retries the SAME backend dispatch on a non-`OverloadError`
  exception (a `claude exited 1` blip) up to N times with `retry_backoff`
  between attempts, wrapped around the `OverloadError`→downgrade loop in
  `runtime/executor/prompt.py::execute_rendered`. Distinct from the
  gate/evaluation `max_retries`; overload still flows through the
  model-downgrade chain (not double-counted). `0` = terminal on first
  backend error (the historical behavior).
- **`--resume` re-runs only failed fan-out children** — a fan-out parent
  with a failed child in the prior checkpoint is dirtied in
  `compile/resume.py::compute_skip_set` (the failed child's `<parent>::<item>`
  id is mapped back to its parent and seeded into the dirty frontier), so the
  parent re-fans; the per-child gate in `compile/dynamic.py::_make_fan_out_node`
  freezes a child on `child_id in _resume_skip` alone, so completed siblings
  are NOT re-billed and only the formerly-failed child (never in the frozen
  snapshot) re-runs. Stable-id-safe: a re-fan that drifts the manifest yields
  new ids absent from the snapshot.
```

- [ ] **Step 3: SKILLS.md** — near the resume / fan-out authoring guidance, add (hard-wrapped to match the file):

```markdown
Set `settings.backend_max_retries: N` to absorb transient backend blips (a
`claude exited 1` that isn't an overload) by retrying the same node dispatch N
times with `retry_backoff` between attempts — separate from the gate
`max_retries`. On `--resume`, a fan-out whose child failed re-runs ONLY that
child: the parent re-fans and completed siblings stay frozen (no re-bill), so a
large parallel build that loses one branch resumes cheaply.
```

- [ ] **Step 4: CHANGELOG.md** — add a new top section above `## [0.7.5]`. Use a `## [Unreleased]` block (there is none currently; the 0.7.5 section is already stamped):

```markdown
## [Unreleased]

### Added

- `settings.backend_max_retries` (default `0`) — opt-in retry of the SAME backend dispatch on a non-overload backend error (a transient `claude exited 1` blip), with `retry_backoff` between attempts. Distinct from the gate/evaluation `max_retries`; overload still uses the `model_downgrade_chain` path and is not double-counted.

### Fixed

- `--resume` now re-runs ONLY the failed leaves of a fan-out: a parent with a failed child in the prior checkpoint is dirtied (so it re-fans), completed siblings stay frozen (no re-bill), and only the formerly-failed child re-runs. Previously a fan-out parent was always skipped on resume (it had emitted the manifest), leaving the failed child unreachable.
```

- [ ] **Step 5: TODO.md** — locate the `### B9` (or `#9`) heading under "Builder-required functionality gaps" (search `B9` / `transient` / `backend.*retry`). Rewrite it to the SHIPPED style matching the file's `### ✅ B8 … — SHIPPED …` convention: `### ✅ B9 — Transient-backend-error resilience for fan-out builds — SHIPPED 0.7.x`, body trimmed to current state (names `settings.backend_max_retries`, the `execute_rendered` wrap, the `compute_skip_set` parent-dirtying, and the `dynamic.py` `child_id in skip` gate). If no B9 entry exists, add the shipped entry in the "Builder-required functionality gaps" section.

- [ ] **Step 6: Sanity + commit**

```bash
uv run sqrlly validate examples/jokes/workflow.yaml   # still Valid
git add SCHEMA.md CLAUDE.md SKILLS.md CHANGELOG.md TODO.md
git commit -m "docs: document backend_max_retries + failed-child resume; mark B9 shipped"
```

---

## Final verification (after all tasks)

```bash
uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -q   # full core suite green
uv run pytest tests/unit/runtime/test_prompt.py tests/unit/runtime/test_cli_backend.py -q   # Part A
uv run pytest tests/unit/compile/test_resume_skip_set.py tests/unit/compile/test_fan_out_resume_skip.py tests/e2e/test_resume_fan_out.py -q  # Part B
uv run pytest tests/architecture/test_layers.py -q              # layer rules intact
```
Expected: full suite passes; `backend_max_retries` retries a transient backend error and `0` is terminal (today's behavior), overload still downgrades without double-counting; on `--resume` only the failed fan-out child re-runs while completed siblings stay frozen.
```
