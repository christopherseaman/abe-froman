import hashlib
from pathlib import Path

import pytest
from click.testing import CliRunner

from sqrlly.cli.main import (
    CHECKPOINT_DB,
    _collect_subgraph_presets,
    _db_path,
    _is_git_repo,
    _thread_id_for,
    cli,
)


@pytest.fixture
def runner():
    return CliRunner()


class TestValidateCommand:
    def test_validate_valid_config(self, runner, kitchen_sink_workflow_path):
        result = runner.invoke(cli, ["validate", str(kitchen_sink_workflow_path)])
        assert result.exit_code == 0
        assert "Valid:" in result.output
        assert "Absurd Academic Paper" in result.output

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
            "  - id: p1\n    name: Node 1\n"
            "    execute:\n      url: t.md\n"
        )
        result = runner.invoke(cli, ["validate", str(config)])
        assert result.exit_code == 0
        assert "1 nodes" in result.output

    def test_validate_warns_on_hyphenated_id(self, runner, tmp_path):
        """A hyphenated node id is a valid config — validate still
        succeeds (exit 0, 'Valid:') but emits an advisory warning."""
        config = tmp_path / "hyphen.yaml"
        config.write_text(
            "name: Test\nversion: '1.0'\nnodes:\n"
            "  - id: research-phase\n    name: Research\n"
            "    execute:\n      url: t.md\n"
        )
        result = runner.invoke(cli, ["validate", str(config)])
        assert result.exit_code == 0
        assert "Valid:" in result.output
        assert "research-phase" in result.stderr
        assert "warning:" in result.stderr


class TestGraphCommand:
    def test_graph_prints_phase_ids(self, runner, kitchen_sink_workflow_path):
        result = runner.invoke(cli, ["graph", str(kitchen_sink_workflow_path)])
        assert result.exit_code == 0
        # absurd-paper has these named nodes
        assert "abstract" in result.output
        assert "paper" in result.output
        assert "reviewer_pool" in result.output

    def test_graph_mermaid_format(self, runner, kitchen_sink_workflow_path):
        """Default format is Mermaid — output should contain the header."""
        result = runner.invoke(cli, ["graph", str(kitchen_sink_workflow_path)])
        assert result.exit_code == 0
        assert "graph TD" in result.output

    def test_graph_shows_gate_edges(self, runner, kitchen_sink_workflow_path):
        """Gated nodes produce eval + decision node pairs (Stage 5d).

        Pre-Stage-5d the eval node had conditional edges (dotted in
        Mermaid). After the eval/decision split, gated nodes compile
        to ``exec → _eval_<id> → _decide_<id>`` plain edges; routing
        is via Command(goto=) at runtime, not static conditional
        edges. Test now asserts the eval/decide pair is visible."""
        result = runner.invoke(cli, ["graph", str(kitchen_sink_workflow_path)])
        assert result.exit_code == 0
        assert "_eval_choose_topic" in result.output
        assert "_decide_choose_topic" in result.output
        assert "_eval_choose_topic --> _decide_choose_topic" in result.output

    def test_graph_shows_start_and_end(self, runner, kitchen_sink_workflow_path):
        """Mermaid output contains LangGraph's start/end terminal nodes."""
        result = runner.invoke(cli, ["graph", str(kitchen_sink_workflow_path)])
        assert result.exit_code == 0
        assert "__start__" in result.output
        assert "__end__" in result.output


