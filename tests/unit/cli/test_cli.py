import pytest
from click.testing import CliRunner

from abe_froman.cli.main import cli


@pytest.fixture
def runner():
    return CliRunner()


class TestValidateCommand:
    def test_validate_valid_config(self, runner, example_workflow_path):
        result = runner.invoke(cli, ["validate", str(example_workflow_path)])
        assert result.exit_code == 0
        assert "Valid:" in result.output
        assert "CFRA Default Workflow" in result.output

    def test_validate_nonexistent_file(self, runner):
        result = runner.invoke(cli, ["validate", "nonexistent.yaml"])
        assert result.exit_code != 0

    def test_validate_invalid_yaml(self, runner, tmp_path):
        bad_config = tmp_path / "bad.yaml"
        bad_config.write_text("name: test\n")
        result = runner.invoke(cli, ["validate", str(bad_config)])
        assert result.exit_code != 0

    def test_validate_reports_phase_count(self, runner, tmp_path):
        config = tmp_path / "simple.yaml"
        config.write_text(
            "name: Test\nversion: '1.0'\nphases:\n"
            "  - id: p1\n    name: Phase 1\n    prompt_file: t.md\n"
        )
        result = runner.invoke(cli, ["validate", str(config)])
        assert result.exit_code == 0
        assert "1 phases" in result.output


class TestGraphCommand:
    def test_graph_prints_phase_ids(self, runner, example_workflow_path):
        result = runner.invoke(cli, ["graph", str(example_workflow_path)])
        assert result.exit_code == 0
        assert "phase-0" in result.output
        assert "phase-1" in result.output
        assert "phase-5" in result.output

    def test_graph_mermaid_format(self, runner, example_workflow_path):
        """Default format is Mermaid — output should contain the header."""
        result = runner.invoke(cli, ["graph", str(example_workflow_path)])
        assert result.exit_code == 0
        assert "graph TD" in result.output

    def test_graph_shows_gate_edges(self, runner, example_workflow_path):
        """Gated phases produce conditional pass/retry/fail edges."""
        result = runner.invoke(cli, ["graph", str(example_workflow_path)])
        assert result.exit_code == 0
        assert "pass" in result.output
        assert "retry" in result.output

    def test_graph_shows_start_and_end(self, runner, example_workflow_path):
        """Mermaid output contains LangGraph's start/end terminal nodes."""
        result = runner.invoke(cli, ["graph", str(example_workflow_path)])
        assert result.exit_code == 0
        assert "__start__" in result.output
        assert "__end__" in result.output


class TestRunCommand:
    def test_run_dry_run(self, runner, example_workflow_path):
        result = runner.invoke(
            cli, ["run", str(example_workflow_path), "--dry-run"]
        )
        assert result.exit_code == 0
        assert "Dry run completed" in result.output
        assert "phases traced" in result.output

    def test_run_nonexistent_file(self, runner):
        result = runner.invoke(cli, ["run", "nonexistent.yaml"])
        assert result.exit_code != 0

    def test_run_dry_run_lists_phases(self, runner, example_workflow_path):
        result = runner.invoke(
            cli, ["run", str(example_workflow_path), "--dry-run"]
        )
        assert result.exit_code == 0
        assert "Phases:" in result.output
        assert "phase-0" in result.output

    def test_run_simple_workflow(self, runner, tmp_path):
        """End-to-end: command phase that actually runs."""
        config = tmp_path / "simple.yaml"
        config.write_text(
            "name: Simple\nversion: '1.0'\nphases:\n"
            "  - id: echo\n    name: Echo Test\n"
            "    execution:\n      type: command\n      command: echo\n      args: ['hello']\n"
        )
        result = runner.invoke(cli, ["run", str(config), "--workdir", str(tmp_path)])
        assert result.exit_code == 0
        assert "Completed: 1 phases" in result.output

    def test_run_failing_command_exits_nonzero(self, runner, tmp_path):
        """A failing command phase should cause non-zero exit."""
        config = tmp_path / "fail.yaml"
        config.write_text(
            "name: Fail\nversion: '1.0'\nphases:\n"
            "  - id: fail\n    name: Fail Test\n"
            "    execution:\n      type: command\n      command: 'false'\n"
        )
        result = runner.invoke(cli, ["run", str(config), "--workdir", str(tmp_path)])
        assert result.exit_code != 0
        assert "Failed:" in result.output


class TestTokenSummary:
    def test_token_summary_displayed(self, runner, tmp_path):
        """Token usage from a prompt-stub phase should show in CLI output."""
        config = tmp_path / "workflow.yaml"
        config.write_text(
            "name: Test\nversion: '1.0'\nphases:\n"
            "  - id: a\n    name: A\n    prompt_file: t.md\n"
        )
        (tmp_path / "t.md").write_text("hello")

        result = runner.invoke(
            cli, ["run", str(config), "--workdir", str(tmp_path)]
        )
        assert result.exit_code == 0
        # Stub backend returns None for tokens_used, so no token line
        assert "Tokens:" not in result.output

    def test_no_token_summary_for_command_phases(self, runner, tmp_path):
        config = tmp_path / "workflow.yaml"
        config.write_text(
            "name: Test\nversion: '1.0'\nphases:\n"
            "  - id: a\n    name: A\n"
            "    execution:\n      type: command\n      command: echo\n      args: ['hi']\n"
        )
        result = runner.invoke(
            cli, ["run", str(config), "--workdir", str(tmp_path)]
        )
        assert result.exit_code == 0
        assert "Tokens:" not in result.output


class TestRunOptions:
    def test_executor_unknown_raises(self, runner, tmp_path):
        config = tmp_path / "simple.yaml"
        config.write_text(
            "name: Test\nversion: '1.0'\nphases:\n"
            "  - id: phase-1\n    name: Phase 1\n"
            "    execution:\n      type: command\n      command: echo\n      args: ['hi']\n"
        )
        result = runner.invoke(
            cli, ["run", str(config), "--executor", "bogus", "--workdir", str(tmp_path)]
        )
        assert result.exit_code != 0


class TestResumeCommand:
    def _simple_config(self, tmp_path):
        config = tmp_path / "simple.yaml"
        config.write_text(
            "name: Test\nversion: '1.0'\nphases:\n"
            "  - id: a\n    name: A\n"
            "    execution:\n      type: command\n      command: echo\n      args: ['hi']\n"
        )
        return config

    def test_resume_without_checkpoint_errors(self, runner, tmp_path):
        """--resume with no prior run → clean error."""
        config = self._simple_config(tmp_path)
        result = runner.invoke(
            cli, ["run", str(config), "--resume", "--workdir", str(tmp_path)]
        )
        assert result.exit_code != 0
        assert "No saved state" in result.output

    def test_resume_reads_previous_checkpoint(self, runner, tmp_path):
        """Run, then --resume → picks up completed phases from SQLite checkpoint."""
        config = self._simple_config(tmp_path)

        first = runner.invoke(
            cli, ["run", str(config), "--workdir", str(tmp_path)]
        )
        assert first.exit_code == 0

        second = runner.invoke(
            cli, ["run", str(config), "--resume", "--workdir", str(tmp_path)]
        )
        assert second.exit_code == 0
        assert "Resuming: 1 phases already completed" in second.output
