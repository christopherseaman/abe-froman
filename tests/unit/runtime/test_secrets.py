"""Tests for `runtime/secrets.py::resolve_secret`.

Covers the layered resolution chain: YAML settings field > process env
> project-local `.env` file. sqrlly never reads from machine-global
keystores — `.env` discovery walks up from CWD only.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from sqrlly.runtime.secrets import _reset_dotenv_cache, resolve_secret


@dataclass
class _FakeSettings:
    """Stand-in for a real Pydantic Settings model — we only need
    attribute access for the YAML-layer test."""

    anthropic_api_key: str | None = None


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch, tmp_path):
    """Each test starts with a fresh dotenv cache and a CWD that
    doesn't have a real .env (tests that want one populate it
    themselves)."""
    monkeypatch.chdir(tmp_path)
    _reset_dotenv_cache()


class TestEnvLayer:
    def test_env_var_resolves(self, monkeypatch):
        monkeypatch.setenv("MY_KEY", "from-env")
        assert resolve_secret("MY_KEY") == "from-env"

    def test_unset_returns_none(self, monkeypatch):
        monkeypatch.delenv("MY_KEY", raising=False)
        assert resolve_secret("MY_KEY") is None

    def test_empty_env_value_treated_as_unset(self, monkeypatch):
        """An empty string in os.environ is the python-dotenv
        convention for "explicitly empty" — but for secrets, an empty
        string is no key. Resolver returns None."""
        monkeypatch.setenv("MY_KEY", "")
        assert resolve_secret("MY_KEY") is None


class TestDotenvLayer:
    def test_dotenv_in_cwd_loads(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MY_KEY", raising=False)
        (tmp_path / ".env").write_text("MY_KEY=from-dotenv\n")
        _reset_dotenv_cache()
        assert resolve_secret("MY_KEY") == "from-dotenv"

    def test_dotenv_walks_up_to_parent(self, tmp_path, monkeypatch):
        """`.env` discovery walks up from CWD; placing it in a parent
        of the running directory still resolves."""
        monkeypatch.delenv("MY_KEY", raising=False)
        (tmp_path / ".env").write_text("MY_KEY=from-parent\n")
        sub = tmp_path / "nested" / "deeper"
        sub.mkdir(parents=True)
        monkeypatch.chdir(sub)
        _reset_dotenv_cache()
        assert resolve_secret("MY_KEY") == "from-parent"

    def test_dotenv_strips_quotes(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MY_KEY", raising=False)
        (tmp_path / ".env").write_text(
            'DOUBLE="from-dotenv"\n'
            "SINGLE='also-from-dotenv'\n"
            "BARE=plain-from-dotenv\n"
        )
        _reset_dotenv_cache()
        assert resolve_secret("DOUBLE") == "from-dotenv"
        assert resolve_secret("SINGLE") == "also-from-dotenv"
        assert resolve_secret("BARE") == "plain-from-dotenv"

    def test_dotenv_skips_comments_and_blanks(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MY_KEY", raising=False)
        (tmp_path / ".env").write_text(
            "# leading comment\n"
            "\n"
            "MY_KEY=value\n"
            "  # indented comment ignored\n"
            "EMPTY_LINE_BELOW=ok\n"
            "\n"
        )
        _reset_dotenv_cache()
        assert resolve_secret("MY_KEY") == "value"
        assert resolve_secret("EMPTY_LINE_BELOW") == "ok"


class TestPrecedence:
    def test_env_wins_over_dotenv(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_KEY", "from-env")
        (tmp_path / ".env").write_text("MY_KEY=from-dotenv\n")
        _reset_dotenv_cache()
        assert resolve_secret("MY_KEY") == "from-env"

    def test_yaml_wins_over_env(self, tmp_path, monkeypatch):
        monkeypatch.setenv("MY_KEY", "from-env")
        s = _FakeSettings(anthropic_api_key="from-yaml")
        # The YAML layer takes precedence — workflow author's explicit
        # intent overrides ambient process state.
        assert resolve_secret(
            "MY_KEY",
            settings=s,
            settings_attr="anthropic_api_key",
        ) == "from-yaml"

    def test_yaml_wins_over_dotenv(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MY_KEY", raising=False)
        (tmp_path / ".env").write_text("MY_KEY=from-dotenv\n")
        _reset_dotenv_cache()
        s = _FakeSettings(anthropic_api_key="from-yaml")
        assert resolve_secret(
            "MY_KEY",
            settings=s,
            settings_attr="anthropic_api_key",
        ) == "from-yaml"

    def test_yaml_field_unset_falls_through_to_env(
        self, tmp_path, monkeypatch,
    ):
        """A settings model where the attr is None falls through to
        the env layer. Lets authors leave the YAML field blank when
        they don't want to pin a value."""
        monkeypatch.setenv("MY_KEY", "from-env")
        s = _FakeSettings(anthropic_api_key=None)
        assert resolve_secret(
            "MY_KEY",
            settings=s,
            settings_attr="anthropic_api_key",
        ) == "from-env"


class TestNoMachineGlobalAccess:
    """Regression: the resolver MUST NOT reach into ~/.pi or any other
    path outside the project tree."""

    def test_does_not_read_home_pi_agent(self, tmp_path, monkeypatch):
        monkeypatch.delenv("MY_KEY", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        # Plant a file at the legacy path — the resolver must IGNORE it.
        legacy = tmp_path / ".pi" / "agent" / "auth.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text('{"anthropic": {"key": "from-legacy"}}')
        _reset_dotenv_cache()
        assert resolve_secret("ANTHROPIC_API_KEY") is None
