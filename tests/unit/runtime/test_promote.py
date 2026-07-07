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
    reconcile_promotions,
)
from sqrlly.schema.models import OutputContract
from helpers import init_git_repo


def _repo(tmp):
    init_git_repo(tmp, files={"a.txt": "orig"})

def _wt(tmp, name="wt"):
    dest = tmp/".sqrlly"/name
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


def _two_writers(tmp_path):
    """Two worktrees: both write shared.txt (different content) + a unique file."""
    _repo(tmp_path)
    wa = _wt(tmp_path, "wa"); wb = _wt(tmp_path, "wb")
    (wa/"shared.txt").write_text("AAA"); (wa/"a.txt").write_text("a")
    (wb/"shared.txt").write_text("BBB"); (wb/"b.txt").write_text("b")
    return [("writer_a", str(wa), None), ("writer_b", str(wb), None)]


def test_reconcile_warn_last_write_wins_and_reports(tmp_path):
    specs = _two_writers(tmp_path)
    plan = reconcile_promotions(specs, str(tmp_path), "warn")
    assert (tmp_path/"shared.txt").read_text() == "BBB"   # later writer wins
    assert (tmp_path/"a.txt").read_text() == "a"
    assert (tmp_path/"b.txt").read_text() == "b"
    assert plan.conflicts == {"shared.txt": ["writer_a", "writer_b"]}


def test_reconcile_skip_first_writer_wins(tmp_path):
    specs = _two_writers(tmp_path)
    reconcile_promotions(specs, str(tmp_path), "skip")
    assert (tmp_path/"shared.txt").read_text() == "AAA"   # first writer kept
    assert (tmp_path/"a.txt").read_text() == "a"
    assert (tmp_path/"b.txt").read_text() == "b"          # non-conflicting still lands


def test_reconcile_fail_aborts_before_any_write(tmp_path):
    specs = _two_writers(tmp_path)
    with pytest.raises(PromoteConflictError):
        reconcile_promotions(specs, str(tmp_path), "fail")
    # Nothing applied — discover-first means the raise precedes all copies.
    # shared.txt and b.txt were never in the base repo.
    # a.txt was in the base repo ("orig") and must not have been overwritten.
    assert not (tmp_path/"shared.txt").exists()
    assert (tmp_path/"a.txt").read_text() == "orig"
    assert not (tmp_path/"b.txt").exists()


def test_reconcile_disjoint_promotes_both(tmp_path):
    _repo(tmp_path)
    wa = _wt(tmp_path, "wa"); wb = _wt(tmp_path, "wb")
    (wa/"a.txt").write_text("a"); (wb/"b.txt").write_text("b")
    specs = [("writer_a", str(wa), None), ("writer_b", str(wb), None)]
    plan = reconcile_promotions(specs, str(tmp_path), "warn")
    assert plan.conflicts == {}
    assert (tmp_path/"a.txt").read_text() == "a"
    assert (tmp_path/"b.txt").read_text() == "b"


def test_reconcile_glob_honors_base_directory(tmp_path):
    """#4: globs from required_paths() (base_directory prepended) promote the
    subdir file; the raw-required_files glob would promote nothing."""
    _repo(tmp_path)
    wt = _wt(tmp_path, "wc")
    (wt/"reference").mkdir()
    (wt/"reference"/"prd-context-map.json").write_text("{}")
    contract = OutputContract(
        base_directory="reference", required_files=["prd-context-map.json"],
    )
    reconcile_promotions(
        [("ref", str(wt), contract.required_paths())], str(tmp_path), "warn",
    )
    assert (tmp_path/"reference"/"prd-context-map.json").read_text() == "{}"


def test_reconcile_raw_required_files_glob_promotes_nothing(tmp_path):
    """Documents the bug #4 fixes: the raw (un-prepended) pathspec matches
    only a root-level file, so the subdir file is excluded."""
    _repo(tmp_path)
    wt = _wt(tmp_path, "wd")
    (wt/"reference").mkdir()
    (wt/"reference"/"prd-context-map.json").write_text("{}")
    reconcile_promotions(
        [("ref", str(wt), ["prd-context-map.json"])], str(tmp_path), "warn",
    )
    assert not (tmp_path/"reference"/"prd-context-map.json").exists()


