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

    def test_graph_shows_dependencies(self, runner, example_workflow_path):
        result = runner.invoke(cli, ["graph", str(example_workflow_path)])
        assert result.exit_code == 0
        # phase-1 depends on phase-0
        assert "depends_on" in result.output

    def test_graph_shows_execution_types(self, runner, example_workflow_path):
        result = runner.invoke(cli, ["graph", str(example_workflow_path)])
        assert "(command)" in result.output
        assert "(prompt)" in result.output

    def test_graph_shows_model_override(self, runner, example_workflow_path):
        result = runner.invoke(cli, ["graph", str(example_workflow_path)])
        assert "[model: sonnet]" in result.output

    def test_graph_shows_gate_info(self, runner, example_workflow_path):
        result = runner.invoke(cli, ["graph", str(example_workflow_path)])
        assert "[gate:" in result.output


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
