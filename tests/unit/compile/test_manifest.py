"""Unit tests for _read_manifest from builder.py."""

import json

import pytest

from sqrlly.compile._manifest import _read_manifest
from sqrlly.runtime.result import ManifestError
from sqrlly.runtime.state import make_initial_state
from sqrlly.schema.models import Execute, FanOut, Node, FanOutTemplate, Settings


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


class TestFanDispatcher:
    """The `_fan_<id>` dispatcher node replaces the conditional-edge router.

    It returns ``Command(goto=...)``: a plain ``{}`` defer while the parent
    is unsettled (step 2), ``Command(goto=no_items_targets)`` + WARN on an
    empty manifest (step 3), a ``make_failure_update`` Command on duplicate
    child ids (step 4, NOT a raise), and ``Command(goto=[Send(...)])`` per
    item otherwise (step 5). Each test seeds ``completed_nodes={"p1"}`` so the
    step-2 defer doesn't fire before the branch under test.
    """

    def test_defers_when_parent_unsettled(self, caplog):
        """Step 2: parent not in completed ∪ failed → plain {} defer, no WARN."""
        import logging

        from sqrlly.compile.graph import _make_fan_dispatcher

        node = _phase_with_dynamic()
        fan = _make_fan_dispatcher(node, no_items_targets=["after"], settings=Settings())
        # completed_nodes empty → parent unsettled.
        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"id": "a"}])}
        )
        with caplog.at_level(logging.WARNING):
            result = fan(state)
        assert result == {}
        assert not any("zero items" in r.message for r in caplog.records)

    def test_warns_and_routes_to_no_items(self, caplog):
        """Step 3: an empty (but valid) manifest logs a WARN and routes to
        the no_items target(s) via Command(goto=...)."""
        import logging

        from langgraph.graph import END
        from langgraph.types import Command

        from sqrlly.compile.graph import _make_fan_dispatcher

        node = _phase_with_dynamic()  # fan_out, no manifest_path
        fan = _make_fan_dispatcher(node, no_items_targets=["after"], settings=Settings())
        state = make_initial_state(
            node_outputs={"p1": ""}, completed_nodes={"p1"},  # settled, no manifest
        )
        with caplog.at_level(logging.WARNING):
            result = fan(state)
        assert isinstance(result, Command)
        # Concrete-id list (not an unwrapped scalar) so an empty manifest can
        # fan to every dependent.
        assert result.goto == ["after"]
        assert any("zero items" in r.message for r in caplog.records)

    def test_parent_failed_routes_END(self):
        """Step 1: parent in failed_nodes → Command(goto=END)."""
        from langgraph.graph import END
        from langgraph.types import Command

        from sqrlly.compile.graph import _make_fan_dispatcher

        node = _phase_with_dynamic()
        fan = _make_fan_dispatcher(node, no_items_targets=["after"], settings=Settings())
        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"id": "a"}])},
            failed_nodes={"p1"},
        )
        result = fan(state)
        assert isinstance(result, Command)
        assert result.goto == END

    def test_duplicate_literal_ids_fail_the_node(self):
        """Step 4: two items with the same 'id' → a make_failure_update
        Command (parent in failed_nodes, fail-loud message), NOT a raise."""
        from langgraph.graph import END
        from langgraph.types import Command

        from sqrlly.compile.graph import _make_fan_dispatcher

        node = _phase_with_dynamic()  # parent id 'p1'
        fan = _make_fan_dispatcher(node, no_items_targets=["after"], settings=Settings())
        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"id": "dup"}, {"id": "dup"}])},
            completed_nodes={"p1"},
        )
        result = fan(state)
        assert isinstance(result, Command)
        assert result.goto == END
        assert result.update["failed_nodes"] == {"p1"}
        assert any(
            "duplicate" in e["error"] for e in result.update["errors"]
        )

    def test_unknown_collapse_fails_the_node(self):
        """Step 4: ≥2 id-less dict items all map to p1::unknown — a duplicate,
        surfaced as a failure Command whose error names 'unknown'."""
        from langgraph.graph import END
        from langgraph.types import Command

        from sqrlly.compile.graph import _make_fan_dispatcher

        node = _phase_with_dynamic()
        fan = _make_fan_dispatcher(node, no_items_targets=["after"], settings=Settings())
        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"topic": "a"}, {"topic": "b"}])},
            completed_nodes={"p1"},
        )
        result = fan(state)
        assert isinstance(result, Command)
        assert result.goto == END
        assert result.update["failed_nodes"] == {"p1"}
        assert any(
            "unknown" in e["error"] for e in result.update["errors"]
        )

    def test_unique_ids_dispatch_one_send_each(self):
        """Step 5: distinct ids → Command(goto=[Send...]), one Send per item,
        each carrying {**state, _fan_out_item}."""
        from langgraph.types import Command, Send

        from sqrlly.compile.graph import _make_fan_dispatcher

        node = _phase_with_dynamic()
        fan = _make_fan_dispatcher(node, no_items_targets=["after"], settings=Settings())
        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"id": "a"}, {"id": "b"}])},
            completed_nodes={"p1"},
        )
        result = fan(state)
        assert isinstance(result, Command)
        sends = result.goto
        assert isinstance(sends, list) and len(sends) == 2
        assert all(isinstance(s, Send) for s in sends)
        sent_ids = sorted(s.arg["_fan_out_item"]["id"] for s in sends)
        assert sent_ids == ["a", "b"]
        # Each Send payload carries committed state (post-gate), so children
        # see the parent's completed_nodes without payload baking.
        assert all("p1" in s.arg["completed_nodes"] for s in sends)

    def test_bad_manifest_path_fails_the_node(self, tmp_path):
        """A `_read_manifest` ManifestError (here: a missing manifest_path
        file) FAILS the parent via a make_failure_update Command(goto=END) —
        it does NOT escape the dispatcher as a raise. A raise would leave a
        gated parent completed-but-not-failed in the checkpoint and bare
        --resume would freeze it and re-fail forever; failing the node makes
        it dirty on --resume so it re-fans once the file is fixed."""
        from langgraph.graph import END
        from langgraph.types import Command

        from sqrlly.compile.graph import _make_fan_dispatcher

        node = _phase_with_dynamic(manifest_path="missing.json")
        fan = _make_fan_dispatcher(node, no_items_targets=["after"], settings=Settings())
        state = make_initial_state(
            workdir=str(tmp_path),
            node_outputs={"p1": "not json"},  # forces the disk-path fallback
            completed_nodes={"p1"},
        )
        result = fan(state)
        assert isinstance(result, Command)
        assert result.goto == END
        assert result.update["failed_nodes"] == {"p1"}
        assert any(
            "could not be read" in e["error"] for e in result.update["errors"]
        )

    def test_drift_fails_the_node_before_send(self):
        """Step 4b: on --resume, a manifest whose new ids DROP prior branch ids
        fails the parent BEFORE any Send. The error partitions the loss:
        a dropped id in _resume_skip = a vanished completed sibling; a dropped
        id NOT in skip = an orphaned failed child."""
        from langgraph.graph import END
        from langgraph.types import Command

        from sqrlly.compile.graph import _make_fan_dispatcher

        node = _phase_with_dynamic()  # parent id 'p1'
        fan = _make_fan_dispatcher(node, no_items_targets=["after"], settings=Settings())
        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"id": "new-a"}, {"id": "new-b"}])},
            completed_nodes={"p1"},
            _fan_prior_children={"p1": {"p1::old-a", "p1::old-b"}},
            _resume_skip={"p1::old-a"},  # old-a completed last run; old-b failed
        )
        result = fan(state)
        assert isinstance(result, Command)
        assert result.goto == END
        assert result.update["failed_nodes"] == {"p1"}
        err = result.update["errors"][0]["error"]
        assert "DRIFTED" in err
        assert "p1::old-a" in err  # vanished completed sibling
        assert "p1::old-b" in err  # orphaned failed child
        # Partition is pinned: the vanished-completed sibling is named before
        # the orphaned-failed child (a swapped partition must not pass).
        assert err.index("p1::old-a") < err.index("p1::old-b")

    def test_empty_manifest_on_resume_drift_fails(self):
        """The maximal drift: on resume the manifest drains to ZERO items while
        prior children exist → fail-loud BEFORE the no-items short-circuit, not
        a silent green route to the no-items target."""
        from langgraph.graph import END
        from langgraph.types import Command

        from sqrlly.compile.graph import _make_fan_dispatcher

        node = _phase_with_dynamic()
        fan = _make_fan_dispatcher(
            node, no_items_targets=[END], settings=Settings(),
        )
        state = make_initial_state(
            node_outputs={"p1": json.dumps([])},  # drained manifest
            completed_nodes={"p1"},
            _fan_prior_children={"p1": {"p1::a", "p1::b"}},
            _resume_skip={"p1::a"},
        )
        result = fan(state)
        assert isinstance(result, Command)
        assert result.goto == END
        assert result.update["failed_nodes"] == {"p1"}
        assert "DRIFTED" in result.update["errors"][0]["error"]

    def test_prior_children_present_but_stable_dispatches(self):
        """Guard no-false-fire at the dispatcher level: prior children present
        but the re-fan reproduces them (stable ids) → normal Send dispatch."""
        from langgraph.types import Command, Send

        from sqrlly.compile.graph import _make_fan_dispatcher

        node = _phase_with_dynamic()
        fan = _make_fan_dispatcher(node, no_items_targets=["after"], settings=Settings())
        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"id": "a"}, {"id": "b"}])},
            completed_nodes={"p1"},
            _fan_prior_children={"p1": {"p1::a", "p1::b"}},  # same ids → no drift
            _resume_skip={"p1::a"},
        )
        result = fan(state)
        assert isinstance(result, Command)
        assert isinstance(result.goto, list) and len(result.goto) == 2
        assert all(isinstance(s, Send) for s in result.goto)

    def test_drift_warn_dispatches_the_new_manifest(self):
        """Step 4b under `on_manifest_drift: warn`: proceed with the drifted
        manifest (opt-in re-fanning) — one Send per new item, no failure."""
        from langgraph.types import Command, Send

        from sqrlly.compile.graph import _make_fan_dispatcher

        node = _phase_with_dynamic()
        fan = _make_fan_dispatcher(
            node, no_items_targets=["after"],
            settings=Settings(on_manifest_drift="warn"),
        )
        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"id": "new-a"}, {"id": "new-b"}])},
            completed_nodes={"p1"},
            _fan_prior_children={"p1": {"p1::old-a", "p1::old-b"}},
            _resume_skip={"p1::old-a"},
        )
        result = fan(state)
        assert isinstance(result, Command)
        assert isinstance(result.goto, list)
        assert sorted(s.arg["_fan_out_item"]["id"] for s in result.goto) == [
            "new-a", "new-b",
        ]

    def test_no_prior_children_skips_drift_check(self):
        """Fresh run (no `_fan_prior_children`) → the drift guard is a no-op;
        the manifest dispatches normally."""
        from langgraph.types import Command, Send

        from sqrlly.compile.graph import _make_fan_dispatcher

        node = _phase_with_dynamic()
        fan = _make_fan_dispatcher(node, no_items_targets=["after"], settings=Settings())
        state = make_initial_state(
            node_outputs={"p1": json.dumps([{"id": "x"}])},
            completed_nodes={"p1"},
        )
        result = fan(state)
        assert isinstance(result, Command)
        assert isinstance(result.goto, list) and len(result.goto) == 1
        assert result.goto[0].arg["_fan_out_item"]["id"] == "x"
