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

import asyncio
import hashlib
import os
import shlex
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


def materialize_shares(base: str, dest: str, shares: list[str]) -> None:
    """Symlink each read-only share path from ``base`` into the worktree
    ``dest`` (relative symlink), then write the worktree exclude so the link
    stays out of the promote footprint. Raises if a configured share path is
    absent in ``base`` (fail fast — a dangling link makes in-branch tooling
    fail confusingly). Idempotent: a correct existing symlink is left as-is."""
    for share in shares:
        src = Path(base) / share
        if not src.exists():
            raise RuntimeError(
                f"worktree_share path {share!r} does not exist in base "
                f"{base!r} — install/build it before the run."
            )
        link = Path(dest) / share
        target = os.path.relpath(src, link.parent)
        if link.is_symlink():
            if os.readlink(link) != target:
                link.unlink()
                link.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(target, link)
        elif link.exists():
            # A real tracked path collides with the share name — leave it.
            continue
        else:
            link.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.symlink(target, link)
            except FileExistsError:
                pass  # race with a sibling acquiring the same group tree
        write_worktree_excludes(dest, [share])


def setup_fingerprint(base: str, commands: list[str]) -> str:
    """Sentinel content: hash of the setup commands + the base HEAD commit.
    Re-runs setup when commands change OR the base advances (a lockfile/schema
    change is a base commit in a git workflow). PM-agnostic — no knowledge of
    pnpm/Prisma file paths (decision: hash base state, not the worktree schema)."""
    try:
        head = subprocess.run(
            ["git", "-C", base, "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        ).stdout.strip()
    except subprocess.CalledProcessError:
        head = ""
    blob = "\n".join(commands) + "\x00" + head
    return hashlib.sha256(blob.encode()).hexdigest()


async def ensure_setup(
    *, base: str, dest: str, commands: list[str], excludes: list[str],
    store_dir: str | None, retries: int = 1,
) -> None:
    """Idempotently run the worktree setup commands in ``dest``.

    Sentinel-gated: skips when ``dest/.sqrlly/setup-ok`` matches the fingerprint.
    Writes ``excludes`` to info/exclude BEFORE running commands (so a crash
    mid-install can't leak). On non-zero exit after ``retries`` retries, raises
    ``RuntimeError`` (fatal for the branch — a clear diagnosable failure, not a
    misleading downstream gate failure). Writes the sentinel only on success.
    (GC registration of ``dest`` is the caller's responsibility, before this is
    awaited.)"""
    if not commands and not excludes:
        return
    marker = Path(dest) / ".sqrlly" / "setup-ok"
    fp = setup_fingerprint(base, commands)
    if marker.exists() and marker.read_text().strip() == fp:
        return
    # Exclude write first — even a crashed install must not leak artifacts.
    write_worktree_excludes(dest, excludes)
    env = dict(os.environ)
    if store_dir is not None:
        store_abs = str((Path(base) / store_dir).resolve())
        env["PNPM_HOME"] = store_abs
        env["npm_config_store_dir"] = store_abs
    for cmd in commands:
        argv = shlex.split(cmd)
        attempt = 0
        while True:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=dest, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            _out, errb = await proc.communicate()
            if proc.returncode == 0:
                break
            if attempt >= retries:
                raise RuntimeError(
                    f"worktree setup failed: {cmd!r} exit {proc.returncode} "
                    f"in {dest}: {errb.decode().strip()}"
                )
            attempt += 1
            await asyncio.sleep(0.5 * attempt)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(fp)
