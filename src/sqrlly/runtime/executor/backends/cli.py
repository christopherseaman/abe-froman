"""Subprocess-per-call CLI backend.

Spawns a fresh ``claude -p`` (or any ``provider`` CLI) subprocess per
``send_prompt``. No warm process, no protocol, no shared accumulator —
every call is fully independent. Concurrent calls get real
``asyncio.create_subprocess_exec`` parallelism without contending for a
single warm session.

Lifecycle: zero. ``close()`` is a no-op. There is no in-process state
to tear down, and each subprocess inherits / exits on its own.

Overload mapping uses the same substring set as the ACP backend
(``ACP_OVERLOAD_SUBSTRINGS = {"529", "overload"}``) — Claude's
overload responses are identical across transports because both
ultimately hit the same upstream API; the name "ACP_OVERLOAD_SUBSTRINGS"
is kept for historical reasons but the set is shared by design.
"""
from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from sqrlly.runtime.executor.backends._overload import (
    ACP_OVERLOAD_SUBSTRINGS,
    maybe_raise_overload,
)
from sqrlly.runtime.result import ExecutionResult


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


class CLIBackend:
    """Subprocess-per-call LLM backend.

    Each ``send_prompt`` spawns a fresh ``claude -p --model <model>``
    subprocess, pipes the prompt on stdin, captures stdout as the
    response. Exit code 0 → success; non-zero stderr is checked for
    overload substrings then surfaced as ``RuntimeError`` otherwise.
    """

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

    def _tool_argv(self) -> list[str]:
        """Tool-permission flags appended to every invocation. Empty when
        the preset sets none → bare ``claude -p`` (the prior behavior)."""
        argv: list[str] = []
        if self._permission_mode:
            argv += ["--permission-mode", self._permission_mode]
        if self._allowed_tools:
            argv += ["--allowedTools", *self._allowed_tools]
        if self._disallowed_tools:
            argv += ["--disallowedTools", *self._disallowed_tools]
        if self._cli_args:
            argv += list(self._cli_args)
        return argv

    async def send_prompt(
        self, prompt: str, model: str, workdir: str,
        timeout: float | None = None,
    ) -> ExecutionResult:
        argv = [*self._argv_prefix, "--model", model, *self._tool_argv()]
        # Overlay the preset env on the inherited environment; empty → None
        # (inherit unchanged), preserving prior behavior exactly.
        proc_env = {**os.environ, **self._env} if self._env else None
        # start_new_session=True puts the child in its OWN process group
        # (the child becomes the group leader), so a runaway `claude` that
        # forked descendants (MCP servers, test runners, headless browsers)
        # is killable as a GROUP on timeout — see _kill_process_group.
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=workdir,
                env=proc_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError as e:
            # The CLI binary (default `claude`) isn't on PATH. Surface an
            # actionable message instead of a bare FileNotFoundError errno —
            # a user who installed sqrlly but not `claude` hits this on the
            # first prompt node.
            missing = e.filename or self._argv_prefix[0]
            raise RuntimeError(
                f"Command not found on PATH: {missing!r}. The "
                f"{self._argv_prefix[0]!r} CLI must be installed and on PATH "
                f"for transport: cli."
            ) from e
        try:
            # timeout=None is stdlib-documented unbounded wait (>=3.11).
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(input=prompt.encode()), timeout,
            )
        except asyncio.TimeoutError:
            # Caller asked for a bounded wait; kill the runaway child AND its
            # descendants (the whole process group) so we don't leak a tree
            # per timeout. The parent is still alive here (it is the group
            # leader), so a single killpg reaches the entire group before any
            # reparenting. _kill_process_group reaps the parent.
            await _kill_process_group(proc)
            raise
        except asyncio.CancelledError:
            # The task was cancelled (e.g. the orchestrator is shutting down
            # an in-flight node). Kill the process group so the `claude` subtree
            # doesn't leak, then re-raise so cooperative cancellation propagates.
            await _kill_process_group(proc)
            raise

        if proc.returncode != 0:
            stderr_text = stderr_b.decode().strip()
            # Substring overload check matches the ACP path: any
            # 529/overload signal triggers ``OverloadError`` so
            # ``PromptExecutor``'s downgrade chain activates. If the
            # error isn't transient, surface the raw stderr.
            maybe_raise_overload(
                RuntimeError(stderr_text),
                message_substrings=ACP_OVERLOAD_SUBSTRINGS,
            )
            raise RuntimeError(
                f"{self._argv_prefix[0]} exited {proc.returncode}: "
                f"{stderr_text}"
            )
        return ExecutionResult(output=stdout_b.decode().strip())

    async def close(self) -> None:
        # Subprocess-per-call backend has no warm state. ``close`` is
        # part of the ``PromptBackend`` protocol; satisfying it as a
        # no-op keeps the executor registry's close-all loop uniform.
        return
