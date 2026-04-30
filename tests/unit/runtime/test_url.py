"""Unit tests for runtime/url.py: resolution + remote fetch gates.

Function-level tests cover:
    - resolve_url: each row of the design doc's examples table
    - fetch_url: each of the four remote-URL gates raises distinctly
    - cache: same URL fetched twice → 1 actual network call
    - ${VAR} expansion in url_headers honors process env
"""

from __future__ import annotations

import http.server
import os
import threading
from pathlib import Path

import pytest

from abe_froman.runtime.url import (
    RemoteURLBlockedError,
    RemoteURLFetchError,
    _RemoteFetchCache,
    canonical,
    fetch_url,
    resolve_url,
)
from abe_froman.schema.models import Settings


# ----- resolve_url: pin each row of the examples table -----

class TestResolveURL:
    def test_relative_path_resolves_against_workdir(self):
        result = resolve_url("prompts/x.md", base_url=None, workdir="/home/me/proj")
        assert result == "file:///home/me/proj/prompts/x.md"

    def test_relative_path_resolves_against_file_base_url(self):
        result = resolve_url(
            "prompts/x.md",
            base_url="file:///home/me/proj/examples/foo/",
            workdir="/home/me/proj",
        )
        assert result == "file:///home/me/proj/examples/foo/prompts/x.md"

    def test_relative_path_resolves_against_https_base_url(self):
        result = resolve_url(
            "prompts/x.md",
            base_url="https://prompts.example.com/v1/",
            workdir="/home/me/proj",
        )
        assert result == "https://prompts.example.com/v1/prompts/x.md"

    def test_absolute_path_wraps_as_file(self):
        result = resolve_url("/etc/scripts/run.sh", base_url=None, workdir="/anywhere")
        assert result == "file:///etc/scripts/run.sh"

    def test_absolute_path_ignores_base_url(self):
        result = resolve_url(
            "/etc/scripts/run.sh",
            base_url="https://x.com/v1/",
            workdir="/anywhere",
        )
        assert result == "file:///etc/scripts/run.sh"

    def test_explicit_https_passes_through(self):
        result = resolve_url(
            "https://x.com/y.yaml",
            base_url="https://other.com/v1/",
            workdir="/anywhere",
        )
        assert result == "https://x.com/y.yaml"

    def test_explicit_file_passes_through(self):
        result = resolve_url("file:///abs/x.md", base_url=None, workdir="/anywhere")
        assert result == "file:///abs/x.md"


# ----- canonical: trailing-slash + case normalization -----

class TestCanonical:
    def test_lowercase_host(self):
        assert canonical("https://Example.COM/path") == "https://example.com/path"

    def test_preserves_path_query_fragment(self):
        url = "https://x.com/a?q=1#frag"
        assert canonical(url) == url


# ----- fetch_url: file:// path -----

class TestFetchFileURL:
    def test_reads_local_file(self, tmp_path):
        path = tmp_path / "local.md"
        path.write_text("hello world")
        cache = _RemoteFetchCache()
        body = fetch_url(f"file://{path}", Settings(), cache)
        assert body == b"hello world"

    def test_caches_local_reads(self, tmp_path):
        path = tmp_path / "local.md"
        path.write_text("first")
        cache = _RemoteFetchCache()
        fetch_url(f"file://{path}", Settings(), cache)
        # Mutate file under a hot cache; cache should still return original.
        path.write_text("second")
        body = fetch_url(f"file://{path}", Settings(), cache)
        assert body == b"first"


# ----- fetch_url: remote URL gates -----

class TestRemoteURLGates:
    def test_blocks_when_allow_remote_urls_false(self):
        cache = _RemoteFetchCache()
        with pytest.raises(RemoteURLBlockedError) as ei:
            fetch_url("https://x.com/a.md", Settings(), cache)
        assert "allow_remote_urls" in str(ei.value)

    def test_blocks_when_host_not_in_allowlist(self):
        cache = _RemoteFetchCache()
        settings = Settings(
            allow_remote_urls=True,
            allowed_url_hosts=["*.internal.example.com"],
        )
        with pytest.raises(RemoteURLBlockedError) as ei:
            fetch_url("https://attacker.com/a.md", settings, cache)
        assert "allowed_url_hosts" in str(ei.value)

    def test_allows_when_host_matches_glob(self):
        # Host matches but no server is running — should reach fetch attempt.
        cache = _RemoteFetchCache()
        settings = Settings(
            allow_remote_urls=True,
            allowed_url_hosts=["*.example.com"],
        )
        with pytest.raises(RemoteURLFetchError):
            fetch_url("https://api.example.com/x.md", settings, cache)

    def test_blocks_remote_script_without_extra_opt_in(self):
        cache = _RemoteFetchCache()
        settings = Settings(allow_remote_urls=True)
        with pytest.raises(RemoteURLBlockedError) as ei:
            fetch_url("https://x.com/run.py", settings, cache)
        assert "allow_remote_scripts" in str(ei.value)

    def test_allows_remote_script_with_extra_opt_in(self):
        cache = _RemoteFetchCache()
        settings = Settings(
            allow_remote_urls=True,
            allow_remote_scripts=True,
        )
        # .invalid is RFC-reserved as never-resolving; fetch fails downstream
        # of the gate, proving the gate passed.
        with pytest.raises(RemoteURLFetchError):
            fetch_url("https://nope.invalid/run.py", settings, cache)


