"""CLI-surface tests for `sqrlly init --example` / `--list-examples`."""
from __future__ import annotations

from click.testing import CliRunner

from sqrlly.cli.main import cli


def test_list_examples_flag_lists_all():
    res = CliRunner().invoke(cli, ["init", "--list-examples"])
    assert res.exit_code == 0
    for name in ("jokes", "route_classify", "explicit_join", "pipeline_style"):
        assert name in res.output


def test_example_scaffolds_into_named_subdir_by_default(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        res = runner.invoke(cli, ["init", "--example", "explicit_join"])
        assert res.exit_code == 0, res.output
        from pathlib import Path
        assert (Path("explicit_join") / "workflow.yaml").is_file()


def test_example_into_explicit_dir(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        res = runner.invoke(cli, ["init", "--example", "jokes", "myj"])
        assert res.exit_code == 0, res.output
        from pathlib import Path
        assert (Path("myj") / "workflow.yaml").is_file()


def test_example_and_skill_are_mutually_exclusive():
    res = CliRunner().invoke(cli, ["init", "--example", "jokes", "--skill"])
    assert res.exit_code != 0
    assert "not be combined" in res.output or "mutually exclusive" in res.output


def test_plain_init_still_defaults_to_cwd(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        res = runner.invoke(cli, ["init"])
        assert res.exit_code == 0, res.output
        from pathlib import Path
        assert Path("workflow.yaml").is_file()   # scaffolded into cwd
