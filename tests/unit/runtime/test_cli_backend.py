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
  whole process GROUP is killed via ``_kill_process_group`` (SIGTERM →
  SIGKILL), reaping both the direct child and any descendants.
- Cancel — an in-flight task cancelled via ``asyncio.Task.cancel()``
  also kills the process group and re-raises ``CancelledError``.
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

        The downgrade chain in execute_with_downgrade relies on this mapping;
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
        """Fake sleeps longer than timeout → TimeoutError; process group killed.

        After the raise, the backend's `except asyncio.TimeoutError` branch
        calls `_kill_process_group` (SIGTERM → 0.5s → SIGKILL via `os.killpg`)
        and reaps the direct child with `proc.wait()`. The next send_prompt
        call against a fresh fast stub succeeds — confirming the backend was
        not poisoned by the timeout path.
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
    execute_with_downgrade backend-retry layer when backend_max_retries is set.
    No mock — a /bin/sh stub stands in for `claude`."""

    def _stub_claude(self, tmp_path, fail_times: int):
        """Write an executable /bin/sh stub that increments a counter file
        and exits 1 until it has been called `fail_times` times, then prints
        a fixed line and exits 0. Returns the stub path."""
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
        from sqrlly.runtime.executor.prompt import execute_with_downgrade
        from sqrlly.schema.models import Settings

        stub, counter = self._stub_claude(tmp_path, fail_times=2)
        backend = CLIBackend(argv_prefix=(str(stub),))
        result = await execute_with_downgrade(
            backend, "prompt body", "sonnet", str(tmp_path), timeout=30.0,
            settings=Settings(backend_max_retries=3),
        )
        assert result.success is True
        assert result.output == "stub-output"
        # 2 transient exits + 1 success = 3 real subprocess invocations.
        assert int(counter.read_text()) == 3

    @pytest.mark.asyncio
    async def test_real_cli_backend_zero_budget_terminal(self, tmp_path):
        from sqrlly.runtime.executor.backends.cli import CLIBackend
        from sqrlly.runtime.executor.prompt import execute_with_downgrade
        from sqrlly.schema.models import Settings

        stub, counter = self._stub_claude(tmp_path, fail_times=2)
        backend = CLIBackend(argv_prefix=(str(stub),))
        result = await execute_with_downgrade(
            backend, "prompt body", "sonnet", str(tmp_path), timeout=30.0,
            settings=Settings(backend_max_retries=0),
        )
        assert result.success is False
        assert "Backend error" in result.error
        # One invocation, no retry.
        assert int(counter.read_text()) == 1


