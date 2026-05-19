from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqrlly.runtime.executor.preset import resolve_preset_name
from sqrlly.runtime.executor.prompt import (
    PromptExecutor,
    prepend_eval_preamble,
    render_template,
)
from sqrlly.runtime.result import ExecutionResult, PromptBackend
from sqrlly.runtime.url import _RemoteFetchCache, fetch_url, resolve_url
from sqrlly.schema.models import Execute, Node, Settings
from sqrlly.schema.params import coerce_params

# Script extension → interpreter prefix. URL → subprocess args via map +
# resolved local path. Stays small; new languages add one row.
_SCRIPT_INTERPRETERS: dict[str, list[str]] = {
    ".py": ["python3"],
    ".js": ["node"],
    ".mjs": ["node"],
    ".ts": ["tsx"],
    ".sh": ["bash"],
}

# Mode-name → interpreter prefix for `execute.mode:` overrides. Lets
# authors force script dispatch when the URL has no extension or a
# misleading one (e.g. `mode: python` on `scripts/run-thing`).
_MODE_INTERPRETERS: dict[str, list[str]] = {
    "python": ["python3"],
    "node": ["node"],
    "tsx": ["tsx"],
    "bash": ["bash"],
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
        prompt_backend: PromptBackend | None = None,
        prompt_backends: dict[str, PromptBackend] | None = None,
        settings: Settings | None = None,
    ):
        """Construct a DispatchExecutor.

        Backend wiring (mutually exclusive — pass at most one):
          - ``prompt_backend``: single-backend convenience for tests
            and the legacy ``settings.executor:`` path. Stored
            internally under the synthetic preset name ``_legacy``.
          - ``prompt_backends``: preset-name → backend dict. Used
            when ``settings.presets:`` declares named presets;
            each preset gets its own backend instance.

        Passing neither is valid — dispatch will refuse prompt nodes
        with a clear error (script / binary / join dispatch still work).
        """
        if prompt_backend is not None and prompt_backends is not None:
            raise ValueError(
                "DispatchExecutor: pass either prompt_backend (single) or "
                "prompt_backends (dict), not both"
            )

        self._workdir = workdir
        self._settings = settings or Settings()
        self._fetch_cache = _RemoteFetchCache()

        # Normalize to internal registry (one PromptExecutor per backend).
        # The ``_legacy`` synthetic name covers the single-backend path so
        # downstream resolution always looks up by name without branching.
        if prompt_backend is not None:
            self._prompt_executors: dict[str, PromptExecutor] = {
                "_legacy": PromptExecutor(
                    backend=prompt_backend,
                    settings=self._settings,
                    workdir=workdir,
                ),
            }
        elif prompt_backends is not None:
            self._prompt_executors = {
                name: PromptExecutor(
                    backend=backend,
                    settings=self._settings,
                    workdir=workdir,
                )
                for name, backend in prompt_backends.items()
            }
        else:
            self._prompt_executors = {}

    def _resolve_prompt_executor(
        self, node: Node, settings: Settings,
    ) -> PromptExecutor | None:
        """Pick the PromptExecutor for a node based on its preset.

        Resolution:
          - No executors registered → None (caller refuses prompt dispatch).
          - Single ``_legacy`` executor → return it (single-backend mode).
          - Multi-preset → ``resolve_preset_name(node, settings)`` →
            look up by name. Raises if the named preset has no
            corresponding backend in the registry (a CLI wiring bug).
        """
        if not self._prompt_executors:
            return None
        if "_legacy" in self._prompt_executors and len(self._prompt_executors) == 1:
            return self._prompt_executors["_legacy"]
        preset_name = resolve_preset_name(node, settings)
        if preset_name not in self._prompt_executors:
            raise RuntimeError(
                f"Node {node.id!r} resolves to preset {preset_name!r}, "
                f"but no backend is registered for it. Registry contains: "
                f"{sorted(self._prompt_executors)}. CLI wiring bug."
            )
        return self._prompt_executors[preset_name]

    async def execute(
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
        if execute.mode in _MODE_INTERPRETERS:
            return await self._dispatch_script(
                node, resolved, params, context, effective_workdir,
                interpreter=_MODE_INTERPRETERS[execute.mode],
            )
        if execute.mode is None and ext in _SCRIPT_INTERPRETERS:
            return await self._dispatch_script(
                node, resolved, params, context, effective_workdir,
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
        prompt_executor = self._resolve_prompt_executor(node, settings)
        if prompt_executor is None:
            raise RuntimeError(
                f"Cannot dispatch prompt node {node.id!r}: no prompt "
                f"backend wired. DispatchExecutor was constructed without "
                f"a prompt_backend or prompt_backends; pass one or run via "
                f"the CLI which auto-detects from settings.presets / env."
            )

        try:
            body = fetch_url(resolved, settings, self._fetch_cache).decode()
        except Exception as e:
            return ExecutionResult(
                success=False,
                error=f"Failed to fetch prompt {resolved!r}: {e}",
            )

        try:
            applied = prompt_executor.apply_preamble(body, settings=settings)
        except FileNotFoundError as e:
            return ExecutionResult(
                success=False,
                error=f"Preamble file not found: {e}",
            )
        rendered = render_template(applied, context)
        rendered = prepend_eval_preamble(rendered, context)

        # Model resolution (explicit-None tests, not `or`, so authored
        # zero/empty values win over the next-lower fallback):
        #   PromptParams.model > Node.model > preset.model (when presets
        #   declared) > Settings.default_model (legacy fallback).
        params_model = getattr(params, "model", None)
        if params_model is not None:
            current_model = params_model
        elif node.model is not None:
            current_model = node.model
        elif settings.presets:
            preset_name = resolve_preset_name(node, settings)
            current_model = settings.presets[preset_name].model
        else:
            current_model = settings.default_model
        params_timeout = getattr(params, "timeout", None)
        timeout = (
            params_timeout if params_timeout is not None
            else node.effective_timeout(settings)
        )
        return await prompt_executor.execute_rendered(
            rendered, current_model, workdir, timeout=timeout,
            settings=settings,
        )

    async def _dispatch_script(
        self,
        node: Node,
        resolved: str,
        params: Any,
        context: dict[str, Any],
        workdir: str,
        *,
        interpreter: list[str] | None = None,
    ) -> ExecutionResult:
        """Run a script under its interpreter (python3 / node / bash / tsx).

        ``interpreter`` overrides the URL-extension lookup — used when
        ``execute.mode:`` forces a specific interpreter regardless of suffix.
        """
        if interpreter is None:
            ext = Path(urlsplit(resolved).path).suffix.lower()
            interpreter = _SCRIPT_INTERPRETERS[ext]
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
        """Return a PromptBackend for LLM-gate dispatch.

        Multi-preset workflows still need a single backend to evaluate
        LLM gates (which aren't keyed by per-node preset). Resolution:
          - No executors registered → None.
          - Single-backend path → the only backend.
          - Multi-preset path → the backend bound to the preset marked
            ``default: true`` in ``Settings.presets``. If somehow no
            default is resolvable (shouldn't happen — schema validator
            blocks it), returns any backend deterministically.
        """
        if not self._prompt_executors:
            return None
        if "_legacy" in self._prompt_executors and len(self._prompt_executors) == 1:
            return self._prompt_executors["_legacy"]._backend
        for name, preset in self._settings.presets.items():
            if preset.default and name in self._prompt_executors:
                return self._prompt_executors[name]._backend
        # Fallback: any backend. Deterministic via sorted key order.
        first_name = sorted(self._prompt_executors)[0]
        return self._prompt_executors[first_name]._backend

    async def close(self) -> None:
        """Close every PromptBackend in the registry."""
        for executor in self._prompt_executors.values():
            await executor.close()
