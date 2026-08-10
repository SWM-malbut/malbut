"""Security tests for backend event delivery."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from homecam_detector.event_poster import build_no_redirect_opener
from homecam_detector.event_poster import EventPoster
from homecam_detector.event_dedupe import ConfirmedEvent


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


def test_close_joins_idle_worker_and_rejects_new_events() -> None:
    poster = EventPoster(
        "https://homecam.example.test",
        VALID_TOKEN,
    )
    poster.close()
    assert not poster._worker.is_alive()


def test_privacy_disable_rejects_and_discards_events() -> None:
    poster = EventPoster(
        "https://homecam.example.test",
        VALID_TOKEN,
        enabled=False,
    )
    event = ConfirmedEvent(
        event_type="person",
        confidence=0.9,
        occurred_at=0.0,
        idempotency_key="a" * 64,
    )
    assert not poster.enqueue(event)
    poster._queue.put_nowait((poster._generation, event))
    poster.set_enabled(False)
    poster._queue.join()
    assert poster._queue.unfinished_tasks == 0
    assert poster._queue.empty()
    assert not poster.enqueue(event)
    poster.close()


def test_unexpected_request_error_does_not_kill_worker() -> None:
    class BrokenOpener:
        def open(self, _request, timeout):
            del timeout
            raise ValueError("malformed request")

    poster = EventPoster("https://homecam.example.test", VALID_TOKEN)
    poster._opener = BrokenOpener()
    poster.enqueue(
        ConfirmedEvent(
            event_type="person",
            confidence=0.9,
            occurred_at=0.0,
            idempotency_key="a" * 64,
        )
    )
    poster._queue.join()
    assert poster._worker.is_alive()
    poster.close()


def test_off_then_on_does_not_retry_an_inflight_old_event() -> None:
    first_attempt = threading.Event()

    class RetryingOpener:
        calls = 0

        def open(self, _request, timeout):
            del timeout
            self.calls += 1
            first_attempt.set()
            raise URLError("temporary failure")

    poster = EventPoster("https://homecam.example.test", VALID_TOKEN)
    opener = RetryingOpener()
    poster._opener = opener
    event = ConfirmedEvent(
        event_type="person",
        confidence=0.9,
        occurred_at=0.0,
        idempotency_key="b" * 64,
    )
    assert poster.enqueue(event)
    assert first_attempt.wait(timeout=1.0)
    poster.set_enabled(False)
    poster.set_enabled(True)
    poster._queue.join()
    assert opener.calls == 1
    poster.close()
