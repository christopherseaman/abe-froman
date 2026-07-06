"""Tests for command presets — named interpreters for script nodes.

Real subprocesses, no mocks (project test doctrine). The dispatch
tests use `python3 -X dev` as the preset command: `-X dev` sets
`sys.flags.dev_mode == 1`, observable from inside the script and
distinct from the extension-map default (`python3` → dev_mode 0) —
so a passing assertion proves the *preset's* command ran.
"""
from __future__ import annotations

import pytest

from sqrlly.runtime.executor.dispatch import (
    DispatchExecutor,
    _assemble_command_argv,
)
from sqrlly.schema.models import (
    CommandPreset,
    Execute,
    Graph,
    LlmPreset,
    Node,
    Settings,
)


class TestAssembleCommandArgv:
    """Pure unit tests for the command-string → argv assembly."""

    def test_default_append_no_placeholders(self):
        assert _assemble_command_argv("uv run", "/abs/x.py", ["-a", "v"]) == [
            "uv", "run", "/abs/x.py", "-a", "v",
        ]

    def test_file_placeholder(self):
        assert _assemble_command_argv(
            "uv run {{file}}", "/abs/x.py", ["-a"],
        ) == ["uv", "run", "/abs/x.py", "-a"]

    def test_args_and_file_explicit_order(self):
        # pytest-style: args before the file path.
        assert _assemble_command_argv(
            "pytest {{args}} {{file}}", "/abs/t.py", ["-q", "--tb=short"],
        ) == ["pytest", "-q", "--tb=short", "/abs/t.py"]

    def test_multi_token_command(self):
        assert _assemble_command_argv(
            "python3 -X dev", "/abs/x.py", [],
        ) == ["python3", "-X", "dev", "/abs/x.py"]

    def test_both_placeholders_with_literal_between(self):
        assert _assemble_command_argv(
            "run {{file}} -- {{args}}", "/abs/x.py", ["a", "b"],
        ) == ["run", "/abs/x.py", "--", "a", "b"]


class TestCommandPresetDispatch:
    @pytest.mark.asyncio
    async def test_preset_command_runs_the_script(self, tmp_path):
        """The preset's command (python3 -X dev) is what runs the script —
        proven by dev_mode being 1, which the extension-map default would
        not produce."""
        script = tmp_path / "s.py"
        script.write_text("import sys; print('devmode', sys.flags.dev_mode)")
        settings = Settings(presets={
            "devpy": CommandPreset(command="python3 -X dev"),
        })
        ex = DispatchExecutor(workdir=str(tmp_path), settings=settings)
        node = Node(
            id="n", name="N",
            execute=Execute(url="s.py", params={"preset": "devpy"}),
        )
        result = await ex.execute(
            node, {}, workdir=str(tmp_path), settings_override=settings,
        )
        assert result.success, result.error
        # -X dev → dev_mode True; the extension-map default (python3,
        # no -X dev) would print "devmode False".
        assert "devmode True" in result.output

    @pytest.mark.asyncio
    async def test_args_passed_through(self, tmp_path):
        script = tmp_path / "s.py"
        script.write_text("import sys; print('argv', sys.argv[1:])")
        settings = Settings(presets={
            "py": CommandPreset(command="python3"),
        })
        ex = DispatchExecutor(workdir=str(tmp_path), settings=settings)
        node = Node(
            id="n", name="N",
            execute=Execute(
                url="s.py", params={"preset": "py", "args": ["--flag", "v"]},
            ),
        )
        result = await ex.execute(
            node, {}, workdir=str(tmp_path), settings_override=settings,
        )
        assert result.success, result.error
        assert "['--flag', 'v']" in result.output

    @pytest.mark.asyncio
    async def test_missing_command_reports_actionable_error(self, tmp_path):
        """A command preset naming a binary absent from PATH yields an
        actionable 'not found on PATH' error naming the command — not a
        bare errno string. A user without `uv` hits exactly this at the
        first script node."""
        script = tmp_path / "s.py"
        script.write_text("print('x')")
        settings = Settings(presets={
            "ghost": CommandPreset(command="sqrlly-no-such-binary-xyz"),
        })
        ex = DispatchExecutor(workdir=str(tmp_path), settings=settings)
        node = Node(
            id="n", name="N",
            execute=Execute(url="s.py", params={"preset": "ghost"}),
        )
        result = await ex.execute(
            node, {}, workdir=str(tmp_path), settings_override=settings,
        )
        assert not result.success
        assert "not found on PATH" in result.error
        assert "sqrlly-no-such-binary-xyz" in result.error

    @pytest.mark.asyncio
    async def test_file_placeholder_substitution(self, tmp_path):
        """{{file}} token places the path explicitly mid-command."""
        script = tmp_path / "s.py"
        script.write_text("print('ok')")
        settings = Settings(presets={
            "py": CommandPreset(command="python3 {{file}}"),
        })
        ex = DispatchExecutor(workdir=str(tmp_path), settings=settings)
        node = Node(
            id="n", name="N",
            execute=Execute(url="s.py", params={"preset": "py"}),
        )
        result = await ex.execute(
            node, {}, workdir=str(tmp_path), settings_override=settings,
        )
        assert result.success, result.error
        assert "ok" in result.output

    @pytest.mark.asyncio
    async def test_llm_preset_on_script_node_errors_clearly(self, tmp_path):
        """A script node referencing an LLM preset gets a clear dispatch
        error, not an opaque crash."""
        script = tmp_path / "s.py"
        script.write_text("print('x')")
        settings = Settings(presets={
            "smart": LlmPreset(
                transport="acp", provider="anthropic",
                model="sonnet", default=True,
            ),
        })
        ex = DispatchExecutor(workdir=str(tmp_path), settings=settings)
        node = Node(
            id="n", name="N",
            execute=Execute(url="s.py", params={"preset": "smart"}),
        )
        result = await ex.execute(
            node, {}, workdir=str(tmp_path), settings_override=settings,
        )
        assert not result.success
        assert "not a command preset" in result.error


class TestCommandPresetValidation:
    def test_command_preset_plus_execute_mode_rejected(self):
        """Q2 — a command preset reference and execute.mode are mutually
        exclusive; the command preset already specifies the interpreter."""
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="mutually exclusive"):
            Graph.model_validate({
                "name": "T", "version": "1.0",
                "settings": {
                    "presets": {
                        "uv": {"kind": "command", "command": "uv run"},
                    },
                },
                "nodes": [{
                    "id": "n", "name": "N",
                    "execute": {
                        "url": "s.py", "mode": "exec",
                        "params": {"preset": "uv"},
                    },
                }],
            })

    def test_command_preset_without_execute_mode_validates(self):
        """Control: same workflow minus execute.mode validates cleanly."""
        Graph.model_validate({
            "name": "T", "version": "1.0",
            "settings": {
                "presets": {
                    "uv": {"kind": "command", "command": "uv run"},
                },
            },
            "nodes": [{
                "id": "n", "name": "N",
                "execute": {"url": "s.py", "params": {"preset": "uv"}},
            }],
        })

    def test_command_only_workflow_needs_no_default(self):
        """A workflow with only command presets (no LLM presets) is valid
        without any default: true — default is an LLM-preset concept."""
        Graph.model_validate({
            "name": "T", "version": "1.0",
            "settings": {
                "presets": {
                    "uv": {"kind": "command", "command": "uv run"},
                    "deno": {"kind": "command", "command": "deno run"},
                },
            },
            "nodes": [{
                "id": "n", "name": "N",
                "execute": {"url": "s.py", "params": {"preset": "uv"}},
            }],
        })
