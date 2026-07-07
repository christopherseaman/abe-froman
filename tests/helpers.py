"""Shared test utilities — imported by test modules, not conftest.py."""

import shutil
import subprocess
from pathlib import Path

from sqrlly.schema.models import Graph, LlmPreset, Settings

# Resolve binaries once at import time.
_ECHO = shutil.which("echo") or "/bin/echo"
_FALSE = shutil.which("false") or "/bin/false"


def init_git_repo(
    path: Path,
    *,
    branch: str = "main",
    files: dict[str, str] | None = None,
    commit: bool = True,
) -> None:
    """Initialize a git repo at ``path`` with a deterministic identity.

    By default makes one commit so ``ForemanExecutor`` can branch
    worktrees off HEAD. ``files`` (``{relpath: content}``) are written
    and committed; without them ``commit=True`` makes an empty initial
    commit. ``commit=False`` leaves the repo with no commits (e.g. a
    bare repo for a scaffolding test).
    """
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "t"], check=True
    )
    if files:
        for rel, content in files.items():
            (path / rel).write_text(content)
        subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    if commit:
        argv = ["git", "-C", str(path), "commit", "-q", "-m", "init"]
        if not files:
            argv.insert(4, "--allow-empty")
        subprocess.run(argv, check=True)


def single_preset_settings(model: str = "sonnet", **extra_settings) -> Settings:
    """Settings with one default preset — for tests that wire a single
    backend through DispatchExecutor(prompt_backends={"default": ...}).
    """
    return Settings(
        presets={
            "default": LlmPreset(
                transport="acp", provider="anthropic",
                model=model, default=True,
            ),
        },
        **extra_settings,
    )


def make_config(nodes, **settings_kwargs) -> Graph:
    """Build a Graph from a node list and optional settings."""
    return Graph(
        name="Test",
        version="1.0.0",
        nodes=nodes,
        settings=settings_kwargs,
    )


def cmd_phase(id, name="", output="ok", depends_on=None, **kwargs):
    """Shorthand for a command node that echoes a known string."""
    return {
        "id": id,
        "name": name or id,
        "execute": {"url": _ECHO, "params": {"args": ["-n", output]}},
        "depends_on": depends_on or [],
        **kwargs,
    }


def fail_phase(id, name="", depends_on=None, **kwargs):
    """Shorthand for a command node that always fails."""
    return {
        "id": id,
        "name": name or id,
        "execute": {"url": _FALSE},
        "depends_on": depends_on or [],
        **kwargs,
    }
