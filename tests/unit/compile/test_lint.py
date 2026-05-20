"""Unit tests for compile-time lint warnings (compile/lint.py).

`collect_warnings` is a pure function over `Graph` — no I/O, no
langgraph. Each check gets a known-good (silent) and known-bad (warns)
fixture per the project's function-level test doctrine.
"""
from __future__ import annotations

from sqrlly.compile.lint import collect_warnings

from helpers import make_config


def _node(node_id: str) -> dict:
    return {"id": node_id, "name": node_id, "execute": {"url": "t.md"}}


class TestHyphenatedIdWarnings:
    def test_underscore_ids_are_silent(self):
        config = make_config([_node("research_phase"), _node("write_up")])
        assert collect_warnings(config) == []

    def test_hyphenated_id_warns(self):
        config = make_config([_node("research-phase")])
        warnings = collect_warnings(config)
        assert len(warnings) == 1
        message = warnings[0]
        assert "research-phase" in message
        # The exact Jinja-subtraction footgun is shown verbatim.
        assert "{{research-phase}}" in message
        # The underscore rename is suggested.
        assert "research_phase" in message

    def test_one_warning_per_hyphenated_id(self):
        config = make_config([_node("a-b"), _node("ok"), _node("c-d-e")])
        warnings = collect_warnings(config)
        assert len(warnings) == 2
        assert any("a-b" in w for w in warnings)
        assert any("c-d-e" in w for w in warnings)

    def test_fan_out_final_node_id_is_checked(self):
        """A hyphenated id on a fan-out final node is flagged too."""
        parent = {
            "id": "fan",
            "name": "fan",
            "execute": {"url": "echo"},
            "fan_out": {
                "template": {"execute": {"url": "t.md"}},
                "final_nodes": [{"id": "merge-results", "name": "Merge"}],
            },
        }
        config = make_config([parent])
        warnings = collect_warnings(config)
        assert len(warnings) == 1
        assert "merge-results" in warnings[0]
        assert "merge_results" in warnings[0]
