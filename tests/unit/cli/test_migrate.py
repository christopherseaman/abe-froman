"""Tests for `abe-froman migrate` — pre-Stage-4 → post-cutover YAML.

Synthetic pre-cutover fixtures (no real pre-Stage-4 files exist in-repo
since the hard cutover already happened). Every test asserts a concrete
rewrite, comment/anchor/template preservation, or idempotency.
"""
from __future__ import annotations

import io
from pathlib import Path

import pytest
from click.testing import CliRunner
from ruamel.yaml import YAML

from abe_froman.cli.main import cli
from abe_froman.cli.migrate import migrate_yaml


def _parse(text: str) -> dict:
    """Parse YAML text via ruamel.yaml round-trip mode."""
    yaml = YAML(typ="rt")
    return yaml.load(text)


# ---------------------------------------------------------------------------
# migrate_yaml — function-level
# ---------------------------------------------------------------------------


class TestPhasesRename:
    def test_top_level_phases_to_nodes(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "phases:\n"
            "  - id: a\n    name: A\n    prompt_file: t.md\n"
        )
        after, changes = migrate_yaml(before)
        assert "phases → nodes" in changes
        data = _parse(after)
        assert "nodes" in data
        assert "phases" not in data
        assert len(data["nodes"]) == 1
        assert data["nodes"][0]["id"] == "a"

    def test_no_phases_key_idempotent(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: a\n    name: A\n    prompt_file: t.md\n"
        )
        after, changes = migrate_yaml(before)
        assert changes == []
        assert after == before


class TestQualityGateRename:
    def test_quality_gate_to_evaluation(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: a\n    name: A\n    prompt_file: t.md\n"
            "    quality_gate:\n"
            "      validator: g.py\n"
            "      threshold: 0.5\n"
        )
        after, changes = migrate_yaml(before)
        assert any("quality_gate → evaluation" in c for c in changes)
        data = _parse(after)
        node = data["nodes"][0]
        assert "evaluation" in node
        assert "quality_gate" not in node
        assert node["evaluation"]["validator"] == "g.py"
        assert node["evaluation"]["threshold"] == 0.5


class TestDynamicSubphasesFlatten:
    def test_template_prompt_lifts_to_parent(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: pool\n    name: Pool\n"
            "    dynamic_subphases:\n"
            "      enabled: true\n"
            "      manifest_path: m.json\n"
            "      template:\n"
            "        prompt_file: rev.md\n"
        )
        after, changes = migrate_yaml(before)
        assert any("dynamic_subphases → fan_out" in c for c in changes)
        data = _parse(after)
        pool = data["nodes"][0]
        assert "dynamic_subphases" not in pool
        assert "fan_out" in pool
        assert pool["fan_out"]["enabled"] is True
        assert pool["fan_out"]["manifest_path"] == "m.json"
        assert pool["fan_out"]["template"]["prompt_file"] == "rev.md"

    def test_final_phases_lift_to_siblings_with_depends_on(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: pool\n    name: Pool\n"
            "    dynamic_subphases:\n"
            "      enabled: true\n"
            "      manifest_path: m.json\n"
            "      template:\n"
            "        prompt_file: rev.md\n"
            "      final_phases:\n"
            "        - id: aggregate\n          name: Aggregate\n          prompt_file: agg.md\n"
        )
        after, changes = migrate_yaml(before)
        data = _parse(after)
        # Pool should still be at index 0; aggregate sibling at index 1.
        assert data["nodes"][0]["id"] == "pool"
        assert data["nodes"][1]["id"] == "aggregate"
        assert data["nodes"][1]["depends_on"] == ["pool"]
        # final_phases should be gone from pool
        assert "final_phases" not in data["nodes"][0].get("fan_out", {})

    def test_chained_final_phases_chain_depends_on(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: pool\n    name: Pool\n"
            "    dynamic_subphases:\n"
            "      template:\n"
            "        prompt_file: rev.md\n"
            "      final_phases:\n"
            "        - id: step1\n          name: S1\n          prompt_file: s1.md\n"
            "        - id: step2\n          name: S2\n          prompt_file: s2.md\n"
        )
        after, _ = migrate_yaml(before)
        data = _parse(after)
        assert data["nodes"][0]["id"] == "pool"
        assert data["nodes"][1]["id"] == "step1"
        assert data["nodes"][1]["depends_on"] == ["pool"]
        assert data["nodes"][2]["id"] == "step2"
        assert data["nodes"][2]["depends_on"] == ["step1"]

    def test_final_phase_quality_gate_renamed(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: pool\n    name: Pool\n"
            "    dynamic_subphases:\n"
            "      template:\n"
            "        prompt_file: rev.md\n"
            "      final_phases:\n"
            "        - id: agg\n          name: Agg\n          prompt_file: a.md\n"
            "          quality_gate:\n"
            "            validator: v.py\n"
            "            threshold: 0.7\n"
        )
        after, _ = migrate_yaml(before)
        data = _parse(after)
        agg = data["nodes"][1]
        assert "quality_gate" not in agg
        assert agg["evaluation"]["validator"] == "v.py"


