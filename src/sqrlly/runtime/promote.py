"""Single-source git-delta promotion helpers (langgraph-free, runtime layer).

Promotion = apply ONE worktree's diff (vs its fork point) onto the base.
The footprint is *discovered* from git, so unanticipated edits/deletes are
captured; an optional git-pathspec glob list filters it. Multi-source-
overlap reconciliation (true 3-way merge) is intentionally out of scope.

Known limitation: porcelain v1 renames emit the destination path followed
by a NUL-separated source token. We record the destination as "modified"
and skip the source token. The source path (deleted under its old name)
does NOT appear as a separate "deleted" entry — the caller sees only the
rename target. LLM-authored edits rarely produce pure git renames, so
this is acceptable.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

# git status --porcelain=v1 XY codes -> change kind.
# We only run promotion on a node's own freshly-forked worktree, so the
# index/worktree column distinction does not matter — collapse to kind.
_STATUS: dict[str, str] = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    # Rename: report the destination as modified; source disappears.
    "R": "modified",
    # C is only emitted under status.renames=copies, which we don't set, so
    # this never fires today; kept to mirror git's documented porcelain codes.
    "C": "added",
}


def discover_changes(worktree: str, globs: list[str] | None = None) -> dict[str, str]:
    """Return ``{path_relative_to_worktree: change_kind}`` for everything the
    worktree changed vs HEAD, including untracked adds and deletions.

    ``globs`` (git pathspec, e.g. ``["prd/**", "*.md"]``) filters the set.
    Change kinds are ``"added"``, ``"modified"``, or ``"deleted"``.
    """
    # -z uses NUL-terminated output, avoiding the path quoting that
    # --porcelain=v1 applies to paths containing spaces or special chars.
    # For rename/copy entries the NUL-separated stream emits the destination
    # token first, then the source as a separate token — we consume and skip
    # the source so only the destination is recorded.
    cmd = ["git", "-C", worktree, "status", "--porcelain=v1", "-z",
           "--untracked-files=all"]
    if globs:
        # :(glob) is required so that ** matches across path separators;
        # without it git treats ** as a literal path component and misses
        # root-level files.
        cmd += ["--", *(f":(glob){g}" for g in globs)]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    tokens = out.split("\0")
    changes: dict[str, str] = {}
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        if not entry:
            i += 1
            continue
        code, path = entry[:2], entry[3:]
        if code == "??":
            changes[path] = "added"
        else:
            # xy is two chars: index-status + worktree-status. The first
            # non-space char is the meaningful code for our purposes.
            c = code.strip()[:1]
            changes[path] = _STATUS.get(c, "modified")
            if c in ("R", "C"):  # rename/copy: next token is the source path
                i += 1           # skip it (destination is what we promote)
        i += 1
    return changes


def apply_changes(worktree: str, base: str, changes: dict[str, str]) -> list[str]:
    """Apply a discovered ``{path: kind}`` delta onto ``base``. Adds/edits
    are copied; deletions are removed. Returns the applied paths. The
    ``changes`` map is what ``discover_changes`` returns (optionally
    filtered, e.g. by conflict reconciliation)."""
    for rel, kind in changes.items():
        dst = Path(base) / rel
        if kind == "deleted":
            dst.unlink(missing_ok=True)
        else:
            src = Path(worktree) / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    return list(changes)


def promote(worktree: str, base: str, globs: list[str] | None = None) -> list[str]:
    """Apply the worktree's discovered delta onto ``base`` (single-source).
    Assumes ``base`` has not diverged for these paths (it is the fork
    point). Returns applied paths."""
    return apply_changes(worktree, base, discover_changes(worktree, globs))
