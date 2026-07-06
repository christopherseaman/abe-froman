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

from sqrlly.runtime.url import (
    RemoteURLBlockedError,
    RemoteURLFetchError,
    canonical,
    fetch_url,
    resolve_url,
)
from sqrlly.schema.models import Settings


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

    def test_path_only_base_url_promoted_to_file_scheme(self):
        """A `base_url:` without a scheme is treated as a file:// prefix
        so downstream scheme inspection still works."""
        result = resolve_url(
            "x.md",
            base_url="/projects/abe/",
            workdir="/unused",
        )
        assert result == "file:///projects/abe/x.md"


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
        cache = {}
        body = fetch_url(f"file://{path}", Settings(), cache)
        assert body == b"hello world"

    def test_caches_local_reads(self, tmp_path):
        path = tmp_path / "local.md"
        path.write_text("first")
        cache = {}
        fetch_url(f"file://{path}", Settings(), cache)
        # Mutate file under a hot cache; cache should still return original.
        path.write_text("second")
        body = fetch_url(f"file://{path}", Settings(), cache)
        assert body == b"first"

    def test_size_cap_rejects_oversize_file(self, tmp_path):
        path = tmp_path / "big.md"
        path.write_bytes(b"x" * 200)
        cache = {}
        settings = Settings(max_remote_fetch_bytes=100)
        with pytest.raises(RemoteURLFetchError) as ei:
            fetch_url(f"file://{path}", settings, cache)
        assert "max_remote_fetch_bytes" in str(ei.value)

    def test_size_cap_allows_file_at_or_below(self, tmp_path):
        path = tmp_path / "ok.md"
        path.write_bytes(b"x" * 100)
        cache = {}
        settings = Settings(max_remote_fetch_bytes=100)
        body = fetch_url(f"file://{path}", settings, cache)
        assert body == b"x" * 100


# ----- fetch_url: remote URL gates -----

class TestRemoteURLGates:
    def test_blocks_when_allow_remote_urls_false(self):
        cache = {}
        with pytest.raises(RemoteURLBlockedError) as ei:
            fetch_url("https://x.com/a.md", Settings(), cache)
        assert "allow_remote_urls" in str(ei.value)

    def test_blocks_when_host_not_in_allowlist(self):
        cache = {}
        settings = Settings(
            allow_remote_urls=True,
            allowed_url_hosts=["*.internal.example.com"],
        )
        with pytest.raises(RemoteURLBlockedError) as ei:
            fetch_url("https://attacker.com/a.md", settings, cache)
        assert "allowed_url_hosts" in str(ei.value)

    def test_allows_when_host_matches_glob(self):
        # Host matches but no server is running — should reach fetch attempt.
        cache = {}
        settings = Settings(
            allow_remote_urls=True,
            allowed_url_hosts=["*.example.com"],
        )
        with pytest.raises(RemoteURLFetchError):
            fetch_url("https://api.example.com/x.md", settings, cache)

    def test_remote_script_fetch_reaches_network_layer(self):
        """Script URLs get no per-extension fetch gate — remote EXECUTION
        is refused at dispatch (file:// required), so the fetch layer
        treats .py like any other remote body."""
        cache = {}
        settings = Settings(allow_remote_urls=True)
        # .invalid is RFC-reserved as never-resolving; failing at the
        # network layer proves no gate rejected the extension first.
        with pytest.raises(RemoteURLFetchError):
            fetch_url("https://nope.invalid/run.py", settings, cache)


# ----- ${VAR} expansion -----

class TestVarExpansion:
    def test_missing_var_raises_clear_error(self, monkeypatch):
        """Missing env var is a configuration error (ValueError), not I/O."""
        monkeypatch.delenv("ABSENT_TOKEN", raising=False)
        cache = {}
        settings = Settings(
            allow_remote_urls=True,
            url_headers={
                "https://nope.invalid/": {"Authorization": "Bearer ${ABSENT_TOKEN}"}
            },
        )
        with pytest.raises(ValueError) as ei:
            fetch_url("https://nope.invalid/a.md", settings, cache)
        assert "ABSENT_TOKEN" in str(ei.value)

    def test_env_file_fallback(self, monkeypatch, tmp_path):
        """A ${VAR} absent from the process env resolves from the
        project-local .env (matches the documented secret chain)."""
        from sqrlly.runtime.url import _expand_vars
        from sqrlly.runtime.secrets import _load_dotenv_once

        monkeypatch.delenv("DOTENV_ONLY_TOKEN", raising=False)
        (tmp_path / ".env").write_text("DOTENV_ONLY_TOKEN=fromdotenv\n")
        monkeypatch.chdir(tmp_path)
        _load_dotenv_once.cache_clear()
        try:
            assert _expand_vars("Bearer ${DOTENV_ONLY_TOKEN}") == "Bearer fromdotenv"
        finally:
            _load_dotenv_once.cache_clear()

    def test_process_env_wins_over_env_file(self, monkeypatch, tmp_path):
        from sqrlly.runtime.url import _expand_vars
        from sqrlly.runtime.secrets import _load_dotenv_once

        (tmp_path / ".env").write_text("PREC_TOKEN=fromdotenv\n")
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("PREC_TOKEN", "fromenv")
        _load_dotenv_once.cache_clear()
        try:
            assert _expand_vars("${PREC_TOKEN}") == "fromenv"
        finally:
            _load_dotenv_once.cache_clear()


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
        cache = {}

        body1 = fetch_url(url, settings, cache)
        body2 = fetch_url(url, settings, cache)
        body3 = fetch_url(url, settings, cache)

        assert body1 == body2 == body3 == b"once"
        assert local_server["handler"].hits[path] == 1

    def test_size_cap_rejects_oversize_body(self, local_server):
        path = "/big.md"
        local_server["handler"].body_for_path[path] = b"x" * 1000
        url = f"http://{local_server['host']}:{local_server['port']}{path}"
        settings = Settings(allow_remote_urls=True, max_remote_fetch_bytes=100)
        cache = {}

        with pytest.raises(RemoteURLFetchError) as ei:
            fetch_url(url, settings, cache)
        assert "max_remote_fetch_bytes" in str(ei.value)

    def test_size_cap_allows_at_or_below(self, local_server):
        path = "/small.md"
        local_server["handler"].body_for_path[path] = b"x" * 100
        url = f"http://{local_server['host']}:{local_server['port']}{path}"
        settings = Settings(allow_remote_urls=True, max_remote_fetch_bytes=100)
        cache = {}

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
        cache = {}

        fetch_url(url, settings, cache)

        seen = local_server["handler"].headers_seen[path]
        assert seen.get("Authorization") == "Bearer secret123"