# ----- ${VAR} expansion -----

class TestVarExpansion:
    def test_missing_var_raises_clear_error(self, monkeypatch):
        monkeypatch.delenv("ABSENT_TOKEN", raising=False)
        cache = _RemoteFetchCache()
        settings = Settings(
            allow_remote_urls=True,
            url_headers={
                "https://nope.invalid/": {"Authorization": "Bearer ${ABSENT_TOKEN}"}
            },
        )
        with pytest.raises(RemoteURLFetchError) as ei:
            fetch_url("https://nope.invalid/a.md", settings, cache)
        assert "ABSENT_TOKEN" in str(ei.value)


# ----- live local server: cache hit, size cap -----

class _CountingHandler(http.server.BaseHTTPRequestHandler):
    """Tiny in-process HTTP server; counts hits per path, records headers."""
    hits: dict[str, int] = {}
    body_for_path: dict[str, bytes] = {}
    headers_seen: dict[str, dict[str, str]] = {}

    def do_GET(self) -> None:  # noqa: N802 (HTTP API name)
        _CountingHandler.hits[self.path] = _CountingHandler.hits.get(self.path, 0) + 1
        _CountingHandler.headers_seen[self.path] = dict(self.headers)
        body = _CountingHandler.body_for_path.get(self.path, b"default")
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs) -> None:
        return  # silence test output


@pytest.fixture
def local_server():
    """Spin up a counting HTTP server on a random port for one test."""
    _CountingHandler.hits = {}
    _CountingHandler.body_for_path = {}
    _CountingHandler.headers_seen = {}
    server = http.server.HTTPServer(("127.0.0.1", 0), _CountingHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield {"port": port, "handler": _CountingHandler, "host": "127.0.0.1"}
    finally:
        server.shutdown()
        thread.join(timeout=2)


class TestCacheAndSizeCap:
    def test_cache_avoids_double_fetch(self, local_server):
        path = "/cached.md"
        local_server["handler"].body_for_path[path] = b"once"
        url = f"http://{local_server['host']}:{local_server['port']}{path}"
        settings = Settings(allow_remote_urls=True)
        cache = _RemoteFetchCache()

        body1 = fetch_url(url, settings, cache)
        body2 = fetch_url(url, settings, cache)
        body3 = fetch_url(url, settings, cache)

        assert body1 == body2 == body3 == b"once"
        assert local_server["handler"].hits[path] == 1
        assert cache.fetch_count == 1

    def test_size_cap_rejects_oversize_body(self, local_server):
        path = "/big.md"
        local_server["handler"].body_for_path[path] = b"x" * 1000
        url = f"http://{local_server['host']}:{local_server['port']}{path}"
        settings = Settings(allow_remote_urls=True, max_remote_fetch_bytes=100)
        cache = _RemoteFetchCache()

        with pytest.raises(RemoteURLFetchError) as ei:
            fetch_url(url, settings, cache)
        assert "max_remote_fetch_bytes" in str(ei.value)

    def test_size_cap_allows_at_or_below(self, local_server):
        path = "/small.md"
        local_server["handler"].body_for_path[path] = b"x" * 100
        url = f"http://{local_server['host']}:{local_server['port']}{path}"
        settings = Settings(allow_remote_urls=True, max_remote_fetch_bytes=100)
        cache = _RemoteFetchCache()

        body = fetch_url(url, settings, cache)
        assert len(body) == 100

    def test_var_expansion_reaches_wire(self, local_server, monkeypatch):
        """Header ${VAR} expansion is visible on the actual request."""
        monkeypatch.setenv("PROMPTS_API_TOKEN", "secret123")
        path = "/auth-check.md"
        local_server["handler"].body_for_path[path] = b"ok"
        base = f"http://{local_server['host']}:{local_server['port']}/"
        url = f"{base.rstrip('/')}{path}"
        settings = Settings(
            allow_remote_urls=True,
            url_headers={base: {"Authorization": "Bearer ${PROMPTS_API_TOKEN}"}},
        )
        cache = _RemoteFetchCache()

        fetch_url(url, settings, cache)

        seen = local_server["handler"].headers_seen[path]
        assert seen.get("Authorization") == "Bearer secret123"
