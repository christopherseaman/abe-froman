"""Scope-aware settings inheritance for subgraphs.

Today's runtime hands a single ``Settings`` instance — the outermost
``Graph.settings`` — to the executor. A subgraph YAML can author its
own ``settings:`` block (it parses cleanly, the schema is identical),
but those values are silently ignored.

This module fixes the silent-loss by giving each scope a *merged*
``Settings`` view: child fields the YAML explicitly sets win for that
subgraph; everything else inherits from the parent. Subgraph wrappers
compute the merge at compile time and thread it via
``NodeExecutor.execute(..., settings_override=merged)``.

Resolution order, lowest to highest:
  1. ``Settings()`` defaults
  2. Outermost ``Graph.settings``
  3. Subgraph ``Graph.settings`` (one per nested level)
  4. Per-node ``execute.params.model`` / ``Node.timeout`` (already
     resolved in the dispatcher; not handled here)

This file is langgraph-free by design — schema and compile both
import it, so it must respect the runtime layer rule.
"""
from __future__ import annotations

from sqrlly.schema.models import Settings


def merge_settings(parent: Settings, child: Settings) -> Settings:
    """Inherit from ``parent``; ``child``'s explicitly-set fields win.

    Uses Pydantic v2's ``model_fields_set`` to distinguish authored
    values from defaults. A subgraph YAML that doesn't mention
    ``default_model`` inherits the parent's; one that says
    ``default_model: opus`` wins for that scope only.

    NOTE: ``model_fields_set`` on the *child* is what drives the
    merge — so ``child`` must be a freshly-parsed ``Settings`` (the
    one Pydantic produced from YAML). The *returned* Settings has
    every field marked as set (round-trip side effect); do not feed
    it back as ``child`` to a subsequent ``merge_settings`` call, or
    every field will appear authored and shadow the parent's.
    Subgraph callers always have a fresh child from YAML parse, so
    the constraint is naturally respected.
    """
    merged = parent.model_dump()
    set_fields = child.model_fields_set
    for field in set_fields:
        merged[field] = getattr(child, field)
    # worktree / worktree_group are a mutually-exclusive pair: if the child
    # authored either, it speaks for this scope — drop the inherited sibling
    # so resolution stays pure scope-specificity (and the exclusion validator
    # doesn't trip on a parent-group + child-mode combination).
    if "worktree" in set_fields and "worktree_group" not in set_fields:
        merged["worktree_group"] = None
    if "worktree_group" in set_fields and "worktree" not in set_fields:
        merged["worktree"] = "auto"
    return Settings.model_validate(merged)