class TestRunCommand:
    def test_run_dry_run(self, runner, kitchen_sink_workflow_path):
        result = runner.invoke(
            cli, ["run", str(kitchen_sink_workflow_path), "--dry-run"]
        )
        assert result.exit_code == 0
        assert "Dry run completed" in result.output
        assert "nodes traced" in result.output

    def test_run_nonexistent_file(self, runner):
        result = runner.invoke(cli, ["run", "nonexistent.yaml"])
        assert result.exit_code != 0

    def test_run_dry_run_lists_nodes(self, runner, kitchen_sink_workflow_path):
        result = runner.invoke(
            cli, ["run", str(kitchen_sink_workflow_path), "--dry-run"]
        )
        assert result.exit_code == 0
        assert "Nodes:" in result.output
        assert "abstract" in result.output

    def test_run_simple_workflow(self, runner, tmp_path):
        """End-to-end: command node that actually runs."""
        import shutil
        echo_bin = shutil.which("echo") or "/bin/echo"
        config = tmp_path / "simple.yaml"
        config.write_text(
            "name: Simple\nversion: '1.0'\nnodes:\n"
            "  - id: echo\n    name: Echo Test\n"
            f"    execute:\n      url: {echo_bin}\n      params:\n        args: ['hello']\n"
        )
        result = runner.invoke(cli, ["run", str(config), "--workdir", str(tmp_path)])
        assert result.exit_code == 0
        assert "Completed: 1 nodes" in result.output

    def test_run_warns_on_hyphenated_id(self, runner, tmp_path):
        """`run` surfaces the same advisory warning as `validate` — the
        node still executes; the warning is non-fatal."""
        import shutil
        echo_bin = shutil.which("echo") or "/bin/echo"
        config = tmp_path / "hyphen.yaml"
        config.write_text(
            "name: Simple\nversion: '1.0'\nnodes:\n"
            "  - id: echo-test\n    name: Echo Test\n"
            f"    execute:\n      url: {echo_bin}\n"
            "      params:\n        args: ['hello']\n"
        )
        result = runner.invoke(
            cli, ["run", str(config), "--workdir", str(tmp_path)]
        )
        assert result.exit_code == 0
        assert "Completed: 1 nodes" in result.output
        assert "echo-test" in result.stderr
        assert "warning:" in result.stderr

    def test_run_failing_command_exits_nonzero(self, runner, tmp_path):
        """A failing command node should cause non-zero exit."""
        import shutil
        false_bin = shutil.which("false") or "/bin/false"
        config = tmp_path / "fail.yaml"
        config.write_text(
            "name: Fail\nversion: '1.0'\nnodes:\n"
            "  - id: fail\n    name: Fail Test\n"
            f"    execute:\n      url: {false_bin}\n"
        )
        result = runner.invoke(cli, ["run", str(config), "--workdir", str(tmp_path)])
        assert result.exit_code != 0
        assert "Failed:" in result.output

    def test_worktree_gc_on_success_removes_trees(self, runner, tmp_path):
        """worktree_gc: on_success removes all .sqrlly/wt-* dirs after a
        clean run; worktree_gc: never (default) leaves them in place.
        Uses a real git repo + /usr/bin/true (no LLM required)."""
        import shutil
        import subprocess

        true_bin = shutil.which("true") or "/usr/bin/true"

        def _init_repo(path: Path) -> None:
            subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
            subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
            (path / "README").write_text("init")
            subprocess.run(["git", "-C", str(path), "add", "README"], check=True)
            subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True)

        # --- on_success: trees must be removed after a clean run ---
        repo_gc = tmp_path / "repo_gc"
        repo_gc.mkdir()
        _init_repo(repo_gc)
        cfg_gc = repo_gc / "workflow.yaml"
        cfg_gc.write_text(
            "name: GcTest\nversion: '1.0'\n"
            "settings:\n  worktree_gc: on_success\n"
            "nodes:\n"
            "  - id: step\n    name: Step\n"
            f"    execute:\n      url: {true_bin}\n"
            "      params:\n        args: []\n"
        )
        result_gc = runner.invoke(cli, ["run", str(cfg_gc), "--workdir", str(repo_gc)])
        assert result_gc.exit_code == 0, result_gc.output + result_gc.stderr
        sqrlly_dir = repo_gc / ".sqrlly"
        leftover = list(sqrlly_dir.glob("wt-*")) if sqrlly_dir.exists() else []
        assert leftover == [], f"on_success GC left trees: {leftover}"

        # --- never (default): tree remains after run ---
        repo_keep = tmp_path / "repo_keep"
        repo_keep.mkdir()
        _init_repo(repo_keep)
        cfg_keep = repo_keep / "workflow.yaml"
        cfg_keep.write_text(
            "name: KeepTest\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: step\n    name: Step\n"
            f"    execute:\n      url: {true_bin}\n"
            "      params:\n        args: []\n"
        )
        result_keep = runner.invoke(cli, ["run", str(cfg_keep), "--workdir", str(repo_keep)])
        assert result_keep.exit_code == 0, result_keep.output + result_keep.stderr
        sqrlly_dir_keep = repo_keep / ".sqrlly"
        leftover_keep = list(sqrlly_dir_keep.glob("wt-*")) if sqrlly_dir_keep.exists() else []
        assert leftover_keep != [], "never GC should leave trees but found none"


