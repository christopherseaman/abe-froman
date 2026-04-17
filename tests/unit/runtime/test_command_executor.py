"""Unit tests for CommandExecutor in executor/command.py."""

import pytest

from abe_froman.runtime.executor.command import CommandExecutor
from abe_froman.schema.models import Phase


class TestCommandExecutor:
    @pytest.mark.asyncio
    async def test_workdir_respected(self, tmp_path):
        test_file = tmp_path / "data.txt"
        test_file.write_text("file contents")
        executor = CommandExecutor(workdir=str(tmp_path))
        phase = Phase(
            id="c1", name="C1",
            execution={"type": "command", "command": "cat", "args": ["data.txt"]},
        )
        result = await executor.execute(phase, {})
        assert result.success is True
        assert result.output == "file contents"

    @pytest.mark.asyncio
    async def test_nonzero_exit_captures_stderr(self):
        executor = CommandExecutor()
        phase = Phase(
            id="c2", name="C2",
            execution={"type": "command", "command": "sh", "args": ["-c", "echo err >&2; exit 1"]},
        )
        result = await executor.execute(phase, {})
        assert result.success is False
        assert "Exit code 1" in result.error
        assert "err" in result.error

    @pytest.mark.asyncio
    async def test_nonexistent_command_returns_error(self):
        executor = CommandExecutor()
        phase = Phase(
            id="c3", name="C3",
            execution={"type": "command", "command": "no_such_command_xyz_123"},
        )
        result = await executor.execute(phase, {})
        assert result.success is False
        assert result.error is not None

    @pytest.mark.asyncio
    async def test_per_call_workdir_overrides_constructor(self, tmp_path):
        """Per-call workdir takes precedence over constructor workdir."""
        other_dir = tmp_path / "other"
        other_dir.mkdir()
        (other_dir / "data.txt").write_text("from-other")
        (tmp_path / "data.txt").write_text("from-base")
        executor = CommandExecutor(workdir=str(tmp_path))
        phase = Phase(
            id="c", name="C",
            execution={"type": "command", "command": "cat", "args": ["data.txt"]},
        )
        result = await executor.execute(phase, {}, workdir=str(other_dir))
        assert result.output == "from-other"

    @pytest.mark.asyncio
    async def test_per_call_workdir_none_falls_back_to_constructor(self, tmp_path):
        (tmp_path / "data.txt").write_text("from-base")
        executor = CommandExecutor(workdir=str(tmp_path))
        phase = Phase(
            id="c", name="C",
            execution={"type": "command", "command": "cat", "args": ["data.txt"]},
        )
        result = await executor.execute(phase, {}, workdir=None)
        assert result.output == "from-base"
