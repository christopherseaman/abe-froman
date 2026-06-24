# `--entry <node>` cold-start + CLI-backend process-group kill (#5 / N2) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. TRANSCRIBE the code verbatim — every block is complete and the line context was traced against the live tree on branch `feat/entry-and-killpg` (HEAD `b8b52aa`, after #9 merged at 0.7.6). Do NOT trust line numbers blindly; they were traced but a prior task may shift them — match on the surrounding code, not the number.

**Goal:** Two INDEPENDENT items, no shared code.

(A) **Item #5 (bug) — CLI backend kills only the direct child on timeout.** `runtime/executor/backends/cli.py` spawns `claude -p` with no new session/process group and, on `asyncio.TimeoutError`, calls `proc.kill()` — which signals only the direct child. A `claude` that spawned descendants (MCP servers, test runners, headless Chromium) LEAKS them. Fix: spawn the CLI child with `start_new_session=True` (own process group, child is the group leader) and, on timeout/cancel, SIGTERM→SIGKILL the whole process GROUP via `os.killpg(os.getpgid(proc.pid), ...)`. Preserve the non-timeout path exactly (including stdin pipe + overload mapping).

(B) **Item N2 (Medium) — `--entry <node>`: run a node / DAG tail from COLD (no checkpoint).** Today `--resume` / `--resume-from` REQUIRE a SQLite checkpoint (`cli/main.py` raises "No saved state" otherwise). There is no way to run a mid-DAG node + its downstream against a curated/out-of-band workdir that already has the upstream artifacts on disk. Add `--entry <node>`: a cold-start (NO checkpoint load) that seeds FRESH state and freezes everything EXCEPT `<node>` and its downstream. Mutually exclusive with `--resume` / `--resume-from` / `--rerun-all`. The entry node trusts ON-DISK inputs — its `{{upstream}}` Jinja vars are EMPTY (upstream never ran, no `node_outputs`), so an `--entry` node must READ FILES, not interpolate upstream output.

**Architecture:**