class TestRunOptions:
    def test_preset_unknown_raises(self, runner, tmp_path):
        import shutil
        echo_bin = shutil.which("echo") or "/bin/echo"
        config = tmp_path / "simple.yaml"
        # YAML with a single declared preset; --preset references one
        # that doesn't exist.
        config.write_text(
            "name: Test\nversion: '1.0'\n"
            "settings:\n"
            "  presets:\n"
            "    default:\n"
            "      transport: acp\n"
            "      provider: anthropic\n"
            "      model: sonnet\n"
            "      default: true\n"
            "nodes:\n"
            "  - id: node-1\n    name: Node 1\n"
            f"    execute:\n      url: {echo_bin}\n      params:\n        args: ['hi']\n"
        )
        result = runner.invoke(
            cli, ["run", str(config), "--preset", "bogus", "--workdir", str(tmp_path)]
        )
        assert result.exit_code != 0
        combined = (result.output or "") + str(result.exception or "")
        assert "bogus" in combined


class TestResumeCommand:
    def _simple_config(self, tmp_path):
        import shutil
        echo_bin = shutil.which("echo") or "/bin/echo"
        config = tmp_path / "simple.yaml"
        config.write_text(
            "name: Test\nversion: '1.0'\nnodes:\n"
            "  - id: a\n    name: A\n"
            f"    execute:\n      url: {echo_bin}\n      params:\n        args: ['hi']\n"
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
        from sqrlly.schema.models import Graph

        config = Graph(
            name="test", version="1.0",
            nodes=[{"id": "a", "name": "A", "execute": {"url": "t.md"}}],
        )
        id1 = _thread_id_for(config, str(tmp_path))
        id2 = _thread_id_for(config, str(tmp_path))
        assert id1 == id2
        assert len(id1) == 16
        assert all(c in "0123456789abcdef" for c in id1)

    def test_thread_id_workdir_sensitive(self, tmp_path):
        from sqrlly.schema.models import Graph

        config = Graph(
            name="test", version="1.0",
            nodes=[{"id": "a", "name": "A", "execute": {"url": "t.md"}}],
        )
        id_a = _thread_id_for(config, str(tmp_path / "a"))
        id_b = _thread_id_for(config, str(tmp_path / "b"))
        assert id_a != id_b

    def test_db_path(self, tmp_path):
        result = _db_path(str(tmp_path))
        assert result == str(Path(tmp_path) / CHECKPOINT_DB)


class TestCollectSubgraphPresets:
    """P1 regression — a subgraph node resolves its own preset, so the
    CLI must collect subgraph-declared presets into the backend set."""

    def _write(self, path: Path, text: str):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    def test_collects_preset_from_referenced_subgraph(self, tmp_path):
        from sqrlly.schema.models import Graph

        self._write(tmp_path / "sub.yaml", (
            "name: Sub\nversion: '1.0'\n"
            "settings:\n"
            "  presets:\n"
            "    sub_default:\n"
            "      transport: acp\n"
            "      provider: anthropic\n"
            "      model: sonnet\n"
            "      default: true\n"
            "nodes:\n"
            "  - id: inner\n    name: Inner\n"
            "    execute:\n      url: inner.md\n"
        ))
        parent = Graph(
            name="parent", version="1.0",
            nodes=[{"id": "s", "name": "S", "execute": {"url": "sub.yaml"}}],
        )
        collected = _collect_subgraph_presets(parent, str(tmp_path))
        assert "sub_default" in collected
        assert collected["sub_default"].model == "sonnet"

    def test_no_subgraphs_returns_empty(self, tmp_path):
        from sqrlly.schema.models import Graph

        parent = Graph(
            name="parent", version="1.0",
            nodes=[{"id": "a", "name": "A", "execute": {"url": "a.md"}}],
        )
        assert _collect_subgraph_presets(parent, str(tmp_path)) == {}

    def test_recurses_into_nested_subgraphs(self, tmp_path):
        from sqrlly.schema.models import Graph

        self._write(tmp_path / "leaf.yaml", (
            "name: Leaf\nversion: '1.0'\n"
            "settings:\n"
            "  presets:\n"
            "    leaf_preset:\n"
            "      transport: acp\n      provider: anthropic\n"
            "      model: haiku\n      default: true\n"
            "nodes:\n  - id: x\n    name: X\n    execute:\n      url: x.md\n"
        ))
        self._write(tmp_path / "mid.yaml", (
            "name: Mid\nversion: '1.0'\n"
            "nodes:\n  - id: m\n    name: M\n    execute:\n      url: leaf.yaml\n"
        ))
        parent = Graph(
            name="parent", version="1.0",
            nodes=[{"id": "s", "name": "S", "execute": {"url": "mid.yaml"}}],
        )
        collected = _collect_subgraph_presets(parent, str(tmp_path))
        assert "leaf_preset" in collected
