from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from sqrlly.runtime.executor.preset import resolve_preset_name
from sqrlly.runtime.executor.prompt import (
    apply_preamble,
    execute_with_downgrade,
    prepend_eval_preamble,
    render_template,
)
from sqrlly.runtime.result import ExecutionResult, PromptBackend
from sqrlly.runtime.url import fetch_url, resolve_url
from sqrlly.schema.models import CommandPreset, Execute, Node, Settings
from sqrlly.schema.params import coerce_params


# Literal tokens recognized inside a command preset's command string.
# Token-level (not string-interpolation) — a token equal to one of
# these is replaced; absent tokens fall back to default append.
_CMD_FILE_TOKEN = "{{file}}"
_CMD_ARGS_TOKEN = "{{args}}"


def _assemble_command_argv(
    command: str, local_path: str, rendered_args: list[str],
) -> list[str]:
    """Build the argv for a command-preset script dispatch.

    ``shlex.split`` the command string, then substitute placeholder
    tokens: a token equal to ``{{file}}`` → the resolved script path,
    ``{{args}}`` → the rendered args spliced in. If a placeholder is
    absent, that piece is appended at the end (file before args) —
    so ``"uv run"`` with no placeholders → ``uv run <path> <args>``,
    and ``"pytest {{args}} {{file}}"`` places them explicitly.
    """
    tokens = shlex.split(command)
    has_file = _CMD_FILE_TOKEN in tokens
    has_args = _CMD_ARGS_TOKEN in tokens
    argv: list[str] = []
    for tok in tokens:
        if tok == _CMD_FILE_TOKEN:
            argv.append(local_path)
        elif tok == _CMD_ARGS_TOKEN:
            argv.extend(rendered_args)
        else:
            argv.append(tok)
    if not has_file:
        argv.append(local_path)
    if not has_args:
        argv.extend(rendered_args)
    return argv

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
    """Routes execution by ``node.execute`` shape.

    - execute.url with prompt extension → _dispatch_prompt
    - execute.url with script extension → _dispatch_script
    - execute.url else (binary path) → _dispatch_binary
    - execute.type=join → no-op (topology marker)
    - execute.url with .yaml → never reached at runtime (compile-time only)
    - execute=None → no-op (gate-only by elision)

    Inline routes (``Node.route``) are dispatched at compile time via
    Command(goto=...) and never reach this executor.
    """

    def __init__(
        self,
        workdir: str = ".",
        prompt_backends: dict[str, PromptBackend] | None = None,
        settings: Settings | None = None,
        prompt_backend_builders: (
            dict[str, Callable[[], PromptBackend]] | None
        ) = None,
    ):
        """Construct a DispatchExecutor.

        Both arguments register preset-name → backend; passing neither
        (the default) is valid — script / binary / join dispatch all
        still work and prompt dispatch raises a clear error.

        ``prompt_backends`` registers already-built backends (used by
        tests / embedders that construct backends directly).
        ``prompt_backend_builders`` registers zero-arg builders that are
        invoked lazily on first dispatch to a preset: a declared-but-unused
        preset never builds its backend, so an optional dependency (e.g.
        the ``acp`` package) is only imported when a node actually
        dispatches to that preset, and a missing dependency surfaces at the
        dispatching node rather than at startup. The CLI passes builders.
        """
        self._workdir = workdir
        self._settings = settings or Settings()
        self._fetch_cache: dict[str, bytes] = {}
        # Concurrency cap on dispatch — bounds how many nodes spawn a backend
        # at once. Owned HERE (not the foreman) so it applies even off-git,
        # where the foreman is disabled; otherwise a non-git fan-out would
        # dispatch every child simultaneously and saturate the upstream API.
        self._dispatch_sem = asyncio.Semaphore(self._settings.max_parallel_jobs)
        # Builder registry: every known preset name → a zero-arg builder.
        # Pre-built backends are wrapped as constant builders so both
        # inputs share one lazy code path (identity preserved).
        builders: dict[str, Callable[[], PromptBackend]] = dict(
            prompt_backend_builders or {}
        )
        for name, backend in (prompt_backends or {}).items():
            builders[name] = lambda b=backend: b
        self._prompt_backend_builders = builders
        # Lazy cache: a preset's backend is built on first resolve.
        self._backends: dict[str, PromptBackend] = {}

    def _backend_for_preset(self, name: str) -> PromptBackend:
        """Build (once) and cache the PromptBackend for a registered preset.

        The backend builder runs here — on first dispatch to ``name`` — not
        at construction, so unused presets never materialize a backend.
        """
        cached = self._backends.get(name)
        if cached is not None:
            return cached
        backend = self._prompt_backend_builders[name]()
        self._backends[name] = backend
        return backend

    def _resolve_prompt_backend(
        self, node: Node, settings: Settings,
    ) -> tuple[str, PromptBackend] | None:
        """Resolve a node's ``(preset_name, backend)`` for prompt dispatch.

        Returns ``None`` when no presets are registered (caller refuses
        prompt dispatch). Raises ``RuntimeError`` if the resolved preset
        has no registered backend (CLI wiring bug — schema validation
        should have caught name typos before this point).
        """
        if not self._prompt_backend_builders:
            return None
        preset_name = resolve_preset_name(node, settings)
        if preset_name not in self._prompt_backend_builders:
            raise RuntimeError(
                f"Node {node.id!r} resolves to preset {preset_name!r}, "
                f"but no backend is registered for it. Registry contains: "
                f"{sorted(self._prompt_backend_builders)}. CLI wiring bug."
            )
        return preset_name, self._backend_for_preset(preset_name)

    async def execute(
        self, node: Node, context: dict[str, Any],
        workdir: str | None = None,
        settings_override: Settings | None = None,
    ) -> ExecutionResult:
        # The dispatch concurrency cap is the ONLY throttle when the foreman
        # is absent (off-git). Under the foreman it is the sole dispatch cap —
        # the foreman gates worktree CREATION only, so a git child never holds
        # two semaphores (no double-count / halving).
        async with self._dispatch_sem:
            return await self._dispatch(node, context, workdir, settings_override)

    async def _dispatch(
        self, node: Node, context: dict[str, Any],
        workdir: str | None = None,
        settings_override: Settings | None = None,
    ) -> ExecutionResult:
        s = settings_override or self._settings

        if node.execute is None:
            # Gate-only-by-elision: a node with `evaluation:` and no
            # `execute:` block runs the gate against an empty output.
            return ExecutionResult(success=True, output=f"[gate-only] {node.id}")

        execute = node.execute

        if execute.type == "join":
            return ExecutionResult(success=True, output="")

        # URL mode
        effective_workdir = workdir or self._workdir
        resolved = resolve_url(execute.url, s.base_url, effective_workdir)

        # Subgraphs are dispatched at compile time (not here). If we see a
        # .yaml URL or a forced-subgraph mode, the compile layer missed it.
        ext = Path(urlsplit(resolved).path).suffix.lower()
        if ext in {".yaml", ".yml"} or execute.mode == "subgraph":
            return ExecutionResult(
                success=False,
                error=(
                    f"Subgraph URL {execute.url!r} on node '{node.id}' should "
                    f"have been wired at compile time, not dispatched at runtime"
                ),
            )

        # Per-mode params validation: catches typos like `args:` on a prompt URL.
        # Honors `execute.mode:` so a forced override picks the right params
        # shape (e.g. `mode: exec` on an .md path → SubprocessParams, not
        # PromptParams).
        try:
            params = coerce_params(resolved, execute.params, mode=execute.mode)
        except Exception as e:
            return ExecutionResult(
                success=False,
                error=f"Node '{node.id}' params invalid for {resolved}: {e}",
            )

        # Mode override → forced dispatch. Otherwise route by URL extension.
        if execute.mode == "prompt" or (execute.mode is None and ext in _PROMPT_EXTS):
            return await self._dispatch_prompt(
                node, resolved, params, context, effective_workdir,
                settings=s,
            )
        if execute.mode is None and ext in _SCRIPT_INTERPRETERS:
            return await self._dispatch_script(
                node, resolved, params, context, effective_workdir,
                settings=s,
            )
        # mode=="exec" or extension-driven fallthrough.
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
        *,
        settings: Settings,
    ) -> ExecutionResult:
        """Read prompt body (file or remote), render Jinja, send to backend."""
        resolved_backend = self._resolve_prompt_backend(node, settings)
        if resolved_backend is None:
            raise RuntimeError(
                f"Cannot dispatch prompt node {node.id!r}: no prompt "
                f"backend wired. DispatchExecutor was constructed without "
                f"a prompt_backend or prompt_backends; pass one or run via "
                f"the CLI which auto-detects from settings.presets / env."
            )
        preset_name, backend = resolved_backend

        try:
            body = fetch_url(resolved, settings, self._fetch_cache).decode()
        except Exception as e:
            return ExecutionResult(
                success=False,
                error=f"Failed to fetch prompt {resolved!r}: {e}",
            )

        try:
            # Preamble lives with the config (base workdir), not the
            # per-node worktree — pass the executor's BASE workdir.
            applied = apply_preamble(
                body, settings=settings, base_workdir=self._workdir,
            )
        except FileNotFoundError as e:
            return ExecutionResult(
                success=False,
                error=f"Preamble file not found: {e}",
            )
        rendered = render_template(applied, context)
        rendered = prepend_eval_preamble(rendered, context)

        # The resolved preset's model. The registry is always populated
        # (schema validation caught name typos before this point).
        current_model = settings.presets[preset_name].model
        params_timeout = getattr(params, "timeout", None)
        timeout = (
            params_timeout if params_timeout is not None
            else node.effective_timeout(settings)
        )
        result = await execute_with_downgrade(
            backend, rendered, current_model, workdir, timeout=timeout,
            settings=settings,
        )
        # Record which preset/model ran this node for the JSONL log.
        # `execute_rendered` may set `result.model` to a downgraded tier
        # on OverloadError; fall back to the configured model otherwise.
        result.preset = preset_name
        if result.model is None:
            result.model = current_model
        return result

    async def _dispatch_script(
        self,
        node: Node,
        resolved: str,
        params: Any,
        context: dict[str, Any],
        workdir: str,
        *,
        settings: Settings,
    ) -> ExecutionResult:
        """Run a script under its interpreter.

        Two interpreter sources:
          - ``params.preset`` naming a command preset → the preset's
            command string (assembled via ``_assemble_command_argv``).
          - Otherwise the URL-extension map (``_SCRIPT_INTERPRETERS``).
            For an arbitrary interpreter (a specific venv, ``uv run``,
            etc.) name a command preset — it is the flexible path.
        """
        scheme = urlsplit(resolved).scheme
        if scheme != "file":
            # Remote script handoff (fetch → temp dir → chmod → run)
            # is deferred to a separate commit; today only file:// works.
            return ExecutionResult(
                success=False,
                error=(
                    f"Remote script execution not yet wired (URL: {resolved}). "
                    f"Use file:// for now."
                ),
            )
        local_path = urlsplit(resolved).path

        # Command-preset path: params.preset names a command preset.
        preset_ref = getattr(params, "preset", None)
        if preset_ref is not None:
            preset = settings.presets.get(preset_ref)
            if not isinstance(preset, CommandPreset):
                return ExecutionResult(
                    success=False,
                    error=(
                        f"Node {node.id!r}: params.preset={preset_ref!r} is "
                        f"not a command preset (kind: command) — script "
                        f"dispatch needs a command preset for interpreter "
                        f"selection."
                    ),
                )
            rendered_args = [render_template(a, context) for a in params.args]
            rendered_env = {
                k: render_template(v, context) for k, v in params.env.items()
            }
            argv = _assemble_command_argv(
                preset.command, local_path, rendered_args,
            )
            return await self._exec_argv(argv, rendered_env, workdir)

        # Extension-map path.
        ext = Path(urlsplit(resolved).path).suffix.lower()
        interpreter = _SCRIPT_INTERPRETERS[ext]
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
        """Extension-map / binary subprocess runner.

        ``params.args`` is Jinja-rendered against ``context`` so authors
        can wire dep outputs into args, then appended after ``cmd_prefix``.
        ``params.env`` is rendered the same way and merged onto the
        parent env.
        """
        rendered_args = [render_template(a, context) for a in params.args]
        rendered_env = {
            k: render_template(v, context) for k, v in params.env.items()
        }
        return await self._exec_argv(
            [*cmd_prefix, *rendered_args], rendered_env, workdir,
        )

    async def _exec_argv(
        self, argv: list[str], rendered_env: dict[str, str], workdir: str,
    ) -> ExecutionResult:
        """Run a fully-assembled argv as a subprocess; capture stdout/stderr.

        Shared exec core for extension-map dispatch (``_run_subprocess``)
        and command-preset dispatch (``_dispatch_script``).
        """
        env = {**os.environ, **rendered_env} if rendered_env else None
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
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
        except FileNotFoundError as e:
            # The interpreter/command/binary isn't on PATH. Name it and say
            # so plainly — a bare errno ("[Errno 2] ...") reads as a bug, not
            # a missing tool. (e.g. a workflow whose script nodes run under
            # `uv` fails here at the first node if `uv` isn't installed.)
            missing = e.filename or (argv[0] if argv else "?")
            return ExecutionResult(
                success=False,
                error=(
                    f"Command not found on PATH: {missing!r}. Install it or "
                    f"correct the node's url/preset command."
                ),
            )
        except OSError as e:
            return ExecutionResult(success=False, error=str(e))

    def get_backend(self) -> PromptBackend | None:
        """Return the default preset's PromptBackend for LLM-gate
        dispatch (gates aren't keyed by per-node preset).

        Returns ``None`` when no presets are registered. The schema
        validator guarantees exactly one ``default: true`` preset when
        ``settings.presets`` is non-empty, so the linear scan is safe.
        Builds (lazily) only the default preset's backend — never a
        non-default one.
        """
        if not self._prompt_backend_builders:
            return None
        for name, preset in self._settings.presets.items():
            # CommandPresets carry no `default` flag; a mixed registry
            # (LlmPreset gates + CommandPreset script dispatch) must skip
            # them without AttributeError, regardless of insertion order.
            if getattr(preset, "default", False) and name in self._prompt_backend_builders:
                return self._backend_for_preset(name)
        # Defensive: registry/settings out-of-sync (shouldn't happen).
        first_name = sorted(self._prompt_backend_builders)[0]
        return self._backend_for_preset(first_name)

    async def close(self) -> None:
        """Close every PromptBackend that was actually built. Lazily-unbuilt
        presets never opened a process/handle, so there is nothing to close."""
        for backend in self._backends.values():
            await backend.close()
