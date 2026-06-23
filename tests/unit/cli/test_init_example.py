"""Unit tests for `sqrlly init --example` scaffolding (cli/init.py)."""
from __future__ import annotations

import pytest

from sqrlly.cli import init as initmod


def test_catalog_has_curated_four():
    assert set(initmod.EXAMPLES) == {
        "jokes", "route_classify", "explicit_join", "pipeline_style",
    }
    for name, spec in initmod.EXAMPLES.items():
        assert spec["description"]
        assert "workflow.yaml" in spec["files"]          # every scaffold has one
        for src in spec["files"].values():
            assert src.startswith("examples/")           # repo-relative source


def test_rewrite_strips_example_prefix_keeps_absolute():
    text = (
        'nodes:\n'
        '  - id: g\n'
        '    execute:\n'
        '      url: "examples/jokes/generate.md"\n'
        '    evaluation:\n'
        '      validator: "examples/jokes/gates/validate_jokes.py"\n'
        '  - id: e\n'
        '    execute:\n'
        '      url: /usr/bin/echo\n'
    )
    out = initmod._rewrite_example_urls(text, "jokes")
    assert 'url: "generate.md"' in out
    assert 'validator: "gates/validate_jokes.py"' in out
    assert "examples/jokes/" not in out
    assert "url: /usr/bin/echo" in out                   # absolute untouched


def test_rewrite_only_touches_named_example():
    text = '      url: "examples/other/x.md"\n'
    assert initmod._rewrite_example_urls(text, "jokes") == text  # different name


def test_load_example_file_reads_repo_source():
    # In a source checkout the wheel resource is absent; the repo fallback
    # returns the real file content.
    text = initmod._load_example_file("examples/jokes/workflow.yaml")
    assert "Joke Generator" in text


def test_load_example_file_unknown_raises():
    from click import ClickException
    with pytest.raises(ClickException):
        initmod._load_example_file("examples/nope/nope.yaml")
