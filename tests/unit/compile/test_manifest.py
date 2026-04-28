"""Unit tests for _read_manifest from builder.py."""

import json

import pytest

from abe_froman.compile.graph import _read_manifest
from abe_froman.runtime.state import make_initial_state
from abe_froman.schema.models import FanOut, Node, FanOutTemplate


def _phase_with_dynamic(manifest_path=None) -> Node:
    return Node(
        id="p1", name="P1",
        prompt_file="t.md",
        fan_out=FanOut(
            enabled=True,
            manifest_path=manifest_path,
            template=FanOutTemplate(prompt_file="sub.md"),
        ),
    )


# ---------------------------------------------------------------------------
# Manifest from node output
# ---------------------------------------------------------------------------


class TestReadManifestFromOutput:
    def test_json_with_items_key(self):
        output = json.dumps({"items": [{"id": "a"}, {"id": "b"}]})
        state = make_initial_state(node_outputs={"p1": output})
        node = _phase_with_dynamic()
        result = _read_manifest(state, node)
        assert result == [{"id": "a"}, {"id": "b"}]

    def test_json_bare_list(self):
        output = json.dumps([{"id": "a"}])
        state = make_initial_state(node_outputs={"p1": output})
        node = _phase_with_dynamic()
        result = _read_manifest(state, node)
        assert result == [{"id": "a"}]

    def test_non_json_falls_through(self):
        state = make_initial_state(node_outputs={"p1": "plain text"})
        node = _phase_with_dynamic()
        result = _read_manifest(state, node)
        assert result == []

    def test_json_dict_without_items(self):
        output = json.dumps({"other": "data"})
        state = make_initial_state(node_outputs={"p1": output})
        node = _phase_with_dynamic()
        result = _read_manifest(state, node)
        assert result == []


# ---------------------------------------------------------------------------
# Manifest from disk
# ---------------------------------------------------------------------------


class TestReadManifestFromDisk:
    def test_disk_items_key(self, tmp_path):
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"items": [{"id": "x"}]}))
        state = make_initial_state(workdir=str(tmp_path))
        node = _phase_with_dynamic(manifest_path="manifest.json")
        result = _read_manifest(state, node)
        assert result == [{"id": "x"}]

    def test_disk_bare_list(self, tmp_path):
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps([{"id": "y"}]))
        state = make_initial_state(workdir=str(tmp_path))
        node = _phase_with_dynamic(manifest_path="manifest.json")
        result = _read_manifest(state, node)
        assert result == [{"id": "y"}]

    def test_disk_file_not_found(self, tmp_path):
        state = make_initial_state(workdir=str(tmp_path))
        node = _phase_with_dynamic(manifest_path="missing.json")
        result = _read_manifest(state, node)
        assert result == []

    def test_disk_bad_json(self, tmp_path):
        manifest = tmp_path / "manifest.json"
        manifest.write_text("not valid json{{{")
        state = make_initial_state(workdir=str(tmp_path))
        node = _phase_with_dynamic(manifest_path="manifest.json")
        result = _read_manifest(state, node)
        assert result == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestReadManifestEdgeCases:
    def test_no_dynamic_subphases(self):
        node = Node(id="p1", name="P1", prompt_file="t.md")
        state = make_initial_state()
        result = _read_manifest(state, node)
        assert result == []

    def test_output_takes_precedence_over_disk(self, tmp_path):
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"items": [{"id": "disk"}]}))
        output = json.dumps({"items": [{"id": "output"}]})
        state = make_initial_state(
            workdir=str(tmp_path),
            node_outputs={"p1": output},
        )
        node = _phase_with_dynamic(manifest_path="manifest.json")
        result = _read_manifest(state, node)
        assert result == [{"id": "output"}]
