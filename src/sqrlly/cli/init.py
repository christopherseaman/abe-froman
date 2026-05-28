"""`sqrlly init` — scaffold a minimal runnable workflow.

Lets users who installed via ``pipx install sqrlly`` get a working
workflow on disk without cloning the repo. The scaffold is intentionally
minimal: one prompt node, one prompt file, one CLI-transport preset.
"""
from __future__ import annotations

from pathlib import Path

import click


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
