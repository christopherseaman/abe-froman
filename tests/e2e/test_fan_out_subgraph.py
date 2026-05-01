"""End-to-end per-child subgraph fan-out (Stage 5b carve-out).

Each fan-out manifest item drives one Send branch through a compiled
subgraph instead of a single executor call. Demonstrates:

    1. test_fan_out_per_child_subgraph_dispatch — 2-item manifest, each
       Send runs a 2-node draft→critique subgraph; downstream sees both
       items' terminal outputs in `child_outputs`.
    2. test_fan_out_subgraph_failure_surfaces_to_parent — a subgraph
       node that fails (real `false` exit code) flips that branch's
       child_id into `failed_nodes` on the parent.
    3. test_fan_out_subgraph_cycle_detected_at_compile_time — a fan-out
       template referencing a cyclic subgraph chain raises
       SubgraphCycleError before any execution.
    4. test_fan_out_subgraph_inputs_render_per_item — each Send branch
       renders its own manifest fields into the subgraph's `inputs:`,
       so per-item state never leaks between branches.

All tests use real DispatchExecutor + real subprocesses (echo / false);
no mocks of any kind.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.compile.subgraph import SubgraphCycleError
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.state import make_initial_state
from abe_froman.schema.models import Graph

_ECHO = shutil.which("echo") or "/bin/echo"
_FALSE = shutil.which("false") or "/bin/false"


def _yaml(path: Path, body: dict) -> None:
    path.write_text(yaml.safe_dump(body))


def _parent_with_fan_out_subgraph(items, sub_yaml_name="single.yaml",
                                  inputs_decl=None):
    """Build a parent-graph dict whose fan-out template runs a subgraph."""
    manifest = json.dumps({"items": items})
    template_execute = {"url": sub_yaml_name}
    if inputs_decl:
        template_execute["params"] = {"inputs": inputs_decl}
    return {
        "name": "Parent", "version": "1.0",
        "nodes": [
            {
                "id": "manifest_emitter",
                "name": "Manifest Emitter",
                "execute": {"url": _ECHO, "params": {"args": ["-n", manifest]}},
                "fan_out": {
                    "enabled": True,
                    "template": {"execute": template_execute},
                },
            },
        ],
    }


@pytest.mark.asyncio
async def test_fan_out_per_child_subgraph_dispatch(tmp_path):
    """2-item manifest → 2 Send branches → each runs a 2-node subgraph.

    Asserts both items' terminal outputs land in `child_outputs[parent::id]`.
    """
    # 2-node subgraph: step1 echoes input, step2 echoes f"final-{step1}".
    _yaml(tmp_path / "single.yaml", {
        "name": "Single", "version": "1.0",
        "nodes": [
            {
                "id": "step1",
                "name": "Step 1",
                "execute": {"url": _ECHO, "params": {"args": ["{{topic}}"]}},
            },
            {
                "id": "step2",
                "name": "Step 2",
                "depends_on": ["step1"],
                "execute": {"url": _ECHO, "params": {"args": ["final-{{step1}}"]}},
            },
        ],
    })

    items = [
        {"id": "alpha", "topic": "ALPHA"},
        {"id": "beta", "topic": "BETA"},
    ]
    parent_dict = _parent_with_fan_out_subgraph(
        items, inputs_decl={"topic": "{{topic}}"},
    )
    parent_config = Graph(**parent_dict)
    executor = DispatchExecutor(workdir=str(tmp_path))
    graph = build_workflow_graph(parent_config, executor, _base_dir=tmp_path)
    result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

    assert "manifest_emitter::alpha" in result["completed_nodes"]
    assert "manifest_emitter::beta" in result["completed_nodes"]

    # Each branch's terminal output is the subgraph's terminal node (step2)
    # which echoes "final-<topic>\n" — but echo strips the literal
    # newline because subprocess buffers it. Either way "final-ALPHA"
    # appears in the alpha-branch output, "final-BETA" in beta-branch.
    alpha_output = result["child_outputs"]["manifest_emitter::alpha"]
    beta_output = result["child_outputs"]["manifest_emitter::beta"]
    assert "final-ALPHA" in alpha_output, f"alpha got: {alpha_output!r}"
    assert "final-BETA" in beta_output, f"beta got: {beta_output!r}"
    # Per-item isolation: alpha branch must NOT see beta's topic
    assert "BETA" not in alpha_output
    assert "ALPHA" not in beta_output


@pytest.mark.asyncio
async def test_fan_out_subgraph_failure_surfaces_to_parent(tmp_path):
    """A failing subgraph node flips the branch's child_id into failed_nodes."""
    _yaml(tmp_path / "fail_sub.yaml", {
        "name": "Fail Sub", "version": "1.0",
        "nodes": [
            {
                "id": "always_fails",
                "name": "Always Fails",
                # `false` always exits 1
                "execute": {"url": _FALSE},
            },
        ],
    })

    items = [{"id": "doomed"}]
    parent_dict = _parent_with_fan_out_subgraph(
        items, sub_yaml_name="fail_sub.yaml",
    )
    parent_config = Graph(**parent_dict)
    executor = DispatchExecutor(workdir=str(tmp_path))
    graph = build_workflow_graph(parent_config, executor, _base_dir=tmp_path)
    result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

    # Subgraph internal failure surfaces as fan-out child failure on parent.
    assert "manifest_emitter::doomed" in result["failed_nodes"]
    assert "manifest_emitter::doomed" not in result["completed_nodes"]


