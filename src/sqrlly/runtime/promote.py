"""Single-source git-delta promotion helpers (langgraph-free, runtime layer).

Promotion = apply ONE worktree's diff (vs its fork point) onto the base.
The footprint is *discovered* from git, so unanticipated edits/deletes are
captured; an optional git-pathspec glob list filters it. Multi-source-
overlap reconciliation (true 3-way merge) is intentionally out of scope.

Known limitation: porcelain v1 renames emit ``R  old -> new`` in a single
line. We extract only the destination path and report it as "modified".
The source path (now deleted under its old name) does NOT appear as a
separate "deleted" entry — the caller sees only the rename target. LLM-
authored edits rarely produce pure git renames, so this is acceptable.
"""
from __future__ import annotations

import subprocess

# git status --porcelain=v1 XY codes -> change kind.
# We only run promotion on a node's own freshly-forked worktree, so the
# index/worktree column distinction does not matter — collapse to kind.
_STATUS: dict[str, str] = {
    "A": "added",
    "M": "modified",
    "D": "deleted",
    # Rename: report the destination as modified; source disappears.
    "R": "modified",
    # Copy: destination is new content.
    "C": "added",
}


def discover_changes(worktree: str, globs: list[str] | None = None) -> dict[str, str]:
    """Return ``{path_relative_to_worktree: change_kind}`` for everything the
    worktree changed vs HEAD, including untracked adds and deletions.

    ``globs`` (git pathspec, e.g. ``["prd/**", "*.md"]``) filters the set.
    Change kinds are ``"added"``, ``"modified"``, or ``"deleted"``.
    """
    cmd = ["git", "-C", worktree, "status", "--porcelain=v1", "--untracked-files=all"]
    if globs:
        # Prefix each glob with :(glob) so that ** matches across path
        # separators, consistent with shell glob expectations.
        cmd += ["--", *[f":(glob){g}" for g in globs]]
    out = subprocess.run(cmd, capture_output=True, text=True, check=True).stdout
    changes: dict[str, str] = {}
    for line in out.splitlines():
        if not line:
            continue
        xy, path = line[:2], line[3:]
        if xy == "??":
            changes[path] = "added"
        else:
            # xy is two chars: index-status + worktree-status. The first
            # non-space char is the meaningful code for our purposes.
            code = xy.strip()[:1]
            kind = _STATUS.get(code, "modified")
            if code == "R":
                # Rename line: "old -> new" — keep only the destination.
                path = path.split(" -> ")[-1]
            changes[path] = kind
    return changes
