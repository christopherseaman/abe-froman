import subprocess
from pathlib import Path
import pytest
from sqrlly.runtime.promote import (
    PromoteConflictError,
    PromotionPlan,
    apply_changes,
    discover_changes,
    plan_promotions,
    promote,
)


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


def test_promote_applies_add_and_edit_to_base(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path)
    (wt/"a.txt").write_text("changed"); (wt/"new.md").write_text("hi")
    applied = promote(str(wt), str(tmp_path))
    assert (tmp_path/"a.txt").read_text() == "changed"
    assert (tmp_path/"new.md").read_text() == "hi"
    assert set(applied) == {"a.txt", "new.md"}


def test_promote_deletion_propagates(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path)
    (wt/"a.txt").unlink()
    promote(str(wt), str(tmp_path))
    assert not (tmp_path/"a.txt").exists()


def test_promote_creates_nested_dirs(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path)
    (wt/"sub").mkdir(); (wt/"sub"/"deep.md").write_text("z")
    promote(str(wt), str(tmp_path))
    assert (tmp_path/"sub"/"deep.md").read_text() == "z"


def test_apply_changes_applies_only_the_given_paths(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path)
    (wt / "a.txt").write_text("changed"); (wt / "new.md").write_text("hi")
    # Hand a subset: only new.md should land; a.txt must stay at base's original.
    applied = apply_changes(str(wt), str(tmp_path), {"new.md": "added"})
    assert (tmp_path / "new.md").read_text() == "hi"
    assert (tmp_path / "a.txt").read_text() == "orig"   # NOT overwritten
    assert applied == ["new.md"]


def _footprints():
    # writer_a and writer_b both touch shared.txt; each also touches a unique file.
    return {
        "writer_a": {"shared.txt": "added", "a.txt": "added"},
        "writer_b": {"shared.txt": "added", "b.txt": "added"},
    }


def test_plan_disjoint_has_no_conflict():
    fp = {"writer_a": {"a.txt": "added"}, "writer_b": {"b.txt": "added"}}
    plan = plan_promotions(fp, "warn")
    assert plan.conflicts == {}
    assert plan.allowed == fp


def test_plan_warn_keeps_full_footprints_and_reports_conflict():
    plan = plan_promotions(_footprints(), "warn")
    assert plan.conflicts == {"shared.txt": ["writer_a", "writer_b"]}
    assert plan.allowed["writer_a"] == {"shared.txt": "added", "a.txt": "added"}
    assert plan.allowed["writer_b"] == {"shared.txt": "added", "b.txt": "added"}


def test_plan_overwrite_matches_warn_allowance():
    assert (plan_promotions(_footprints(), "overwrite").allowed
            == plan_promotions(_footprints(), "warn").allowed)


def test_plan_skip_first_writer_keeps_conflicting_path():
    plan = plan_promotions(_footprints(), "skip")
    # writer_a (first in order) keeps shared.txt; writer_b drops it but
    # keeps its non-conflicting b.txt.
    assert plan.allowed["writer_a"] == {"shared.txt": "added", "a.txt": "added"}
    assert plan.allowed["writer_b"] == {"b.txt": "added"}
    assert plan.conflicts == {"shared.txt": ["writer_a", "writer_b"]}


def test_plan_fail_raises_before_any_decision():
    with pytest.raises(PromoteConflictError) as ei:
        plan_promotions(_footprints(), "fail")
    assert "shared.txt" in str(ei.value)
    assert ei.value.conflicts == {"shared.txt": ["writer_a", "writer_b"]}


def test_plan_fail_no_conflict_returns_plan():
    fp = {"writer_a": {"a.txt": "added"}, "writer_b": {"b.txt": "added"}}
    plan = plan_promotions(fp, "fail")
    assert isinstance(plan, PromotionPlan)
    assert plan.conflicts == {}


def test_plan_skip_three_node_conflict_only_first_keeps():
    fp = {
        "a": {"shared.txt": "modified", "ua.txt": "added"},
        "b": {"shared.txt": "modified", "ub.txt": "added"},
        "c": {"shared.txt": "modified", "uc.txt": "added"},
    }
    plan = plan_promotions(fp, "skip")
    assert plan.allowed["a"] == {"shared.txt": "modified", "ua.txt": "added"}
    assert plan.allowed["b"] == {"ub.txt": "added"}
    assert plan.allowed["c"] == {"uc.txt": "added"}
    assert plan.conflicts == {"shared.txt": ["a", "b", "c"]}


def test_plan_delete_vs_edit_is_a_conflict():
    fp = {"a": {"shared.txt": "deleted"}, "b": {"shared.txt": "modified"}}
    assert plan_promotions(fp, "warn").conflicts == {"shared.txt": ["a", "b"]}
    with pytest.raises(PromoteConflictError):
        plan_promotions(fp, "fail")


def test_plan_empty_footprints_no_conflict_any_mode():
    for mode in ("fail", "warn", "overwrite", "skip"):
        plan = plan_promotions({}, mode)
        assert plan.allowed == {}
        assert plan.conflicts == {}
