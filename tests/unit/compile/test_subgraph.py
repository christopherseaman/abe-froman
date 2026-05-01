"""Unit tests for compile/subgraph.py — single functions, known-good/bad pairs.

End-to-end recursive composition is exercised in tests/e2e/test_recursive_subgraph.py.
This module covers the helpers in isolation.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from abe_froman.compile.subgraph import (
    SubgraphCycleError,
    detect_config_cycle,
    load_graph,
)
from abe_froman.schema.models import Graph


def _write_yaml(tmp_path, name: str, body: dict) -> str:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(body))
    return name


class TestLoadGraph:
    def test_loads_valid_graph(self, tmp_path):
        rel = _write_yaml(tmp_path, "ok.yaml", {
            "name": "G",
            "version": "1.0.0",
            "nodes": [{"id": "x", "name": "X", "execute": {"url": "x.md"}}],
        })
        g = load_graph(rel, base_dir=tmp_path)
        assert isinstance(g, Graph)
        assert g.nodes[0].id == "x"

    def test_raises_on_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_graph("nope.yaml", base_dir=tmp_path)

    def test_raises_on_invalid_schema(self, tmp_path):
        rel = _write_yaml(tmp_path, "bad.yaml", {"name": "G", "version": "1.0.0"})
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            load_graph(rel, base_dir=tmp_path)


class TestDetectConfigCycle:
    """Walk config-reference DAG; raise on cycle, return None on valid graph."""

    def test_no_cycle_passes(self, tmp_path):
        _write_yaml(tmp_path, "leaf.yaml", {
            "name": "Leaf", "version": "1.0",
            "nodes": [{"id": "x", "name": "X", "execute": {"url": "x.md"}}],
        })
        _write_yaml(tmp_path, "root.yaml", {
            "name": "Root", "version": "1.0",
            "nodes": [
                {"id": "uses_leaf", "name": "Uses Leaf", "execute": {"url": "leaf.yaml"}}
            ],
        })
        # No exception
        detect_config_cycle("root.yaml", base_dir=tmp_path)

    def test_self_reference_cycle(self, tmp_path):
        _write_yaml(tmp_path, "loop.yaml", {
            "name": "Loop", "version": "1.0",
            "nodes": [
                {"id": "self", "name": "Self", "execute": {"url": "loop.yaml"}}
            ],
        })
        with pytest.raises(SubgraphCycleError) as exc:
            detect_config_cycle("loop.yaml", base_dir=tmp_path)
        assert "loop.yaml" in str(exc.value)

    def test_two_step_cycle(self, tmp_path):
        _write_yaml(tmp_path, "a.yaml", {
            "name": "A", "version": "1.0",
            "nodes": [{"id": "x", "name": "X", "execute": {"url": "b.yaml"}}],
        })
        _write_yaml(tmp_path, "b.yaml", {
            "name": "B", "version": "1.0",
            "nodes": [{"id": "y", "name": "Y", "execute": {"url": "a.yaml"}}],
        })
        with pytest.raises(SubgraphCycleError) as exc:
            detect_config_cycle("a.yaml", base_dir=tmp_path)
        assert "a.yaml" in str(exc.value)


class TestNodeFieldsForSubgraph:
    """Schema validation for the Stage-5b subgraph shape: `execute.url` points
    at a `.yaml` file; `params.inputs` / `params.outputs` carry projection."""

    def test_subgraph_url_only(self):
        g = Graph(
            name="P", version="1.0",
            nodes=[{
                "id": "sub", "name": "Sub",
                "execute": {
                    "url": "child.yaml",
                    "params": {"inputs": {"topic": "{{intake}}"}},
                },
            }],
        )
        n = g.nodes[0]
        assert n.execute is not None
        assert n.execute.url == "child.yaml"
        assert n.execute.params == {"inputs": {"topic": "{{intake}}"}}

    def test_outputs_default_empty_when_omitted(self):
        g = Graph(
            name="P", version="1.0",
            nodes=[{"id": "sub", "name": "Sub", "execute": {"url": "child.yaml"}}],
        )
        n = g.nodes[0]
        # Default: no params at all → empty dict
        assert n.execute.params == {}

    def test_subgraph_with_explicit_outputs(self):
        g = Graph(
            name="P", version="1.0",
            nodes=[{
                "id": "sub", "name": "Sub",
                "execute": {
                    "url": "child.yaml",
                    "params": {"outputs": {"key": "{{terminal}}"}},
                },
            }],
        )
        assert g.nodes[0].execute.params["outputs"] == {"key": "{{terminal}}"}
