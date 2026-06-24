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


def _gated_node(node_id: str, *, threshold: float, blocking: bool) -> dict:
    return {
        "id": node_id, "name": node_id, "execute": {"url": "t.md"},
        "evaluation": {"validator": "gate.py", "threshold": threshold,
                       "blocking": blocking},
    }


class TestAdvisoryGateWarnings:
    def test_blocking_gate_with_threshold_is_silent(self):
        config = make_config([_gated_node("g", threshold=0.7, blocking=True)])
        assert collect_warnings(config) == []

    def test_nonblocking_zero_threshold_is_silent(self):
        # threshold 0.0 can never fail → not a footgun, no warning.
        config = make_config([_gated_node("g", threshold=0.0, blocking=False)])
        assert collect_warnings(config) == []

    def test_nonblocking_gate_with_threshold_warns(self):
        config = make_config([_gated_node("phase_2a", threshold=0.42, blocking=False)])
        warnings = collect_warnings(config)
        assert len(warnings) == 1
        msg = warnings[0]
        assert "phase_2a" in msg
        assert "0.42" in msg
        assert "blocking" in msg
        assert "advisory" in msg.lower()

    def test_nonblocking_fan_out_final_node_gate_warns(self):
        parent = {
            "id": "fan", "name": "fan", "execute": {"url": "echo"},
            "fan_out": {
                "template": {"execute": {"url": "t.md"}},
                "final_nodes": [{
                    "id": "gate_2c", "name": "Gate",
                    "evaluation": {"validator": "g.py", "threshold": 0.85,
                                   "blocking": False},
                }],
            },
        }
        config = make_config([parent])
        assert any("gate_2c" in w and "0.85" in w and "advisory" in w.lower()
                   for w in collect_warnings(config))

    def test_nonblocking_fan_out_template_gate_warns(self):
        parent = {
            "id": "fan", "name": "fan", "execute": {"url": "echo"},
            "fan_out": {
                "template": {
                    "execute": {"url": "t.md"},
                    "evaluation": {"validator": "g.py", "threshold": 0.7,
                                   "blocking": False},
                },
            },
        }
        config = make_config([parent])
        assert any("fan" in w and "0.7" in w for w in collect_warnings(config))


class TestFanoutParentPromoteWarnings:
    def test_fanout_parent_promote_warns(self):
        from sqrlly.compile.lint import collect_warnings
        from sqrlly.schema.models import Graph
        cfg = Graph(**{
            "name": "t", "version": "0.0.0",
            "nodes": [{
                "id": "build", "name": "build", "promote": True,
                "fan_out": {"manifest_path": "m.json",
                            "template": {"execute": {"url": "w.yaml"}}},
            }],
            "settings": {},
        })
        warns = collect_warnings(cfg)
        assert any("fan_out.promote" in w and "build" in w for w in warns)

    def test_fanout_promote_no_warn(self):
        from sqrlly.compile.lint import collect_warnings
        from sqrlly.schema.models import Graph
        cfg = Graph(**{
            "name": "t", "version": "0.0.0",
            "nodes": [{
                "id": "build", "name": "build",
                "fan_out": {"manifest_path": "m.json",
                            "template": {"execute": {"url": "w.yaml"}},
                            "promote": True},
            }],
            "settings": {},
        })
        assert not any("fan_out.promote" in w for w in collect_warnings(cfg))


class TestWorktreeSetupExcludeWarnings:
    def test_warns_prisma_generate_without_exclude(self):
        config = make_config(
            [_node("n")],
            worktree_setup=["pnpm exec prisma generate"],
        )
        warnings = collect_warnings(config)
        assert any("prisma generate" in w and "worktree_setup_exclude" in w for w in warnings)

    def test_no_warn_when_exclude_present(self):
        config = make_config(
            [_node("n")],
            worktree_setup=["pnpm exec prisma generate"],
            worktree_setup_exclude=["src/generated/prisma"],
        )
        assert not any("prisma generate" in w for w in collect_warnings(config))

    def test_no_warn_when_no_prisma_generate(self):
        config = make_config(
            [_node("n")],
            worktree_setup=["pnpm install --prefer-offline"],
        )
        assert not any("prisma generate" in w for w in collect_warnings(config))
