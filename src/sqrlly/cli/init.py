"""`sqrlly init` — scaffold a minimal runnable workflow.

Lets users who installed via ``pipx install sqrlly`` get a working
workflow on disk without cloning the repo. The scaffold is intentionally
minimal: one prompt node, one prompt file, one CLI-transport preset.

``init --skill`` instead installs the agent skill doc (the repo's
``SKILLS.md``) into a working repo at ``.agents/skills/sqrlly/SKILL.md``
so a coding agent auto-discovers it.
"""
from __future__ import annotations

import importlib.resources
import re
import subprocess
from pathlib import Path

import click

# Where the skill doc lands in a consumer repo (Codex / Claude layout).
SKILL_INSTALL_REL = Path(".agents/skills/sqrlly/SKILL.md")

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


_WORKFLOW_YAML = """\
# Minimal sqrlly workflow — scaffolded by `sqrlly init`.
#
# Edit `prompts/hello.md` to change the prompt. To add quality gates,
# routing, fan-out, subgraphs, or more presets:
#   https://github.com/christopherseaman/sqrlly/blob/main/README.md
#   https://github.com/christopherseaman/sqrlly/blob/main/SCHEMA.md

name: "My sqrlly workflow"
version: "0.1.0"

nodes:
  - id: hello
    name: "Hello"
    execute:
      url: "prompts/hello.md"

settings:
  presets:
    default:
      transport: cli         # cli | acp
      provider: anthropic
      model: sonnet
      default: true
"""

_HELLO_MD = "Write a one-sentence friendly greeting.\n"


def init_command(directory: str) -> None:
    """Scaffold a minimal workflow into ``directory``.

    Creates ``directory`` if missing. Refuses to clobber an existing
    ``workflow.yaml`` — caller can ``rm`` it if intentional. Prints
    next-step invocation hints on success.
    """
    target = Path(directory)
    workflow_path = target / "workflow.yaml"
    prompt_dir = target / "prompts"
    prompt_path = prompt_dir / "hello.md"

    if workflow_path.exists():
        raise click.ClickException(
            f"{workflow_path} already exists. "
            f"Remove it or pick a different directory."
        )

    target.mkdir(parents=True, exist_ok=True)
    prompt_dir.mkdir(parents=True, exist_ok=True)
    workflow_path.write_text(_WORKFLOW_YAML)
    prompt_path.write_text(_HELLO_MD)

    display = str(target) if str(target) != "." else "the current directory"
    click.echo(f"Scaffolded sqrlly workflow into {display}.")
    click.echo()
    click.echo("Next steps:")
    if str(target) != ".":
        click.echo(f"  cd {target}")
    click.echo("  sqrlly validate workflow.yaml")
    click.echo("  sqrlly run workflow.yaml")


def _load_skill_doc() -> str:
    """The canonical skill doc text.

    In an installed wheel it's `force-include`d at ``sqrlly/_skill.md``
    (mapped from the repo's ``SKILLS.md`` at build). Running from a
    source checkout that file isn't materialized, so fall back to the
    repo-root ``SKILLS.md`` found by walking up from this module.
    """
    res = importlib.resources.files("sqrlly").joinpath("_skill.md")
    if res.is_file():
        return res.read_text(encoding="utf-8")
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "SKILLS.md"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
    raise click.ClickException("Could not locate the sqrlly skill doc.")


def _repo_root(start: str) -> Path:
    """Git top-level of ``start`` if inside a working tree, else ``start``.

    Makes ``init --skill`` repo-aware: the skill lands at the repo root's
    ``.agents/`` even when invoked from a subdirectory.
    """
    try:
        out = subprocess.run(
            ["git", "-C", start, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass
    return Path(start)


def init_skill(directory: str) -> None:
    """Install the agent skill doc into a working repo.

    Writes ``<repo-root>/.agents/skills/sqrlly/SKILL.md`` (repo-aware;
    falls back to ``directory`` outside a git tree). Overwrites on
    re-run so the skill refreshes to the installed version. Reports the
    path written.
    """
    dest = _repo_root(directory) / SKILL_INSTALL_REL
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_load_skill_doc(), encoding="utf-8")
    click.echo(f"Installed sqrlly skill → {dest.resolve()}")
