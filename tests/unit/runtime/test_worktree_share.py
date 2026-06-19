"""Worktree dep-sharing helpers (runtime, langgraph-free)."""
import subprocess
from pathlib import Path

from sqrlly.runtime.promote import discover_changes
from sqrlly.runtime.worktree_share import write_worktree_excludes


def _repo(tmp):
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp)], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp), "config", "user.name", "t"], check=True)
    (tmp / "a.txt").write_text("orig")
    subprocess.run(["git", "-C", str(tmp), "add", "."], check=True)
    subprocess.run(["git", "-C", str(tmp), "commit", "-qm", "init"], check=True)


def _wt(tmp, name):
    dest = tmp / ".sqrlly" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(tmp), "worktree", "add", "-q", str(dest), "HEAD"], check=True)
    return dest


def _exclude_lines(worktree, needle):
    rel = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "--git-path", "info/exclude"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    p = Path(rel) if Path(rel).is_absolute() else Path(worktree) / rel
    return [l for l in p.read_text().splitlines() if l.strip() == needle]


def test_write_excludes_hides_dir_from_git_status(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "wa")
    (wt / "node_modules").mkdir()
    (wt / "node_modules" / "dep.js").write_text("x")
    assert any(p.startswith("node_modules") for p in discover_changes(str(wt)))
    write_worktree_excludes(str(wt), ["node_modules"])
    assert not any(p.startswith("node_modules") for p in discover_changes(str(wt)))


def test_write_excludes_is_idempotent(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "wb")
    write_worktree_excludes(str(wt), ["node_modules"])
    write_worktree_excludes(str(wt), ["node_modules"])
    assert len(_exclude_lines(wt, "node_modules")) == 1


def test_write_excludes_strips_trailing_slash(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "wc")
    write_worktree_excludes(str(wt), ["node_modules/"])
    assert len(_exclude_lines(wt, "node_modules")) == 1  # slash stripped


import os
from sqrlly.runtime.worktree_share import materialize_shares


def test_materialize_shares_symlinks_and_excludes(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "wmc")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("lib")
    materialize_shares(str(tmp_path), str(wt), ["node_modules"])
    link = wt / "node_modules"
    assert link.is_symlink()
    assert (link / "dep.js").read_text() == "lib"
    assert not os.path.isabs(os.readlink(link))  # relative target
    assert not any(p.startswith("node_modules") for p in discover_changes(str(wt)))


def test_materialize_shares_missing_base_path_raises(tmp_path):
    import pytest
    _repo(tmp_path); wt = _wt(tmp_path, "wmd")
    with pytest.raises(RuntimeError, match="worktree_share"):
        materialize_shares(str(tmp_path), str(wt), ["node_modules"])


def test_materialize_shares_idempotent(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "wme")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "dep.js").write_text("lib")
    materialize_shares(str(tmp_path), str(wt), ["node_modules"])
    materialize_shares(str(tmp_path), str(wt), ["node_modules"])  # no raise
    assert (wt / "node_modules").is_symlink()


import pytest as _pytest
from sqrlly.runtime.worktree_share import ensure_setup


@_pytest.mark.asyncio
async def test_ensure_setup_runs_commands_writes_excludes_and_sentinel(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "ws1")
    await ensure_setup(
        base=str(tmp_path), dest=str(wt),
        commands=["sh -c 'echo hi > marker.txt'"],
        excludes=["node_modules"], store_dir=None,
    )
    assert (wt / "marker.txt").read_text().strip() == "hi"
    assert (wt / ".sqrlly" / "setup-ok").exists()
    # exclude written: a node_modules dir would be hidden from the footprint
    (wt / "node_modules").mkdir(); (wt / "node_modules" / "d.js").write_text("x")
    assert not any(p.startswith("node_modules") for p in discover_changes(str(wt)))


@_pytest.mark.asyncio
async def test_ensure_setup_is_idempotent(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "ws2")
    cmds = ["sh -c 'echo x >> count.txt'"]
    await ensure_setup(base=str(tmp_path), dest=str(wt), commands=cmds, excludes=[], store_dir=None)
    await ensure_setup(base=str(tmp_path), dest=str(wt), commands=cmds, excludes=[], store_dir=None)
    assert (wt / "count.txt").read_text().count("x") == 1  # ran once (sentinel)


@_pytest.mark.asyncio
async def test_ensure_setup_failure_raises_branch_fatal(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "ws3")
    with _pytest.raises(RuntimeError, match="setup failed"):
        await ensure_setup(
            base=str(tmp_path), dest=str(wt),
            commands=["sh -c 'exit 3'"], excludes=[], store_dir=None, retries=0,
        )
    assert not (wt / ".sqrlly" / "setup-ok").exists()  # no sentinel on failure


@_pytest.mark.asyncio
async def test_ensure_setup_store_dir_env(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "ws4")
    await ensure_setup(
        base=str(tmp_path), dest=str(wt),
        commands=["sh -c 'echo $PNPM_HOME > store.txt'"],
        excludes=[], store_dir=".sqrlly/.pnpm-store",
    )
    got = (wt / "store.txt").read_text().strip()
    assert got.endswith(".sqrlly/.pnpm-store")  # absolute, under base
