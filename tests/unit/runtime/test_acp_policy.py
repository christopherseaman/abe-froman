"""Unit tests for the ACP tool-permission gate predicate.

Pure logic — imports `_acp_policy` directly (no `acp` package), so this
runs in the cli-only suite. Pins the kind-gate semantics that
`request_permission` enforces on the acp transport.
"""
from __future__ import annotations

import pytest

from sqrlly.runtime.executor.backends._acp_policy import acp_tool_allowed


def _allow(kind, title="", *, mode=None, allow=None, deny=None) -> bool:
    return acp_tool_allowed(
        kind, title,
        permission_mode=mode, allowed_tools=allow, disallowed_tools=deny,
    )


class TestACPToolPolicy:
    def test_nothing_set_allows_all(self):
        # Back-compat: the historical acp default is allow-all.
        assert _allow("execute", "Bash") is True
        assert _allow("edit", "Edit file") is True

    def test_bypass_allows_everything(self):
        assert _allow("execute", "Bash", mode="bypassPermissions") is True
        # bypass overrides even an explicit denylist.
        assert _allow("execute", "Bash", mode="bypassPermissions",
                      deny=["execute"]) is True

    @pytest.mark.parametrize("kind,allowed", [
        ("read", True), ("search", True), ("fetch", True), ("think", True),
        ("edit", True), ("move", True), ("delete", True),
        ("execute", False),   # acceptEdits gates execution
    ])
    def test_accept_edits_allows_edits_not_execute(self, kind, allowed):
        assert _allow(kind, mode="acceptEdits") is allowed

    @pytest.mark.parametrize("mode", ["default", "plan"])
    @pytest.mark.parametrize("kind,allowed", [
        ("read", True), ("search", True), ("fetch", True), ("think", True),
        ("edit", False), ("execute", False), ("delete", False), ("move", False),
    ])
    def test_default_and_plan_are_read_only(self, mode, kind, allowed):
        assert _allow(kind, mode=mode) is allowed

    def test_disallowed_denies_by_kind(self):
        assert _allow("execute", "Bash", deny=["execute"]) is False

    def test_disallowed_denies_by_title_substring(self):
        # claude-name entry matched best-effort against the title.
        assert _allow("execute", "Bash(rm -rf)", deny=["bash"]) is False

    def test_allowed_requires_match(self):
        assert _allow("read", "Read", allow=["read"]) is True
        assert _allow("execute", "Bash", allow=["read"]) is False

    def test_disallow_wins_over_allow(self):
        assert _allow("edit", "Edit", allow=["edit"], deny=["edit"]) is False

    def test_mode_and_allow_compose(self):
        # acceptEdits permits edit, but the allowlist narrows to read only.
        assert _allow("edit", "Edit", mode="acceptEdits", allow=["read"]) is False
        assert _allow("read", "Read", mode="acceptEdits", allow=["read"]) is True
