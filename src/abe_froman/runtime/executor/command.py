from __future__ import annotations

import asyncio
from typing import Any

from abe_froman.runtime.result import ExecutionResult
from abe_froman.schema.models import CommandExecution, Phase


class CommandExecutor:
    """Executes phases that have CommandExecution by running subprocesses."""

    def __init__(self, workdir: str = "."):
        self.workdir = workdir

    async def execute(self, phase: Phase, context: dict[str, Any]) -> ExecutionResult:
        if not isinstance(phase.execution, CommandExecution):
            return ExecutionResult(
                success=False,
                error=f"CommandExecutor requires CommandExecution, got {type(phase.execution).__name__}",
            )

        cmd = [phase.execution.command, *phase.execution.args]

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.workdir,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode == 0:
                return ExecutionResult(
                    success=True,
                    output=stdout.decode(),
                )
            else:
                return ExecutionResult(
                    success=False,
                    output=stdout.decode(),
                    error=f"Exit code {proc.returncode}: {stderr.decode()}",
                )
        except FileNotFoundError as e:
            return ExecutionResult(success=False, error=str(e))
        except OSError as e:
            return ExecutionResult(success=False, error=str(e))
