"""Project-local ``.env`` discovery + parsing.

Consumed by ``runtime/url.py::_expand_vars`` for ``${VAR}`` expansion in
``settings.url_headers``: process environment first, then the first
``.env`` found walking up from CWD. sqrlly never reads keys from any
path outside the project tree.
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path


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
