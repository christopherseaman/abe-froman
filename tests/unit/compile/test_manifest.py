"""Unit tests for _read_manifest from builder.py."""

import json

import pytest

from sqrlly.compile._manifest import _read_manifest
from sqrlly.runtime.result import ManifestError
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Execute, FanOut, Node, FanOutTemplate


def _phase_with_dynamic(manifest_path=None) -> Node:
    return Node(
        id="p1", name="P1",
        execute=Execute(url="t.md"),
        fan_out=FanOut(
            
            manifest_path=manifest_path,
            template=FanOutTemplate(execute=Execute(url="sub.md")),
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

    def test_fenced_json_array_is_unwrapped(self):
        """An LLM parent that wraps the manifest in a ```json fence still
        fans out (not 'resolved to zero items')."""
        output = '```json\n[{"id": "a"}, {"id": "b"}]\n```'
        state = make_initial_state(node_outputs={"p1": output})
        result = _read_manifest(state, _phase_with_dynamic())
        assert result == [{"id": "a"}, {"id": "b"}]

    def test_preamble_then_array_is_extracted(self):
        output = 'Here is the manifest:\n[{"id": "x"}]'
        state = make_initial_state(node_outputs={"p1": output})
        result = _read_manifest(state, _phase_with_dynamic())
        assert result == [{"id": "x"}]

    def test_fenced_items_object_is_unwrapped(self):
        output = '```\n{"items": [{"id": "y"}]}\n```'
        state = make_initial_state(node_outputs={"p1": output})
        result = _read_manifest(state, _phase_with_dynamic())
        assert result == [{"id": "y"}]

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

    def test_disk_file_not_found_halts(self, tmp_path):
        """A declared manifest_path that doesn't exist is an author error
        — halt loudly rather than silently fanning out over zero items."""
        state = make_initial_state(workdir=str(tmp_path))
        node = _phase_with_dynamic(manifest_path="missing.json")
        with pytest.raises(ManifestError, match="could not be read"):
            _read_manifest(state, node)

    def test_disk_bad_json_halts(self, tmp_path):
        manifest = tmp_path / "manifest.json"
        manifest.write_text("not valid json{{{")
        state = make_initial_state(workdir=str(tmp_path))
        node = _phase_with_dynamic(manifest_path="manifest.json")
        with pytest.raises(ManifestError, match="not valid JSON"):
            _read_manifest(state, node)

    def test_disk_wrong_structure_halts(self, tmp_path):
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps({"not_items": 1}))
        state = make_initial_state(workdir=str(tmp_path))
        node = _phase_with_dynamic(manifest_path="manifest.json")
        with pytest.raises(ManifestError, match="must be a JSON array"):
            _read_manifest(state, node)

    def test_disk_empty_list_is_ok(self, tmp_path):
        """A valid but empty manifest is NOT an error — it returns []
        and the router sends it to no_items (with a warning)."""
        manifest = tmp_path / "manifest.json"
        manifest.write_text(json.dumps([]))
        state = make_initial_state(workdir=str(tmp_path))
        node = _phase_with_dynamic(manifest_path="manifest.json")
        assert _read_manifest(state, node) == []


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestReadManifestEdgeCases:
    def test_no_fan_out(self):
        node = Node(id="p1", name="P1", execute=Execute(url="t.md"))
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


class TestNormalizeItems:
    def test_scalar_items_coerced_to_id_objects_from_output(self):
        """`["alpha","beta"]` must fan out as {"id": ...} objects, not
        crash later on item.get("id")."""
        state = make_initial_state(node_outputs={"p1": json.dumps(["alpha", "beta"])})
        result = _read_manifest(state, _phase_with_dynamic())
        assert result == [{"id": "alpha"}, {"id": "beta"}]

    def test_scalar_items_coerced_from_disk(self, tmp_path):
        (tmp_path / "manifest.json").write_text(json.dumps([1, 2]))
        state = make_initial_state(workdir=str(tmp_path))
        node = _phase_with_dynamic(manifest_path="manifest.json")
        assert _read_manifest(state, node) == [{"id": "1"}, {"id": "2"}]

    def test_objects_pass_through_unchanged(self):
        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"id": "a", "topic": "x"}])}
        )
        assert _read_manifest(state, _phase_with_dynamic()) == [
            {"id": "a", "topic": "x"}
        ]

    def test_nested_list_item_raises(self):
        state = make_initial_state(node_outputs={"p1": json.dumps([["nested"]])})
        with pytest.raises(ValueError, match="must be an object or scalar"):
            _read_manifest(state, _phase_with_dynamic())


    def test_dict_item_missing_id_warns(self, caplog):
        """A dict manifest item with no 'id' collapses every branch to one
        ::unknown child — _normalize_items must WARN so the author sees it."""
        import logging

        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"topic": "x"}, {"topic": "y"}])}
        )
        with caplog.at_level(logging.WARNING):
            result = _read_manifest(state, _phase_with_dynamic())
        # Items pass through unchanged (no synthetic id injected).
        assert result == [{"topic": "x"}, {"topic": "y"}]
        # One warning per id-less dict item.
        missing_id_warnings = [
            r for r in caplog.records if "missing 'id'" in r.message
        ]
        assert len(missing_id_warnings) == 2

    def test_dict_item_with_id_is_silent(self, caplog):
        import logging

        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"id": "a"}, {"id": "b"}])}
        )
        with caplog.at_level(logging.WARNING):
            _read_manifest(state, _phase_with_dynamic())
        assert not any("missing 'id'" in r.message for r in caplog.records)

    def test_scalar_items_do_not_warn(self, caplog):
        """Scalars are coerced to {'id': str(item)} → they have an id → silent."""
        import logging

        state = make_initial_state(node_outputs={"p1": json.dumps(["alpha", "beta"])})
        with caplog.at_level(logging.WARNING):
            _read_manifest(state, _phase_with_dynamic())
        assert not any("missing 'id'" in r.message for r in caplog.records)


