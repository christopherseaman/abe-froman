"""compute_skip_set: prior_completed minus the dirty closure."""
import pytest

from sqrlly.compile.resume import compute_skip_set
from sqrlly.schema.models import Graph


def _g(nodes):
    return Graph(name="T", version="1.0", nodes=nodes)


def _exec(id_, **kw):
    return {"id": id_, "name": id_, "execute": {"url": "t.md"}, **kw}


def test_linear_failure_dirties_downstream():
    # a -> b -> c ; b failed => b and its dependent c are dirty, a is skippable.
    g = _g([_exec("a"), _exec("b", depends_on=["a"]), _exec("c", depends_on=["b"])])
    assert compute_skip_set(g, {"a"}, {"b"}, set()) == {"a"}


def test_clean_run_skips_everything():
    g = _g([_exec("a"), _exec("b", depends_on=["a"])])
    assert compute_skip_set(g, {"a", "b"}, set(), set()) == {"a", "b"}


def test_resume_from_dirties_node_and_downstream():
    # a -> b -> c all clean; --resume-from b => b,c dirty, a skippable.
    g = _g([_exec("a"), _exec("b", depends_on=["a"]), _exec("c", depends_on=["b"])])
    assert compute_skip_set(g, {"a", "b", "c"}, set(), {"b"}) == {"a"}


def test_diamond_only_failure_branch_dirty():
    # a -> {b,c} -> d ; b failed => b,d dirty; a,c skippable.
    g = _g([
        _exec("a"),
        _exec("b", depends_on=["a"]),
        _exec("c", depends_on=["a"]),
        _exec("d", depends_on=["b", "c"]),
    ])
    assert compute_skip_set(g, {"a", "b", "c", "d"}, {"b"}, set()) == {"a", "c"}


def test_route_target_of_failure_is_dirty():
    # a routes to b via goto; a failed => its route target b is dirty even with
    # no depends_on edge (route targets have no static depends_on).
    g = _g([
        {"id": "a", "name": "a", "execute": {"url": "t.md"},
         "route": {"goto": "b"}},
        _exec("b"),
    ])
    assert compute_skip_set(g, {"a", "b"}, {"a"}, set()) == set()


def test_worktree_group_sibling_force_dirty():
    # a,b share a worktree_group; a dirty => b force-dirty (shared mutable tree).
    g = _g([
        _exec("a", worktree_group="team"),
        _exec("b", worktree_group="team"),
    ])
    assert compute_skip_set(g, {"a", "b"}, {"a"}, set()) == set()


def test_empty_inputs():
    g = _g([_exec("a")])
    assert compute_skip_set(g, set(), set(), set()) == set()


def test_settings_level_worktree_group_force_dirty():
    # Graph-level settings.worktree_group shares one tree even without
    # per-node groups; a dirty => b force-dirty (the false-skip guard).
    g = Graph(name="T", version="1.0",
              nodes=[_exec("a"), _exec("b")],
              settings={"worktree_group": "shared"})
    assert compute_skip_set(g, {"a", "b"}, {"a"}, set()) == set()


def test_group_sibling_dependent_is_transitively_dirty():
    # a,b share a group; b has dependent c. a dirty => b (group) => c (dep).
    g = _g([
        _exec("a", worktree_group="team"),
        _exec("b", worktree_group="team"),
        _exec("c", depends_on=["b"]),
    ])
    assert compute_skip_set(g, {"a", "b", "c"}, {"a"}, set()) == set()


def test_cyclic_route_terminates():
    # a <-> b goto loop; b failed. Closure must terminate via the visited-guard.
    g = _g([
        {"id": "a", "name": "a", "execute": {"url": "t.md"}, "route": {"goto": "b"}},
        {"id": "b", "name": "b", "execute": {"url": "t.md"}, "route": {"goto": "a"}},
    ])
    assert compute_skip_set(g, {"a", "b"}, {"b"}, set()) == set()
