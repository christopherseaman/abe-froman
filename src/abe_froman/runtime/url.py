"""URL resolution + remote fetch with security gates.

Stage 5b's `execute: { url, params }` schema needs deterministic URL
resolution at compile time so cycle detection and caching see canonical
URLs. This module provides:

- `resolve_url(url, base_url, workdir) -> str` — three rules in order:
  explicit protocol passthrough, absolute path → file://, relative
  resolves against base_url (else workdir).
- `fetch_url(resolved_url, settings, cache) -> bytes` — validates against
  the four security gates (allow_remote_urls, allowed_url_hosts,
  allow_remote_scripts, max_remote_fetch_bytes), consults the cache,
  applies url_headers with ${VAR} env expansion.
- `canonical(url) -> str` — lowercase host + reassembly via urlsplit so
  trailing-slash variance and case-different hosts compare equal.
- `_RemoteFetchCache` — per-compile dict keyed by canonical resolved URL.

Layer rule: this module is langgraph-free (enforced by
tests/architecture/test_layers.py) so schema and compile can import it
freely without dragging LangGraph imports across layer boundaries.
"""

from __future__ import annotations

import fnmatch
import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit, urlunsplit

if TYPE_CHECKING:
    from abe_froman.schema.models import Settings


_SCRIPT_EXTS = {".py", ".js", ".mjs", ".ts", ".sh"}


class RemoteURLBlockedError(ValueError):
    """A remote URL was rejected by one of the Settings gates."""


class RemoteURLFetchError(IOError):
    """A remote URL fetch failed (network error, status code, body too large)."""


@dataclass
class _RemoteFetchCache:
    """Per-compile cache of fetched remote URL bodies.

    Keyed by canonical resolved URL. Lifetime is one compile invocation;
    persistent caching is deferred (would need ETag / cache-control).
    """
    bodies: dict[str, bytes] = field(default_factory=dict)
    fetch_count: int = 0  # observable for tests; not load-bearing


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
      2. Absolute path (starts with /) — wrap as file://.
      3. Relative path — urljoin against base_url; else file:// + workdir.
    """
    if "://" in url:
        return canonical(url)

    if url.startswith("/"):
        return canonical(f"file://{url}")

    if base_url:
        return canonical(urljoin(base_url, url))

    abs_workdir = Path(workdir).resolve()
    return canonical(f"file://{abs_workdir}/{url}")


_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_vars(value: str) -> str:
    """Expand ${VAR} from process env; raise on missing var."""
    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in os.environ:
            raise RemoteURLFetchError(
                f"Header references env var ${{{name}}} but it is not set"
            )
        return os.environ[name]
    return _VAR_RE.sub(repl, value)


def _matches_allowlist(host: str, patterns: list[str]) -> bool:
    """Glob match host against allowlist patterns (fnmatch on host only)."""
    return any(fnmatch.fnmatch(host, pattern) for pattern in patterns)


def _select_headers(
    resolved_url: str, header_map: dict[str, dict[str, str]]
) -> dict[str, str]:
    """First-prefix-wins header lookup."""
    for prefix, headers in header_map.items():
        if resolved_url.startswith(prefix):
            return {k: _expand_vars(v) for k, v in headers.items()}
    return {}


def fetch_url(
    resolved_url: str, settings: Settings, cache: _RemoteFetchCache
) -> bytes:
    """Fetch a remote URL body, gated by Settings + cached per compile.

    File URLs return the raw bytes. Remote URLs go through:
      1. allow_remote_urls (master switch).
      2. allowed_url_hosts (glob host match if non-empty).
      3. allow_remote_scripts (extra opt-in for .py/.js/.sh/etc).
      4. max_remote_fetch_bytes (size cap).
      5. Cache lookup; on miss, urlopen with url_headers.
    """
    canon = canonical(resolved_url)
    if canon in cache.bodies:
        return cache.bodies[canon]

    parts = urlsplit(canon)
    if parts.scheme == "file":
        path = Path(parts.path)
        body = path.read_bytes()
        cache.bodies[canon] = body
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

    ext = Path(parts.path).suffix.lower()
    if ext in _SCRIPT_EXTS and not settings.allow_remote_scripts:
        raise RemoteURLBlockedError(
            f"Remote script {canon!r} blocked: settings.allow_remote_scripts "
            f"is False (extension {ext!r} requires extra opt-in)"
        )

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

    cache.bodies[canon] = body
    cache.fetch_count += 1
    return body
