"""v1 worktree-control schema.

Pins the inherited isolation field (`auto`/`isolated`/`off`) and the
per-node override accessor that mirrors `Node.effective_timeout`. Named
shared-worktree groups are a fast-follow (v2); in v1 a non-reserved token
is rejected so a group name can't silently no-op.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from sqrlly.schema.models import Node, Settings


def test_settings_worktree_defaults_to_auto():
    assert Settings().worktree == "auto"


def test_node_worktree_defaults_to_none():
    assert Node(id="a", name="a").worktree is None


@pytest.mark.parametrize(
    "value,normalized",
    [("auto", "auto"), ("isolated", "isolated"), ("off", "off"), ("none", "off")],
)
def test_settings_worktree_accepts_reserved_and_normalizes_none(value, normalized):
    assert Settings(worktree=value).worktree == normalized


def test_settings_worktree_rejects_group_token_in_v1():
    with pytest.raises(ValidationError) as exc:
        Settings(worktree="team-a")
    msg = str(exc.value)
    assert "auto" in msg and "isolated" in msg and "off" in msg


def test_node_worktree_override_wins_over_settings():
    s = Settings(worktree="isolated")
    n = Node(id="a", name="a", worktree="off")
    assert n.effective_worktree(s) == "off"


def test_node_worktree_inherits_settings_when_unset():
    s = Settings(worktree="off")
    n = Node(id="a", name="a")
    assert n.effective_worktree(s) == "off"


def test_node_effective_worktree_default_is_auto():
    assert Node(id="a", name="a").effective_worktree(Settings()) == "auto"


def test_node_worktree_none_alias_normalizes_to_off():
    n = Node(id="a", name="a", worktree="none")
    assert n.worktree == "off"
    assert n.effective_worktree(Settings()) == "off"


@pytest.mark.parametrize("yaml_bool,expected", [(False, "off"), (True, "isolated")])
def test_worktree_coerces_yaml_booleans(yaml_bool, expected):
    """Bare `worktree: off`/`on` parse as YAML booleans (the Norway problem).
    `off`/`no`/`false` → False → "off"; `on`/`yes`/`true` → True → "isolated"."""
    assert Settings(worktree=yaml_bool).worktree == expected
    assert Node(id="a", name="a", worktree=yaml_bool).worktree == expected


def test_settings_worktree_group_defaults_none():
    assert Settings().worktree_group is None


def test_node_worktree_group_defaults_none():
    assert Node(id="a", name="a").worktree_group is None


def test_settings_group_alone_is_valid():
    s = Settings(worktree_group="prd")
    assert s.worktree_group == "prd"
    assert s.worktree == "auto"  # neutral default, no conflict


def test_node_group_with_auto_is_valid():
    n = Node(id="a", name="a", worktree="auto", worktree_group="team-a")
    assert n.worktree_group == "team-a"
    assert n.worktree == "auto"


@pytest.mark.parametrize("mode", ["isolated", "off"])
def test_group_with_explicit_mode_is_rejected(mode):
    with pytest.raises(ValidationError) as exc:
        Node(id="a", name="a", worktree=mode, worktree_group="team-a")
    assert "worktree_group" in str(exc.value)


@pytest.mark.parametrize("mode", ["isolated", "off"])
def test_settings_group_with_explicit_mode_is_rejected(mode):
    with pytest.raises(ValidationError) as exc:
        Settings(worktree=mode, worktree_group="grp")
    assert "worktree_group" in str(exc.value)