- **Item A** is local to `runtime/executor/backends/cli.py::CLIBackend.send_prompt`. The only behavioral change is (1) `start_new_session=True` on the `create_subprocess_exec` call and (2) the `except asyncio.TimeoutError` branch now process-group-kills instead of `proc.kill()`. A new private module helper `_kill_process_group(proc)` does the SIGTERM→brief-wait→SIGKILL group escalation. The ACP backend already solves a HARDER variant (descendants reparent to PID 1 after the adapter's graceful `__aexit__`, defeating a group kill, so ACP captures PIDs pre-shutdown and `/proc`-walks); the CLI case does NOT need the `/proc` walk — see the killpg-discipline note in Global Constraints below.

- **Item B** is local to `cli/main.py`: a new `@click.option("--entry", ...)`, threaded through `run` → `_run_async` → `_execute_workflow`. The cold-start branch reuses `make_initial_state` (no checkpoint read) and reuses `compute_skip_set` UNCHANGED — passing `prior_completed = {all top-level node ids}`, `prior_failed = set()`, `rerun_targets = {entry}`. The existing dirty closure then dirties `entry` + everything downstream and freezes the rest; `completed_nodes` and `_resume_skip` are reseeded to that frozen skip set, exactly like the resume reseed (so downstream join/barrier guards see upstream as done). `compute_skip_set`'s signature already matches: `compute_skip_set(config, prior_completed, prior_failed, rerun_targets) -> set[str]`. No `compile/` or `schema/` change — `--entry` is a CLI-only flag.

**Tech Stack:** Python 3.11+, Pydantic v2 (`extra="forbid"`), LangGraph, `AsyncSqliteSaver` checkpointer, Click CLI, pytest, pytest-asyncio.

## Global Constraints

- Python `>=3.11`; use `sys.executable` for the Python interpreter in subprocess-driven tests — the bare `python` binary is unavailable in this env. (`tests/e2e/test_resume_fan_out.py` binds `_PYTHON = sys.executable`; the new `--entry` e2e reuses that pattern. The killpg stub is a `/bin/sh` script, no Python needed for the parent, but the descendant it forks is a `/bin/sh` `sleep` loop.)
- Layer rules (`tests/architecture/test_layers.py`): `schema/` must not import `langgraph`; `runtime/` must not import `sqrlly.compile` or `langgraph`; `compile/` must not import `sqrlly.cli`; `cli/` may import both. Item A lives entirely in `runtime/executor/backends/cli.py` (adds only stdlib `signal` to its imports — `os`, `asyncio` already present). Item B lives entirely in `cli/main.py`, which already imports `make_initial_state` and lazily imports `compute_skip_set` — no new cross-layer import.
- **No mocks of external systems** — real subprocess / real git / real `AsyncSqliteSaver`. For the killpg test use a REAL `/bin/sh` stub that forks a descendant (a `sleep` loop) which OUTLIVES its parent; assert the descendant is dead after the timeout fires (poll the descendant pid via `os.kill(pid, 0)` AND check that the sentinel file the descendant would have written never appears). NO `unittest.mock`. `MockExecutor` (`tests/mock_executor.py`) is the sanctioned `NodeExecutor` double; it is NOT used here (both items exercise real subprocess / real CLI run path).
- **killpg discipline (Item A) — killpg-only, NO `/proc` walk; justification from reading `acp.py`:** `acp.py::close` captures descendant PIDs BEFORE graceful shutdown and signals each PID individually (`_kill_pids` / `_collect_descendants`) precisely because the adapter shuts down via `__aexit__` FIRST — when `npx` exits, its grandchildren (`node`, `claude`) reparent to PID 1, so a `killpg` on the original spawn group misses them (see `acp.py` lines ~197–201 and the `_kill_pids` docstring ~231–239). The CLI timeout case has NO graceful-shutdown phase: on `asyncio.TimeoutError` the parent `claude` process is STILL ALIVE and, with `start_new_session=True`, is the process-GROUP LEADER. We `os.killpg` the group while the leader is alive (BEFORE `proc.wait()` reaps it), so descendants have NOT reparented yet and the single group signal reaches the entire tree. Adding a `/proc` walk here would be dead complexity (YAGNI) — the group kill is sufficient because we never let the parent exit first. The CLI backend therefore mirrors only the SIGTERM→wait→SIGKILL *escalation* from ACP, not the pre-capture/`/proc`-walk machinery.
- Conventional-commit messages; **no attribution trailers** (no `Co-Authored-By`, no `via Happy`, no `Claude-Session`).
- `extra="forbid"` on all schema models — no schema field is added by this plan (Item B is a CLI flag), but do not add stray fields.
- Do not change the signatures of `PromptBackend.send_prompt`, `DispatchExecutor._dispatch_prompt`, `compute_skip_set`, or `make_initial_state`.

---

### Task 1: CLI backend — spawn in a new session, process-group kill on timeout

**Files:**
- Modify: `src/sqrlly/runtime/executor/backends/cli.py` (`CLIBackend.send_prompt` spawn + timeout branch; add a module-level `_kill_process_group` helper; add `import signal`)
- Test: `tests/unit/runtime/test_cli_backend.py` (add a `TestCLIBackendProcessGroupKill` class — real `/bin/sh` stub that forks a sentinel-writing descendant)

**Interfaces:**
- Consumes: nothing new.
- Produces: `CLIBackend.send_prompt` spawns the child with `start_new_session=True`; on `asyncio.TimeoutError` it signals the child's process GROUP (SIGTERM → 0.5s wait → SIGKILL) before reaping, killing descendants too. `send_prompt`'s signature, return type, success path, stdin pipe, env overlay, and overload mapping are UNCHANGED.

**Context:** Current `send_prompt` (traced at `cli.py` lines ~84–127):

```python
    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
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
        try:
            stdout_b, stderr_b = await _await_with_timeout(
                proc.communicate(input=prompt.encode()),
                timeout,
            )
        except asyncio.TimeoutError:
            # Caller asked for a bounded wait; kill the runaway child
            # so we don't leak a process per timeout. ``wait()`` reaps
            # so the OS doesn't accumulate zombies.
            proc.kill()
            await proc.wait()
            raise
```

The current import block (`cli.py` lines ~18–28):

```python
from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable

from sqrlly.runtime.executor.backends._overload import (
    ACP_OVERLOAD_SUBSTRINGS,
    maybe_raise_overload,
)
from sqrlly.runtime.result import ExecutionResult
```

- [ ] **Step 1: Write the failing test** — add a `TestCLIBackendProcessGroupKill` class to `tests/unit/runtime/test_cli_backend.py`. The existing `_write_fake` / `_backend_for` helpers (top of that file) are reused. The stub forks a descendant in the background that, AFTER a sleep, writes a sentinel file; the parent itself sleeps long enough to trip the timeout. If only the direct child were killed (today's `proc.kill()`), the backgrounded descendant would survive and write the sentinel; with the group kill it dies first.

```python
# tests/unit/runtime/test_cli_backend.py — add near the end, after TestCLIBackendClose


class TestCLIBackendProcessGroupKill:
    """On timeout the backend must kill the whole process GROUP, not just the
    direct child. A real /bin/sh stub forks a descendant that writes a sentinel
    file after a delay; if only the direct child were killed, the descendant
    survives and writes the sentinel. With the process-group kill it dies first
    — no sentinel, pid gone. No mock: real subprocess, real fork."""

    def _stub_with_descendant(self, tmp_path: Path):
        """Stub `claude` that:
          - forks a backgrounded descendant which sleeps 3s then writes
            `descendant.sentinel` and records its own pid in `descendant.pid`;
          - records the descendant's pid immediately (before the sleep);
          - then sleeps 10s itself (so the parent is alive and is the group
            leader when the timeout fires).
        The descendant writes its pid up front so the test can poll liveness,
        and the sentinel only AFTER the sleep so its presence proves it
        survived the kill."""
        sentinel = tmp_path / "descendant.sentinel"
        pidfile = tmp_path / "descendant.pid"
        stub = tmp_path / "claude_group_stub.sh"
        stub.write_text(
            "#!/bin/sh\n"
            "cat >/dev/null\n"  # drain the prompt on stdin
            "(\n"
            f'  echo "$$" > "{pidfile}"\n'   # descendant records its pid now
            "  sleep 3\n"
            f'  echo alive > "{sentinel}"\n'  # only reached if NOT killed
            ") &\n"
            "sleep 10\n"  # parent stays alive past the timeout
        )
        stub.chmod(0o755)
        return stub, sentinel, pidfile

    @pytest.mark.asyncio
    async def test_timeout_kills_descendant_not_just_child(self, tmp_path):
        import time

        stub, sentinel, pidfile = self._stub_with_descendant(tmp_path)
        backend = _backend_for(stub)

        with pytest.raises(asyncio.TimeoutError):
            await backend.send_prompt(
                "prompt", "sonnet", str(tmp_path), timeout=0.5,
            )

        # The descendant must have recorded its pid before the parent's
        # timeout (the fork + echo happen immediately). Poll briefly for it.
        deadline = time.monotonic() + 2.0
        while not pidfile.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert pidfile.exists(), "stub never spawned its descendant"
        descendant_pid = int(pidfile.read_text().strip())

        # Give the kill a beat to propagate through the group, then assert the
        # descendant pid is GONE. os.kill(pid, 0) raises ProcessLookupError on
        # a dead pid; if it does NOT raise, the descendant leaked.
        await asyncio.sleep(0.6)
        with pytest.raises(ProcessLookupError):
            os.kill(descendant_pid, 0)

        # And the sentinel the descendant would have written after sleeping 3s
        # must NEVER appear — proving it was killed before its sleep finished.
        await asyncio.sleep(3.0)
        assert not sentinel.exists(), (
            "descendant survived the timeout and wrote its sentinel — "
            "only the direct child was killed, not the process group"
        )

    @pytest.mark.asyncio
    async def test_normal_run_still_succeeds_after_session_change(self, tmp_path):
        """start_new_session=True must not break the happy path: a fast stub
        still returns its stdout, stripped."""
        fake = _write_fake(
            tmp_path, "claude-ok", '#!/bin/sh\ncat >/dev/null\necho ok-output\n',
        )
        backend = _backend_for(fake)
        result = await backend.send_prompt(
            "x", "sonnet", str(tmp_path), timeout=10.0,
        )
        assert result.success is True
        assert result.output == "ok-output"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/runtime/test_cli_backend.py -q -k "TestCLIBackendProcessGroupKill"`
Expected: FAIL — `test_timeout_kills_descendant_not_just_child` fails at the final assertion (or the `ProcessLookupError` assertion). Today `proc.kill()` signals ONLY the direct child (the stub's top-level `/bin/sh`); the backgrounded `( ... ) &` descendant is in the SAME process group but is NOT signaled by `proc.kill()` (which is a single-pid `SIGKILL` to the child), so it survives, sleeps 3s, and writes `descendant.sentinel` — `assert not sentinel.exists()` fails (and/or `os.kill(descendant_pid, 0)` does not raise). `test_normal_run_still_succeeds_after_session_change` PASSES already (the happy path is unchanged). Report which assertion tripped.

- [ ] **Step 3: Implement** —

(3a) Add `import signal` to `cli.py`'s imports. Current:

```python
from __future__ import annotations

import asyncio
import os
from typing import Any, Awaitable
```

becomes:

```python
from __future__ import annotations

import asyncio
import os
import signal
from typing import Any, Awaitable
```

(3b) Add a module-level helper after `_await_with_timeout` (after `cli.py` line ~41, before `class CLIBackend`):

```python
async def _kill_process_group(proc: Any) -> None:
    """SIGTERM → brief wait → SIGKILL the child's process GROUP, then reap.

    The child is spawned with ``start_new_session=True``, so it is the
    leader of its own process group and ``os.getpgid(proc.pid)`` is the
    group containing every descendant ``claude`` forked (MCP servers, test
    runners, headless browsers). ``proc.kill()`` would signal only the
    direct child and leak that tree; ``os.killpg`` reaches the whole group.

    No ``/proc`` walk is needed (unlike ``backends/acp.py``): the ACP path
    kills AFTER the adapter's graceful ``__aexit__``, by which point its
    grandchildren have reparented to PID 1 and escaped the spawn group. Here
    the parent is still alive (it is the group leader) when we signal, so the
    descendants have not reparented and a single group signal reaches them
    all. We reap the parent with ``proc.wait()`` afterward so the OS does not
    accumulate a zombie.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        # Already exited between the timeout and here; just reap.
        await proc.wait()
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            break  # group already gone
        if sig is signal.SIGTERM:
            await asyncio.sleep(0.5)  # grace period for clean shutdown
    await proc.wait()
```

(3c) Change `send_prompt`'s spawn to use a new session, and its timeout branch to group-kill. Current:

```python
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workdir,
            env=proc_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout_b, stderr_b = await _await_with_timeout(
                proc.communicate(input=prompt.encode()),
                timeout,
            )
        except asyncio.TimeoutError:
            # Caller asked for a bounded wait; kill the runaway child
            # so we don't leak a process per timeout. ``wait()`` reaps
            # so the OS doesn't accumulate zombies.
            proc.kill()
            await proc.wait()
            raise
```

becomes:

```python
        # start_new_session=True puts the child in its OWN process group
        # (the child becomes the group leader), so a runaway `claude` that
        # forked descendants (MCP servers, test runners, headless browsers)
        # is killable as a GROUP on timeout — see _kill_process_group.
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workdir,
            env=proc_env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout_b, stderr_b = await _await_with_timeout(
                proc.communicate(input=prompt.encode()),
                timeout,
            )
        except asyncio.TimeoutError:
            # Caller asked for a bounded wait; kill the runaway child AND its
            # descendants (the whole process group) so we don't leak a tree
            # per timeout. The parent is still alive here (it is the group
            # leader), so a single killpg reaches the entire group before any
            # reparenting. _kill_process_group reaps the parent.
            await _kill_process_group(proc)
            raise
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/runtime/test_cli_backend.py -q`
Expected: PASS — the full CLI-backend suite, including the new `TestCLIBackendProcessGroupKill` (2 cases) and the pre-existing `TestCLIBackendTimeout::test_timeout_raises_and_reaps_subprocess` (still passes: it asserts the timeout raises and a subsequent call works; the group-kill reaps the same way `proc.kill()` did). Also confirm `TestCLIBackendRetryViaDispatch` (the #9 e2e) still passes — its stub exits cleanly, never hitting the timeout branch, and `start_new_session=True` does not affect the happy path.

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/runtime/executor/backends/cli.py tests/unit/runtime/test_cli_backend.py
git commit -m "fix: CLI backend kills the whole process group on timeout, not just the child"
```

---

### Task 2: `--entry <node>` CLI flag — cold-start, freeze everything but the node + downstream

**Files:**
- Modify: `src/sqrlly/cli/main.py` (add the `--entry` option to `run`; validate it; thread `entry` through `_run_async` → `_execute_workflow`; add the cold-start seed branch)
- Test: `tests/unit/cli` — confirm the directory, else co-locate with existing CLI tests. This task's argument-validation tests use Click's `CliRunner`; the behavioral proof is the e2e in Task 3.

**Interfaces:**
- Consumes: `compute_skip_set(config, prior_completed, prior_failed, rerun_targets)` (existing, UNCHANGED), `make_initial_state` (existing).
- Produces: `sqrlly run config.yaml --entry <node>` seeds FRESH state (no checkpoint read), computes `skip = compute_skip_set(config, {all top-level node ids}, set(), {entry})`, and reseeds `completed_nodes = skip` and `_resume_skip = skip` so that `<entry>` + its downstream run and everything else is frozen. Mutually exclusive with `--resume` / `--resume-from` / `--rerun-all`; `<entry>` must be a real top-level node id (reject `::` fan-out child ids).

**Context:** The `run` command's option block + validation (traced at `cli/main.py` lines ~594–662):

```python
@cli.command()
@click.argument("config_file", type=click.Path())
@click.option("--workdir", "-w", default=".", help="Working directory")
@click.option(
    "--dry-run", is_flag=True, help="Validate and trace without executing"
)
@click.option(
    "--preset", "-p",
    help=(
        "Force a specific named preset as the default for this run. "
        "Must exist in settings.presets. Without this flag, the YAML's "
        "default: true preset applies; if settings.presets is empty, "
        "only script/binary/subgraph nodes can run (LLM nodes have no "
        "backend)."
    ),
)
@click.option(
    "--resume", is_flag=True, help="Resume from the last checkpoint"
)
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
@click.option("--log", "log_file", type=click.Path(), help="JSONL log output file")
@click.option(
    "--quiet", "-q", is_flag=True,
    help="Suppress the live terminal renderer (useful in CI/piped runs).",
)
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

    resume = resume or bool(resume_from)
    if rerun_all and resume_from:
        raise click.ClickException(
            "--rerun-all and --resume-from are mutually exclusive"
        )
    valid_ids = {n.id for n in config.nodes}
    for rid in resume_from:
        if "::" in rid:
            raise click.ClickException(
                f"--resume-from {rid!r}: fan-out children are not addressable across runs"
            )
        if rid not in valid_ids:
            raise click.ClickException(
                f"--resume-from {rid!r}: unknown node id. "
                f"Valid: {', '.join(sorted(valid_ids))}"
            )

    _emit_warnings(config)
```

The `asyncio.run(_run_async(...))` call (traced at `cli/main.py` lines ~664–668):

```python
    from sqrlly.runtime.result import EvaluationError, ManifestError, RouteError
    try:
        result = asyncio.run(
            _run_async(config, workdir, dry_run, preset, resume, resume_from, rerun_all, log_file, quiet)
        )
```

`_run_async`'s signature + its call to `_execute_workflow` (traced at lines ~262–321):

```python
async def _run_async(
    config: Graph,
    workdir: str,
    dry_run: bool,
    preset_override: str | None,
    resume: bool,
    resume_from: tuple[str, ...],
    rerun_all: bool,
    log_file: str | None,
    quiet: bool = False,
) -> dict:
    ...
        result = await _execute_workflow(
            config, workdir, dry_run, preset_override, resume, resume_from, rerun_all,
            thread_id=thread_id, logger=logger,
        )
```

`_execute_workflow`'s signature + its state-seed branch (traced at lines ~377–505):

```python
async def _execute_workflow(
    config: Graph,
    workdir: str,
    dry_run: bool,
    preset_override: str | None,
    resume: bool,
    resume_from: tuple[str, ...],
    rerun_all: bool,
    *,
    thread_id: str,
    logger: Any | None,
) -> dict:
    ...
    async with AsyncSqliteSaver.from_conn_string(_db_path(workdir)) as cp:
        await cp.setup()
        state: dict
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
                "completed_nodes": skip,
                "failed_nodes": set(), "retries": {}, "errors": [],
                "workdir": workdir, "dry_run": False, "_resume_skip": skip,
            }
            if rerun_all:
                source = "all nodes (rerun-all)"
            elif resume_from:
                source = ", ".join(sorted(resume_from))
            else:
                source = "failed nodes"
            click.echo(
                f"Resuming: skipping {len(skip)} completed; re-running "
                f"{len(prior_completed - skip)} (from: {source})."
            )
            # Wipe the thread so reducers don't merge with stale state
            await cp.adelete_thread(thread_id)
        else:
            await cp.adelete_thread(thread_id)
            state = make_initial_state(
                workflow_name=config.name, workdir=workdir, dry_run=False,
            )
```

The plan threads a new `entry: str | None` parameter (keyword-only, defaulting `None`) through `_run_async` and `_execute_workflow`, then adds an `elif entry is not None:` branch to the seed `if/else`. Note the entry branch must come BEFORE the bare `else` (fresh run) and is exclusive with `resume`.

- [ ] **Step 1: Write the failing test** — confirm the CLI test dir, then add argument-validation tests. First:

```bash
ls tests/unit/cli/ 2>/dev/null || ls tests/cli/ 2>/dev/null || echo "NO_CLI_TEST_DIR"
```

If `tests/unit/cli/` exists, add the file there. If only `tests/cli/` exists (that dir holds live `claude`-CLI tests, gated by `--ignore=tests/cli`), do NOT put pure-validation tests there — create `tests/unit/cli/test_entry_flag.py` (make the dir; do NOT create `tests/__init__.py` — its absence is required per CLAUDE.md). The validation tests use `CliRunner` and never execute a backend, so they run in the default `tests/` suite.

```python
# tests/unit/cli/test_entry_flag.py
"""--entry <node>: cold-start argument validation. The behavioral proof
(node a does NOT run, b+c DO, from a fresh workdir) is the e2e in
tests/e2e/test_entry_cold_start.py. These pin the CLI guards only."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
from click.testing import CliRunner

from sqrlly.cli.main import cli


def _write_workflow(tmp_path: Path) -> Path:
    """Minimal 3-node linear command workflow (echo nodes — no backend)."""
    import shutil
    echo = shutil.which("echo") or "/bin/echo"
    wf = tmp_path / "wf.yaml"
    wf.write_text(textwrap.dedent(f"""\
        name: entry-validate
        version: "0.1.0"
        nodes:
          - id: a
            name: a
            execute:
              url: {echo}
              params: {{args: ["-n", "a-out"]}}
          - id: b
            name: b
            depends_on: [a]
            execute:
              url: {echo}
              params: {{args: ["-n", "b-out"]}}
          - id: c
            name: c
            depends_on: [b]
            execute:
              url: {echo}
              params: {{args: ["-n", "c-out"]}}
    """))
    return wf


def test_entry_unknown_node_errors(tmp_path):
    wf = _write_workflow(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        cli, ["run", str(wf), "-w", str(tmp_path), "--entry", "nope", "-q"],
    )
    assert res.exit_code != 0
    assert "unknown node id" in res.output
    assert "nope" in res.output


def test_entry_rejects_fanout_child_id(tmp_path):
    wf = _write_workflow(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        cli, ["run", str(wf), "-w", str(tmp_path), "--entry", "a::x", "-q"],
    )
    assert res.exit_code != 0
    assert "fan-out children are not addressable" in res.output


def test_entry_with_resume_is_rejected(tmp_path):
    wf = _write_workflow(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        cli,
        ["run", str(wf), "-w", str(tmp_path), "--entry", "b", "--resume", "-q"],
    )
    assert res.exit_code != 0
    assert "--entry" in res.output
    assert "mutually exclusive" in res.output


def test_entry_with_resume_from_is_rejected(tmp_path):
    wf = _write_workflow(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        cli,
        ["run", str(wf), "-w", str(tmp_path),
         "--entry", "b", "--resume-from", "a", "-q"],
    )
    assert res.exit_code != 0
    assert "--entry" in res.output
    assert "mutually exclusive" in res.output


def test_entry_with_rerun_all_is_rejected(tmp_path):
    wf = _write_workflow(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        cli,
        ["run", str(wf), "-w", str(tmp_path),
         "--entry", "b", "--rerun-all", "-q"],
    )
    assert res.exit_code != 0
    assert "--entry" in res.output
    assert "mutually exclusive" in res.output
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/unit/cli/test_entry_flag.py -q`
Expected: FAIL — Click rejects the unknown `--entry` option itself with "No such option: --entry" (exit code 2), so all five tests fail (the specific assertion strings are absent because the option does not exist yet). Report the Click "No such option" message as the observed failure.

- [ ] **Step 3: Implement** —

(3a) Add the `--entry` option to `run`, after the `--rerun-all` option (before `--log`):

```python
@click.option(
    "--rerun-all", "rerun_all", is_flag=True,
    help="With --resume: re-execute every node (pre-0.6 full replay; "
         "disables skip-completed).",
)
```

gets a sibling immediately after it:

```python
@click.option(
    "--entry", "entry", default=None, metavar="NODE",
    help="Cold-start at NODE: run NODE and everything downstream, freezing "
         "everything upstream WITHOUT a checkpoint (the upstream artifacts "
         "must already be on disk). The entry node sees EMPTY {{upstream}} "
         "template vars — it must read files, not interpolate upstream "
         "output. Mutually exclusive with --resume / --resume-from / "
         "--rerun-all.",
)
```

(3b) Add `entry: str | None` to `run`'s signature, after `rerun_all`:

```python
def run(
    config_file: str,
    workdir: str,
    dry_run: bool,
    preset: str | None,
    resume: bool,
    resume_from: tuple[str, ...],
    rerun_all: bool,
    entry: str | None,
    log_file: str | None,
    quiet: bool,
):
```

(3c) Add `--entry` validation in `run`'s body. Insert the mutual-exclusion + node-id checks AFTER the existing `resume = resume or bool(resume_from)` line and the `--rerun-all`/`--resume-from` check, and reuse the `valid_ids` set already computed for `--resume-from`. Current:

```python
    resume = resume or bool(resume_from)
    if rerun_all and resume_from:
        raise click.ClickException(
            "--rerun-all and --resume-from are mutually exclusive"
        )
    valid_ids = {n.id for n in config.nodes}
    for rid in resume_from:
        if "::" in rid:
            raise click.ClickException(
                f"--resume-from {rid!r}: fan-out children are not addressable across runs"
            )
        if rid not in valid_ids:
            raise click.ClickException(
                f"--resume-from {rid!r}: unknown node id. "
                f"Valid: {', '.join(sorted(valid_ids))}"
            )

    _emit_warnings(config)
```

becomes:

```python
    resume = resume or bool(resume_from)
    if rerun_all and resume_from:
        raise click.ClickException(
            "--rerun-all and --resume-from are mutually exclusive"
        )
    valid_ids = {n.id for n in config.nodes}
    for rid in resume_from:
        if "::" in rid:
            raise click.ClickException(
                f"--resume-from {rid!r}: fan-out children are not addressable across runs"
            )
        if rid not in valid_ids:
            raise click.ClickException(
                f"--resume-from {rid!r}: unknown node id. "
                f"Valid: {', '.join(sorted(valid_ids))}"
            )

    if entry is not None:
        # --entry is a COLD start (no checkpoint); --resume family reads a
        # checkpoint. The two seed strategies are incompatible.
        if resume or rerun_all:
            raise click.ClickException(
                "--entry is mutually exclusive with --resume, "
                "--resume-from, and --rerun-all"
            )
        if "::" in entry:
            raise click.ClickException(
                f"--entry {entry!r}: fan-out children are not addressable "
                f"(name the top-level fan-out parent instead)"
            )
        if entry not in valid_ids:
            raise click.ClickException(
                f"--entry {entry!r}: unknown node id. "
                f"Valid: {', '.join(sorted(valid_ids))}"
            )

    _emit_warnings(config)
```

(3d) Thread `entry` into the `_run_async` call. Current:

```python
        result = asyncio.run(
            _run_async(config, workdir, dry_run, preset, resume, resume_from, rerun_all, log_file, quiet)
        )
```

becomes:

```python
        result = asyncio.run(
            _run_async(config, workdir, dry_run, preset, resume, resume_from, rerun_all, log_file, quiet, entry)
        )
```

(3e) Add `entry` to `_run_async`'s signature (after `quiet`, since `quiet` has a default it must stay last among non-defaulted — give `entry` a default too) and forward it to `_execute_workflow`. Current signature:

```python
async def _run_async(
    config: Graph,
    workdir: str,
    dry_run: bool,
    preset_override: str | None,
    resume: bool,
    resume_from: tuple[str, ...],
    rerun_all: bool,
    log_file: str | None,
    quiet: bool = False,
) -> dict:
```

becomes:

```python
async def _run_async(
    config: Graph,
    workdir: str,
    dry_run: bool,
    preset_override: str | None,
    resume: bool,
    resume_from: tuple[str, ...],
    rerun_all: bool,
    log_file: str | None,
    quiet: bool = False,
    entry: str | None = None,
) -> dict:
```

and its `_execute_workflow` call. Current:

```python
        result = await _execute_workflow(
            config, workdir, dry_run, preset_override, resume, resume_from, rerun_all,
            thread_id=thread_id, logger=logger,
        )
```

becomes:

```python
        result = await _execute_workflow(
            config, workdir, dry_run, preset_override, resume, resume_from, rerun_all,
            thread_id=thread_id, logger=logger, entry=entry,
        )
```

(3f) Add `entry` to `_execute_workflow`'s keyword-only params and the cold-start seed branch. Current signature:

```python
async def _execute_workflow(
    config: Graph,
    workdir: str,
    dry_run: bool,
    preset_override: str | None,
    resume: bool,
    resume_from: tuple[str, ...],
    rerun_all: bool,
    *,
    thread_id: str,
    logger: Any | None,
) -> dict:
```

becomes:

```python
async def _execute_workflow(
    config: Graph,
    workdir: str,
    dry_run: bool,
    preset_override: str | None,
    resume: bool,
    resume_from: tuple[str, ...],
    rerun_all: bool,
    *,
    thread_id: str,
    logger: Any | None,
    entry: str | None = None,
) -> dict:
```

(3g) Add the `elif entry is not None:` branch between the `if resume:` block and the bare `else:`. Current:

```python
            # Wipe the thread so reducers don't merge with stale state
            await cp.adelete_thread(thread_id)
        else:
            await cp.adelete_thread(thread_id)
            state = make_initial_state(
                workflow_name=config.name, workdir=workdir, dry_run=False,
            )
```

becomes:

```python
            # Wipe the thread so reducers don't merge with stale state
            await cp.adelete_thread(thread_id)
        elif entry is not None:
            # Cold start at `entry`: NO checkpoint read. Seed FRESH state, then
            # compute the skip set as if every top-level node had already
            # completed and `entry` were the sole rerun target — the existing
            # dirty closure dirties `entry` + its downstream and freezes the
            # rest. Reseed completed_nodes + _resume_skip to that frozen set so
            # downstream join/barrier guards see upstream as done, exactly like
            # the resume reseed. The entry node trusts ON-DISK upstream
            # artifacts: upstream never ran this session, so node_outputs is
            # empty and `{{upstream}}` template vars resolve to nothing — an
            # --entry node must read files, not interpolate upstream output.
            from sqrlly.compile.resume import compute_skip_set
            all_ids = {n.id for n in config.nodes}
            skip = compute_skip_set(config, all_ids, set(), {entry})
            await cp.adelete_thread(thread_id)
            state = make_initial_state(
                workflow_name=config.name, workdir=workdir, dry_run=False,
            )
            state["completed_nodes"] = set(skip)
            state["_resume_skip"] = set(skip)
            click.echo(
                f"Cold start at {entry!r}: running "
                f"{len(all_ids - skip)} node(s) ({entry} + downstream); "
                f"freezing {len(skip)} upstream (trusting on-disk artifacts)."
            )
        else:
            await cp.adelete_thread(thread_id)
            state = make_initial_state(
                workflow_name=config.name, workdir=workdir, dry_run=False,
            )
```

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/unit/cli/test_entry_flag.py -q`
Expected: PASS (5 cases) — the option now exists; the unknown-id, fan-out-child, and three mutual-exclusion guards fire with the asserted messages.

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/cli/main.py tests/unit/cli/test_entry_flag.py
git commit -m "feat: --entry <node> cold-start — run a node + downstream without a checkpoint"
```

---

### Task 3: Load-bearing e2e — `--entry b` from a FRESH workdir runs b+c, not a

**Files:**
- Test: new `tests/e2e/test_entry_cold_start.py` (models the `_run_phase` / `_worker_path` / `_read_runs` / `_PYTHON` pattern from `tests/e2e/test_resume_fan_out.py`, but with the COLD `--entry` seed branch — no checkpoint).

**Interfaces:**
- Consumes: Task 2 (`--entry` flag), `compute_skip_set`, `make_initial_state`, `DispatchExecutor`, `AsyncSqliteSaver`.

**Context:** Model on `tests/e2e/test_resume_fan_out.py::_run_phase` (real `AsyncSqliteSaver`, real `DispatchExecutor`, NO foreman — nodes run in the shared workdir). The worker `_WORKER_SRC` there writes a per-id run-counter file (`runs_<qid>.txt`); we reuse exactly that pattern so a counter proves `a` never ran. The entry node `b` must read an ON-DISK input file the test pre-creates (NOT an upstream `{{}}` var), matching the `--entry` contract: upstream never ran, so `node_outputs` is empty and `{{a}}` would render empty. The worker reads its input file and writes an output file so `c` (downstream of `b`) can chain in turn.

This test builds a SELF-CONTAINED cold-start phase function (`_run_entry`) that mirrors the `elif entry is not None:` branch added in Task 2 — fresh `make_initial_state`, `compute_skip_set(config, all_ids, set(), {entry})`, reseed `completed_nodes` + `_resume_skip`. It does NOT shell out to the CLI (the e2e suite drives the run path directly, like `_run_phase`), so it proves the seed-and-skip behavior end-to-end through the real graph.

- [ ] **Step 1: Write the failing test** — create `tests/e2e/test_entry_cold_start.py`:

```python
"""End-to-end: `--entry <node>` cold-start. A 3-node linear workflow a -> b -> c
is run with entry=b from a FRESH workdir (no checkpoint). `a` must NOT execute
(its run-counter stays 0); `b` (reading an on-disk input file the test
pre-creates) and `c` DO run. This mirrors cli/main.py's `elif entry is not
None:` seed branch with a real AsyncSqliteSaver + DispatchExecutor."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from sqrlly.compile.graph import build_workflow_graph
from sqrlly.compile.resume import compute_skip_set
from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.runner import run_workflow
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Graph

_PYTHON = sys.executable

# Each node runs this worker keyed by its id. It bumps a per-id run-counter,
# then — for nodes after the first — reads `in_<qid>.txt` (an on-disk input)
# and writes `out_<qid>.txt`. The `a` node would write `out_a.txt`, but with
# --entry b it never runs, so the test pre-creates `in_b.txt` to stand in for
# a's on-disk artifact.
_WORKER_SRC = """
import sys
from pathlib import Path
qid = sys.argv[1]
counter = Path(f"runs_{qid}.txt")
n = int(counter.read_text()) if counter.exists() else 0
counter.write_text(str(n + 1))
infile = Path(f"in_{qid}.txt")
content = infile.read_text() if infile.exists() else f"no-input-for-{qid}"
Path(f"out_{qid}.txt").write_text(content + f"|{qid}")
print(f"ok:{qid}")
"""


def _worker_path(workdir: Path) -> Path:
    p = workdir / "worker.py"
    p.write_text(_WORKER_SRC)
    return p


def _read_runs(workdir: Path, qid: str) -> int:
    p = workdir / f"runs_{qid}.txt"
    return int(p.read_text()) if p.exists() else 0


def _build_chain(workdir: Path) -> Graph:
    """a -> b -> c, each running worker.py keyed by its id. b reads in_b.txt;
    c reads in_c.txt (which b's body writes as out_b.txt — wired below via a
    pre-created in_c after b runs is NOT needed; c reads its own in_c which the
    test seeds OR b's out — here we keep them independent on-disk inputs)."""
    worker = _worker_path(workdir)

    def node(nid, deps=None):
        return {
            "id": nid, "name": nid,
            "depends_on": deps or [],
            "execute": {"url": _PYTHON, "params": {"args": [str(worker), nid]}},
        }

    return Graph(
        name="entry-chain",
        version="0.1.0",
        nodes=[node("a"), node("b", ["a"]), node("c", ["b"])],
    )


async def _run_entry(
    workdir: Path, config: Graph, db_path: str, thread_id: str, *, entry: str,
) -> dict:
    """Mirror cli/main.py's `elif entry is not None:` cold-start branch."""
    async with AsyncSqliteSaver.from_conn_string(db_path) as cp:
        await cp.setup()
        await cp.adelete_thread(thread_id)
        all_ids = {n.id for n in config.nodes}
        skip = compute_skip_set(config, all_ids, set(), {entry})
        state = make_initial_state(workdir=str(workdir), dry_run=False)
        state["completed_nodes"] = set(skip)
        state["_resume_skip"] = set(skip)
        compiled = build_workflow_graph(
            config, DispatchExecutor(workdir=str(workdir)), checkpointer=cp,
        )
        return await run_workflow(
            compiled, state, config, thread_id=thread_id,
        )


class TestEntryColdStart:
    @pytest.mark.asyncio
    async def test_entry_runs_node_and_downstream_not_upstream(self, tmp_path):
        config = _build_chain(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "entry-cold"

        # Pre-create the ON-DISK input that `a` would normally have produced.
        # With --entry b, `a` never runs, so b must read this file (not a
        # `{{a}}` template var — node_outputs is empty on a cold start).
        (tmp_path / "in_b.txt").write_text("disk-artifact-from-a")
        (tmp_path / "in_c.txt").write_text("disk-artifact-for-c")

        result = await _run_entry(
            tmp_path, config, db_path, thread_id, entry="b",
        )

        # `a` is frozen (upstream of the entry) — it never executed.
        assert _read_runs(tmp_path, "a") == 0, "upstream node a must NOT run"
        assert not (tmp_path / "out_a.txt").exists()

        # `b` (the entry) and `c` (downstream) ran exactly once.
        assert _read_runs(tmp_path, "b") == 1
        assert _read_runs(tmp_path, "c") == 1
        assert "b" in result["completed_nodes"]
        assert "c" in result["completed_nodes"]
        assert result["failed_nodes"] == set()

        # `b` read the on-disk artifact, not an (empty) upstream var.
        assert (tmp_path / "out_b.txt").read_text() == "disk-artifact-from-a|b"

        # The frozen `a` is reseeded into completed_nodes (the skip set), so the
        # join/barrier on `b` saw its upstream as satisfied without running it.
        assert "a" in result["completed_nodes"]

    @pytest.mark.asyncio
    async def test_entry_at_head_runs_all(self, tmp_path):
        """--entry a (the head) freezes nothing → the whole chain runs."""
        config = _build_chain(tmp_path)
        db_path = str(tmp_path / ".checkpoint.db")
        thread_id = "entry-head"
        (tmp_path / "in_a.txt").write_text("seed")
        (tmp_path / "in_b.txt").write_text("seed-b")
        (tmp_path / "in_c.txt").write_text("seed-c")
        result = await _run_entry(
            tmp_path, config, db_path, thread_id, entry="a",
        )
        assert _read_runs(tmp_path, "a") == 1
        assert _read_runs(tmp_path, "b") == 1
        assert _read_runs(tmp_path, "c") == 1
        assert result["completed_nodes"] == {"a", "b", "c"}
```

- [ ] **Step 2: Run to verify it fails / passes**

Run: `uv run pytest tests/e2e/test_entry_cold_start.py -q`
Expected: This test exercises the SAME seed logic Task 2 added (cold start + `compute_skip_set(config, all_ids, set(), {entry})` + reseed). Since the e2e drives the run path directly (not via the CLI), it depends only on `compute_skip_set` (already present) and the existing skip-guard machinery — so it PASSES once written, independent of Task 2's CLI plumbing. If you write this BEFORE Task 2, it still PASSES (it does not call the CLI). The CLI plumbing is proven by Task 2's `CliRunner` validation tests; THIS test proves the cold-start SEMANTICS (a frozen, b+c run, b reads disk). If `_read_runs(a) != 0`, the skip-set seed is wrong — debug the `compute_skip_set` call (it must pass `all_ids` as `prior_completed` and `{entry}` as `rerun_targets`).

- [ ] **Step 3: Implement** — no source change (the seed logic is Task 2's; `compute_skip_set` is unchanged). This is the cold-start semantics proof.

- [ ] **Step 4: Run to verify it passes**

Run: `uv run pytest tests/e2e/test_entry_cold_start.py -q`
Expected: PASS (2 cases).

- [ ] **Step 5: Commit**

```bash
git add tests/e2e/test_entry_cold_start.py
git commit -m "test: e2e proving --entry runs the node + downstream from a cold workdir"
```

---

### Task 4: Full-suite regression + architecture gate

**Files:** none (verification only).

- [ ] **Step 1: Architecture layer rules** — Item A added only stdlib `signal` to `runtime/`; Item B added only a Click option + a lazy `from sqrlly.compile.resume import compute_skip_set` inside `cli/` (cli may import compile). Confirm:

Run: `uv run pytest tests/architecture/test_layers.py -q`
Expected: PASS — `runtime/executor/backends/cli.py` imports only `asyncio` / `os` / `signal` (stdlib) + existing modules; `cli/main.py`'s compile import is already allowed (cli → compile).

- [ ] **Step 2: Targeted suites**

Run:
```bash
uv run pytest tests/unit/runtime/test_cli_backend.py tests/unit/cli/test_entry_flag.py tests/e2e/test_entry_cold_start.py -q
```
Expected: PASS (all of Tasks 1–3).

- [ ] **Step 3: Full core suite**

Run: `uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -q`
Expected: PASS. Item A only changes the timeout/spawn path (the happy path is byte-identical except `start_new_session=True`, which does not alter stdout capture); Item B adds an opt-in flag whose default (`entry=None`) leaves the run path unchanged. The pre-existing `tests/e2e/test_timeout.py` and `tests/unit/runtime/test_cli_backend.py::TestCLIBackendTimeout` must stay green.

- [ ] **Step 4: No commit** (verification task — no file changes). If a pre-existing test fails, STOP and diagnose; do not paper over.

---

### Task 5: Docs + CHANGELOG + TODO

**Files:**
- Modify: `README.md` (CLI overview if `--entry` belongs there), `SCHEMA.md` (URL/CLI section if it lists run flags), `CLAUDE.md` (Build & Test + Known limitations), `SKILLS.md`, `CHANGELOG.md`, `TODO.md`

**Interfaces:** none.

- [ ] **Step 1: CLAUDE.md** —

(1a) In the "Build & Test" CLI block (the `uv run sqrlly run ...` examples), add the `--entry` line under the resume lines:

```markdown
uv run sqrlly run config.yaml --resume    # resume from checkpoint
uv run sqrlly run config.yaml --entry <node>  # cold-start: run <node> + downstream, no checkpoint
```

(1b) In "Known limitations", add two entries (hard-wrapped to match the file):

```markdown
- **`--entry <node>` is a COLD start, not a resume** — `sqrlly run
  --entry <node>` seeds FRESH state (no checkpoint read), freezes everything
  upstream of `<node>`, and runs `<node>` + its downstream
  (`cli/main.py::_execute_workflow` reuses `compile/resume.py::compute_skip_set`
  with `prior_completed = {all node ids}`, `prior_failed = set()`,
  `rerun_targets = {entry}`). Mutually exclusive with `--resume` /
  `--resume-from` / `--rerun-all`. **The entry node trusts ON-DISK inputs:**
  upstream never ran this session, so `node_outputs` is empty and a `{{upstream}}`
  Jinja var resolves to nothing — an `--entry` node must READ FILES, not
  interpolate upstream output. Rejects `::` fan-out child ids (name the
  top-level parent). v1: like `--resume`, subgraph inner nodes are not
  individually addressable as the entry.
- **CLI backend kills the whole process group on timeout** — the CLI backend
  spawns `claude -p` with `start_new_session=True` (own process group) and, on
  timeout/cancel, SIGTERM→SIGKILLs the process GROUP
  (`runtime/executor/backends/cli.py::_kill_process_group`), so descendants
  (MCP servers, test runners, headless browsers) are reaped too. Unlike ACP —
  which `/proc`-walks because its descendants reparent to PID 1 after the
  adapter's graceful `__aexit__` — the CLI parent is still alive (the group
  leader) when we signal, so a single `killpg` reaches the whole tree; no
  `/proc` walk needed.
```

- [ ] **Step 2: SKILLS.md** — near the run/resume authoring guidance, add (hard-wrapped):

```markdown
Use `sqrlly run --entry <node>` to re-run a synthesis/integration tail against
a hand-prepared workdir with NO prior checkpoint: it freezes everything
upstream and runs `<node>` + its downstream. The entry node must READ the
upstream artifacts from disk — `{{upstream}}` template vars are EMPTY on a cold
start (upstream never ran). Distinct from `--resume-from`, which requires a
checkpoint.
```

- [ ] **Step 3: CHANGELOG.md** — add (or extend) the top `## [Unreleased]` block (the #9 plan already added one; if present, append to its `### Added` / `### Fixed`; else create it above `## [0.7.6]`):

```markdown
### Added

- `sqrlly run --entry <node>` — cold-start at `<node>`: run it and everything downstream WITHOUT a checkpoint, freezing everything upstream (whose artifacts must already be on disk). Mutually exclusive with `--resume` / `--resume-from` / `--rerun-all`. The entry node trusts on-disk inputs — `{{upstream}}` template vars are empty on a cold start.

### Fixed

- CLI backend now kills the whole process group on timeout (`start_new_session=True` + `os.killpg` SIGTERM→SIGKILL), so a `claude -p` that spawned descendants (MCP servers, test runners, headless browsers) no longer leaks them. Previously only the direct child was reaped (`proc.kill()`).
```

- [ ] **Step 4: TODO.md** — mark N2 and the #5 killpg half SHIPPED.

(4a) The `N2` entry (search `**N2 — `): rewrite it to the file's shipped convention. If the file uses `### ✅ … — SHIPPED …` headings, match it; otherwise replace the bullet with a shipped note:

```markdown
- [x] **N2 — `--entry <node>`: run a node / DAG tail from COLD — SHIPPED 0.7.x**
  — `sqrlly run --entry <node>` cold-starts at `<node>` (no checkpoint), freezing
  everything upstream and running `<node>` + downstream. Reuses
  `compute_skip_set(config, {all ids}, set(), {entry})`; reseeds
  `completed_nodes` + `_resume_skip`. Entry node reads on-disk inputs (empty
  `{{upstream}}` vars). Mutually exclusive with `--resume` family.
```

(4b) The `#5` entry (search `**#5 —`): it bundles a token-budget half (a) and the killpg half (b). The killpg half is now shipped; the token-budget half is NOT. Edit the bullet to strike only the killpg half:

```markdown
- [ ] **#5 — per-node token budget (Low)** — a declarative `budget_tokens`
  per node/preset that fails the node when exceeded (parallel to `timeout`).
  (The CLI-killpg half of the original #5 — process-group kill on timeout —
  SHIPPED 0.7.x: `cli.py` now spawns with `start_new_session=True` and
  `os.killpg`-escalates on timeout, matching the ACP teardown discipline.)
```

- [ ] **Step 5: Sanity + commit**

```bash
uv run sqrlly validate examples/jokes/workflow.yaml   # still Valid
git add README.md SCHEMA.md CLAUDE.md SKILLS.md CHANGELOG.md TODO.md
git commit -m "docs: document --entry cold-start + CLI process-group kill; mark N2/#5-killpg shipped"
```

(Only `git add` the doc files actually edited — if `README.md` / `SCHEMA.md` have no run-flag list to extend, drop them from the `add`.)

---

## Final verification (after all tasks)

```bash
uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -q   # full core suite green
uv run pytest tests/unit/runtime/test_cli_backend.py -q          # Item A
uv run pytest tests/unit/cli/test_entry_flag.py tests/e2e/test_entry_cold_start.py -q  # Item B
uv run pytest tests/architecture/test_layers.py -q               # layer rules intact
```
Expected: full suite passes; on CLI-backend timeout the whole process group (including a forked descendant) is killed and its sentinel never appears; `--entry <node>` runs the node + downstream from a fresh workdir while the upstream node never executes, and `--entry` + `--resume` / a bad id / a `::` child id are all rejected.
