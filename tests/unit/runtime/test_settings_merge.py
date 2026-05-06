"""Tests for ``runtime/settings_merge.merge_settings``.

The merge function is the foundation of Phase 3 scope-aware settings.
It must:
  - Inherit any field the child YAML didn't explicitly set.
  - Honor any field the child YAML did set, even if its value matches
    the schema default (author intent: "I declared this on purpose").
  - Compose across nested levels via repeated calls.

Pydantic's ``model_fields_set`` powers the "explicitly set?" decision.
These tests pin its behavior at the point of YAML parse, which is
where merge_settings is supposed to be called.
"""
from __future__ import annotations

from abe_froman.runtime.settings_merge import merge_settings
from abe_froman.schema.models import Settings


class TestSingleFieldOverride:
    def test_child_default_model_wins(self):
        parent = Settings(default_model="sonnet")
        child = Settings(default_model="opus")
        merged = merge_settings(parent, child)
        assert merged.default_model == "opus"

    def test_unset_child_inherits_parent(self):
        parent = Settings(default_model="sonnet", max_retries=5)
        child = Settings()  # nothing authored
        merged = merge_settings(parent, child)
        assert merged.default_model == "sonnet"
        assert merged.max_retries == 5

    def test_partial_child_keeps_parent_for_other_fields(self):
        parent = Settings(default_model="sonnet", default_timeout=300.0)
        child = Settings(default_model="haiku")
        merged = merge_settings(parent, child)
        assert merged.default_model == "haiku"
        assert merged.default_timeout == 300.0


class TestExplicitDefaultStillWins:
    """A subgraph that *explicitly* sets a field to the schema default
    must still beat parent — author intent, not absence."""

    def test_child_explicitly_resets_to_default(self):
        parent = Settings(max_retries=5)
        child = Settings(max_retries=3)  # 3 == schema default
        merged = merge_settings(parent, child)
        assert merged.max_retries == 3

    def test_child_explicitly_overrides_executor(self):
        parent = Settings(executor="acp")
        child = Settings(executor="anthropic")
        merged = merge_settings(parent, child)
        assert merged.executor == "anthropic"


class TestComposesNested:
    """Real workflows have parent → subgraph → sub-subgraph chains.
    merge_settings must compose left-to-right; the right-most explicit
    set wins per-field."""

    def test_three_level_inheritance(self):
        top = Settings(default_model="opus", max_retries=2, default_timeout=60.0)
        mid = Settings(default_model="sonnet")
        bot = Settings(max_retries=10)

        # Each layer merges with the result of the previous merge.
        l1 = merge_settings(top, mid)
        l2 = merge_settings(l1, bot)

        assert l2.default_model == "sonnet"   # mid's win held through l2
        assert l2.max_retries == 10           # bot wins
        assert l2.default_timeout == 60.0     # top's flowed through


class TestMultiFieldCarriage:
    def test_url_security_fields_compose_correctly(self):
        parent = Settings(
            allow_remote_urls=True,
            allowed_url_hosts=["*.parent.com"],
            url_headers={"https://parent.com/": {"X": "p"}},
        )
        child = Settings(allowed_url_hosts=["*.child.com"])
        merged = merge_settings(parent, child)
        assert merged.allow_remote_urls is True   # inherited
        assert merged.allowed_url_hosts == ["*.child.com"]  # child wins
        assert merged.url_headers == {"https://parent.com/": {"X": "p"}}  # inherited

    def test_concurrency_fields_compose_correctly(self):
        parent = Settings(max_parallel_jobs=8, per_model_limits={"opus": 2})
        child = Settings(per_model_limits={"sonnet": 4})
        merged = merge_settings(parent, child)
        assert merged.max_parallel_jobs == 8
        # Child REPLACES (not augments) since model_fields_set marks the
        # whole dict as authored. Document this — authors who want both
        # caps must restate the parent's keys in the subgraph YAML.
        assert merged.per_model_limits == {"sonnet": 4}


class TestRoundTripConstraint:
    """``model_fields_set`` is preserved by ``Settings(**kwargs)`` and
    ``model_validate(dict)``, but NOT by ``model_dump() →
    model_validate(...)``. The merge function relies on the freshly-
    validated case (the YAML-parse case). Pin that behavior so future
    refactors don't accidentally use a round-trip path."""

    def test_fields_set_preserved_through_kwargs(self):
        s = Settings(default_model="opus")
        assert "default_model" in s.model_fields_set
        # Reconstruction via kwargs preserves the set.
        s2 = Settings(**s.model_dump(exclude_unset=True))
        assert "default_model" in s2.model_fields_set

    def test_fields_set_lost_after_full_dump_round_trip(self):
        """Documents the gotcha — full dump round-trip loses authorship.
        merge_settings must NOT be passed a Settings created this way."""
        s = Settings(default_model="opus")
        s_dumped = Settings(**s.model_dump())
        # All fields show up as "set" because we passed them all explicitly.
        # That's precisely the wrong shape for merge_settings — every
        # field would win over parent. Hence the function's docstring
        # warning.
        assert len(s_dumped.model_fields_set) > 1
