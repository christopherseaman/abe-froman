"""URL resolution + remote fetch with security gates.

Stage 5b's `execute: { url, params }` schema needs deterministic URL
resolution at compile time so cycle detection and caching see canonical
URLs. This module provides:

- `resolve_url(url, base_url, workdir) -> str` — three rules in order:
  explicit protocol passthrough, absolute path → file://, relative
  resolves against base_url (else workdir).
- `fetch_url(resolved_url, settings, cache) -> bytes` — validates against
  the four security gates (allow_remote_urls, allowed_url_hosts,
  max_remote_fetch_bytes), consults the cache,
  applies url_headers with ${VAR} env expansion.
- `canonical(url) -> str` — lowercase host + reassembly via urlsplit so
  trailing-slash variance and case-different hosts compare equal.
- fetch caching: a plain per-compile ``dict[str, bytes]`` keyed by canonical resolved URL.

Layer rule: this module is langgraph-free (enforced by
tests/architecture/test_layers.py) so schema and compile can import it
freely without dragging LangGraph imports across layer boundaries.
"""

from __future__ import annotations

import fnmatch
import os
import re
import urllib.request
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit, urlunsplit


if TYPE_CHECKING:
    from sqrlly.schema.models import Settings


class RemoteURLBlockedError(ValueError):
    """A remote URL was rejected by one of the Settings gates."""


class RemoteURLFetchError(IOError):
    """A remote URL fetch failed (network error, status code, body too large)."""


def canonical(url: str) -> str:
    """Canonical form: lowercase host, no trailing-slash variance."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    # Reassemble with lowercase host; preserve port, path, query, fragment.
    netloc = host.lower()
    if parts.port is not None:
        netloc = f"{netloc}:{parts.port}"
    if parts.username:
        userinfo = parts.username
        if parts.password:
            userinfo = f"{userinfo}:{parts.password}"
        netloc = f"{userinfo}@{netloc}"
    return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))


def resolve_url(url: str, base_url: str | None, workdir: str) -> str:
    """Resolve a YAML `url:` value into a canonical absolute URL.

    Rules (in order):
      1. Explicit protocol — pass through unchanged (after canonicalization).
      2. Absolute path (starts with `/`) — wrap as ``file://`` from filesystem root.
      3. Relative path — extend ``base_url`` if set, else extend ``workdir``.
         A path-only ``base_url:`` (no scheme) is treated as a ``file://`` prefix
         so downstream scheme inspection works uniformly.

    The default behavior — bare relative path, no ``base_url`` set — produces
    ``file://<workdir>/<url>``. Authors can think of this as "base_url
    defaults to cwd."
    """
    if "://" in url:
        return canonical(url)

    if url.startswith("/"):
        return canonical(f"file://{url}")

    if base_url:
        # Promote a path-only base to file:// so urljoin produces a proper URL.
        normalized_base = base_url if "://" in base_url else f"file://{base_url}"
        return canonical(urljoin(normalized_base, url))

    abs_workdir = Path(workdir).resolve()
    return canonical(f"file://{abs_workdir}/{url}")


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_vars(value: str) -> str:
    """Expand ${VAR} from the process env, then the project-local
    ``.env``; raise on missing var.

    Matches the documented secret chain (env → ``.env``) so a token kept
    only in ``.env`` resolves in ``url_headers`` too. Missing-var is a
    configuration error, not an I/O error — surfaces as ValueError so
    callers don't catch it via IOError handlers meant for network
    failures.
    """
    from sqrlly.runtime.secrets import _load_dotenv_once

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name in os.environ:
            return os.environ[name]
        dotenv = _load_dotenv_once()
        if name in dotenv:
            return dotenv[name]
        raise ValueError(
            f"Header references env var ${{{name}}} but it is not set "
            f"(checked process env and .env)"
        )
    return _VAR_RE.sub(repl, value)


def _matches_allowlist(host: str, patterns: list[str]) -> bool:
    """Glob match host against allowlist patterns (fnmatch on host only)."""
    return any(fnmatch.fnmatch(host, pattern) for pattern in patterns)


def _select_headers(
    resolved_url: str, header_map: dict[str, dict[str, str]]
) -> dict[str, str]:
    """First-prefix-wins header lookup.

    ``header_map`` keys are URL *prefixes* (e.g. ``https://api.example.com/``),
    not hostnames — the first key that ``resolved_url`` starts with wins,
    so order entries most-specific-first in ``settings.url_headers``.
    """
    for prefix, headers in header_map.items():
        if resolved_url.startswith(prefix):
            return {k: _expand_vars(v) for k, v in headers.items()}
    return {}


def fetch_url(
    resolved_url: str, settings: Settings, cache: dict[str, bytes]
) -> bytes:
    """Fetch a remote URL body, gated by Settings + cached per compile.

    File URLs return the raw bytes, still subject to the
    max_remote_fetch_bytes size cap. Remote URLs go through:
      1. allow_remote_urls (master switch).
      2. allowed_url_hosts (glob host match if non-empty).
      4. max_remote_fetch_bytes (size cap).
      5. Cache lookup; on miss, urlopen with url_headers.

    Precondition: ``resolved_url`` is already in canonical form (from
    ``resolve_url``); callers don't need to re-canonicalize.
    """
    canon = resolved_url
    if canon in cache:
        return cache[canon]

    parts = urlsplit(canon)
    if parts.scheme == "file":
        path = Path(parts.path)
        body = path.read_bytes()
        max_bytes = settings.max_remote_fetch_bytes
        if len(body) > max_bytes:
            raise RemoteURLFetchError(
                f"File URL {canon!r} body exceeds "
                f"settings.max_remote_fetch_bytes={max_bytes}"
            )
        cache[canon] = body
        return body

    if not settings.allow_remote_urls:
        raise RemoteURLBlockedError(
            f"Remote URL {canon!r} blocked: settings.allow_remote_urls is False"
        )

    host = parts.hostname or ""
    if settings.allowed_url_hosts and not _matches_allowlist(
        host, settings.allowed_url_hosts
    ):
        raise RemoteURLBlockedError(
            f"Remote URL {canon!r} blocked: host {host!r} not in "
            f"settings.allowed_url_hosts={settings.allowed_url_hosts!r}"
        )

    # Remote script/binary EXECUTION is refused downstream at dispatch
    # (script and binary dispatch require file://); only prompt bodies
    # are ever fetched remotely, so no per-extension gate is needed here.
    headers = _select_headers(canon, settings.url_headers)
    request = urllib.request.Request(canon, headers=headers)

    try:
        with urllib.request.urlopen(request) as resp:
            max_bytes = settings.max_remote_fetch_bytes
            body = resp.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise RemoteURLFetchError(
                    f"Remote URL {canon!r} body exceeds "
                    f"settings.max_remote_fetch_bytes={max_bytes}"
                )
    except RemoteURLFetchError:
        raise
    except Exception as e:
        raise RemoteURLFetchError(f"Remote URL {canon!r} fetch failed: {e}") from e

    cache[canon] = body
    return body