class TestEmptyManifestRouting:
    def test_router_warns_and_routes_to_no_items(self, caplog):
        """An empty (but valid) manifest is not silent: the dynamic
        router logs a warning and routes to the no_items target(s)."""
        import logging

        from sqrlly.compile.graph import _make_dynamic_router

        node = _phase_with_dynamic()  # fan_out, no manifest_path
        router = _make_dynamic_router(node, no_items_targets=["after"])
        state = make_initial_state(node_outputs={"p1": ""})  # no manifest
        with caplog.at_level(logging.WARNING):
            result = router(state)
        assert result == ["after"]
        assert any("zero items" in r.message for r in caplog.records)


class TestDuplicateChildIds:
    def test_duplicate_literal_ids_raise(self):
        """Two manifest items with the same 'id' would dispatch two Send
        branches onto one child id — raise before dispatch."""
        from sqrlly.compile.graph import _make_dynamic_router
        from sqrlly.runtime.result import ManifestError

        node = _phase_with_dynamic()  # parent id 'p1'
        router = _make_dynamic_router(node, no_items_targets=["after"])
        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"id": "dup"}, {"id": "dup"}])}
        )
        with pytest.raises(ManifestError, match="duplicate"):
            router(state)

    def test_unknown_collapse_raises(self):
        """≥2 id-less dict items all map to p1::unknown — a duplicate."""
        from sqrlly.compile.graph import _make_dynamic_router
        from sqrlly.runtime.result import ManifestError

        node = _phase_with_dynamic()
        router = _make_dynamic_router(node, no_items_targets=["after"])
        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"topic": "a"}, {"topic": "b"}])}
        )
        with pytest.raises(ManifestError, match="unknown"):
            router(state)

    def test_unique_ids_dispatch_one_send_each(self):
        """Distinct ids → no raise; one Send per item."""
        from langgraph.types import Send

        from sqrlly.compile.graph import _make_dynamic_router

        node = _phase_with_dynamic()
        router = _make_dynamic_router(node, no_items_targets=["after"])
        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"id": "a"}, {"id": "b"}])}
        )
        result = router(state)
        assert isinstance(result, list) and len(result) == 2
        assert all(isinstance(s, Send) for s in result)
        sent_ids = sorted(s.arg["_fan_out_item"]["id"] for s in result)
        assert sent_ids == ["a", "b"]
