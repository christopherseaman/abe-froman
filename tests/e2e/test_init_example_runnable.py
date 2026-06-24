"""Every scaffolded example compiles from its own dir; a no-backend one runs."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sqrlly.cli.init import EXAMPLES, init_example
from sqrlly.cli.main import load_config
from sqrlly.compile.graph import build_workflow_graph


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_scaffolded_example_validates(name, tmp_path):
    dest = tmp_path / name
    init_example(name, str(dest))
    cwd = os.getcwd()
    os.chdir(dest)            # relative URLs resolve against the workdir
    try:
        config = load_config("workflow.yaml")
        build_workflow_graph(config)          # raises on any wiring error
    finally:
        os.chdir(cwd)
    assert config.name


def test_scaffolded_explicit_join_runs(tmp_path):
    import asyncio

    from sqrlly.runtime.executor.dispatch import DispatchExecutor
    from sqrlly.runtime.state import make_initial_state

    dest = tmp_path / "ej"
    init_example("explicit_join", str(dest))
    cwd = os.getcwd()
    os.chdir(dest)
    try:
        config = load_config("workflow.yaml")
        graph = build_workflow_graph(config, DispatchExecutor(workdir="."))
        result = asyncio.run(graph.ainvoke(make_initial_state(workdir=".")))
    finally:
        os.chdir(cwd)
    assert "report" in result["completed_nodes"]
    assert not result.get("failed_nodes")
