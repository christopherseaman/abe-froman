"""Tests for `sqrlly init` — workflow scaffolding + skill install."""
from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from sqrlly.cli.init import _load_skill_doc
from sqrlly.cli.main import cli
from helpers import init_git_repo


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _git_init(path):
    init_git_repo(path, commit=False)

class TestVersion:
    def test_version_flag_prints_version(self, runner):
        import importlib.metadata as m
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert "sqrlly" in result.output
        assert m.version("sqrlly") in result.output


class TestInit:
    def test_creates_workflow_and_prompt(self, runner, tmp_path):
        target = tmp_path / "my-workflow"
        result = runner.invoke(cli, ["init", str(target)])
        assert result.exit_code == 0
        assert (target / "workflow.yaml").exists()
        assert (target / "prompts" / "hello.md").exists()
        wf = (target / "workflow.yaml").read_text()
        assert "provider: openai" in wf
        assert "model: gpt-5.6-luna" in wf
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


class TestInitSkill:
    def test_load_skill_doc_returns_real_doc(self):
        """Loader resolves the canonical skill text (frontmatter + the
        Prerequisites section we ship), via the source-tree fallback in
        tests or the packaged resource in a wheel."""
        doc = _load_skill_doc()
        assert doc.startswith("---")
        assert "name: sqrlly" in doc
        assert "## Prerequisites" in doc

    def test_skill_installs_to_agents_dir_and_reports_path(self, runner, tmp_path):
        target = tmp_path / "proj"
        target.mkdir()
        result = runner.invoke(cli, ["init", "--skill", str(target)])
        assert result.exit_code == 0
        installed = target / ".agents" / "skills" / "sqrlly" / "SKILL.md"
        assert installed.exists()
        # Reports the path it wrote.
        assert "Installed sqrlly skill" in result.output
        assert str(installed.resolve()) in result.output
        # Content is the canonical skill doc.
        assert installed.read_text() == _load_skill_doc()

    def test_skill_is_repo_aware_climbs_to_git_root(self, runner, tmp_path):
        """Invoked against a subdirectory of a git repo, the skill lands
        at the repo root's .agents/, not the subdirectory."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _git_init(repo)
        sub = repo / "src" / "deep"
        sub.mkdir(parents=True)
        result = runner.invoke(cli, ["init", "--skill", str(sub)])
        assert result.exit_code == 0
        assert (repo / ".agents" / "skills" / "sqrlly" / "SKILL.md").exists()
        assert not (sub / ".agents").exists()

    def test_skill_refreshes_on_rerun(self, runner, tmp_path):
        """Re-running overwrites (installs the current version) rather
        than refusing — the skill install is idempotent."""
        target = tmp_path / "proj"
        target.mkdir()
        installed = target / ".agents" / "skills" / "sqrlly" / "SKILL.md"
        runner.invoke(cli, ["init", "--skill", str(target)])
        installed.write_text("stale\n")
        result = runner.invoke(cli, ["init", "--skill", str(target)])
        assert result.exit_code == 0
        assert installed.read_text() == _load_skill_doc()

    def test_plain_init_still_scaffolds_workflow(self, runner, tmp_path):
        """--skill is opt-in; bare init is unchanged."""
        target = tmp_path / "wf"
        result = runner.invoke(cli, ["init", str(target)])
        assert result.exit_code == 0
        assert (target / "workflow.yaml").exists()
        assert not (target / ".agents").exists()
