"""Tests for scripts/migrate_legacy_executor_to_presets.py.

The one-shot legacy-executor → presets migrator lives as a standalone
PEP-723 script (not a CLI subcommand). It's loaded here by file path
since `scripts/` is not an importable package.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "migrate_legacy_executor_to_presets.py"
)


def _load_migrator():
    spec = importlib.util.spec_from_file_location(
        "_migrate_legacy_executor_to_presets", _SCRIPT_PATH,
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load_migrator()
migrate_text = _mod.migrate_text


class TestPromptWorkflowMigration:
    def test_executor_and_default_model_become_presets(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "settings:\n"
            "  executor: anthropic\n"
            "  default_model: sonnet\n"
            "nodes:\n"
            "  - id: a\n    name: A\n"
            "    execute:\n      url: t.md\n"
        )
        after, changes = migrate_text(before)
        assert "presets:" in after
        assert "transport: api" in after
        assert "provider: anthropic" in after
        assert "model: sonnet" in after
        assert "default: true" in after
        assert "executor:" not in after
        assert "default_model:" not in after
        assert any("settings.presets" in c for c in changes)

    def test_acp_executor_maps_to_acp_transport(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "settings:\n"
            "  executor: acp\n"
            "  default_model: sonnet\n"
            "nodes:\n"
            "  - id: a\n    name: A\n"
            "    execute:\n      url: t.md\n"
        )
        after, _ = migrate_text(before)
        assert "transport: acp" in after

    def test_idempotent_on_already_migrated(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "settings:\n"
            "  presets:\n"
            "    default:\n"
            "      transport: api\n"
            "      provider: anthropic\n"
            "      model: sonnet\n"
            "      default: true\n"
            "nodes:\n"
            "  - id: a\n    name: A\n"
            "    execute:\n      url: t.md\n"
        )
        after, changes = migrate_text(before)
        assert changes == []
        assert after == before


class TestModelOverrideRewrite:
    def test_normal_node_model_override_migrates_to_params_preset(self):
        """A node WITH an execute block gets its model moved to params.preset."""
        before = (
            "name: T\nversion: '1.0'\n"
            "settings:\n"
            "  executor: anthropic\n"
            "  default_model: sonnet\n"
            "nodes:\n"
            "  - id: heavy\n    name: H\n"
            "    model: opus\n"
            "    execute:\n      url: t.md\n"
        )
        after, changes = migrate_text(before)
        assert "node.model='opus' → params.preset='_auto_opus'" in "\n".join(changes)
        assert "preset: _auto_opus" in after
        node_section = after[after.index("  - id: heavy"):]
        assert "    model:" not in node_section

    def test_gate_only_node_model_override_surfaces_as_change(self):
        """Gate-only nodes (no execute block) can't carry params.preset,
        so a Node.model override is dropped at migrate time. The change
        log must surface the loss so the operator sees the behavior change.
        """
        before = (
            "name: T\nversion: '1.0'\n"
            "settings:\n"
            "  executor: anthropic\n"
            "  default_model: sonnet\n"
            "nodes:\n"
            "  - id: worker\n    name: W\n"
            "    execute:\n      url: t.md\n"
            "  - id: gate_only\n    name: G\n"
            "    model: opus\n"
            "    evaluation:\n      validator: g.py\n      threshold: 0.5\n"
        )
        _, changes = migrate_text(before)
        drop_msgs = [c for c in changes if "gate_only" in c and "dropped" in c]
        assert len(drop_msgs) == 1, f"expected one drop message, got: {changes}"
        assert "node.model='opus'" in drop_msgs[0]
        assert "default preset's model" in drop_msgs[0]
        assert "'sonnet'" in drop_msgs[0]


class TestScriptWorkflowMigration:
    def test_pure_script_workflow_drops_vestigial_fields(self):
        """No prompt dispatch → executor:/default_model: dropped, no
        presets block added (auto-detect handles it at runtime)."""
        echo_bin = "/bin/echo"
        before = (
            "name: T\nversion: '1.0'\n"
            "settings:\n"
            "  default_model: sonnet\n"
            "nodes:\n"
            "  - id: a\n    name: A\n"
            f"    execute:\n      url: {echo_bin}\n"
            "      params:\n        args: ['hi']\n"
        )
        after, changes = migrate_text(before)
        assert "default_model:" not in after
        assert "presets:" not in after
        assert any("pure-script" in c for c in changes)


class TestUnknownExecutor:
    def test_unknown_executor_raises(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "settings:\n"
            "  executor: bogus\n"
            "nodes:\n"
            "  - id: a\n    name: A\n"
            "    execute:\n      url: t.md\n"
        )
        with pytest.raises(_mod.MigrateError, match="Unknown legacy executor"):
            migrate_text(before)
