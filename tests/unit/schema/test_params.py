"""Unit tests for schema/params.py: per-mode dataclasses + URL resolver.

Function-level tests cover known-good and known-bad pairs:
    - Each per-mode model parses its expected keys
    - Each per-mode model rejects mode-mismatched keys
    - params_for_url returns the right model type per extension
    - coerce_params surfaces ValidationError on typos
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from abe_froman.schema.params import (
    ExecParams,
    PromptParams,
    ScriptParams,
    SubgraphParams,
    coerce_params,
    params_for_url,
)


class TestPromptParams:
    def test_parses_known_keys(self):
        p = PromptParams(model="opus", agent="claude", timeout=30.0)
        assert p.model == "opus"
        assert p.agent == "claude"
        assert p.timeout == 30.0

    def test_defaults_to_none(self):
        p = PromptParams()
        assert p.model is None
        assert p.agent is None
        assert p.timeout is None

    def test_rejects_args_key(self):
        with pytest.raises(ValidationError):
            PromptParams(args=["x"])

    def test_rejects_typo(self):
        with pytest.raises(ValidationError):
            PromptParams(model_name="opus")  # should be `model`


class TestSubgraphParams:
    def test_parses_inputs_outputs(self):
        p = SubgraphParams(
            inputs={"topic": "{{paper}}"},
            outputs={"summary": "{{step2}}"},
        )
        assert p.inputs == {"topic": "{{paper}}"}
        assert p.outputs == {"summary": "{{step2}}"}

    def test_rejects_args_key(self):
        with pytest.raises(ValidationError):
            SubgraphParams(args=["x"])

    def test_rejects_model_key(self):
        with pytest.raises(ValidationError):
            SubgraphParams(model="opus")


class TestScriptParams:
    def test_parses_args_env(self):
        p = ScriptParams(args=["--flag", "value"], env={"X": "y"})
        assert p.args == ["--flag", "value"]
        assert p.env == {"X": "y"}

    def test_rejects_model_key(self):
        with pytest.raises(ValidationError):
            ScriptParams(model="opus")

    def test_rejects_inputs_key(self):
        with pytest.raises(ValidationError):
            ScriptParams(inputs={"x": "y"})


class TestExecParams:
    def test_parses_args_env(self):
        p = ExecParams(args=["arg1"], env={"PATH": "/usr/bin"})
        assert p.args == ["arg1"]
        assert p.env == {"PATH": "/usr/bin"}

    def test_rejects_model_key(self):
        with pytest.raises(ValidationError):
            ExecParams(model="opus")


class TestParamsForURL:
    @pytest.mark.parametrize("ext", [".md", ".txt", ".prompt"])
    def test_prompt_extensions(self, ext):
        assert params_for_url(f"file:///x/y{ext}") is PromptParams

    @pytest.mark.parametrize("ext", [".yaml", ".yml"])
    def test_subgraph_extensions(self, ext):
        assert params_for_url(f"file:///x/y{ext}") is SubgraphParams

    @pytest.mark.parametrize("ext", [".py", ".js", ".mjs", ".ts", ".sh"])
    def test_script_extensions(self, ext):
        assert params_for_url(f"file:///x/y{ext}") is ScriptParams

    def test_no_extension_falls_through_to_exec(self):
        assert params_for_url("file:///bin/echo") is ExecParams

    def test_unknown_extension_falls_through_to_exec(self):
        assert params_for_url("file:///x/y.unknown") is ExecParams

    def test_https_scheme_routes_by_extension(self):
        assert params_for_url("https://x.com/y.md") is PromptParams
        assert params_for_url("https://x.com/y.yaml") is SubgraphParams
        assert params_for_url("https://x.com/y.py") is ScriptParams

    def test_case_insensitive_extension(self):
        assert params_for_url("file:///x/Y.MD") is PromptParams
        assert params_for_url("file:///x/Y.YAML") is SubgraphParams


class TestCoerceParams:
    def test_coerce_prompt_url_with_model(self):
        p = coerce_params("file:///x.md", {"model": "opus"})
        assert isinstance(p, PromptParams)
        assert p.model == "opus"

    def test_coerce_subgraph_url_with_inputs(self):
        p = coerce_params("file:///sub.yaml", {"inputs": {"x": "y"}})
        assert isinstance(p, SubgraphParams)
        assert p.inputs == {"x": "y"}

    def test_coerce_rejects_mode_mismatch(self):
        # `args:` on a prompt URL is a typo / mode confusion
        with pytest.raises(ValidationError):
            coerce_params("file:///x.md", {"args": ["x"]})

    def test_coerce_empty_dict(self):
        p = coerce_params("file:///x.md", {})
        assert isinstance(p, PromptParams)