@pytest.mark.asyncio
async def test_fan_out_subgraph_cycle_detected_at_compile_time(tmp_path):
    """Template subgraph that references back into the parent's chain
    raises SubgraphCycleError at compile (not runtime)."""
    # a.yaml references b.yaml; b.yaml references a.yaml
    _yaml(tmp_path / "a.yaml", {
        "name": "A", "version": "1.0",
        "nodes": [
            {
                "id": "ref_to_b",
                "name": "ref to b",
                "execute": {"url": "b.yaml"},
            },
        ],
    })
    _yaml(tmp_path / "b.yaml", {
        "name": "B", "version": "1.0",
        "nodes": [
            {
                "id": "ref_to_a",
                "name": "ref to a",
                "execute": {"url": "a.yaml"},
            },
        ],
    })

    items = [{"id": "x"}]
    parent_dict = _parent_with_fan_out_subgraph(
        items, sub_yaml_name="a.yaml",
    )
    parent_config = Graph(**parent_dict)
    executor = DispatchExecutor(workdir=str(tmp_path))
    # Cycle is detected during the subgraph compilation that the fan-out
    # invoker triggers — happens at parent-graph compile time, before
    # any runtime ainvoke.
    with pytest.raises(SubgraphCycleError):
        build_workflow_graph(parent_config, executor, _base_dir=tmp_path)


@pytest.mark.asyncio
async def test_fan_out_subgraph_inputs_render_per_item(tmp_path):
    """Each Send branch's `inputs:` render against its own manifest fields.

    The subgraph receives `{{persona}}` rendered freshly per branch. If
    state leaked, both branches would see the same persona.
    """
    _yaml(tmp_path / "persona_sub.yaml", {
        "name": "Persona Sub", "version": "1.0",
        "nodes": [
            {
                "id": "greet",
                "name": "Greet",
                "execute": {
                    "url": _ECHO,
                    "params": {"args": ["hello-{{persona}}"]},
                },
            },
        ],
    })

    items = [
        {"id": "x", "persona": "wizard"},
        {"id": "y", "persona": "rogue"},
    ]
    parent_dict = _parent_with_fan_out_subgraph(
        items, sub_yaml_name="persona_sub.yaml",
        inputs_decl={"persona": "{{persona}}"},
    )
    parent_config = Graph(**parent_dict)
    executor = DispatchExecutor(workdir=str(tmp_path))
    graph = build_workflow_graph(parent_config, executor, _base_dir=tmp_path)
    result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

    x_out = result["child_outputs"]["manifest_emitter::x"]
    y_out = result["child_outputs"]["manifest_emitter::y"]
    assert "hello-wizard" in x_out
    assert "hello-rogue" in y_out
    # Cross-branch isolation
    assert "rogue" not in x_out
    assert "wizard" not in y_out
