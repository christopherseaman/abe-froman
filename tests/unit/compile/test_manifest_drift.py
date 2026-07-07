"""Pure helpers for the fan-out `--resume` manifest-drift guard (Phase 1).

`direct_child_ids` extracts a fan-out parent's DIRECT branch ids
(`<parent>::<item>`) from a node-id set, excluding deeper subgraph-inner
ids (`<parent>::<item>::<inner>`) — critical so the drift guard never
false-fires on the working subgraph-template shape, whose branches record
both their own id AND their inner nodes' ids.

`manifest_drift` returns the prior branch ids NOT covered by the freshly
read manifest (`prior - new`). It is empty for a stable-id resume OR a
purely additive re-fan (new is a superset), so those never trip the guard.
"""
import pytest

from sqrlly.compile._manifest import direct_child_ids, manifest_drift


class TestDirectChildIds:
    def test_extracts_only_direct_branches(self):
        node_ids = {
            "cfan",                # bare parent — excluded
            "cfan::alpha",         # direct branch — kept
            "cfan::beta",          # direct branch — kept
            "cfan::alpha::step1",  # subgraph-inner — excluded
            "cfan::beta::step2",   # subgraph-inner — excluded
            "other::x",            # different parent — excluded
            "up",                  # unrelated — excluded
        }
        assert direct_child_ids("cfan", node_ids) == {"cfan::alpha", "cfan::beta"}

    def test_no_children_returns_empty(self):
        assert direct_child_ids("cfan", {"up", "other::x", "cfan"}) == set()

    def test_prefix_is_not_a_substring_match(self):
        # 'cfanx::a' must NOT count as a child of 'cfan'.
        assert direct_child_ids("cfan", {"cfanx::a", "cfan::a"}) == {"cfan::a"}


class TestManifestDrift:
    @pytest.mark.parametrize(
        "prior,new,expected",
        [
            ({"cfan::a", "cfan::b", "cfan::c"},
             {"cfan::a", "cfan::b", "cfan::c"}, set()),          # stable → no drift
            ({"cfan::a", "cfan::b"},
             {"cfan::a", "cfan::b", "cfan::c"}, set()),          # additive superset → no drift
            ({"cfan::r1a", "cfan::r1b", "cfan::r1c"},
             {"cfan::r2a", "cfan::r2b", "cfan::r2c"},
             {"cfan::r1a", "cfan::r1b", "cfan::r1c"}),           # full drift → all prior
            ({"cfan::a", "cfan::b", "cfan::c"},
             {"cfan::a", "cfan::c"}, {"cfan::b"}),               # one dropped
        ],
    )
    def test_drift_is_prior_minus_new(self, prior, new, expected):
        assert manifest_drift(prior, new) == expected
