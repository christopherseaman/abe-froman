# `sqrlly init --example` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let `pipx`/`uv tool install` users scaffold a real, runnable bundled example onto disk with `sqrlly init --example <name>`.

**Architecture:** Curate a small set of run-essential example files; `force-include` them into the wheel at `sqrlly/_examples/…` (mirroring `SKILLS.md → sqrlly/_skill.md`). New `init --example <name> [dir]` + `init --list-examples` read those files (repo fallback in a source checkout) and write them to a target dir, rewriting the `examples/<name>/` URL prefixes so the copy is flat and self-contained.

**Tech Stack:** Python 3.11+, Click, `importlib.resources`, Hatchling (`force-include`), pytest.

## Global Constraints

- Python `>=3.11`; the system `python` binary is unavailable — use `sys.executable` for subprocess calls in tests.
- No mocks of external systems; tests use the real filesystem and the real compiler (`load_config` + `build_workflow_graph`).
- Conventional-commit messages; no attribution trailers.
- Curated set is exactly: `jokes`, `route_classify`, `explicit_join`, `pipeline_style`. Heavy examples and a separate `sqrlly-examples` package / `[examples]` extra are out of scope.
- `cli/init.py` is the runtime/presentation home for all scaffolding logic; `cli/main.py` only wires Click options to `init.py` functions.

---

### Task 1: Curated catalog + URL-rewrite + resource loader

**Files:**
- Modify: `src/sqrlly/cli/init.py` (add catalog + 2 helpers near the top, after the existing imports)
- Test: `tests/unit/cli/test_init_example.py` (create)

**Interfaces:**
- Produces:
  - `EXAMPLES: dict[str, dict]` — `{name: {"description": str, "files": {dest_relpath: src_repo_relpath}}}`. `dest_relpath` is where the file lands under the target dir; `src_repo_relpath` is repo-relative (always starts `examples/`).
  - `_rewrite_example_urls(yaml_text: str, name: str) -> str` — strips the `examples/<name>/` prefix from quoted `url:`/`validator:` path values.
  - `_load_example_file(src_repo_relpath: str) -> str` — reads a bundled example file from `sqrlly/_examples/<rest>` (wheel) or repo `<src_repo_relpath>` (source checkout).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/cli/test_init_example.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/cli/test_init_example.py -q`
Expected: FAIL — `AttributeError: module 'sqrlly.cli.init' has no attribute 'EXAMPLES'`.

- [ ] **Step 3: Implement the catalog + helpers**

Add to `src/sqrlly/cli/init.py` (after the existing `import` block — it already imports `importlib.resources`, `subprocess`, `Path`, `click`; add `import re`):

```python
import re  # add to the existing imports

# Curated examples scaffoldable via `sqrlly init --example`. dest -> repo src.
EXAMPLES: dict[str, dict] = {
    "jokes": {
        "description": "LLM prompt + quality gate + multi-node (needs an authed `claude` CLI).",
        "files": {
            "workflow.yaml": "examples/jokes/workflow.yaml",
            "generate.md": "examples/jokes/generate.md",
            "select.md": "examples/jokes/select.md",
            "gates/validate_jokes.py": "examples/jokes/gates/validate_jokes.py",
        },
    },
    "route_classify": {
        "description": "Inline routing on structured output (pure script — no backend).",
        "files": {
            "workflow.yaml": "examples/route_classify/workflow.yaml",
            "scripts/triage.py": "examples/route_classify/scripts/triage.py",
        },
    },
    "explicit_join": {
        "description": "Fan-in / join topology (pure script — no backend).",
        "files": {"workflow.yaml": "examples/explicit_join.yaml"},
    },
    "pipeline_style": {
        "description": "Forward-edge `route: goto` authoring (pure script — no backend).",
        "files": {"workflow.yaml": "examples/pipeline_style/workflow.yaml"},
    },
}


def _rewrite_example_urls(yaml_text: str, name: str) -> str:
    """Strip the ``examples/<name>/`` prefix from quoted ``url:``/``validator:``
    path values so a scaffolded workflow resolves them relative to its own
    directory. Absolute paths (e.g. ``/usr/bin/echo``) and refs to other
    examples are left untouched."""
    prefix = f"examples/{name}/"
    pattern = re.compile(
        r'((?:url|validator):\s*)(["\'])' + re.escape(prefix) + r'([^"\']+)\2'
    )
    return pattern.sub(r"\1\2\3\2", yaml_text)


