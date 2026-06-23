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


from pathlib import Path
from click import ClickException


def test_init_example_scaffolds_files_and_rewrites(tmp_path):
    initmod.init_example("jokes", str(tmp_path))
    assert (tmp_path / "workflow.yaml").is_file()
    assert (tmp_path / "generate.md").is_file()
    assert (tmp_path / "select.md").is_file()
    assert (tmp_path / "gates" / "validate_jokes.py").is_file()
    wf = (tmp_path / "workflow.yaml").read_text()
    assert "examples/jokes/" not in wf          # rewrite applied
    assert 'url: "generate.md"' in wf


def test_init_example_single_file_lands_as_workflow_yaml(tmp_path):
    initmod.init_example("explicit_join", str(tmp_path))
    # examples/explicit_join.yaml -> <dir>/workflow.yaml
    assert (tmp_path / "workflow.yaml").is_file()
    assert "Explicit Join" in (tmp_path / "workflow.yaml").read_text()


def test_init_example_refuses_clobber(tmp_path):
    (tmp_path / "workflow.yaml").write_text("existing")
    with pytest.raises(ClickException):
        initmod.init_example("jokes", str(tmp_path))


def test_init_example_unknown_name_lists_available(tmp_path):
    with pytest.raises(ClickException) as ei:
        initmod.init_example("bogus", str(tmp_path))
    assert "jokes" in str(ei.value)             # error lists valid names


def test_catalog_lines_cover_all_examples():
    lines = initmod._example_catalog_lines()
    text = "\n".join(lines)
    for name in initmod.EXAMPLES:
        assert name in text
