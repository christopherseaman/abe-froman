from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from abe_froman.runtime.executor.command import CommandExecutor
from abe_froman.runtime.executor.prompt import PromptExecutor, render_template
from abe_froman.runtime.result import ExecutionResult, PromptBackend
from abe_froman.runtime.url import _RemoteFetchCache, fetch_url, resolve_url
from abe_froman.schema.models import (
    CommandExecution,
    Execute,
    GateOnlyExecution,
    JoinExecution,
    Node,
    PromptExecution,
    Settings,
)
from abe_froman.schema.params import coerce_params

# Script extension → interpreter prefix. URL → subprocess args via map +
# resolved local path. Stays small; new languages add one row.
_SCRIPT_INTERPRETERS: dict[str, list[str]] = {
    ".py": ["python3"],
    ".js": ["node"],
    ".mjs": ["node"],
    ".ts": ["tsx"],
    ".sh": ["bash"],
}

_PROMPT_EXTS = {".md", ".txt", ".prompt"}


class DispatchExecutor:
    """Routes execution to the appropriate executor.

    Stage-4 path (legacy ``node.execution`` discriminated union):
        - CommandExecution → CommandExecutor (subprocess)
        - GateOnlyExecution → no-op (gate evaluation happens downstream)
        - JoinExecution → no-op (topology marker)
        - PromptExecution → PromptExecutor with pluggable PromptBackend

    Stage-5b path (new ``node.execute`` shape):
        - execute.url with prompt extension → _dispatch_prompt
        - execute.url with script extension → _dispatch_script
        - execute.url else (binary path) → _dispatch_binary
        - execute.type=join → no-op
        - execute.type=route → never reached at runtime (compile-time only)
        - execute.url with .yaml → never reached at runtime (compile-time only)

    Both paths coexist during the dual-mode window; Commit 8 deletes the
    Stage-4 path.
    """

    def __init__(
        self,
        workdir: str = ".",
        prompt_backend: PromptBackend | None = None,
        settings: Settings | None = None,
    ):
        self._command_executor = CommandExecutor(workdir=workdir)
        self._workdir = workdir
        self._settings = settings or Settings()
        self._fetch_cache = _RemoteFetchCache()

        if prompt_backend is not None:
            self._prompt_executor: PromptExecutor | None = PromptExecutor(
                backend=prompt_backend,
                settings=self._settings,
                workdir=workdir,
            )
        else:
            self._prompt_executor = None

    async def execute(
        self, node: Node, context: dict[str, Any], workdir: str | None = None
    ) -> ExecutionResult:
        # Stage 5b path
        if node.execute is not None:
            return await self._dispatch_execute(node, context, workdir=workdir)

        # Legacy Stage-4 path (deleted in Commit 8)
        execution = node.execution
        if isinstance(execution, CommandExecution):
            return await self._command_executor.execute(node, context, workdir=workdir)
        if isinstance(execution, GateOnlyExecution):
            return ExecutionResult(success=True, output=f"[gate-only] {node.id}")
        if isinstance(execution, JoinExecution):
            return ExecutionResult(success=True, output="")
        if isinstance(execution, PromptExecution):
            if self._prompt_executor is not None:
                return await self._prompt_executor.execute(
                    node, context, workdir=workdir
                )
            return ExecutionResult(
                success=True,
                output=f"[prompt-stub] {node.id}: {execution.prompt_file}",
            )
        if execution is None:
            return ExecutionResult(
                success=False,
                error=f"Node '{node.id}' has no execution configuration",
            )
        return ExecutionResult(
            success=False,
            error=f"Unknown execution type: {type(execution).__name__}",
        )

    async def _dispatch_execute(
        self, node: Node, context: dict[str, Any], workdir: str | None = None
    ) -> ExecutionResult:
        """Stage-5b dispatch: walk handler table by URL extension/scheme."""
        execute = node.execute
        assert execute is not None  # caller-guarded

        if execute.type == "join":
            return ExecutionResult(success=True, output="")

        if execute.type == "route":
            # Compile-time only — _make_route_node handles routing via
            # Command(goto=). Reaching here is a programming error.
            return ExecutionResult(
                success=False,
                error=f"Route node '{node.id}' should not reach DispatchExecutor",
            )

        # URL mode
        effective_workdir = workdir or self._workdir
        resolved = resolve_url(
            execute.url, self._settings.base_url, effective_workdir
        )

        # Subgraphs are dispatched at compile time (not here). If we see a
        # .yaml URL, the compile layer failed to detect it.
        ext = Path(urlsplit(resolved).path).suffix.lower()
        if ext in {".yaml", ".yml"}:
            return ExecutionResult(
                success=False,
                error=(
                    f"Subgraph URL {execute.url!r} on node '{node.id}' should "
                    f"have been wired at compile time, not dispatched at runtime"
                ),
            )

        # Per-mode params validation: catches typos like `args:` on a prompt URL.
        try:
            params = coerce_params(resolved, execute.params)
        except Exception as e:
            return ExecutionResult(
                success=False,
                error=f"Node '{node.id}' params invalid for {resolved}: {e}",
            )

        if ext in _PROMPT_EXTS:
            return await self._dispatch_prompt(
                node, resolved, params, context, effective_workdir
            )
        if ext in _SCRIPT_INTERPRETERS:
            return await self._dispatch_script(
                node, resolved, params, context, effective_workdir
            )
        # Bare binary or unrecognized extension
        return await self._dispatch_binary(
            node, resolved, params, context, effective_workdir
        )

    async def _dispatch_prompt(
        self,
        node: Node,
        resolved: str,
        params: Any,
        context: dict[str, Any],
        workdir: str,
    ) -> ExecutionResult:
        """Read prompt body (file or remote), render Jinja, send to backend.

        Delegates the preamble + downgrade-chain loop to PromptExecutor
        helpers so both the legacy and Stage-5b paths share one
        implementation of the overload-handling.
        """
        if self._prompt_executor is None:
            return ExecutionResult(
                success=True,
                output=f"[prompt-stub] {node.id}: {resolved}",
            )

        try:
            body = fetch_url(resolved, self._settings, self._fetch_cache).decode()
        except Exception as e:
            return ExecutionResult(
                success=False,
                error=f"Failed to fetch prompt {resolved!r}: {e}",
            )

        applied = self._prompt_executor.apply_preamble(body)
        if isinstance(applied, ExecutionResult):
            return applied
        rendered = render_template(applied, context)

        # PromptParams.model overrides Node.model overrides Settings.default.
        current_model = (
            getattr(params, "model", None)
            or node.model
            or self._settings.default_model
        )
        timeout = (
            getattr(params, "timeout", None)
            or node.effective_timeout(self._settings)
        )
        return await self._prompt_executor.execute_rendered(
            rendered, current_model, workdir, timeout=timeout,
        )

    async def _dispatch_script(
        self,
        node: Node,
        resolved: str,
        params: Any,
        context: dict[str, Any],
        workdir: str,
    ) -> ExecutionResult:
        """Run a script under its interpreter (e.g. python3 / node / bash)."""
        ext = Path(urlsplit(resolved).path).suffix.lower()
        interpreter = _SCRIPT_INTERPRETERS[ext]
        # Resolve to local path. Remote scripts: fetch_url validates +
        # caches; for now, only file:// is supported runtime-side. Remote
        # script handoff (write to temp + chmod) is wired in a future commit.
        scheme = urlsplit(resolved).scheme
        if scheme != "file":
            return ExecutionResult(
                success=False,
                error=(
                    f"Remote script execution not yet wired (URL: {resolved}). "
                    f"Use file:// for now."
                ),
            )
        local_path = urlsplit(resolved).path
        return await self._run_subprocess(
            [*interpreter, local_path], params, context, workdir,
        )

    async def _dispatch_binary(
        self,
        node: Node,
        resolved: str,
        params: Any,
        context: dict[str, Any],
        workdir: str,
    ) -> ExecutionResult:
        """Run a binary directly (no interpreter)."""
        scheme = urlsplit(resolved).scheme
        if scheme != "file":
            return ExecutionResult(
                success=False,
                error=f"Direct exec requires a file:// URL, got: {resolved}",
            )
        local_path = urlsplit(resolved).path
        return await self._run_subprocess(
            [local_path], params, context, workdir,
        )

    async def _run_subprocess(
        self,
        cmd_prefix: list[str],
        params: Any,
        context: dict[str, Any],
        workdir: str,
    ) -> ExecutionResult:
        """Shared subprocess runner for script + binary dispatch.

        ``params.args`` is Jinja-rendered against ``context`` so authors
        can wire dep outputs into args. ``params.env`` is rendered the
        same way and merged onto the parent env.
        """
        rendered_args = [render_template(a, context) for a in params.args]
        rendered_env: dict[str, str] = {}
        for k, v in params.env.items():
            rendered_env[k] = render_template(v, context)

        import os
        env = {**os.environ, **rendered_env} if rendered_env else None

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_prefix, *rendered_args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workdir,
                env=env,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode == 0:
                return ExecutionResult(success=True, output=stdout.decode())
            return ExecutionResult(
                success=False,
                output=stdout.decode(),
                error=f"Exit code {proc.returncode}: {stderr.decode()}",
            )
        except (FileNotFoundError, OSError) as e:
            return ExecutionResult(success=False, error=str(e))

    def get_backend(self) -> PromptBackend | None:
        """Return the PromptBackend, if one is configured.

        Used by the orchestrator to dispatch .md LLM gates through the
        same backend the node executor uses.
        """
        if self._prompt_executor is None:
            return None
        return self._prompt_executor._backend

    async def close(self) -> None:
        """Clean up backend resources."""
        if self._prompt_executor is not None:
            await self._prompt_executor.close()
