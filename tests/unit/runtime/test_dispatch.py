"""Unit tests for DispatchExecutor's Stage-5b execute.url path.

Function-level + small e2e tests cover the four dispatch branches:
    - prompt URL (.md/.txt/.prompt) → prompt pipeline
    - script URL (.py/.js/.sh) → interpreter subprocess
    - binary URL (no extension / unknown) → direct subprocess
    - join sentinel → no-op output

Plus negative cases: subgraph URL at runtime (compile-time error
escape), per-mode params typo (catches `args:` on a prompt URL), bad
commands surfacing OSError. Route is dispatched at compile time only
(via Command(goto=)) and never reaches the runtime dispatcher.

The legacy Stage-4 path (node.execution discriminated union) is
covered by existing tests and remains green during dual-mode.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from sqrlly.runtime.executor.dispatch import DispatchExecutor
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Execute, Node, Settings


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
    async def test_python_script_runs(self, tmp_path, monkeypatch):
        """Python interpreter resolves to the test's own sys.executable
        (guaranteed available); script output is asserted unconditionally."""
        script = tmp_path / "say.py"
        script.write_text("print('hello-from-python')\n")
        node = Node(
            id="s", name="S",
            execute=Execute(url=f"file://{script}"),
        )
        # Pin the interpreter to the running pytest's sys.executable so
        # the test doesn't depend on a system-installed python3.
        from sqrlly.runtime.executor import dispatch
        monkeypatch.setitem(
            dispatch._SCRIPT_INTERPRETERS, ".py", [sys.executable]
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        result = await executor.execute(node, {}, workdir=str(tmp_path))
        assert result.success is True, result.error
        assert "hello-from-python" in result.output

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
        settings = Settings(allow_remote_urls=True)
        executor = DispatchExecutor(workdir=str(tmp_path), settings=settings)
        result = await executor.execute(node, {}, workdir=str(tmp_path))
        assert result.success is False
        assert "Remote script execution not yet wired" in result.error


class TestPromptDispatch:
    @pytest.mark.asyncio
    async def test_prompt_without_backend_raises(self, tmp_path):
        """Post-stub-removal: a DispatchExecutor with no prompt_backend
        cannot dispatch prompt URLs. The dispatcher raises a clear
        RuntimeError rather than emitting fake output."""
        prompt = tmp_path / "p.md"
        prompt.write_text("Hello {{name}}")
        node = Node(
            id="p", name="P",
            execute=Execute(url=f"file://{prompt}"),
        )
        executor = DispatchExecutor(workdir=str(tmp_path))  # no backend
        with pytest.raises(RuntimeError) as ei:
            await executor.execute(node, {"name": "world"}, workdir=str(tmp_path))
        assert "no prompt backend" in str(ei.value).lower()
        assert "p" in str(ei.value)  # node id surfaced for debuggability


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


class TestExecuteModeOverride:
    """`execute.mode:` forces a dispatch handler regardless of URL extension."""

    @pytest.mark.asyncio
    async def test_prompt_mode_routes_unknown_extension_through_prompt(self, tmp_path):
        """A URL with `.foo` suffix runs through the prompt pipeline when mode=prompt.

        Verification signal: with no backend wired, the prompt branch
        raises the no-backend RuntimeError. Other branches return
        ExecutionResult(success=False, ...) instead — so the raise IS
        the proof that mode=prompt routed this `.foo` URL through the
        prompt dispatcher rather than the binary/script fallback.
        """
        body = tmp_path / "instructions.foo"
        body.write_text("hello {{name}}")
        node = Node(
            id="p", name="P",
            execute=Execute(url=f"file://{body}", mode="prompt"),
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        with pytest.raises(RuntimeError) as ei:
            await executor.execute(node, {"name": "world"}, workdir=str(tmp_path))
        assert "no prompt backend" in str(ei.value).lower()

    @pytest.mark.asyncio
    async def test_exec_mode_overrides_md_extension(self, tmp_path):
        """`mode: exec` runs an .md path as a binary instead of as a prompt.

        Authors a tiny shell script at `looks-like-prompt.md` to prove the
        extension is ignored; without `mode: exec`, the `.md` suffix would
        send the file through PromptBackend.
        """
        fake = tmp_path / "looks-like-prompt.md"
        fake.write_text("#!/bin/sh\necho from-exec-mode-$1\n")
        fake.chmod(0o755)
        node = Node(
            id="b", name="B",
            execute=Execute(
                url=f"file://{fake}",
                mode="exec",
                params={"args": ["forced"]},
            ),
        )
        executor = DispatchExecutor(workdir=str(tmp_path))
        result = await executor.execute(node, {}, workdir=str(tmp_path))
        assert result.success is True
        assert "from-exec-mode-forced" in result.output


class TestDispatchConcurrencyCap:
    """max_parallel_jobs is enforced by DispatchExecutor itself, so the cap
    applies even when there is no ForemanExecutor — i.e. off-git, where the
    foreman is disabled. Regression for the fan-out saturation blocker: a
    non-git fan-out previously dispatched every child at once."""

    @pytest.mark.asyncio
    async def test_cap_throttles_without_foreman(self, tmp_path):
        import asyncio
        import shutil
        import time

        sleep_bin = shutil.which("sleep") or "/bin/sleep"
        sleep_s = 0.3
        dispatch = DispatchExecutor(
            workdir=str(tmp_path), settings=Settings(max_parallel_jobs=2),
        )

        def _node(i):
            return Node(
                id=f"p{i}", name=f"p{i}",
                execute=Execute(url=sleep_bin, params={"args": [str(sleep_s)]}),
            )

        start = time.perf_counter()
        results = await asyncio.gather(*[dispatch.execute(_node(i), {}) for i in range(4)])
        elapsed = time.perf_counter() - start

        assert all(r.success for r in results), [r.error for r in results]
        # cap=2 over 4 jobs → two serialized waves ≈ 2×sleep. Uncapped (the
        # bug) runs all four at once ≈ 1×sleep. 1.5× cleanly separates them.
        assert elapsed >= sleep_s * 1.5, (
            f"elapsed {elapsed:.3f}s < {sleep_s * 1.5:.3f}s — cap not enforced"
        )

    @pytest.mark.asyncio
    async def test_high_cap_runs_parallel(self, tmp_path):
        """Control: a cap >= job count runs fully parallel (~1 sleep)."""
        import asyncio
        import shutil
        import time

        sleep_bin = shutil.which("sleep") or "/bin/sleep"
        sleep_s = 0.3
        dispatch = DispatchExecutor(
            workdir=str(tmp_path), settings=Settings(max_parallel_jobs=8),
        )

        def _node(i):
            return Node(
                id=f"p{i}", name=f"p{i}",
                execute=Execute(url=sleep_bin, params={"args": [str(sleep_s)]}),
            )

        start = time.perf_counter()
        await asyncio.gather(*[dispatch.execute(_node(i), {}) for i in range(4)])
        elapsed = time.perf_counter() - start
        assert elapsed < sleep_s * 2, f"elapsed {elapsed:.3f}s too long for cap=8"
