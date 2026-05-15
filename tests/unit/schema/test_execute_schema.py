"""Unit tests for the Stage 5b ``execute:`` schema.

After Stage 5c, ``Execute`` only models URL mode and the join sentinel
(route lifted to ``Node.route`` — see ``test_route_schema.py`` for that).

Function-level tests cover:
    - Execute.validate_shape: URL and join modes parse cleanly
    - Mutual exclusion of mode-specific fields
    - Node mutual-exclusion validator: execute / execution / config
    - Settings extension parses with new remote-URL fields
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from sqrlly.schema.models import (
    Execute,
    Node,
    Settings,
)


class TestExecuteURLMode:
    def test_url_only_parses(self):
        e = Execute(url="prompts/x.md")
        assert e.url == "prompts/x.md"
        assert e.type is None
        assert e.params == {}

    def test_url_with_params_parses(self):
        e = Execute(url="prompts/x.md", params={"model": "opus"})
        assert e.params == {"model": "opus"}


class TestExecuteJoinMode:
    def test_join_only_parses(self):
        e = Execute(type="join")
        assert e.type == "join"
        assert e.url is None

    def test_join_rejects_url(self):
        with pytest.raises(ValidationError):
            Execute(type="join", url="x.md")

    def test_join_rejects_params(self):
        with pytest.raises(ValidationError):
            Execute(type="join", params={"x": "y"})


class TestExecuteEmpty:
    def test_no_mode_set_rejected(self):
        with pytest.raises(ValidationError) as ei:
            Execute()
        assert "exactly one" in str(ei.value).lower()


class TestExecuteModeOverride:
    """``execute.mode:`` forces dispatch routing when the URL extension
    is missing or misleading. Only legal in URL mode."""

    def test_url_mode_with_python_override_parses(self):
        e = Execute(url="scripts/run-thing", mode="python")
        assert e.mode == "python"

    def test_url_mode_with_subgraph_override_parses(self):
        e = Execute(url="subgraphs/registry-entry", mode="subgraph")
        assert e.mode == "subgraph"

    @pytest.mark.parametrize(
        "mode", ["prompt", "subgraph", "exec", "python", "node", "tsx", "bash"],
    )
    def test_all_documented_modes_parse(self, mode):
        Execute(url="x", mode=mode)

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValidationError):
            Execute(url="x", mode="ruby")

    def test_mode_on_join_rejected(self):
        with pytest.raises(ValidationError) as ei:
            Execute(type="join", mode="prompt")
        assert "join" in str(ei.value).lower()


class TestNodeExecuteShape:
    """After Stage-5b hard cutover, Node carries only `execute: Execute | None`.
    Legacy `execution`/`config`/`prompt_file` fields no longer exist."""

    def test_node_with_execute_url(self):
        n = Node(id="a", name="A", execute=Execute(url="x.md"))
        assert n.execute.url == "x.md"

    def test_node_with_execute_join(self):
        n = Node(id="a", name="A", execute=Execute(type="join"), depends_on=["x"])
        assert n.execute.type == "join"

    def test_node_with_execute_subgraph_yaml(self):
        n = Node(id="a", name="A", execute=Execute(url="sub.yaml"))
        assert n.execute.url == "sub.yaml"

    def test_node_rejects_legacy_execution_field(self):
        with pytest.raises(ValidationError):
            Node.model_validate({
                "id": "a", "name": "A",
                "execution": {"type": "command", "command": "echo"},
            })

    def test_node_rejects_legacy_config_field(self):
        with pytest.raises(ValidationError):
            Node(id="a", name="A", config="sub.yaml")

    def test_node_rejects_legacy_prompt_file_field(self):
        with pytest.raises(ValidationError):
            Node(id="a", name="A", prompt_file="x.md")

    def test_node_with_no_execute_is_gate_only(self):
        # A bare Node (no execute) is gate-only-by-elision.
        n = Node(id="a", name="A")
        assert n.execute is None


class TestSettingsExtension:
    def test_defaults_reproduce_today(self):
        s = Settings()
        assert s.base_url is None
        assert s.allow_remote_urls is False
        assert s.allow_remote_scripts is False
        assert s.allowed_url_hosts == []
        assert s.url_headers == {}
        assert s.max_remote_fetch_bytes == 5_000_000

    def test_parses_all_new_fields(self):
        s = Settings(
            base_url="https://prompts.example.com/v1/",
            allow_remote_urls=True,
            allow_remote_scripts=True,
            allowed_url_hosts=["*.internal.example.com"],
            url_headers={"https://prompts.example.com/": {"Authorization": "Bearer x"}},
            max_remote_fetch_bytes=1_000_000,
        )
        assert s.base_url == "https://prompts.example.com/v1/"
        assert s.allow_remote_urls is True
        assert s.allow_remote_scripts is True
        assert s.allowed_url_hosts == ["*.internal.example.com"]
        assert s.url_headers["https://prompts.example.com/"] == {
            "Authorization": "Bearer x"
        }
        assert s.max_remote_fetch_bytes == 1_000_000


class TestExecuteFromYAML:
    def test_url_mode_yaml(self):
        src = """
        url: prompts/x.md
        params:
          model: opus
        """
        e = Execute.model_validate(yaml.safe_load(src))
        assert e.url == "prompts/x.md"
        assert e.params == {"model": "opus"}

    def test_subgraph_mode_yaml(self):
        src = """
        url: subgraphs/sub.yaml
        params:
          inputs:
            topic: "{{paper}}"
          outputs:
            summary: "{{step2}}"
        """
        e = Execute.model_validate(yaml.safe_load(src))
        assert e.url == "subgraphs/sub.yaml"
        assert "inputs" in e.params
