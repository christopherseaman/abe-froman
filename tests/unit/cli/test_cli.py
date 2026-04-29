import hashlib
from pathlib import Path

import pytest
from click.testing import CliRunner

from abe_froman.cli.main import (
    CHECKPOINT_DB,
    _db_path,
    _is_git_repo,
    _thread_id_for,
    cli,
)


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
            "name: Test\nversion: '1.0'\nnodes:\n"
            "  - id: p1\n    name: Node 1\n    prompt_file: t.md\n"
        )
        result = runner.invoke(cli, ["validate", str(config)])
        assert result.exit_code == 0
        assert "1 nodes" in result.output


class TestGraphCommand:
    def test_graph_prints_phase_ids(self, runner, example_workflow_path):
        result = runner.invoke(cli, ["graph", str(example_workflow_path)])
        assert result.exit_code == 0
        assert "node-0" in result.output
        assert "node-1" in result.output
        assert "node-5" in result.output

    def test_graph_mermaid_format(self, runner, example_workflow_path):
        """Default format is Mermaid — output should contain the header."""
        result = runner.invoke(cli, ["graph", str(example_workflow_path)])
        assert result.exit_code == 0
        assert "graph TD" in result.output

    def test_graph_shows_gate_edges(self, runner, example_workflow_path):
        """Gated nodes produce conditional (dotted) edges in mermaid output."""
        result = runner.invoke(cli, ["graph", str(example_workflow_path)])
        assert result.exit_code == 0
        assert "-.->" in result.output

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
        assert "nodes traced" in result.output

    def test_run_nonexistent_file(self, runner):
        result = runner.invoke(cli, ["run", "nonexistent.yaml"])
        assert result.exit_code != 0

    def test_run_dry_run_lists_nodes(self, runner, example_workflow_path):
        result = runner.invoke(
            cli, ["run", str(example_workflow_path), "--dry-run"]
        )
        assert result.exit_code == 0
        assert "Nodes:" in result.output
        assert "node-0" in result.output

    def test_run_simple_workflow(self, runner, tmp_path):
        """End-to-end: command node that actually runs."""
        config = tmp_path / "simple.yaml"
        config.write_text(
            "name: Simple\nversion: '1.0'\nnodes:\n"
            "  - id: echo\n    name: Echo Test\n"
            "    execution:\n      type: command\n      command: echo\n      args: ['hello']\n"
        )
        result = runner.invoke(cli, ["run", str(config), "--workdir", str(tmp_path)])
        assert result.exit_code == 0
        assert "Completed: 1 nodes" in result.output

    def test_run_failing_command_exits_nonzero(self, runner, tmp_path):
        """A failing command node should cause non-zero exit."""
        config = tmp_path / "fail.yaml"
        config.write_text(
            "name: Fail\nversion: '1.0'\nnodes:\n"
            "  - id: fail\n    name: Fail Test\n"
            "    execution:\n      type: command\n      command: 'false'\n"
        )
        result = runner.invoke(cli, ["run", str(config), "--workdir", str(tmp_path)])
        assert result.exit_code != 0
        assert "Failed:" in result.output


class TestTokenSummary:
    def test_token_summary_displayed(self, runner, tmp_path):
        """Token usage from a prompt-stub node should show in CLI output."""
        config = tmp_path / "workflow.yaml"
        config.write_text(
            "name: Test\nversion: '1.0'\nnodes:\n"
            "  - id: a\n    name: A\n    prompt_file: t.md\n"
        )
        (tmp_path / "t.md").write_text("hello")

        result = runner.invoke(
            cli, ["run", str(config), "--workdir", str(tmp_path)]
        )
        assert result.exit_code == 0
        # Stub backend returns None for tokens_used, so no token line
        assert "Tokens:" not in result.output

    def test_no_token_summary_for_command_nodes(self, runner, tmp_path):
        config = tmp_path / "workflow.yaml"
        config.write_text(
            "name: Test\nversion: '1.0'\nnodes:\n"
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
            "name: Test\nversion: '1.0'\nnodes:\n"
            "  - id: node-1\n    name: Node 1\n"
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
            "name: Test\nversion: '1.0'\nnodes:\n"
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
        """Run, then --resume → picks up completed nodes from SQLite checkpoint."""
        config = self._simple_config(tmp_path)

        first = runner.invoke(
            cli, ["run", str(config), "--workdir", str(tmp_path)]
        )
        assert first.exit_code == 0

        second = runner.invoke(
            cli, ["run", str(config), "--resume", "--workdir", str(tmp_path)]
        )
        assert second.exit_code == 0
        assert "Resuming: 1 nodes already completed" in second.output


# ---------------------------------------------------------------------------
# Token summary positive path (J8)
# ---------------------------------------------------------------------------


class TestTokenSummaryPositive:
    def test_token_summary_displayed_when_tokens_present(self, runner, tmp_path, monkeypatch):
        """Wire a backend that returns tokens_used; verify summary appears."""
        from abe_froman.runtime.result import ExecutionResult

        class TokenBackend:
            async def send_prompt(self, prompt, model, workdir, timeout=None):
                return ExecutionResult(
                    output="ok",
                    tokens_used={"input": 100, "output": 50},
                )

            async def close(self):
                pass

        monkeypatch.setattr(
            "abe_froman.runtime.executor.backends.factory.create_prompt_backend",
            lambda _type: TokenBackend(),
        )

        config = tmp_path / "workflow.yaml"
        config.write_text(
            "name: Test\nversion: '1.0'\nnodes:\n"
            "  - id: a\n    name: A\n    prompt_file: t.md\n"
        )
        (tmp_path / "t.md").write_text("hello")

        result = runner.invoke(
            cli, ["run", str(config), "--workdir", str(tmp_path)]
        )
        assert result.exit_code == 0
        assert "Tokens:" in result.output
        assert "100" in result.output
        assert "50" in result.output


# ---------------------------------------------------------------------------
# CLI helper unit tests (J9)
# ---------------------------------------------------------------------------


class TestCliHelpers:
    def test_is_git_repo_true(self, tmp_path):
        import subprocess
        subprocess.run(["git", "init", str(tmp_path)], capture_output=True, check=True)
        assert _is_git_repo(str(tmp_path)) is True

    def test_is_git_repo_false(self, tmp_path):
        assert _is_git_repo(str(tmp_path)) is False

    def test_thread_id_deterministic(self, tmp_path):
        from abe_froman.schema.models import Graph

        config = Graph(
            name="test", version="1.0",
            nodes=[{"id": "a", "name": "A", "prompt_file": "t.md"}],
        )
        id1 = _thread_id_for(config, str(tmp_path))
        id2 = _thread_id_for(config, str(tmp_path))
        assert id1 == id2
        assert len(id1) == 16
        assert all(c in "0123456789abcdef" for c in id1)

    def test_thread_id_workdir_sensitive(self, tmp_path):
        from abe_froman.schema.models import Graph

        config = Graph(
            name="test", version="1.0",
            nodes=[{"id": "a", "name": "A", "prompt_file": "t.md"}],
        )
        id_a = _thread_id_for(config, str(tmp_path / "a"))
        id_b = _thread_id_for(config, str(tmp_path / "b"))
        assert id_a != id_b

    def test_db_path(self, tmp_path):
        result = _db_path(str(tmp_path))
        assert result == str(Path(tmp_path) / CHECKPOINT_DB)
