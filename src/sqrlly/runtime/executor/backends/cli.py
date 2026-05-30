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
from typing import Any, Awaitable

from sqrlly.runtime.executor.backends._overload import (
    ACP_OVERLOAD_SUBSTRINGS,
    maybe_raise_overload,
)
from sqrlly.runtime.result import ExecutionResult


async def _await_with_timeout(coro: Awaitable[Any], timeout: float | None) -> Any:
    """Await ``coro`` with optional timeout. ``timeout=None`` awaits
    without bound; otherwise delegates to ``asyncio.wait_for``.

    Duplicated from ``backends/acp.py`` rather than extracted — the
    function is one ``if`` over ``asyncio.wait_for`` and the two
    backends have no other shared seams worth a helper module.
    """
    if timeout is None:
        return await coro
    return await asyncio.wait_for(coro, timeout=timeout)


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
    ):
        self._argv_prefix = argv_prefix
        self._permission_mode = permission_mode
        self._allowed_tools = allowed_tools
        self._disallowed_tools = disallowed_tools
        self._cli_args = cli_args

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
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=workdir,
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
