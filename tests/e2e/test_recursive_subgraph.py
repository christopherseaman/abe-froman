"""End-to-end recursive subgraph composition.

Multi-function flow tests following the testing methodology — known-good
artifacts (subgraph YAML files) drive concrete output assertions:

    1. Simple two-level: parent has one subgraph reference; subgraph
       has two nodes; assert subgraph terminal output projects to parent.
    2. Inputs projection: parent's `inputs:` declaration renders parent
       state into subgraph context; subgraph node uses {{input_var}}
       in its template.
    3. Multiple outputs: parent's `outputs:` exposes named subgraph
       node outputs; downstream parent nodes can consume them.
    4. Standalone runnability: the same YAML file is invokable both as
       a top-level workflow AND as a subgraph reference (proving the
       schema is identical).
    5. Cycle detection: A→B→A config chain raises SubgraphCycleError
       at compile time.
    6. Depth cap: nested chain longer than max_subgraph_depth raises.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from abe_froman.compile.graph import build_workflow_graph
from abe_froman.compile.subgraph import SubgraphCycleError, SubgraphDepthError
from abe_froman.runtime.executor.dispatch import DispatchExecutor
from abe_froman.runtime.state import make_initial_state
from abe_froman.schema.models import Graph


def _yaml(path: Path, body: dict) -> None:
    path.write_text(yaml.safe_dump(body))


@pytest.mark.asyncio
async def test_simple_recursive_subgraph(tmp_path):
    """Parent has one subgraph reference; subgraph terminal output projects up."""
    _yaml(tmp_path / "sub.yaml", {
        "name": "Sub", "version": "1.0",
        "nodes": [
            {
                "id": "child_a",
                "name": "Child A",
                "execution": {"type": "command", "command": "echo", "args": ["-n", "from-child-a"]},
            },
            {
                "id": "child_b",
                "name": "Child B",
                "depends_on": ["child_a"],
                "execution": {"type": "command", "command": "echo", "args": ["-n", "from-child-b"]},
            },
        ],
    })
    _yaml(tmp_path / "parent.yaml", {
        "name": "Parent", "version": "1.0",
        "nodes": [
            {
                "id": "sub_node",
                "name": "Subgraph Reference",
                "config": "sub.yaml",
            },
        ],
    })

    raw = yaml.safe_load((tmp_path / "parent.yaml").read_text())
    parent_config = Graph(**raw)
    executor = DispatchExecutor(workdir=str(tmp_path))
    graph = build_workflow_graph(parent_config, executor, _base_dir=tmp_path)

    result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

    assert "sub_node" in result["completed_nodes"]
    # Default outputs: subgraph terminal node (child_b) → parent.node_outputs[sub_node]
    assert result["node_outputs"]["sub_node"] == "from-child-b"


@pytest.mark.asyncio
async def test_subgraph_inputs_projection(tmp_path):
    """`inputs: {topic: '{{intake}}'}` makes {{topic}} available to subgraph prompt."""
    from abe_froman.runtime.executor.backends.stub import StubBackend

    (tmp_path / "research.md").write_text("Research about: {{topic}}")
    _yaml(tmp_path / "sub.yaml", {
        "name": "Research Sub", "version": "1.0",
        "nodes": [{
            "id": "research", "name": "Research",
            "prompt_file": "research.md",
        }],
    })
    _yaml(tmp_path / "parent.yaml", {
        "name": "Parent", "version": "1.0",
        "nodes": [
            {
                "id": "intake",
                "name": "Intake",
                "execution": {"type": "command", "command": "echo", "args": ["-n", "absurd nematodes"]},
            },
            {
                "id": "deep_research",
                "name": "Deep Research",
                "config": "sub.yaml",
                "depends_on": ["intake"],
                "inputs": {"topic": "{{intake}}"},
            },
        ],
    })

    raw = yaml.safe_load((tmp_path / "parent.yaml").read_text())
    parent_config = Graph(**raw)
    executor = DispatchExecutor(workdir=str(tmp_path), prompt_backend=StubBackend())
    graph = build_workflow_graph(parent_config, executor, _base_dir=tmp_path)

    result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

    # StubBackend echoes prompt_length. The rendered prompt is
    # "Research about: absurd nematodes" — len = 32. If {{topic}} were
    # NOT projected, the literal "{{topic}}" would remain (8 chars) and
    # length would differ.
    expected_len = len("Research about: absurd nematodes")
    sub_output = result["node_outputs"]["deep_research"]
    assert f"prompt_length={expected_len}" in sub_output, (
        f"expected rendered template (length {expected_len}); got {sub_output!r}"
    )


@pytest.mark.asyncio
async def test_subgraph_outputs_named_projection(tmp_path):
    """Explicit `outputs:` exposes named subgraph node outputs to parent."""
    _yaml(tmp_path / "sub.yaml", {
        "name": "Sub", "version": "1.0",
        "nodes": [
            {
                "id": "compile",
                "name": "Compile",
                "execution": {"type": "command", "command": "echo", "args": ["-n", "compiled-output"]},
            },
            {
                "id": "summary",
                "name": "Summary",
                "depends_on": ["compile"],
                "execution": {"type": "command", "command": "echo", "args": ["-n", "summary-output"]},
            },
        ],
    })
    _yaml(tmp_path / "parent.yaml", {
        "name": "Parent", "version": "1.0",
        "nodes": [{
            "id": "sub_node",
            "name": "Sub Ref",
            "config": "sub.yaml",
            "outputs": {"compiled": "{{compile}}", "summed": "{{summary}}"},
        }],
    })

    raw = yaml.safe_load((tmp_path / "parent.yaml").read_text())
    parent_config = Graph(**raw)
    executor = DispatchExecutor(workdir=str(tmp_path))
    graph = build_workflow_graph(parent_config, executor, _base_dir=tmp_path)

    result = await graph.ainvoke(make_initial_state(workdir=str(tmp_path)))

    assert result["node_outputs"]["sub_node.compiled"] == "compiled-output"
    assert result["node_outputs"]["sub_node.summed"] == "summary-output"


@pytest.mark.asyncio
async def test_subgraph_runnable_standalone(tmp_path):
    """Same YAML file runs both standalone and as a subgraph reference.

    Proves graphs and subgraphs are definitionally identical — there's
    no special schema for a "subgraph YAML"; it's just a graph file.
    """
    _yaml(tmp_path / "shared.yaml", {
        "name": "Shared", "version": "1.0",
        "nodes": [{
            "id": "work",
            "name": "Work",
            "execution": {"type": "command", "command": "echo", "args": ["-n", "shared-work"]},
        }],
    })

    # Standalone invocation
    raw = yaml.safe_load((tmp_path / "shared.yaml").read_text())
    standalone_config = Graph(**raw)
    executor = DispatchExecutor(workdir=str(tmp_path))
    standalone_graph = build_workflow_graph(standalone_config, executor)
    standalone_result = await standalone_graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
    assert standalone_result["node_outputs"]["work"] == "shared-work"

    # As a subgraph reference
    _yaml(tmp_path / "wrapper.yaml", {
        "name": "Wrapper", "version": "1.0",
        "nodes": [{"id": "ref", "name": "Ref", "config": "shared.yaml"}],
    })
    raw = yaml.safe_load((tmp_path / "wrapper.yaml").read_text())
    wrapper_config = Graph(**raw)
    wrapper_graph = build_workflow_graph(wrapper_config, executor, _base_dir=tmp_path)
    wrapper_result = await wrapper_graph.ainvoke(make_initial_state(workdir=str(tmp_path)))
    assert wrapper_result["node_outputs"]["ref"] == "shared-work"


def test_cycle_detected_at_compile_time(tmp_path):
    _yaml(tmp_path / "a.yaml", {
        "name": "A", "version": "1.0",
        "nodes": [{"id": "x", "name": "X", "config": "b.yaml"}],
    })
    _yaml(tmp_path / "b.yaml", {
        "name": "B", "version": "1.0",
        "nodes": [{"id": "y", "name": "Y", "config": "a.yaml"}],
    })

    raw = yaml.safe_load((tmp_path / "a.yaml").read_text())
    config = Graph(**raw)
    executor = DispatchExecutor(workdir=str(tmp_path))
    with pytest.raises(SubgraphCycleError):
        build_workflow_graph(config, executor, _base_dir=tmp_path)


def test_depth_limit_enforced(tmp_path):
    """Chain of 12 nested subgraphs exceeds default max_subgraph_depth=10.

    Each yaml's only node references the next yaml; recursion bottoms
    out when build_workflow_graph hits _depth > max_subgraph_depth.
    """
    chain_len = 12
    for i in range(chain_len):
        next_ref = f"link_{i+1}.yaml" if i + 1 < chain_len else None
        node = {"id": f"n{i}", "name": f"N{i}"}
        if next_ref:
            node["config"] = next_ref
        else:
            node["execution"] = {"type": "command", "command": "echo", "args": ["-n", "leaf"]}
        _yaml(tmp_path / f"link_{i}.yaml", {
            "name": f"L{i}", "version": "1.0",
            "nodes": [node],
        })

    raw = yaml.safe_load((tmp_path / "link_0.yaml").read_text())
    config = Graph(**raw)
    executor = DispatchExecutor(workdir=str(tmp_path))

    # Compile-time depth check: build_workflow_graph eagerly compiles
    # subgraphs (via make_subgraph_node), so the depth error fires at
    # compile time, not at invocation.
    with pytest.raises(SubgraphDepthError):
        build_workflow_graph(config, executor, _base_dir=tmp_path)
