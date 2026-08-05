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

# Curated examples scaffoldable via `sqrlly init --example`. dest -> repo src.
EXAMPLES: dict[str, dict] = {
    "jokes": {
        "description": "LLM prompt + quality gate + multi-node (needs an authed `codex` CLI).",
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
    "absurd-paper": {
        "description": "Full showcase: prompts + multi-dim gates + subgraph → rendered PDF (needs authed `codex` CLI + `uv`).",
        "files": {
            "gates/abstract_multi_dim.md": "examples/absurd-paper/gates/abstract_multi_dim.md",
            "gates/choose_topic_eval.md": "examples/absurd-paper/gates/choose_topic_eval.md",
            "gates/outline_json.py": "examples/absurd-paper/gates/outline_json.py",
            "gates/submission_check.py": "examples/absurd-paper/gates/submission_check.py",
            "preamble.md": "examples/absurd-paper/preamble.md",
            "prompts/abstract.md": "examples/absurd-paper/prompts/abstract.md",
            "prompts/choose_topic.md": "examples/absurd-paper/prompts/choose_topic.md",
            "prompts/discussion.md": "examples/absurd-paper/prompts/discussion.md",
            "prompts/intro.md": "examples/absurd-paper/prompts/intro.md",
            "prompts/methods.md": "examples/absurd-paper/prompts/methods.md",
            "prompts/outline.md": "examples/absurd-paper/prompts/outline.md",
            "prompts/reconcile.md": "examples/absurd-paper/prompts/reconcile.md",
            "prompts/results.md": "examples/absurd-paper/prompts/results.md",
            "scripts/persist_paper.py": "examples/absurd-paper/scripts/persist_paper.py",
            "scripts/pick_topic.py": "examples/absurd-paper/scripts/pick_topic.py",
            "scripts/render_pdf.py": "examples/absurd-paper/scripts/render_pdf.py",
            "subgraphs/compose_and_validate.yaml": "examples/absurd-paper/subgraphs/compose_and_validate.yaml",
            "workflow.yaml": "examples/absurd-paper/workflow.yaml",
        },
    },
}


def _rewrite_example_urls(yaml_text: str, name: str) -> str:
    """Strip the ``examples/<name>/`` prefix wherever it appears in a
    scaffolded example's YAML (workflow + subgraphs) — quoted
    ``url:``/``validator:`` path values AND header-comment run commands —
    so the copy is self-contained and runnable from its own directory.
    Absolute paths (e.g. ``/usr/bin/echo``) and refs to other examples
    don't carry the prefix, so they're left untouched."""
    return yaml_text.replace(f"examples/{name}/", "")


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


def init_example(name: str, directory: str) -> None:
    """Scaffold the curated example ``name`` into ``directory``.

    Writes each catalog file (creating subdirs like ``gates/``/``scripts/``),
    rewriting every YAML's ``examples/<name>/`` URL prefixes (workflow +
    subgraphs) so the copy is self-contained. Refuses to clobber an existing
    ``workflow.yaml``.
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
        # Rewrite every YAML (workflow + subgraphs) — subgraph files also
        # carry ``examples/<name>/`` prefixes on their prompt/gate URLs.
        if dest_rel.endswith((".yaml", ".yml")):
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
      transport: cli
      provider: openai       # uses Codex CLI; anthropic selects Claude
      model: gpt-5.6-luna
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
