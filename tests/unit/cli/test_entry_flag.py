"""--entry <node>: cold-start argument validation. The behavioral proof
(node a does NOT run, b+c DO, from a fresh workdir) is the e2e in
tests/e2e/test_entry_cold_start.py. These pin the CLI guards only."""
from __future__ import annotations

import textwrap
from pathlib import Path

from click.testing import CliRunner

from sqrlly.cli.main import cli


def _write_workflow(tmp_path: Path) -> Path:
    """Minimal 3-node linear command workflow (echo nodes — no backend)."""
    import shutil
    echo = shutil.which("echo") or "/bin/echo"
    wf = tmp_path / "wf.yaml"
    wf.write_text(textwrap.dedent(f"""\
        name: entry-validate
        version: "0.1.0"
        nodes:
          - id: a
            name: a
            execute:
              url: {echo}
              params: {{args: ["-n", "a-out"]}}
          - id: b
            name: b
            depends_on: [a]
            execute:
              url: {echo}
              params: {{args: ["-n", "b-out"]}}
          - id: c
            name: c
            depends_on: [b]
            execute:
              url: {echo}
              params: {{args: ["-n", "c-out"]}}
    """))
    return wf


def test_entry_unknown_node_errors(tmp_path):
    wf = _write_workflow(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        cli, ["run", str(wf), "-w", str(tmp_path), "--entry", "nope", "-q"],
    )
    assert res.exit_code != 0
    assert "unknown node id" in res.output
    assert "nope" in res.output


def test_entry_rejects_fanout_child_id(tmp_path):
    wf = _write_workflow(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        cli, ["run", str(wf), "-w", str(tmp_path), "--entry", "a::x", "-q"],
    )
    assert res.exit_code != 0
    assert "fan-out children are not addressable" in res.output


def test_entry_with_resume_is_rejected(tmp_path):
    wf = _write_workflow(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        cli,
        ["run", str(wf), "-w", str(tmp_path), "--entry", "b", "--resume", "-q"],
    )
    assert res.exit_code != 0
    assert "--entry" in res.output
    assert "mutually exclusive" in res.output


def test_entry_with_resume_from_is_rejected(tmp_path):
    wf = _write_workflow(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        cli,
        ["run", str(wf), "-w", str(tmp_path),
         "--entry", "b", "--resume-from", "a", "-q"],
    )
    assert res.exit_code != 0
    assert "--entry" in res.output
    assert "mutually exclusive" in res.output


def test_entry_with_rerun_all_is_rejected(tmp_path):
    wf = _write_workflow(tmp_path)
    runner = CliRunner()
    res = runner.invoke(
        cli,
        ["run", str(wf), "-w", str(tmp_path),
         "--entry", "b", "--rerun-all", "-q"],
    )
    assert res.exit_code != 0
    assert "--entry" in res.output
    assert "mutually exclusive" in res.output