def test_discover_changes_excludes_pathspec(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "wex")
    (wt / "real.txt").write_text("keep")
    (wt / "node_modules").mkdir()
    (wt / "node_modules" / "dep.js").write_text("x")
    assert "node_modules/dep.js" in discover_changes(str(wt))
    changes = discover_changes(str(wt), excludes=["node_modules"])
    assert "real.txt" in changes
    assert not any(p.startswith("node_modules") for p in changes)


def test_reconcile_threads_excludes(tmp_path):
    _repo(tmp_path); wt = _wt(tmp_path, "wex2")
    (wt / "real.txt").write_text("keep")
    (wt / "node_modules").mkdir()
    (wt / "node_modules" / "dep.js").write_text("x")
    reconcile_promotions(
        [("n", str(wt), None)], str(tmp_path), "warn", excludes=["node_modules"],
    )
    assert (tmp_path / "real.txt").read_text() == "keep"
    assert not (tmp_path / "node_modules").exists()


from sqrlly.runtime.promote import fanout_branch_specs


def test_fanout_branch_specs_selects_promote_parents():
    node_worktrees = {
        "build::feat_a": "/wt/build__feat_a",
        "build::feat_b": "/wt/build__feat_b",
        "plan::v1": "/wt/plan__v1",     # parent not promoting
        "toplevel_node": "/wt/toplevel", # not a branch (no ::)
    }
    specs = fanout_branch_specs({"build"}, node_worktrees)
    assert specs == [
        ("build::feat_a", "/wt/build__feat_a", None),
        ("build::feat_b", "/wt/build__feat_b", None),
    ]

def test_fanout_branch_specs_empty_when_no_promote_parents():
    assert fanout_branch_specs(set(), {"build::a": "/wt/a"}) == []


def test_discover_changes_reinclude_keeps_subpath(tmp_path):
    _repo(tmp_path)
    wt = _wt(tmp_path, "wt-ri")
    # Create changes: a kept subpath under an excluded dir, a dropped sibling,
    # and an unrelated file.
    (wt / "log" / "phases").mkdir(parents=True)
    (wt / "log" / "phases" / "keep.txt").write_text("keep")
    (wt / "log" / "noise.txt").write_text("noise")
    (wt / "src").mkdir()
    (wt / "src" / "main.py").write_text("x")

    out = discover_changes(
        str(wt), excludes=["log/"], includes=["log/phases/**"],
    )
    assert "src/main.py" in out                 # unrelated change kept
    assert "log/phases/keep.txt" in out         # re-included by allow-list
    assert "log/noise.txt" not in out           # excluded, not re-included


def test_discover_changes_no_includes_unchanged(tmp_path):
    _repo(tmp_path)
    wt = _wt(tmp_path, "wt-noinc")
    (wt / "log").mkdir()
    (wt / "log" / "a.txt").write_text("a")
    (wt / "src").mkdir(); (wt / "src" / "b.py").write_text("b")
    out = discover_changes(str(wt), excludes=["log/"])  # includes default None
    assert "src/b.py" in out
    assert "log/a.txt" not in out               # excluded, no re-include


def test_discover_changes_include_without_exclude(tmp_path):
    """Re-include pass unions in extra files even with no base glob + no excludes."""
    _repo(tmp_path)
    wt = _wt(tmp_path, "wt-inc-no-exc")
    (wt / "src").mkdir()
    (wt / "src" / "main.py").write_text("x")
    (wt / "extra").mkdir()
    (wt / "extra" / "note.txt").write_text("note")
    out = discover_changes(
        str(wt), globs=["src/**"], includes=["extra/**"],
    )
    # Glob alone would exclude extra/; include re-adds it.
    assert "src/main.py" in out
    assert "extra/note.txt" in out


def test_reconcile_promotions_reinclude(tmp_path):
    _repo(tmp_path)
    wt = _wt(tmp_path, "wt-rec")
    (wt / "log" / "phases").mkdir(parents=True)
    (wt / "log" / "phases" / "keep.txt").write_text("keep")
    (wt / "log" / "noise.txt").write_text("noise")
    plan = reconcile_promotions(
        [("n", str(wt), None)], str(tmp_path), "warn",
        excludes=["log/"], includes=["log/phases/**"],
    )
    # The re-included path is applied to base; the excluded sibling is not.
    assert (tmp_path / "log" / "phases" / "keep.txt").exists()
    assert not (tmp_path / "log" / "noise.txt").exists()
    # Assert plan structure matches established pattern
    assert "log/phases/keep.txt" in plan.allowed["n"]
    assert "log/noise.txt" not in plan.allowed["n"]
