"""Worktree dependency-sharing helpers (runtime layer, langgraph-free).

The load-bearing piece is ``write_worktree_excludes``: it appends paths to a
worktree's ``info/exclude`` so shared/generated artifacts (node_modules, build
caches, in-tree generated clients) never appear in ``git status
--untracked-files=all`` — i.e. never enter ``promote.discover_changes``'
footprint, so ``apply_changes`` can't follow them into base. A real
``.gitignore`` rule cannot be trusted (a ``node_modules/`` dir-slash rule does
NOT hide a symlink named ``node_modules``); writing the bare path here does.
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def _info_exclude_path(worktree: str) -> Path:
    """Resolve the worktree's git ``info/exclude`` file (absolute)."""
    rel = subprocess.run(
        ["git", "-C", worktree, "rev-parse", "--git-path", "info/exclude"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    p = Path(rel)
    return p if p.is_absolute() else Path(worktree) / p


def _ends_with_newline(p: Path) -> bool:
    with p.open("rb") as fh:
        try:
            fh.seek(-1, 2)
        except OSError:
            return True  # empty file
        return fh.read(1) == b"\n"


def write_worktree_excludes(worktree: str, paths: list[str]) -> None:
    """Append each path (trailing slash stripped) to the worktree's
    ``info/exclude`` if not already a line. Bare (slash-less) entries match a
    symlink/dir of that name, which a ``foo/`` .gitignore rule does not.
    Idempotent — safe to call on every worktree hand-back."""
    if not paths:
        return
    exclude_file = _info_exclude_path(worktree)
    exclude_file.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        set(exclude_file.read_text().splitlines())
        if exclude_file.exists() else set()
    )
    to_add = [p.rstrip("/") for p in paths if p.rstrip("/") not in existing]
    if not to_add:
        return
    with exclude_file.open("a") as fh:
        if exclude_file.stat().st_size and not _ends_with_newline(exclude_file):
            fh.write("\n")
        for p in to_add:
            fh.write(p + "\n")
