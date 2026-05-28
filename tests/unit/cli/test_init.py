"""Tests for `sqrlly init` — workflow scaffolding."""
from __future__ import annotations

import pytest
from click.testing import CliRunner

from sqrlly.cli.main import cli


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestInit:
    def test_creates_workflow_and_prompt(self, runner, tmp_path):
        target = tmp_path / "my-workflow"
        result = runner.invoke(cli, ["init", str(target)])
        assert result.exit_code == 0
        assert (target / "workflow.yaml").exists()
        assert (target / "prompts" / "hello.md").exists()
        wf = (target / "workflow.yaml").read_text()
        assert "transport: cli" in wf
        assert 'name: "My sqrlly workflow"' in wf

    def test_scaffolded_workflow_validates(self, runner, tmp_path):
        """The scaffold's output must round-trip through `sqrlly validate`.

        The strongest contract: whatever `init` writes is a real, parseable
        sqrlly workflow. If schema drifts, this test fails.
        """
        target = tmp_path / "fresh"
        runner.invoke(cli, ["init", str(target)])
        result = runner.invoke(cli, ["validate", str(target / "workflow.yaml")])
        assert result.exit_code == 0
        assert "Valid:" in result.output
        assert "1 nodes" in result.output

    def test_refuses_to_clobber(self, runner, tmp_path):
        target = tmp_path / "preexisting"
        target.mkdir()
        (target / "workflow.yaml").write_text("existing: content\n")
        result = runner.invoke(cli, ["init", str(target)])
        assert result.exit_code != 0
        assert "already exists" in result.output
        # Original content unchanged.
        assert (target / "workflow.yaml").read_text() == "existing: content\n"

    def test_creates_missing_directory(self, runner, tmp_path):
        target = tmp_path / "does" / "not" / "exist" / "yet"
        assert not target.exists()
        result = runner.invoke(cli, ["init", str(target)])
        assert result.exit_code == 0
        assert target.is_dir()
        assert (target / "workflow.yaml").exists()

    def test_default_dir_is_cwd(self, runner, tmp_path):
        """Without an argument, init scaffolds into the current directory."""
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, ["init"])
            assert result.exit_code == 0
            from pathlib import Path
            assert Path("workflow.yaml").exists()
            assert Path("prompts/hello.md").exists()

    def test_next_steps_printed(self, runner, tmp_path):
        target = tmp_path / "fresh"
        result = runner.invoke(cli, ["init", str(target)])
        assert "Next steps:" in result.output
        assert "sqrlly validate workflow.yaml" in result.output
        assert "sqrlly run workflow.yaml" in result.output
        assert f"cd {target}" in result.output
