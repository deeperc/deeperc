"""Unit tests for download_with_backoff's connect/read timeout cap (TODO-71).

These exercise the *real* requests transport against local servers so the
actual DOWNLOAD_CONNECT_TIMEOUT_SEC / DOWNLOAD_READ_TIMEOUT_SEC values are
verified end-to-end — mock/responses libraries short-circuit the transport
layer where timeouts are raised, so they cannot validate timeout behaviour.

Timing budgets assume a single attempt, so the timeout tests pass
max_retries=1 to isolate one connect/read bound. (download_with_backoff
fails fast on a timeout — no in-call retry — so the per-attempt bound is
also the per-URL bound; the per-board negative cache handles coarse retry.)

Servers used:
- connect timeout  -> RFC5737 TEST-NET-1 blackhole 192.0.2.1 (SYN dropped)
- read timeout     -> raw TCP server that accepts then sends no HTTP response
                      (the Akamai-class tarpit: connect fast, read stalls)
- fast success     -> http.server returning a valid %PDF immediately
- slow-but-valid   -> http.server streaming a PDF in chunks, each gap well
                      under the read timeout (legitimate slow download)
"""

import http.server
import os
import socket
import sys
import threading
import time

import pytest
import requests

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import download_datasheets as dl  # noqa: E402


# Blackhole host whose SYNs are dropped -> deterministic connect timeout.
_BLACKHOLE_URL = "http://192.0.2.1:12345/x.pdf"


def _session():
    return requests.Session()


# ---------------------------------------------------------------------------
# Local servers
# ---------------------------------------------------------------------------

class _StallServer:
    """Accept TCP connections and never send an HTTP response.

    Connect succeeds immediately; requests then blocks reading the status
    line until the read timeout fires (requests.ReadTimeout)."""

    def __init__(self):
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(5)
        self.port = self._srv.getsockname()[1]
        self._held = []
        self._stop = False
        self._t = threading.Thread(target=self._run, daemon=True)
        self._t.start()

    def _run(self):
        self._srv.settimeout(0.5)
        while not self._stop:
            try:
                conn, _ = self._srv.accept()
                self._held.append(conn)  # hold open, send nothing
            except socket.timeout:
                continue
            except OSError:
                break

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}/x.pdf"

    def close(self):
        self._stop = True
        for c in self._held:
            try:
                c.close()
            except OSError:
                pass
        try:
            self._srv.close()
        except OSError:
            pass


def _make_http_server(handler_cls):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}/x.pdf"


class _FastPDFHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = b"%PDF-1.4\n" + b"0" * 1024
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):  # silence
        pass


class _SlowPDFHandler(http.server.BaseHTTPRequestHandler):
    # Stream a valid PDF in chunks spaced well under DOWNLOAD_READ_TIMEOUT_SEC.
    # The read timeout is per socket read, so a download whose chunks each
    # arrive in time succeeds even though it is "slow" overall.
    chunk = 256 * 1024
    n_chunks = 6
    gap = 1.0

    def do_GET(self):
        body_len = 4 + self.chunk * self.n_chunks
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Length", str(body_len))
        self.end_headers()
        self.wfile.write(b"%PDF")
        for _ in range(self.n_chunks):
            self.wfile.write(b"0" * self.chunk)
            self.wfile.flush()
            time.sleep(self.gap)

    def log_message(self, *a):
        pass


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_connect_timeout_is_bounded(tmp_path):
    """Server unreachable (SYN dropped) -> bounded by connect timeout, False."""
    dest = tmp_path / "out.pdf"
    start = time.monotonic()
    ok = dl.download_with_backoff(
        _BLACKHOLE_URL, dest, delay=0.0, session=_session(), max_retries=1
    )
    elapsed = time.monotonic() - start
    assert ok is False
    # Single attempt: ~connect_timeout (10s) + small budget; never the old 60s.
    assert elapsed <= dl.DOWNLOAD_CONNECT_TIMEOUT_SEC + 5, f"took {elapsed:.1f}s"
    assert not dest.exists()


def test_read_timeout_is_bounded(tmp_path):
    """Connect fast, response stalls -> bounded by read timeout, False."""
    server = _StallServer()
    try:
        dest = tmp_path / "out.pdf"
        start = time.monotonic()
        ok = dl.download_with_backoff(
            server.url, dest, delay=0.0, session=_session(), max_retries=1
        )
        elapsed = time.monotonic() - start
        assert ok is False
        # Read of the status line stalls -> ReadTimeout at ~30s, not ~60s.
        assert elapsed >= dl.DOWNLOAD_READ_TIMEOUT_SEC - 3, f"too fast: {elapsed:.1f}s"
        assert elapsed <= dl.DOWNLOAD_READ_TIMEOUT_SEC + 5, f"too slow: {elapsed:.1f}s"
        assert not dest.exists()
    finally:
        server.close()


def test_fast_valid_pdf_succeeds(tmp_path):
    """A normal fast PDF download is not falsely failed by the timeout cap."""
    srv, url = _make_http_server(_FastPDFHandler)
    try:
        dest = tmp_path / "out.pdf"
        start = time.monotonic()
        ok = dl.download_with_backoff(url, dest, delay=0.0, session=_session())
        elapsed = time.monotonic() - start
        assert ok is True
        assert dest.exists()
        assert dest.read_bytes()[:4] == b"%PDF"
        assert elapsed < 5, f"unexpectedly slow: {elapsed:.1f}s"
    finally:
        srv.shutdown()


def test_slow_valid_pdf_succeeds(tmp_path):
    """A legitimately slow chunked download (each read < read_timeout) succeeds.

    Guards against using a *total* timeout: the cap is per-read, so a download
    whose chunks each arrive in time must not be falsely timed out."""
    srv, url = _make_http_server(_SlowPDFHandler)
    try:
        dest = tmp_path / "out.pdf"
        start = time.monotonic()
        ok = dl.download_with_backoff(url, dest, delay=0.0, session=_session())
        elapsed = time.monotonic() - start
        assert ok is True
        assert dest.exists()
        assert dest.read_bytes()[:4] == b"%PDF"
        # The download genuinely took several seconds of spaced chunks.
        assert elapsed >= _SlowPDFHandler.n_chunks * _SlowPDFHandler.gap - 1
    finally:
        srv.shutdown()
