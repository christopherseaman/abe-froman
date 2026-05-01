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

    def test_post_5b_yaml_idempotent(self):
        """Stage-5b shape (execute.url) is the new fixed-point."""
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: a\n    name: A\n"
            "    execute:\n      url: t.md\n"
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
        # Stage 5b chained: prompt_file → execute.url within fan_out.template
        assert pool["fan_out"]["template"]["execute"]["url"] == "rev.md"

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
        """Already-migrated Stage-5b YAML migrates to itself (no changes)."""
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: a\n    name: A\n"
            "    execute:\n      url: t.md\n"
            "    evaluation:\n      validator: g.py\n      threshold: 0.5\n"
        )
        after, changes = migrate_yaml(before)
        assert changes == []
        assert after == before


class TestStage5bTransforms:
    """Stage 4 → Stage 5b transforms (collapse to execute.url)."""

    def test_prompt_file_to_execute_url(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: a\n    name: A\n    prompt_file: t.md\n"
        )
        after, changes = migrate_yaml(before)
        assert any("prompt_file → execute.url" in c for c in changes)
        data = _parse(after)
        node = data["nodes"][0]
        assert "prompt_file" not in node
        assert node["execute"]["url"] == "t.md"

    def test_execution_prompt_to_execute_url(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: a\n    name: A\n"
            "    execution:\n      type: prompt\n      prompt_file: t.md\n"
        )
        after, changes = migrate_yaml(before)
        assert any("execution type=prompt → execute.url" in c for c in changes)
        data = _parse(after)
        node = data["nodes"][0]
        assert "execution" not in node
        assert node["execute"]["url"] == "t.md"

    def test_execution_command_resolves_via_path(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: a\n    name: A\n"
            "    execution:\n      type: command\n      command: echo\n"
            "      args: ['hello']\n"
        )
        after, changes = migrate_yaml(before)
        assert any("execution type=command" in c for c in changes)
        data = _parse(after)
        node = data["nodes"][0]
        # echo is on every Linux $PATH; resolves to a real absolute path.
        assert node["execute"]["url"].endswith("/echo")
        assert node["execute"]["url"].startswith("/")
        assert node["execute"]["params"]["args"] == ["hello"]

    def test_execution_command_absolute_passes_through(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: a\n    name: A\n"
            "    execution:\n      type: command\n      command: /bin/false\n"
        )
        after, _ = migrate_yaml(before)
        data = _parse(after)
        assert data["nodes"][0]["execute"]["url"] == "/bin/false"

    def test_execution_command_not_on_path_raises(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: a\n    name: A\n"
            "    execution:\n      type: command\n"
            "      command: definitely-not-a-real-binary-xyz\n"
        )
        from abe_froman.cli.migrate import MigrateError

        with pytest.raises(MigrateError) as ei:
            migrate_yaml(before)
        assert "definitely-not-a-real-binary-xyz" in str(ei.value)
        assert "not found on $PATH" in str(ei.value)

    def test_execution_gate_only_elided(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: a\n    name: A\n"
            "    execution:\n      type: gate_only\n"
        )
        after, changes = migrate_yaml(before)
        assert any("gate_only → elided" in c for c in changes)
        data = _parse(after)
        node = data["nodes"][0]
        assert "execution" not in node
        assert "execute" not in node

    def test_execution_join_to_execute_type(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: a\n    name: A\n"
            "    execution:\n      type: join\n"
        )
        after, changes = migrate_yaml(before)
        assert any("join → execute.type=join" in c for c in changes)
        data = _parse(after)
        node = data["nodes"][0]
        assert "execution" not in node
        assert node["execute"]["type"] == "join"

    def test_execution_route_to_execute_type(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: a\n    name: A\n"
            "  - id: r\n    name: R\n    depends_on: [a]\n"
            "    execution:\n      type: route\n"
            "      cases:\n        - when: 'True'\n          goto: a\n"
            "      else: __end__\n"
        )
        after, _ = migrate_yaml(before)
        data = _parse(after)
        route = data["nodes"][1]
        assert "execution" not in route
        assert route["execute"]["type"] == "route"
        assert route["execute"]["cases"][0]["when"] == "True"
        assert route["execute"]["else"] == "__end__"

    def test_config_with_inputs_outputs_lifts_to_params(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: a\n    name: A\n"
            "    config: subgraphs/sub.yaml\n"
            "    inputs:\n      topic: '{{paper}}'\n"
            "    outputs:\n      summary: '{{step2}}'\n"
        )
        after, changes = migrate_yaml(before)
        assert any("config + inputs/outputs → execute" in c for c in changes)
        data = _parse(after)
        node = data["nodes"][0]
        assert "config" not in node
        assert "inputs" not in node
        assert "outputs" not in node
        assert node["execute"]["url"] == "subgraphs/sub.yaml"
        assert node["execute"]["params"]["inputs"]["topic"] == "{{paper}}"
        assert node["execute"]["params"]["outputs"]["summary"] == "{{step2}}"

    def test_fan_out_template_prompt_file_lifts(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: pool\n    name: Pool\n"
            "    fan_out:\n      enabled: true\n      manifest_path: m.json\n"
            "      template:\n        prompt_file: rev.md\n"
        )
        after, _ = migrate_yaml(before)
        data = _parse(after)
        template = data["nodes"][0]["fan_out"]["template"]
        assert "prompt_file" not in template
        assert template["execute"]["url"] == "rev.md"

    def test_idempotent_on_stage5b_yaml(self):
        before = (
            "name: T\nversion: '1.0'\n"
            "nodes:\n"
            "  - id: a\n    name: A\n"
            "    execute:\n      url: t.md\n      params:\n        model: opus\n"
            "  - id: r\n    name: R\n    depends_on: [a]\n"
            "    execute:\n      type: route\n"
            "      cases:\n        - when: 'True'\n          goto: a\n"
            "      else: __end__\n"
        )
        after, changes = migrate_yaml(before)
        assert changes == []
        assert after == before


class TestRoundTripInRepoExamples:
    """Every checked-in example must reach a fixed point under migrate.

    During the Stage 5b dual-mode window, in-repo YAMLs are still in
    Stage-4 shape; the first migrate produces Stage-5b output, and a
    second migrate on that output is idempotent. After Commit 7 (when
    fixtures migrate), the first migrate also becomes idempotent.
    """

    @pytest.mark.parametrize("rel_path", [
        "examples/smoke_test.yaml",
        "examples/explicit_join.yaml",
        "examples/route_classify/workflow.yaml",
        "examples/jokes/workflow.yaml",
        "examples/absurd-paper/workflow.yaml",
    ])
    def test_in_repo_yaml_reaches_fixed_point(self, rel_path):
        repo_root = Path(__file__).resolve().parents[3]
        text = (repo_root / rel_path).read_text()
        once, _ = migrate_yaml(text)
        twice, second_changes = migrate_yaml(once)
        assert second_changes == [], (
            f"{rel_path}: post-migration output not idempotent: {second_changes}"
        )
        assert once == twice


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
            "  - id: a\n    name: A\n"
            "    execute:\n      url: t.md\n"
        )
        runner = CliRunner()
        result = runner.invoke(cli, ["migrate", str(f)])
        assert result.exit_code == 0
        # Status message goes to stderr; stdout has no rewritten YAML
        assert "No changes needed" in result.output or "No changes needed" in (result.stderr or "")