class TestCLIBackendProcessGroupKill:
    """On timeout the backend must kill the whole process GROUP, not just the
    direct child. A real /bin/sh stub forks a descendant that writes a sentinel
    file after a delay; if only the direct child were killed, the descendant
    survives and writes the sentinel. With the process-group kill it dies first
    — no sentinel, pid gone. No mock: real subprocess, real fork."""

    def _stub_with_descendant(self, tmp_path: Path):
        """Stub `claude` that:
          - forks a backgrounded descendant which sleeps 3s then writes
            `descendant.sentinel`;
          - the PARENT captures the background subshell's PID via `$!` and
            writes it to `descendant.pid` (using `$!` is correct: `$$` inside
            a subshell still expands to the outer shell's PID, not the
            subshell's PID, so it cannot be used here);
          - then sleeps 10s itself (so the parent is alive and is the group
            leader when the timeout fires).
        The sentinel only appears AFTER the descendant's sleep finishes, so its
        absence proves the descendant was killed before completing."""
        sentinel = tmp_path / "descendant.sentinel"
        pidfile = tmp_path / "descendant.pid"
        stub = tmp_path / "claude_group_stub.sh"
        stub.write_text(
            "#!/bin/sh\n"
            "cat >/dev/null\n"  # drain the prompt on stdin
            "(\n"
            "  sleep 3\n"
            f'  echo alive > "{sentinel}"\n'  # only reached if NOT killed
            ") &\n"
            f'echo "$!" > "{pidfile}"\n'  # parent records descendant pid via $!
            "sleep 10\n"  # parent stays alive past the timeout
        )
        stub.chmod(0o755)
        return stub, sentinel, pidfile

    @pytest.mark.asyncio
    async def test_timeout_kills_descendant_not_just_child(self, tmp_path):
        import time

        stub, sentinel, pidfile = self._stub_with_descendant(tmp_path)
        backend = _backend_for(stub)

        with pytest.raises(asyncio.TimeoutError):
            await backend.send_prompt(
                "prompt", "sonnet", str(tmp_path), timeout=0.5,
            )

        # The parent writes the descendant's PID (via $!) immediately after
        # forking, before its own sleep. Poll briefly to let it appear.
        deadline = time.monotonic() + 2.0
        while not pidfile.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.02)
        assert pidfile.exists(), "stub never spawned its descendant"
        descendant_pid = int(pidfile.read_text().strip())

        # Give the kill a beat to propagate through the group. The sentinel
        # absence below is the real behavioral proof — os.kill(pid, 0) probes
        # the pid as a secondary check, but sentinel absence is definitive:
        # the descendant was killed before it could complete its sleep and
        # write. With the old proc.kill() (child-only), the background subshell
        # (descendant_pid != proc.pid) would survive and write the sentinel.
        await asyncio.sleep(0.6)
        with pytest.raises(ProcessLookupError):
            os.kill(descendant_pid, 0)

        # Belt-and-suspenders: the sentinel the descendant would write after
        # its 3s sleep must never appear — confirming it was killed, not just
        # temporarily paused.
        await asyncio.sleep(3.0)
        assert not sentinel.exists(), (
            "descendant survived the timeout and wrote its sentinel — "
            "only the direct child was killed, not the process group"
        )

    @pytest.mark.asyncio
    async def test_cancel_kills_descendant_not_just_child(self, tmp_path):
        """Task cancellation must kill the whole process GROUP, not just the
        direct child. Same descendant-sentinel proof as the timeout test:
        the descendant writes a file only if it survives long enough; a proper
        group kill (via CancelledError handler) prevents the write.

        The stub forks a backgrounded descendant (sleep 3 → write sentinel)
        and records the descendant's PID via $! (not $$, which stays the outer
        shell's PID inside a subshell). The parent sleeps 10s so the group
        leader is alive when cancel fires."""
        stub, sentinel, pidfile = self._stub_with_descendant(tmp_path)
        backend = _backend_for(stub)

        task = asyncio.get_event_loop().create_task(
            backend.send_prompt("prompt", "sonnet", str(tmp_path))
        )

        # Let the stub fork its descendant before we cancel. The parent writes
        # the pidfile immediately after the fork, so polling for it is sufficient.
        import time
        deadline = time.monotonic() + 2.0
        while not pidfile.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert pidfile.exists(), "stub never spawned its descendant"

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The group kill must have propagated before the descendant's 3s sleep
        # completes. Poll briefly to let the signal settle, then assert sentinel
        # absence — the definitive proof the descendant was killed.
        await asyncio.sleep(0.6)
        await asyncio.sleep(3.0)
        assert not sentinel.exists(), (
            "descendant survived cancellation and wrote its sentinel — "
            "only the direct child was killed, not the process group"
        )

    @pytest.mark.asyncio
    async def test_normal_run_still_succeeds_after_session_change(self, tmp_path):
        """start_new_session=True must not break the happy path: a fast stub
        still returns its stdout, stripped."""
        fake = _write_fake(
            tmp_path, "claude-ok", '#!/bin/sh\ncat >/dev/null\necho ok-output\n',
        )
        backend = _backend_for(fake)
        result = await backend.send_prompt(
            "x", "sonnet", str(tmp_path), timeout=10.0,
        )
        assert result.success is True
        assert result.output == "ok-output"


class TestCLIBackendMissingBinary:
    @pytest.mark.asyncio
    async def test_missing_binary_reports_actionable_error(self, tmp_path):
        """When the backend binary is absent from PATH the spawn raises a
        clear error naming the missing command — not a bare
        FileNotFoundError errno. A user who installed sqrlly but not
        `claude` hits this at the first prompt node."""
        backend = CLIBackend(argv_prefix=("sqrlly-no-such-claude-xyz", "-p"))
        with pytest.raises(RuntimeError) as ei:
            await backend.send_prompt("hi", "sonnet", str(tmp_path), timeout=10.0)
        msg = str(ei.value)
        assert "not found on PATH" in msg
        assert "sqrlly-no-such-claude-xyz" in msg
