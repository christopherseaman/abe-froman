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