class TestPreservation:
    def test_comments_preserved(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "# top-of-nodes comment\n"
            "phases:\n"
            "  # comment above first node\n"
            "  - id: a\n    name: A\n    prompt_file: t.md\n"
        )
        after, _ = migrate_yaml(before)
        # Both comments survive (verbatim text presence)
        assert "# top-of-nodes comment" in after
        assert "# comment above first node" in after

    def test_template_strings_preserved(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "phases:\n"
            "  - id: cmd\n    name: Cmd\n"
            "    execution:\n"
            "      type: command\n"
            "      command: echo\n"
            "      args: [\"{{prev_step}}\"]\n"
        )
        after, _ = migrate_yaml(before)
        # The Jinja-style braces must round-trip byte-identical
        assert "{{prev_step}}" in after

    def test_anchor_and_alias_preserved(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "phases:\n"
            "  - id: a\n    name: A\n    prompt_file: &shared t.md\n"
            "  - id: b\n    name: B\n    prompt_file: *shared\n"
        )
        after, _ = migrate_yaml(before)
        # Anchors preserved as &shared / *shared
        assert "&shared" in after
        assert "*shared" in after

    def test_idempotent_on_post_cutover_yaml(self):
        """Already-migrated YAML migrates to itself (no changes)."""
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: a\n    name: A\n    prompt_file: t.md\n"
            "    evaluation:\n      validator: g.py\n      threshold: 0.5\n"
        )
        after, changes = migrate_yaml(before)
        assert changes == []
        assert after == before


class TestRoundTripInRepoExamples:
    """Every checked-in example must migrate to itself (post-cutover already)."""

    @pytest.mark.parametrize("rel_path", [
        "examples/smoke_test.yaml",
        "examples/example_workflow.yaml",
        "examples/jokes/workflow.yaml",
        "examples/absurd-paper/workflow.yaml",
    ])
    def test_in_repo_yaml_idempotent(self, rel_path):
        repo_root = Path(__file__).resolve().parents[3]
        text = (repo_root / rel_path).read_text()
        _, changes = migrate_yaml(text)
        assert changes == [], (
            f"{rel_path}: expected idempotent migration but got changes: {changes}"
        )


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


class TestMigrateCLI:
    def test_dry_run_does_not_modify_file(self, tmp_path):
        f = tmp_path / "old.yaml"
        f.write_text(
            "name: T\nversion: '1.0'\n"
            "phases:\n"
            "  - id: a\n    name: A\n    prompt_file: t.md\n"
        )
        original = f.read_text()
        runner = CliRunner()
        result = runner.invoke(cli, ["migrate", str(f), "--dry-run"])
        assert result.exit_code == 0
        assert f.read_text() == original  # unchanged on disk

    def test_in_place_writes_file(self, tmp_path):
        f = tmp_path / "old.yaml"
        f.write_text(
            "name: T\nversion: '1.0'\n"
            "phases:\n"
            "  - id: a\n    name: A\n    prompt_file: t.md\n"
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["migrate", str(f), "--in-place"])
        assert result.exit_code == 0
        assert "nodes:" in f.read_text()
        assert "phases:" not in f.read_text()

    def test_default_prints_to_stdout(self, tmp_path):
        f = tmp_path / "old.yaml"
        f.write_text(
            "name: T\nversion: '1.0'\n"
            "phases:\n"
            "  - id: a\n    name: A\n    prompt_file: t.md\n"
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["migrate", str(f)])
        assert result.exit_code == 0
        assert "nodes:" in result.output
        # File on disk unchanged when --in-place not set
        assert "phases:" in f.read_text()

    def test_no_changes_message(self, tmp_path):
        f = tmp_path / "new.yaml"
        f.write_text(
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: a\n    name: A\n    prompt_file: t.md\n"
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["migrate", str(f)])
        assert result.exit_code == 0
        # Status message goes to stderr; stdout has no rewritten YAML
        assert "No changes needed" in result.output or "No changes needed" in (result.stderr or "")
