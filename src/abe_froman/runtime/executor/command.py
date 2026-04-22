from __future__ import annotations

import asyncio
from typing import Any

from abe_froman.runtime.executor.prompt import render_template
from abe_froman.runtime.result import ExecutionResult
from abe_froman.schema.models import CommandExecution, Phase


class CommandExecutor:
    """Executes phases that have CommandExecution by running subprocesses."""

    def __init__(self, workdir: str = "."):
        self.workdir = workdir

    async def execute(
        self, phase: Phase, context: dict[str, Any], workdir: str | None = None
    ) -> ExecutionResult:
        if not isinstance(phase.execution, CommandExecution):
            return ExecutionResult(
                success=False,
                error=f"CommandExecutor requires CommandExecution, got {type(phase.execution).__name__}",
            )

        # Render each arg as a Jinja2 template against the phase's context.
        # Lets authors wire a command phase to dep outputs: e.g. args:
        # ["--input", "{{upstream_phase}}"]. Bare strings with no template
        # syntax render to themselves.
        rendered_args = [render_template(a, context) for a in phase.execution.args]
        cmd = [phase.execution.command, *rendered_args]
        cwd = workdir or self.workdir

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
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
