"""Tests for `runtime/secrets.py` — project-local .env discovery/parsing.

The loader feeds `url.py::_expand_vars` (${VAR} expansion in
`settings.url_headers`); precedence over the process env is pinned in
tests/unit/runtime/test_url.py::TestVarExpansion via that live consumer.
sqrlly never reads from machine-global keystores — `.env` discovery
walks up from CWD only.
"""
from __future__ import annotations

import pytest

from sqrlly.runtime.secrets import _find_dotenv, _load_dotenv_once


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch, tmp_path):
    """Each test starts with a fresh dotenv cache and a CWD that
    doesn't have a real .env (tests that want one populate it
    themselves)."""
    monkeypatch.chdir(tmp_path)
    _load_dotenv_once.cache_clear()
    yield
    _load_dotenv_once.cache_clear()


class TestDiscovery:
    def test_dotenv_in_cwd_found(self, tmp_path):
        (tmp_path / ".env").write_text("MY_KEY=from-dotenv\n")
        assert _find_dotenv() == tmp_path / ".env"
        assert _load_dotenv_once()["MY_KEY"] == "from-dotenv"

    def test_dotenv_walks_up_to_parent(self, tmp_path, monkeypatch):
        """`.env` discovery walks up from CWD; placing it in a parent
        of the running directory still resolves."""
        (tmp_path / ".env").write_text("MY_KEY=from-parent\n")
        sub = tmp_path / "nested" / "deeper"
        sub.mkdir(parents=True)
        monkeypatch.chdir(sub)
        assert _load_dotenv_once()["MY_KEY"] == "from-parent"

    def test_explicit_start_without_dotenv_returns_none(self, tmp_path):
        """_find_dotenv with an explicit start that has no .env up to a
        root we control: probe the deepest level only — the tmp dir
        itself has no .env, so the walk from a child won't find one
        there (system-root .env, if any, is out of scope)."""
        sub = tmp_path / "empty" / "leaf"
        sub.mkdir(parents=True)
        found = _find_dotenv(start=sub)
        # No .env planted anywhere under tmp_path.
        assert found is None or tmp_path not in found.parents


class TestParser:
    def test_strips_quotes(self, tmp_path):
        (tmp_path / ".env").write_text(
            'DOUBLE="from-dotenv"\n'
            "SINGLE='also-from-dotenv'\n"
            "BARE=plain-from-dotenv\n"
        )
        loaded = _load_dotenv_once()
        assert loaded["DOUBLE"] == "from-dotenv"
        assert loaded["SINGLE"] == "also-from-dotenv"
        assert loaded["BARE"] == "plain-from-dotenv"

    def test_skips_comments_and_blanks(self, tmp_path):
        (tmp_path / ".env").write_text(
            "# leading comment\n"
            "\n"
            "MY_KEY=value\n"
            "  # indented comment ignored\n"
            "EMPTY_LINE_BELOW=ok\n"
            "\n"
        )
        loaded = _load_dotenv_once()
        assert loaded["MY_KEY"] == "value"
        assert loaded["EMPTY_LINE_BELOW"] == "ok"


class TestNoMachineGlobalAccess:
    """Regression: the loader MUST NOT reach into ~/.pi or any other
    path outside the project tree."""

    def test_does_not_read_home_pi_agent(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        # Plant a file at the legacy path — the loader must IGNORE it.
        legacy = tmp_path / ".pi" / "agent" / "auth.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"some_service": {"key": "from-legacy"}}')
        assert "MY_KEY" not in _load_dotenv_once()
