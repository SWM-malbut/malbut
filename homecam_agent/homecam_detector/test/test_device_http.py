"""Security tests for credential-bearing device HTTP requests."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from homecam_detector.device_http import build_no_redirect_opener


VALID_TOKEN = (
    "hc1.123e4567-e89b-42d3-a456-426614174000." + "a" * 64
)


class _TargetHandler(BaseHTTPRequestHandler):
    authorization_headers = []

    def do_GET(self) -> None:
        """Record any leaked authorization header."""
        self.authorization_headers.append(self.headers.get("Authorization"))
        self.send_response(200)
        self.end_headers()

    def log_message(self, _format, *args) -> None:
        """Keep test output quiet."""
        del args


class _RedirectHandler(BaseHTTPRequestHandler):
    target_url = ""
    request_count = 0

    def do_GET(self) -> None:
        """Redirect to the separate target server."""
        type(self).request_count += 1
        self.send_response(302)
        self.send_header("Location", type(self).target_url)
        self.end_headers()

    def log_message(self, _format, *args) -> None:
        """Keep test output quiet."""
        del args


def _start_server(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def test_authorization_is_never_forwarded_across_redirect() -> None:
    _TargetHandler.authorization_headers = []
    _RedirectHandler.request_count = 0
    target, target_thread = _start_server(_TargetHandler)
    redirect, redirect_thread = _start_server(_RedirectHandler)
    try:
        _RedirectHandler.target_url = (
            f"http://127.0.0.1:{target.server_address[1]}/capture"
        )
        request = Request(
            f"http://127.0.0.1:{redirect.server_address[1]}/event",
            headers={"Authorization": f"Bearer {VALID_TOKEN}"},
        )
        with pytest.raises(HTTPError) as raised:
            build_no_redirect_opener().open(request, timeout=1.0)
        assert raised.value.code == 302
        assert _RedirectHandler.request_count == 1
        assert _TargetHandler.authorization_headers == []
    finally:
        redirect.shutdown()
        target.shutdown()
        redirect.server_close()
        target.server_close()
        redirect_thread.join(timeout=1.0)
        target_thread.join(timeout=1.0)
