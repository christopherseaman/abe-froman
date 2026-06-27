"""CLI-surface tests for `sqrlly init --example` / `--list-examples`."""
from __future__ import annotations

from click.testing import CliRunner

from sqrlly.cli.main import cli


def test_list_examples_flag_lists_all():
    res = CliRunner().invoke(cli, ["init", "--list-examples"])
    assert res.exit_code == 0
    for name in (
        "jokes", "route_classify", "explicit_join", "pipeline_style",
        "absurd-paper",
    ):
        assert name in res.output


def test_absurd_paper_scaffolds_multifile_and_rewrites_subgraph(tmp_path):
    """The multi-file showcase scaffolds every runnable file and rewrites
    the ``examples/absurd-paper/`` prefix in ALL yaml (workflow + subgraphs),
    not just workflow.yaml — and the result is a valid workflow."""
    from pathlib import Path

    from sqrlly.cli.main import load_config

    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        res = runner.invoke(cli, ["init", "--example", "absurd-paper"])
        assert res.exit_code == 0, res.output
        root = Path("absurd-paper")
        # Representative files across every subdir are present.
        for rel in (
            "workflow.yaml",
            "subgraphs/compose_and_validate.yaml",
            "prompts/choose_topic.md",
            "gates/outline_json.py",
            "scripts/render_pdf.py",
            "preamble.md",
        ):
            assert (root / rel).is_file(), rel
        # The prefix is stripped from the subgraph too (not only workflow.yaml).
        sub = (root / "subgraphs/compose_and_validate.yaml").read_text()
        assert "examples/absurd-paper/" not in sub
        assert "scripts/persist_paper.py" in sub
        # And the scaffolded copy is a valid, self-contained workflow.
        config = load_config(str(root / "workflow.yaml"))
        assert config.name == "Absurd Academic Paper"


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
