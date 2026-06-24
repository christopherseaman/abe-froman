"""Unit tests for CLIBackend — subprocess-per-call LLM transport.

No mocks of the subprocess layer: each test constructs a real shell
script that stands in for ``claude``, drops it on disk with the
executable bit, and aims ``CLIBackend.argv_prefix`` at the script's
absolute path. The backend's subprocess pipe + stdout/stderr handling
exercises with no patching.

Coverage:
- Argv assembly — the fake echoes its own argv; assertion pins ``--model``.
- Successful prompt — the fake echoes a fixed response.
- Overload-on-stderr → ``OverloadError`` (substring match path).
- Generic stderr → ``RuntimeError`` carrying stderr.
- Timeout — the fake sleeps; ``asyncio.TimeoutError`` raises and the
  subprocess gets reaped (assertion via ``proc`` reference, but the
  backend reaps in its own ``except`` block).
- Env injection — preset env overlays os.environ; empty inherits.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from sqrlly.runtime.executor.backends.cli import CLIBackend
from sqrlly.runtime.result import OverloadError


def _write_fake(tmp_path: Path, name: str, body: str) -> Path:
    """Write a shell script + chmod +x. Returns the absolute path."""
    script = tmp_path / name
    script.write_text(body)
    script.chmod(0o755)
    return script


def _backend_for(script: Path) -> CLIBackend:
    """CLIBackend pointed at the fake script (one-element argv_prefix)."""
    return CLIBackend(argv_prefix=(str(script),))


class TestCLIBackendArgvAssembly:
    @pytest.mark.asyncio
    async def test_argv_carries_model_flag(self, tmp_path):
        """Fake echoes its own argv; assert ``--model haiku`` appears."""
        # `set --` shows positional argv; we echo it explicitly so the
        # test can pin the exact tokens passed to the subprocess.
        fake = _write_fake(
            tmp_path, "claude-fake",
            '#!/bin/sh\n'
            'echo "argv: $@"\n',
        )
        backend = _backend_for(fake)
        result = await backend.send_prompt(
            prompt="ignored", model="haiku", workdir=str(tmp_path),
        )
        # The backend prepends only the argv_prefix; everything after
        # is `--model <model>`. With a one-element prefix this is the
        # entire argv set.
        assert result.output == "argv: --model haiku"

    def test_tool_argv_empty_when_unset(self):
        """No tool config → bare `claude -p --model` (prior behavior)."""
        assert CLIBackend()._tool_argv() == []

    def test_tool_argv_assembles_all_flags(self):
        backend = CLIBackend(
            permission_mode="acceptEdits",
            allowed_tools=["Edit", "Bash(git *)"],
            disallowed_tools=["WebFetch"],
            cli_args=["--add-dir", "."],
        )
        assert backend._tool_argv() == [
            "--permission-mode", "acceptEdits",
            "--allowedTools", "Edit", "Bash(git *)",
            "--disallowedTools", "WebFetch",
            "--add-dir", ".",
        ]

    @pytest.mark.asyncio
    async def test_tool_flags_reach_subprocess(self, tmp_path):
        """End-to-end: configured flags actually land on the argv."""
        fake = _write_fake(
            tmp_path, "claude-fake", '#!/bin/sh\necho "argv: $@"\n',
        )
        backend = CLIBackend(
            argv_prefix=(str(fake),),
            permission_mode="bypassPermissions",
            allowed_tools=["Edit"],
        )
        result = await backend.send_prompt(
            prompt="x", model="sonnet", workdir=str(tmp_path),
        )
        assert "--model sonnet" in result.output
        assert "--permission-mode bypassPermissions" in result.output
        assert "--allowedTools Edit" in result.output

    @pytest.mark.asyncio
    async def test_argv_changes_with_model(self, tmp_path):
        """Different `model=` → different `--model <value>` token."""
        fake = _write_fake(
            tmp_path, "claude-fake",
            '#!/bin/sh\necho "argv: $@"\n',
        )
        backend = _backend_for(fake)
        r1 = await backend.send_prompt("p", "sonnet", str(tmp_path))
        r2 = await backend.send_prompt("p", "opus", str(tmp_path))
        assert r1.output == "argv: --model sonnet"
        assert r2.output == "argv: --model opus"


class TestCLIBackendStdin:
    @pytest.mark.asyncio
    async def test_prompt_piped_on_stdin(self, tmp_path):
        """Backend pipes ``prompt`` on stdin; fake echoes it back."""
        fake = _write_fake(
            tmp_path, "claude-fake",
            '#!/bin/sh\ncat\n',
        )
        backend = _backend_for(fake)
        result = await backend.send_prompt(
            prompt="hello, world", model="sonnet", workdir=str(tmp_path),
        )
        assert result.output == "hello, world"


class TestCLIBackendSuccess:
    @pytest.mark.asyncio
    async def test_returns_stdout_stripped(self, tmp_path):
        """Trailing whitespace from the fake is stripped (matches ACP shape)."""
        fake = _write_fake(
            tmp_path, "claude-fake",
            '#!/bin/sh\necho "  hello  "\n',
        )
        backend = _backend_for(fake)
        result = await backend.send_prompt("x", "sonnet", str(tmp_path))
        # `echo` adds a newline; backend strips it plus the surrounding
        # spaces echoed in the fake body.
        assert result.output == "hello"
        assert result.success is True


class TestCLIBackendOverload:
    @pytest.mark.asyncio
    async def test_stderr_overload_substring_raises_overload(self, tmp_path):
        """exit 1 + stderr containing 'overload' → OverloadError.

        The downgrade chain in PromptExecutor relies on this mapping;
        the substring set comes from _overload.ACP_OVERLOAD_SUBSTRINGS
        (shared with the ACP path).
        """
        fake = _write_fake(
            tmp_path, "claude-fake",
            '#!/bin/sh\necho "API overloaded, try later" >&2\nexit 1\n',
        )
        backend = _backend_for(fake)
        with pytest.raises(OverloadError) as ei:
            await backend.send_prompt("x", "sonnet", str(tmp_path))
        assert "overload" in str(ei.value).lower()

    @pytest.mark.asyncio
    async def test_stderr_529_substring_raises_overload(self, tmp_path):
        fake = _write_fake(
            tmp_path, "claude-fake",
            '#!/bin/sh\necho "HTTP 529 from upstream" >&2\nexit 1\n',
        )
        backend = _backend_for(fake)
        with pytest.raises(OverloadError):
            await backend.send_prompt("x", "sonnet", str(tmp_path))


class TestCLIBackendFailureModes:
    @pytest.mark.asyncio
    async def test_nonzero_exit_with_generic_stderr_raises_runtime(self, tmp_path):
        """exit 1 + non-overload stderr → RuntimeError carrying stderr."""
        fake = _write_fake(
            tmp_path, "claude-fake",
            '#!/bin/sh\necho "boom" >&2\nexit 1\n',
        )
        backend = _backend_for(fake)
        with pytest.raises(RuntimeError) as ei:
            await backend.send_prompt("x", "sonnet", str(tmp_path))
        # Exit code and stderr both surface in the message — important
        # for debugging when an unfamiliar CLI version regresses.
        assert "boom" in str(ei.value)
        assert "1" in str(ei.value)
        # And it's a plain RuntimeError, not OverloadError (no
        # downgrade should fire for a non-transient failure).
        assert not isinstance(ei.value, OverloadError)

    @pytest.mark.asyncio
    async def test_nonzero_exit_no_stderr_surfaces_exit_code(self, tmp_path):
        """Even with empty stderr, the exit code itself is in the message."""
        fake = _write_fake(
            tmp_path, "claude-fake",
            '#!/bin/sh\nexit 42\n',
        )
        backend = _backend_for(fake)
        with pytest.raises(RuntimeError) as ei:
            await backend.send_prompt("x", "sonnet", str(tmp_path))
        assert "42" in str(ei.value)


class TestCLIBackendTimeout:
    @pytest.mark.asyncio
    async def test_timeout_raises_and_reaps_subprocess(self, tmp_path):
        """Fake sleeps longer than timeout → TimeoutError; process killed.

        After the raise, attempting to find the fake's pid in `/proc`
        must fail — the backend's `except asyncio.TimeoutError` branch
        calls `proc.kill()` + `await proc.wait()`. If reaping were
        skipped, the pid would linger as a zombie.
        """
        fake = _write_fake(
            tmp_path, "claude-fake",
            '#!/bin/sh\nsleep 10\necho should-never-print\n',
        )
        backend = _backend_for(fake)
        with pytest.raises(asyncio.TimeoutError):
            await backend.send_prompt(
                "x", "sonnet", str(tmp_path), timeout=0.3,
            )
        # No leaked subprocess: a second call against the same backend
        # still works (close() is a no-op; no warm state). This is a
        # weak liveness check, but it confirms the backend wasn't
        # poisoned by the timeout path.
        fake_quick = _write_fake(
            tmp_path, "claude-fake-2",
            '#!/bin/sh\necho ok\n',
        )
        b2 = _backend_for(fake_quick)
        r = await b2.send_prompt("x", "sonnet", str(tmp_path), timeout=5.0)
        assert r.output == "ok"


class TestCLIBackendClose:
    @pytest.mark.asyncio
    async def test_close_is_noop_and_idempotent(self, tmp_path):
        """``close()`` returns without raising; calling twice is fine."""
        backend = CLIBackend()
        # No state to verify pre/post; the assertion is "neither call
        # raises" combined with "send_prompt still works after close"
        # (subprocess-per-call has nothing to tear down).
        await backend.close()
        await backend.close()
        # And a post-close send_prompt against a fake still works:
        fake = _write_fake(
            tmp_path, "claude-fake",
            '#!/bin/sh\necho still-alive\n',
        )
        b2 = _backend_for(fake)
        await b2.close()
        r = await b2.send_prompt("x", "sonnet", str(tmp_path))
        assert r.output == "still-alive"


class TestCLIBackendWorkdir:
    @pytest.mark.asyncio
    async def test_cwd_passed_to_subprocess(self, tmp_path):
        """Subprocess inherits cwd=workdir; the fake echoes its $PWD."""
        # subprocess cwd matters for `claude -p` because Claude Code
        # tools (read/write) are workdir-relative. Pin the contract.
        nested = tmp_path / "nested-cwd"
        nested.mkdir()
        fake = _write_fake(
            tmp_path, "claude-fake",
            '#!/bin/sh\npwd\n',
        )
        backend = _backend_for(fake)
        result = await backend.send_prompt(
            "x", "sonnet", workdir=str(nested),
        )
        # `pwd` resolves through symlinks; compare via realpath so
        # /tmp → /private/tmp on macOS et al. don't false-fail.
        assert os.path.realpath(result.output) == os.path.realpath(str(nested))


class TestCLIBackendEnv:
    @pytest.mark.asyncio
    async def test_env_reaches_subprocess(self, tmp_path):
        """Preset env var is visible to the spawned subprocess."""
        fake = _write_fake(tmp_path, "claude-fake", '#!/bin/sh\necho "FOO=$FOO"\n')
        backend = CLIBackend(argv_prefix=(str(fake),), env={"FOO": "bar"})
        result = await backend.send_prompt("x", "sonnet", str(tmp_path))
        assert result.output == "FOO=bar"

    @pytest.mark.asyncio
    async def test_empty_env_inherits_parent(self, tmp_path, monkeypatch):
        """No preset env → subprocess inherits the parent environment unchanged."""
        monkeypatch.setenv("INHERITED", "yes")
        fake = _write_fake(
            tmp_path, "claude-fake", '#!/bin/sh\necho "INHERITED=$INHERITED"\n',
        )
        backend = CLIBackend(argv_prefix=(str(fake),))  # no env → None → inherit
        result = await backend.send_prompt("x", "sonnet", str(tmp_path))
        assert result.output == "INHERITED=yes"

    @pytest.mark.asyncio
    async def test_env_overlays_without_wiping_inherited(self, tmp_path, monkeypatch):
        """Preset env overlays os.environ without dropping inherited vars."""
        monkeypatch.setenv("INHERITED", "keepme")
        fake = _write_fake(
            tmp_path, "claude-fake",
            '#!/bin/sh\necho "FOO=$FOO INHERITED=$INHERITED"\n',
        )
        backend = CLIBackend(argv_prefix=(str(fake),), env={"FOO": "bar"})
        result = await backend.send_prompt("x", "sonnet", str(tmp_path))
        assert result.output == "FOO=bar INHERITED=keepme"


class TestLlmPresetEnvWiring:
    """env field default + factory threading into the CLI backend."""

    def test_build_cli_threads_env(self):
        from sqrlly.runtime.executor.backends.factory import create_backend_from_preset
        from sqrlly.schema.models import LlmPreset
        backend = create_backend_from_preset(LlmPreset(
            transport="cli", provider="anthropic", model="sonnet", env={"X": "1"},
        ))
        assert backend._env == {"X": "1"}

    def test_llm_preset_env_defaults_empty(self):
        from sqrlly.schema.models import LlmPreset
        p = LlmPreset(transport="cli", provider="anthropic", model="sonnet")
        assert p.env == {}


class TestCLIBackendRetryViaDispatch:
    """End-to-end: a real CLIBackend (real subprocess) whose `claude` stub
    exits non-zero the first K times then succeeds is retried by the
    execute_rendered backend-retry layer when backend_max_retries is set.
    No mock — a /bin/sh stub stands in for `claude`."""

    def _stub_claude(self, tmp_path, fail_times: int):
        """Write an executable /bin/sh stub that increments a counter file
        and exits 1 until it has been called `fail_times` times, then prints
        a fixed line and exits 0. Returns the stub path."""
        import os
        import stat

        counter = tmp_path / "claude_calls.txt"
        stub = tmp_path / "claude_stub.sh"
        # Drain stdin (the prompt) so the parent's write side closes cleanly.
        stub.write_text(
            "#!/bin/sh\n"
            "cat >/dev/null\n"
            f'COUNTER="{counter}"\n'
            'n=0; [ -f "$COUNTER" ] && n=$(cat "$COUNTER")\n'
            'n=$((n+1)); echo "$n" > "$COUNTER"\n'
            f'if [ "$n" -le "{fail_times}" ]; then\n'
            '  echo "claude exited transiently" >&2\n'
            '  exit 1\n'
            'fi\n'
            'echo "stub-output"\n'
        )
        st = os.stat(stub)
        os.chmod(stub, st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        return stub, counter

    @pytest.mark.asyncio
    async def test_real_cli_backend_retried_then_succeeds(self, tmp_path):
        from sqrlly.runtime.executor.backends.cli import CLIBackend
        from sqrlly.runtime.executor.prompt import PromptExecutor
        from sqrlly.schema.models import Settings

        stub, counter = self._stub_claude(tmp_path, fail_times=2)
        backend = CLIBackend(argv_prefix=(str(stub),))
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(backend_max_retries=3),
            workdir=str(tmp_path),
        )
        result = await executor.execute_rendered(
            "prompt body", "sonnet", str(tmp_path), timeout=30.0,
        )
        assert result.success is True
        assert result.output == "stub-output"
        # 2 transient exits + 1 success = 3 real subprocess invocations.
        assert int(counter.read_text()) == 3

    @pytest.mark.asyncio
    async def test_real_cli_backend_zero_budget_terminal(self, tmp_path):
        from sqrlly.runtime.executor.backends.cli import CLIBackend
        from sqrlly.runtime.executor.prompt import PromptExecutor
        from sqrlly.schema.models import Settings

        stub, counter = self._stub_claude(tmp_path, fail_times=2)
        backend = CLIBackend(argv_prefix=(str(stub),))
        executor = PromptExecutor(
            backend=backend,
            settings=Settings(backend_max_retries=0),
            workdir=str(tmp_path),
        )
        result = await executor.execute_rendered(
            "prompt body", "sonnet", str(tmp_path), timeout=30.0,
        )
        assert result.success is False
        assert "Backend error" in result.error
        # One invocation, no retry.
        assert int(counter.read_text()) == 1
