"""Generic secret/config resolver.

Single resolution chain shared across backends — provider-agnostic.

Layers, highest precedence first:

  1. YAML settings field — when ``settings`` and ``settings_attr`` are
     supplied, the value of that attribute on the model wins. Used
     when a workflow YAML carries an inline value (or a templated
     reference) that should override anything in the environment.
  2. Process environment — ``os.environ[name]``. Set this directly
     in the parent shell, by ``uv run --env-file <path>``, by a
     systemd unit, etc.
  3. Project-local ``.env`` file — walked up from CWD; first match
     wins. Loaded once per process and cached. abe-froman does not
     read keys from any path outside the project tree.

Returns ``None`` if no layer has the value. Callers decide whether
that's a hard error (e.g. ``factory.py`` raises ``ValueError`` for
explicit backend selection) or a soft signal (e.g. auto-detect skips
to the next provider).
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any


def _find_dotenv(start: Path | None = None) -> Path | None:
    """Walk up from ``start`` (default: CWD) looking for the first
    ``.env``. Stops at the filesystem root."""
    cwd = (start or Path.cwd()).resolve()
    for d in [cwd, *cwd.parents]:
        p = d / ".env"
        if p.is_file():
            return p
    return None


@lru_cache(maxsize=1)
def _load_dotenv_once() -> dict[str, str]:
    """Read the project-local ``.env`` once per process. Doesn't
    mutate ``os.environ`` — keeps the precedence layers distinct.

    Tiny parser: ``KEY=value`` per line, ``#`` comments, blank lines
    skipped, optional surrounding single/double quotes stripped from
    the value. No interpolation, no exports — just enough for the
    common case. Authors who need richer .env semantics can pre-load
    via ``uv run --env-file`` or ``set -a; source .env; set +a``.
    """
    p = _find_dotenv()
    if p is None:
        return {}
    out: dict[str, str] = {}
    for raw in p.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip()
        if (v.startswith('"') and v.endswith('"')) or (
            v.startswith("'") and v.endswith("'")
        ):
            v = v[1:-1]
        if k:
            out[k] = v
    return out


def resolve_secret(
    name: str,
    *,
    settings: Any | None = None,
    settings_attr: str | None = None,
) -> str | None:
    """Resolve ``name`` via the standard layer chain. See module docstring.

    Calling shape:

        # plain env / .env lookup
        key = resolve_secret("ANTHROPIC_API_KEY")

        # with optional YAML override (when a workflow's
        # ``Settings`` model carries a matching field)
        key = resolve_secret(
            "ANTHROPIC_API_KEY",
            settings=cfg.settings,
            settings_attr="anthropic_api_key",
        )
    """
    if settings is not None and settings_attr:
        v = getattr(settings, settings_attr, None)
        if v:
            return str(v)
    v = os.environ.get(name)
    if v:
        return v
    return _load_dotenv_once().get(name)


def _reset_dotenv_cache() -> None:
    """Test helper — clears the per-process .env cache between cases."""
    _load_dotenv_once.cache_clear()
