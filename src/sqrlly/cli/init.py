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
import subprocess
from pathlib import Path

import click

# Where the skill doc lands in a consumer repo (Codex / Claude layout).
SKILL_INSTALL_REL = Path(".agents/skills/sqrlly/SKILL.md")


_WORKFLOW_YAML = """\
# Minimal sqrlly workflow — scaffolded by `sqrlly init`.
#
# Edit `prompts/hello.md` to change the prompt. To add quality gates,
# routing, fan-out, subgraphs, or more presets:
#   https://github.com/christopherseaman/sqrlly/blob/main/README.md
#   https://github.com/christopherseaman/sqrlly/blob/main/docs/schema-reference.md

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
