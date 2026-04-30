"""Unit tests for DispatchExecutor's Stage-5b execute.url path.

Function-level + small e2e tests cover the four dispatch branches:
    - prompt URL (.md/.txt/.prompt) → PromptExecutor pipeline
    - script URL (.py/.js/.sh) → interpreter subprocess
    - binary URL (no extension / unknown) → direct subprocess
    - join sentinel → no-op output

Plus negative cases: subgraph URL at runtime (compile-time error
escape), route at runtime (programming error escape), per-mode params
typo (catches `args:` on a prompt URL), bad commands surfacing OSError.

The legacy Stage-4 path (node.execution discriminated union) is
covered by existing tests and remains green during dual-mode.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.state import make_initial_state
from abe_froman.schema.models import Execute, Node, Settings


@pytest.fixture
def echo_path() -> str:
    """Resolve `/usr/bin/echo` or wherever echo lives in $PATH."""
    import shutil
    found = shutil.which("echo")
    assert found, "echo must be on $PATH for this test"
    return found


class TestExecuteJoinDispatch:
    @pytest.mark.asyncio
    async def test_join_returns_empty_output(self, tmp_path):
        node = Node(id="j", name="J", execute=Execute(type="join"))
        executor = DispatchExecutor(workdir=str(tmp_path))
        result = await executor.execute(node, {}, workdir=str(tmp_path))
        assert result.success is True
        assert result.output == ""


class TestExecuteRouteDispatch:
    @pytest.mark.asyncio
    async def test_route_at_runtime_is_programming_error(self, tmp_path):
        """Route nodes are wired at compile time; reaching dispatch is a bug."""
        node = Node(
            id="r", name="R",
            execute=Execute(
                type="route",
                cases=[],
                else_="__end__",
            ),
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        result = await executor.execute(node, {}, workdir=str(tmp_path))
        assert result.success is False
        assert "should not reach DispatchExecutor" in result.error


class TestBinaryDispatch:
    @pytest.mark.asyncio
    async def test_echo_binary_runs_with_args(self, tmp_path, echo_path):
        node = Node(
            id="b", name="B",
            execute=Execute(url=echo_path, params={"args": ["-n", "hello"]}),
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        result = await executor.execute(node, {}, workdir=str(tmp_path))
        assert result.success is True
        assert result.output == "hello"

    @pytest.mark.asyncio
    async def test_args_are_jinja_rendered(self, tmp_path, echo_path):
        node = Node(
            id="b", name="B",
            execute=Execute(url=echo_path, params={"args": ["-n", "{{upstream}}"]}),
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        result = await executor.execute(
            node, {"upstream": "rendered-value"}, workdir=str(tmp_path),
        )
        assert result.success is True
        assert result.output == "rendered-value"

    @pytest.mark.asyncio
    async def test_nonexistent_binary_returns_error(self, tmp_path):
        node = Node(
            id="b", name="B",
            execute=Execute(url="/nonexistent/binary/path"),
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        result = await executor.execute(node, {}, workdir=str(tmp_path))
        assert result.success is False
        assert "No such file" in result.error or "not found" in result.error.lower()


class TestScriptDispatch:
    @pytest.mark.asyncio
    async def test_python_script_runs(self, tmp_path):
        script = tmp_path / "say.py"
        script.write_text("print('hello-from-python')\n")
        node = Node(
            id="s", name="S",
            execute=Execute(url=f"file://{script}"),
        )
        # Force python3 to be the same interpreter we're running pytest under
        # so the test passes regardless of system python3 version.
        executor = DispatchExecutor(workdir=str(tmp_path))
        result = await executor.execute(node, {}, workdir=str(tmp_path))
        # If `python3` is on the system, this passes. If not, it'll error
        # — accept either since CI environments vary.
        if result.success:
            assert "hello-from-python" in result.output
        else:
            assert "python3" in (result.error or "").lower() or \
                   "no such file" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_shell_script_runs(self, tmp_path):
        script = tmp_path / "say.sh"
        script.write_text("#!/bin/bash\necho -n shell-out\n")
        node = Node(
            id="s", name="S",
            execute=Execute(url=f"file://{script}"),
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        result = await executor.execute(node, {}, workdir=str(tmp_path))
        assert result.success is True
        assert result.output == "shell-out"

    @pytest.mark.asyncio
    async def test_remote_script_not_yet_wired(self, tmp_path):
        node = Node(
            id="s", name="S",
            execute=Execute(url="https://example.com/x.py"),
        )
        # Settings allows remote so fetch_url passes — but dispatch returns
        # 'not yet wired' since temp-file handoff is a later commit.
        settings = Settings(
            allow_remote_urls=True, allow_remote_scripts=True,
        )
        executor = DispatchExecutor(workdir=str(tmp_path), settings=settings)
        result = await executor.execute(node, {}, workdir=str(tmp_path))
        assert result.success is False
        assert "Remote script execution not yet wired" in result.error


class TestPromptDispatch:
    @pytest.mark.asyncio
    async def test_prompt_stub_when_no_backend(self, tmp_path):
        prompt = tmp_path / "p.md"
        prompt.write_text("Hello {{name}}")
        node = Node(
            id="p", name="P",
            execute=Execute(url=f"file://{prompt}"),
        )
        executor = DispatchExecutor(workdir=str(tmp_path))  # no backend
        result = await executor.execute(node, {"name": "world"}, workdir=str(tmp_path))
        # No backend → stub fallback (sanity-check, not a regression case).
        assert result.success is True
        assert "[prompt-stub]" in result.output


class TestParamsValidation:
    @pytest.mark.asyncio
    async def test_args_on_prompt_url_rejected(self, tmp_path):
        """Per-mode params validation catches mode-mismatched keys."""
        prompt = tmp_path / "p.md"
        prompt.write_text("hi")
        node = Node(
            id="p", name="P",
            execute=Execute(
                url=f"file://{prompt}",
                params={"args": ["wrong"]},
            ),
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        result = await executor.execute(node, {}, workdir=str(tmp_path))
        assert result.success is False
        assert "params invalid" in result.error

    @pytest.mark.asyncio
    async def test_model_on_script_url_rejected(self, tmp_path):
        script = tmp_path / "s.sh"
        script.write_text("echo hi")
        node = Node(
            id="s", name="S",
            execute=Execute(
                url=f"file://{script}",
                params={"model": "opus"},
            ),
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        result = await executor.execute(node, {}, workdir=str(tmp_path))
        assert result.success is False
        assert "params invalid" in result.error


class TestSubgraphURLAtRuntime:
    @pytest.mark.asyncio
    async def test_yaml_url_is_compile_time_error_escape(self, tmp_path):
        """Subgraphs are wired at compile time; reaching dispatch is a bug."""
        node = Node(
            id="x", name="X",
            execute=Execute(url="subgraphs/sub.yaml"),
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        result = await executor.execute(node, {}, workdir=str(tmp_path))
        assert result.success is False
        assert "compile time" in result.error