def _load_example_file(src_repo_relpath: str) -> str:
    """Read a bundled example file. In an installed wheel it lives at
    ``sqrlly/_examples/<rest>`` (force-included; ``<rest>`` is the path with
    the leading ``examples/`` dropped). Running from a source checkout that
    resource is absent, so fall back to the repo file found by walking up
    from this module."""
    rest = src_repo_relpath[len("examples/"):]
    res = importlib.resources.files("sqrlly").joinpath("_examples")
    for seg in rest.split("/"):
        res = res.joinpath(seg)
    if res.is_file():
        return res.read_text(encoding="utf-8")
    for parent in Path(__file__).resolve().parents:
        candidate = parent / src_repo_relpath
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise click.ClickException(
        f"Could not locate bundled example file {src_repo_relpath!r}."
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/cli/test_init_example.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/cli/init.py tests/unit/cli/test_init_example.py
git commit -m "feat: init example catalog + url-rewrite + resource loader"
```

---

### Task 2: Scaffolder + lister

**Files:**
- Modify: `src/sqrlly/cli/init.py` (add two functions after the Task 1 helpers)
- Test: `tests/unit/cli/test_init_example.py` (extend)

**Interfaces:**
- Consumes: `EXAMPLES`, `_rewrite_example_urls`, `_load_example_file` (Task 1).
- Produces:
  - `init_example(name: str, directory: str) -> None` — scaffold `name` into `directory`; rewrites the workflow YAML; refuses to clobber an existing `workflow.yaml`; errors on unknown `name`; prints next-step hints.
  - `init_list_examples() -> None` — print the catalog (one line per example).
  - `_example_catalog_lines() -> list[str]` — pure helper backing the lister.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/cli/test_init_example.py
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/cli/test_init_example.py -q`
Expected: FAIL — `AttributeError: ... has no attribute 'init_example'`.

- [ ] **Step 3: Implement**

Add to `src/sqrlly/cli/init.py`:

```python
def init_example(name: str, directory: str) -> None:
    """Scaffold the curated example ``name`` into ``directory``.

    Writes each catalog file (creating subdirs like ``gates/``/``scripts/``),
    rewriting the workflow YAML's ``examples/<name>/`` URL prefixes so the
    copy is self-contained. Refuses to clobber an existing ``workflow.yaml``.
    """
    if name not in EXAMPLES:
        raise click.ClickException(
            f"Unknown example {name!r}. Available: "
            f"{', '.join(sorted(EXAMPLES))}."
        )
    target = Path(directory)
    workflow_path = target / "workflow.yaml"
    if workflow_path.exists():
        raise click.ClickException(
            f"{workflow_path} already exists. "
            f"Remove it or pick a different directory."
        )
    for dest_rel, src_rel in EXAMPLES[name]["files"].items():
        text = _load_example_file(src_rel)
        if dest_rel == "workflow.yaml":
            text = _rewrite_example_urls(text, name)
        out = target / dest_rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

    display = str(target) if str(target) != "." else "the current directory"
    click.echo(f"Scaffolded example {name!r} into {display}.")
    click.echo()
    click.echo("Next steps:")
    if str(target) != ".":
        click.echo(f"  cd {target}")
    click.echo("  sqrlly validate workflow.yaml")
    click.echo("  sqrlly run workflow.yaml")


def _example_catalog_lines() -> list[str]:
    lines = ["Available examples (sqrlly init --example <name>):"]
    for name in sorted(EXAMPLES):
        lines.append(f"  {name:<16}{EXAMPLES[name]['description']}")
    return lines


def init_list_examples() -> None:
    """Print the curated example catalog."""
    for line in _example_catalog_lines():
        click.echo(line)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/cli/test_init_example.py -q`
Expected: PASS (10 passed).

- [ ] **Step 5: Commit**

```bash
git add src/sqrlly/cli/init.py tests/unit/cli/test_init_example.py
git commit -m "feat: init_example scaffolder + init_list_examples"
```

---

### Task 3: Wire the CLI options

**Files:**
- Modify: `src/sqrlly/cli/main.py` (the `init` command: import line + decorators + body)
- Test: `tests/unit/cli/test_init_example_cli.py` (create)

**Interfaces:**
- Consumes: `init_example`, `init_list_examples` (Task 2), existing `init_command`, `init_skill`.

The current command (in `src/sqrlly/cli/main.py`):

```python
from sqrlly.cli.init import init_command, init_skill
...
@cli.command()
@click.argument("directory", default=".", type=click.Path())
@click.option(
    "--skill", is_flag=True,
    help="Install the agent skill into <repo>/.agents/skills/sqrlly/ "
         "instead of scaffolding a workflow.",
)
def init(directory: str, skill: bool):
    """Scaffold a minimal runnable workflow into DIRECTORY (default `.`).

    With --skill, install the sqrlly agent skill into the working repo
    (repo-aware) so a coding agent auto-discovers it.
    """
    if skill:
        init_skill(directory)
    else:
        init_command(directory)
```

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/cli/test_init_example_cli.py
"""CLI-surface tests for `sqrlly init --example` / `--list-examples`."""
from __future__ import annotations

from click.testing import CliRunner

from sqrlly.cli.main import cli


def test_list_examples_flag_lists_all():
    res = CliRunner().invoke(cli, ["init", "--list-examples"])
    assert res.exit_code == 0
    for name in ("jokes", "route_classify", "explicit_join", "pipeline_style"):
        assert name in res.output


def test_example_scaffolds_into_named_subdir_by_default(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        res = runner.invoke(cli, ["init", "--example", "explicit_join"])
        assert res.exit_code == 0, res.output
        from pathlib import Path
        assert (Path("explicit_join") / "workflow.yaml").is_file()


def test_example_into_explicit_dir(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        res = runner.invoke(cli, ["init", "--example", "jokes", "myj"])
        assert res.exit_code == 0, res.output
        from pathlib import Path
        assert (Path("myj") / "workflow.yaml").is_file()


def test_example_and_skill_are_mutually_exclusive():
    res = CliRunner().invoke(cli, ["init", "--example", "jokes", "--skill"])
    assert res.exit_code != 0
    assert "not be combined" in res.output or "mutually exclusive" in res.output


def test_plain_init_still_defaults_to_cwd(tmp_path):
    runner = CliRunner()
    with runner.isolated_filesystem(temp_dir=tmp_path):
        res = runner.invoke(cli, ["init"])
        assert res.exit_code == 0, res.output
        from pathlib import Path
        assert Path("workflow.yaml").is_file()   # scaffolded into cwd
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/unit/cli/test_init_example_cli.py -q`
Expected: FAIL — `--list-examples` / `--example` are unknown options (exit_code != 0, "No such option").

- [ ] **Step 3: Implement**

In `src/sqrlly/cli/main.py`, update the import and the `init` command:

```python
from sqrlly.cli.init import (
    init_command, init_example, init_list_examples, init_skill,
)
```

```python
@cli.command()
@click.argument("directory", default=None, required=False, type=click.Path())
@click.option(
    "--skill", is_flag=True,
    help="Install the agent skill into <repo>/.agents/skills/sqrlly/ "
         "instead of scaffolding a workflow.",
)
@click.option(
    "--example", "example", default=None, metavar="NAME",
    help="Scaffold a bundled example (see --list-examples).",
)
@click.option(
    "--list-examples", "list_examples", is_flag=True,
    help="List the bundled examples available to --example.",
)
def init(
    directory: str | None, skill: bool, example: str | None,
    list_examples: bool,
):
    """Scaffold a runnable workflow into DIRECTORY.

    Bare: a minimal workflow (DIRECTORY default `.`). With --skill: install
    the agent skill doc into the working repo. With --example NAME: scaffold
    a bundled example (DIRECTORY default `./NAME`). --list-examples prints the
    available examples.
    """
    if sum(bool(x) for x in (skill, example, list_examples)) > 1:
        raise click.ClickException(
            "--skill, --example, and --list-examples may not be combined."
        )
    if list_examples:
        init_list_examples()
    elif example:
        init_example(example, directory or example)
    elif skill:
        init_skill(directory or ".")
    else:
        init_command(directory or ".")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/unit/cli/test_init_example_cli.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Run the existing init tests to confirm no regression**

Run: `uv run pytest tests/unit/cli -q`
Expected: PASS (all green — the `directory or "."` dispatch preserves the old default-to-cwd behavior).

- [ ] **Step 6: Commit**

```bash
git add src/sqrlly/cli/main.py tests/unit/cli/test_init_example_cli.py
git commit -m "feat: wire init --example / --list-examples CLI options"
```

---

### Task 4: E2E — every scaffolded example validates (and one runs)

**Files:**
- Test: `tests/e2e/test_init_example_runnable.py` (create)

**Interfaces:**
- Consumes: `init_example` (Task 2); `load_config` + `build_workflow_graph` (`sqrlly.cli.main` / `sqrlly.compile.graph`).

Why this task: the rewrite (Task 1) is only correct if the scaffolded workflow's URLs actually resolve. Compiling each scaffolded example from its own directory proves self-containment end-to-end. `explicit_join` additionally runs (pure echo — no backend).

- [ ] **Step 1: Write the failing test**

```python
# tests/e2e/test_init_example_runnable.py
"""Every scaffolded example compiles from its own dir; a no-backend one runs."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from sqrlly.cli.init import EXAMPLES, init_example
from sqrlly.cli.main import load_config
from sqrlly.compile.graph import build_workflow_graph


@pytest.mark.parametrize("name", sorted(EXAMPLES))
def test_scaffolded_example_validates(name, tmp_path):
    dest = tmp_path / name
    init_example(name, str(dest))
    cwd = os.getcwd()
    os.chdir(dest)            # relative URLs resolve against the workdir
    try:
        config = load_config("workflow.yaml")
        build_workflow_graph(config)          # raises on any wiring error
    finally:
        os.chdir(cwd)
    assert config.name


def test_scaffolded_explicit_join_runs(tmp_path):
    import asyncio

    from sqrlly.runtime.executor.dispatch import DispatchExecutor
    from sqrlly.runtime.state import make_initial_state

    dest = tmp_path / "ej"
    init_example("explicit_join", str(dest))
    cwd = os.getcwd()
    os.chdir(dest)
    try:
        config = load_config("workflow.yaml")
        graph = build_workflow_graph(config, DispatchExecutor(workdir="."))
        result = asyncio.run(graph.ainvoke(make_initial_state(workdir=".")))
    finally:
        os.chdir(cwd)
    assert "report" in result["completed_nodes"]
    assert not result.get("failed_nodes")
```

- [ ] **Step 2: Run the test to verify it fails (before any code) / passes (after Tasks 1-2)**

Run: `uv run pytest tests/e2e/test_init_example_runnable.py -q`
Expected: PASS once Tasks 1–2 are in (this task adds no production code — it is the integration gate). If a scaffolded example fails to compile, the rewrite or catalog is wrong — fix the offending `EXAMPLES` entry or `_rewrite_example_urls`, not the test.

- [ ] **Step 3: Commit**

```bash
git add tests/e2e/test_init_example_runnable.py
git commit -m "test: scaffolded examples compile from their own dir; explicit_join runs"
```

---

### Task 5: Package the curated files into the wheel

**Files:**
- Modify: `pyproject.toml` (the `[tool.hatch.build.targets.wheel.force-include]` block)
- Test: `tests/unit/test_packaging_examples.py` (create)

**Interfaces:** none (build config). `_load_example_file` already reads `sqrlly/_examples/<rest>` when present.

Current block:

```toml
[tool.hatch.build.targets.wheel.force-include]
"SKILLS.md" = "sqrlly/_skill.md"
```

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_packaging_examples.py
"""The curated examples must ship in the wheel under sqrlly/_examples/."""
from __future__ import annotations

import subprocess
import sys
import zipfile
from pathlib import Path

EXPECTED = [
    "sqrlly/_examples/jokes/workflow.yaml",
    "sqrlly/_examples/jokes/generate.md",
    "sqrlly/_examples/jokes/select.md",
    "sqrlly/_examples/jokes/gates/validate_jokes.py",
    "sqrlly/_examples/route_classify/workflow.yaml",
    "sqrlly/_examples/route_classify/scripts/triage.py",
    "sqrlly/_examples/explicit_join.yaml",
    "sqrlly/_examples/pipeline_style/workflow.yaml",
]


def test_curated_examples_present_in_wheel(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    subprocess.run(
        [sys.executable, "-m", "uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        cwd=repo, check=True, capture_output=True,
    )
    wheel = next(tmp_path.glob("*.whl"))
    names = set(zipfile.ZipFile(wheel).namelist())
    missing = [p for p in EXPECTED if p not in names]
    assert not missing, f"missing from wheel: {missing}"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/unit/test_packaging_examples.py -q`
Expected: FAIL — `missing from wheel: ['sqrlly/_examples/jokes/workflow.yaml', ...]`.

- [ ] **Step 3: Implement — extend the force-include block**

Replace the `force-include` block in `pyproject.toml` with:

```toml
[tool.hatch.build.targets.wheel.force-include]
"SKILLS.md" = "sqrlly/_skill.md"
# Curated examples scaffoldable via `sqrlly init --example` — pipx/uv-tool
# users have no repo checkout, so the run-essential files ride in the wheel
# under sqrlly/_examples/ (read by cli/init.py::_load_example_file).
"examples/jokes/workflow.yaml" = "sqrlly/_examples/jokes/workflow.yaml"
"examples/jokes/generate.md" = "sqrlly/_examples/jokes/generate.md"
"examples/jokes/select.md" = "sqrlly/_examples/jokes/select.md"
"examples/jokes/gates/validate_jokes.py" = "sqrlly/_examples/jokes/gates/validate_jokes.py"
"examples/route_classify/workflow.yaml" = "sqrlly/_examples/route_classify/workflow.yaml"
"examples/route_classify/scripts/triage.py" = "sqrlly/_examples/route_classify/scripts/triage.py"
"examples/explicit_join.yaml" = "sqrlly/_examples/explicit_join.yaml"
"examples/pipeline_style/workflow.yaml" = "sqrlly/_examples/pipeline_style/workflow.yaml"
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/unit/test_packaging_examples.py -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Verify the loader prefers the packaged copy** (build already validated by the test). Confirm nothing else broke:

Run: `uv run pytest tests/unit/cli/test_init_example.py tests/unit/cli/test_init_example_cli.py -q`
Expected: PASS (still green — repo fallback unaffected).

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml tests/unit/test_packaging_examples.py
git commit -m "build: force-include curated examples into wheel (sqrlly/_examples)"
```

---

### Task 6: Docs + changelog + close the TODO

**Files:**
- Modify: `README.md` (CLI table + a one-line `init --example` mention)
- Modify: `SKILLS.md` (Prerequisites/quickstart — note `init --example` for repo-less users)
- Modify: `CHANGELOG.md` (a new `## [Unreleased]` → `### Added`)
- Modify: `TODO.md` (remove the now-built `init --example` Features entry)

**Interfaces:** none.

- [ ] **Step 1: README — extend the CLI table**

In `README.md`, the `## CLI` table lists commands. Find the `init` row (or the table body) and ensure an entry exists:

```markdown
| `sqrlly init --example <name>` | Scaffold a bundled example into `./<name>` (`--list-examples` to see them). |
```

Add a one-liner under the CLI section prose:

```markdown
`init --example <name>` scaffolds a runnable bundled example (`jokes`, `route_classify`, `explicit_join`, `pipeline_style`) — handy after `uv tool install sqrlly` when you have no repo checkout. `init --list-examples` lists them.
```

- [ ] **Step 2: SKILLS — note it in the Prerequisites / quickstart area**

In `SKILLS.md`, near where `init` is first mentioned (or the Prerequisites section), add:

```markdown
No repo checkout? `sqrlly init --example jokes` scaffolds a runnable example on disk (`--list-examples` for the set: `jokes`, `route_classify`, `explicit_join`, `pipeline_style`).
```

- [ ] **Step 3: CHANGELOG — add an Unreleased entry**

At the top of `CHANGELOG.md`, above the latest released section, add:

```markdown
## [Unreleased]

### Added

- `sqrlly init --example <name>` scaffolds a runnable bundled example (`jokes`, `route_classify`, `explicit_join`, `pipeline_style`) onto disk, and `sqrlly init --list-examples` lists them. The run-essential files ship in the wheel, so `pipx`/`uv tool install` users with no repo checkout can scaffold and run an example immediately.
```

- [ ] **Step 4: TODO — remove the now-built entry**

In `TODO.md`, delete the `## Features` bullet beginning **"`sqrlly init --example <name>` — scaffold a bundled example"** (it's now shipped; CHANGELOG + git history record it).

- [ ] **Step 5: Sanity-check docs build / no broken validate**

Run: `uv run sqrlly init --list-examples`
Expected: lists the four examples.
Run: `uv run sqrlly validate examples/jokes/workflow.yaml`
Expected: `Valid: Joke Generator v0.1.0 (2 nodes)`.

- [ ] **Step 6: Commit**

```bash
git add README.md SKILLS.md CHANGELOG.md TODO.md
git commit -m "docs: document init --example; changelog; close TODO entry"
```

---

## Final verification (after all tasks)

```bash
uv run pytest tests/ --ignore=tests/acp --ignore=tests/cli -q   # full core suite green
uv run sqrlly init --example route_classify /tmp/sqrlly-ex && \
  ( cd /tmp/sqrlly-ex && uv run sqrlly run workflow.yaml ) && rm -rf /tmp/sqrlly-ex
```
Expected: full suite passes; the scaffolded `route_classify` runs end-to-end (pure script) and prints the run epilogue.
