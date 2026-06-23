"""`sqrlly run` end-of-run "where to find things" epilogue.

The live terminal renderer shows only per-node STATUS, never node stdout,
so the only user-visible place to announce output / log / artifact
locations is a CLI epilogue printed after the run. These tests pin the
exact content of that summary (real files on disk, no mocks)."""
from __future__ import annotations

from pathlib import Path

from sqrlly.cli.main import CHECKPOINT_DB, _run_artifact_summary
from sqrlly.schema.models import (
    Execute,
    Graph,
    Node,
    OutputContract,
    Settings,
)


def _graph(nodes):
    return Graph(name="t", version="0.0.0", nodes=nodes, settings=Settings())


def test_output_contract_files_listed_when_present(tmp_path):
    (tmp_path / "output").mkdir()
    paper = tmp_path / "output" / "paper.md"
    paper.write_text("hi")
    g = _graph([
        Node(
            id="write", name="Write",
            execute=Execute(url="/usr/bin/echo", params={"args": ["x"]}),
            output_contract=OutputContract(
                base_directory="output", required_files=["paper.md"],
            ),
            promote=True,
        ),
    ])
    lines = _run_artifact_summary(g, str(tmp_path), None)
    text = "\n".join(lines)
    assert str(paper.resolve()) in text
    # The output row, not the log/artifacts rows, carries it.
    out_row = next(l for l in lines if l.lstrip().startswith("output"))
    assert str(paper.resolve()) in out_row


def test_declared_output_not_yet_written_falls_back_to_run_state(tmp_path):
    """Contract declared but file absent → don't claim a path that isn't there."""
    g = _graph([
        Node(
            id="write", name="Write",
            execute=Execute(url="/usr/bin/echo", params={"args": ["x"]}),
            output_contract=OutputContract(
                base_directory="output", required_files=["missing.md"],
            ),
        ),
    ])
    lines = _run_artifact_summary(g, str(tmp_path), None)
    out_row = next(l for l in lines if l.lstrip().startswith("output"))
    assert "run state" in out_row
    assert "missing.md" not in out_row


def test_no_outputs_points_to_run_state_and_log_hint(tmp_path):
    g = _graph([
        Node(id="a", name="A", execute=Execute(url="a.md")),
    ])
    lines = _run_artifact_summary(g, str(tmp_path), None)
    text = "\n".join(lines)
    assert "run state" in text
    # No --log given → epilogue tells the user how to capture one.
    log_row = next(l for l in lines if l.lstrip().startswith("log"))
    assert "--log" in log_row


def test_log_path_surfaced_when_given(tmp_path):
    g = _graph([Node(id="a", name="A", execute=Execute(url="a.md"))])
    log = tmp_path / "run.jsonl"
    lines = _run_artifact_summary(g, str(tmp_path), str(log))
    log_row = next(l for l in lines if l.lstrip().startswith("log"))
    assert str(log.resolve()) in log_row
    assert "--log" not in log_row


def test_checkpoint_and_worktree_pool_listed(tmp_path):
    (tmp_path / CHECKPOINT_DB).write_text("")  # AsyncSqliteSaver leaves this
    (tmp_path / ".sqrlly").mkdir()              # foreman worktree pool
    g = _graph([Node(id="a", name="A", execute=Execute(url="a.md"))])
    lines = _run_artifact_summary(g, str(tmp_path), None)
    art_row = next(l for l in lines if l.lstrip().startswith("artifacts"))
    assert str((tmp_path / CHECKPOINT_DB).resolve()) in art_row
    assert str((tmp_path / ".sqrlly").resolve()) in art_row


def test_no_artifacts_row_when_none_exist(tmp_path):
    """A pure non-git script run leaves no checkpoint/worktree pool → no
    artifacts row (don't point at paths that don't exist)."""
    g = _graph([Node(id="a", name="A", execute=Execute(url="a.md"))])
    lines = _run_artifact_summary(g, str(tmp_path), None)
    assert not any(l.lstrip().startswith("artifacts") for l in lines)
