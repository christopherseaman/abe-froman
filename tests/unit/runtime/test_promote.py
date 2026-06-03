import subprocess
from pathlib import Path
import pytest
from sqrlly.runtime.promote import discover_changes


def _repo(tmp):
    subprocess.run(["git","init","-q","-b","main",str(tmp)],check=True)
    subprocess.run(["git","-C",str(tmp),"config","user.email","t@t"],check=True)
    subprocess.run(["git","-C",str(tmp),"config","user.name","t"],check=True)
    (tmp/"a.txt").write_text("orig")
    subprocess.run(["git","-C",str(tmp),"add","."],check=True)
    subprocess.run(["git","-C",str(tmp),"commit","-qm","init"],check=True)


def _wt(tmp):
    dest = tmp/".sqrlly"/"wt"
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git","-C",str(tmp),"worktree","add","-q",str(dest),"HEAD"],check=True)
    return dest


def test_discover_add_and_edit(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path)
    (wt/"a.txt").write_text("changed")   # edit tracked file
    (wt/"new.md").write_text("hi")        # add untracked file
    changes = discover_changes(str(wt))
    assert changes["new.md"] == "added"
    assert changes["a.txt"] == "modified"


def test_discover_captures_deletion(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path)
    (wt/"a.txt").unlink()
    assert discover_changes(str(wt))["a.txt"] == "deleted"


def test_glob_filter_pathspec(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path)
    (wt/"keep.md").write_text("x"); (wt/"skip.txt").write_text("y")
    changes = discover_changes(str(wt), globs=["**/*.md"])
    assert "keep.md" in changes and "skip.txt" not in changes


def test_discover_path_with_spaces(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path)
    (wt/"has space.md").write_text("x")
    changes = discover_changes(str(wt))
    assert "has space.md" in changes          # no surrounding quotes
    assert changes["has space.md"] == "added"
