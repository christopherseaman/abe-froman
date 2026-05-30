"""ACP tool-permission policy — pure, no `acp` package import.

Lives apart from `acp.py` (which imports the optional `acp` extra at
module top) so the gate predicate is unit-testable in the cli-only
suite. ACP gates by tool *kind* (read/edit/execute/…), not claude tool
names, so `permission_mode` maps to a kind policy and the claude-name
lists are matched best-effort against the tool's kind and title.
"""
from __future__ import annotations

# permission_mode → allowed ACP tool kinds. `bypassPermissions` allows
# everything (handled before this table); `acceptEdits` allows file
# edits + reads but not execution; `default`/`plan` are read-only.
_MODE_ALLOWED_KINDS: dict[str, set[str]] = {
    "acceptEdits": {
        "read", "edit", "delete", "move", "search", "fetch", "think",
        "switch_mode", "other",
    },
    "default": {"read", "search", "fetch", "think"},
    "plan": {"read", "search", "fetch", "think"},
}


def acp_tool_allowed(
    kind: str | None,
    title: str | None,
    *,
    permission_mode: str | None,
    allowed_tools: list[str] | None,
    disallowed_tools: list[str] | None,
) -> bool:
    """Whether an ACP tool call is permitted under the preset's policy.

    With nothing configured this returns True — preserving the historical
    allow-all default. ``disallowed_tools`` denies, then ``permission_mode``
    gates by kind, then ``allowed_tools`` (if set) requires a match. The
    claude-name lists match best-effort: an entry matches if it equals the
    tool ``kind`` or appears (case-insensitive) in the ``title``.
    """
    if permission_mode == "bypassPermissions":
        return True
    k = (kind or "").lower()
    t = (title or "").lower()

    def _matches(entries: list[str]) -> bool:
        return any(e.lower() == k or e.lower() in t for e in entries)

    if disallowed_tools and _matches(disallowed_tools):
        return False
    if (
        permission_mode in _MODE_ALLOWED_KINDS
        and k not in _MODE_ALLOWED_KINDS[permission_mode]
    ):
        return False
    if allowed_tools and not _matches(allowed_tools):
        return False
    return True
