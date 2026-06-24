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
from dataclasses import dataclass
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


def discover_changes(
    worktree: str, globs: list[str] | None = None,
    excludes: list[str] | None = None,
) -> dict[str, str]:
    """Return ``{path_relative_to_worktree: change_kind}`` for everything the
    worktree changed vs HEAD, including untracked adds and deletions.

    ``globs`` (git pathspec) filters to matches; ``excludes`` removes matches
    (a git ``:(exclude)`` pathspec — prefix match, so ``"node_modules"`` drops
    the dir/symlink and everything under it). Change kinds are ``"added"``,
    ``"modified"``, or ``"deleted"``.
    """
    # -z uses NUL-terminated output, avoiding the path quoting that
    # --porcelain=v1 applies to paths containing spaces or special chars.
    # For rename/copy entries the NUL-separated stream emits the destination
    # token first, then the source as a separate token — we consume and skip
    # the source so only the destination is recorded.
    cmd = ["git", "-C", worktree, "status", "--porcelain=v1", "-z",
           "--untracked-files=all"]
    pathspecs: list[str] = []
    if globs:
        # :(glob) is required so that ** matches across path separators;
        # without it git treats ** as a literal path component and misses
        # root-level files.
        pathspecs += [f":(glob){g}" for g in globs]
    if excludes:
        pathspecs += [f":(exclude){e}" for e in excludes]
    if pathspecs:
        cmd += ["--", *pathspecs]
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


@dataclass
class PromotionPlan:
    """Reconciliation result for multiple nodes' promote footprints.

    ``allowed`` maps ``node_id`` → the ``{path: kind}`` subset that node is
    cleared to apply. ``conflicts`` maps ``path`` → the owning ``node_id``s
    for any path claimed by more than one node (the report the caller
    surfaces to the user)."""
    allowed: dict[str, dict[str, str]]
    conflicts: dict[str, list[str]]


class PromoteConflictError(Exception):
    """Raised by ``plan_promotions`` under ``on_promote_conflict='fail'``
    when two or more nodes promote the same path. Carries the conflict map
    so the CLI can render an actionable message."""

    def __init__(self, conflicts: dict[str, list[str]]):
        self.conflicts = conflicts
        detail = "; ".join(
            f"{path} <- {', '.join(nodes)}"
            for path, nodes in sorted(conflicts.items())
        )
        super().__init__(
            f"Promote conflict ({len(conflicts)} path(s)) — the same path is "
            f"promoted by multiple nodes: {detail}"
        )


def reconcile_promotions(
    specs: list[tuple[str, str, list[str] | None]], base: str, mode: str,
    excludes: list[str] | None = None,
) -> PromotionPlan:
    """Discover each node's footprint, plan under ``mode``, then apply.

    ``specs`` is ``[(node_id, worktree, globs), ...]`` in promote order.
    ``excludes`` (git ``:(exclude)`` pathspecs) are filtered from EVERY node's
    footprint. Returns the ``PromotionPlan``. Raises ``PromoteConflictError``
    (``mode='fail'``) before any file is written."""
    footprints = {
        node_id: discover_changes(worktree, globs, excludes=excludes)
        for node_id, worktree, globs in specs
    }
    plan = plan_promotions(footprints, mode)
    for node_id, worktree, _ in specs:
        apply_changes(worktree, base, plan.allowed[node_id])
    return plan


def fanout_branch_specs(
    promote_parents: set[str], node_worktrees: dict[str, str],
) -> list[tuple[str, str, list[str] | None]]:
    """Promote specs for the branch worktrees of the given fan-out parents.

    ``node_worktrees`` (workflow state) records each Send branch's worktree
    keyed ``<parent_id>::<item_id>``. Each branch promotes its FULL delta
    (``None`` globs — a fan-out template has no ``output_contract``);
    ``promote_exclude`` still filters downstream. Returned sorted by child
    id for deterministic conflict ordering. Empty when no parent opts in.
    """
    specs: list[tuple[str, str, list[str] | None]] = []
    for child_id in sorted(node_worktrees):
        parent = child_id.split("::", 1)[0]
        if "::" in child_id and parent in promote_parents:
            specs.append((child_id, node_worktrees[child_id], None))
    return specs


def plan_promotions(
    footprints: dict[str, dict[str, str]], mode: str,
) -> PromotionPlan:
    """Reconcile per-node promote footprints under ``mode``.

    ``footprints`` is ``{node_id: {path: kind}}`` in promote order (the
    iteration order of ``config.nodes``). A path appearing in two or more
    footprints is a conflict (deletions count — a delete-vs-edit on the
    same path collides). Modes:

    - ``fail``      → raise ``PromoteConflictError`` (before any apply).
    - ``warn``      → every node keeps its full footprint (last-write-wins);
                      ``conflicts`` rides along for the caller to log.
    - ``overwrite`` → same allowance as ``warn``; the caller stays silent.
    - ``skip``      → the first owner (promote order) keeps a conflicting
                      path; later owners drop it (other paths still apply).
    """
    owners: dict[str, list[str]] = {}
    for node_id, changes in footprints.items():
        for path in changes:
            owners.setdefault(path, []).append(node_id)
    conflicts = {p: ns for p, ns in owners.items() if len(ns) > 1}

    if conflicts and mode == "fail":
        raise PromoteConflictError(conflicts)

    if mode == "skip":
        allowed: dict[str, dict[str, str]] = {}
        claimed: set[str] = set()
        for node_id, changes in footprints.items():
            allowed[node_id] = {
                p: k for p, k in changes.items() if p not in claimed
            }
            claimed.update(changes)
        return PromotionPlan(allowed=allowed, conflicts=conflicts)

    return PromotionPlan(
        allowed={nid: dict(ch) for nid, ch in footprints.items()},
        conflicts=conflicts,
    )
