"""HTTP contracts for the content-free Robot Web counting proxy."""

from contextlib import contextmanager
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading
import time

import pytest

from malbut_scenarios.counting_robot_web_proxy import (
    CountingRobotWebProxy,
    RobotWebProxyCounts,
    is_literal_loopback_origin,
    request_body_digest,
)


class _UpstreamRecorder:
    """Thread-safe fake Robot Web state owned by one test."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls = []
        self.status = 200
        self.body = b'{"ok":true}'
        self.headers = {
            'Cache-Control': 'no-store',
            'Content-Type': 'application/json',
            'Set-Cookie': 'upstream-session=opaque; HttpOnly',
            'X-Content-Type-Options': 'nosniff',
            'X-Private-Upstream': 'must-not-forward',
        }
        self.entered = threading.Event()
        self.release = None

    def record(self, method, path, headers, body) -> None:
        """Retain a private request for assertions without logging it."""
        with self._lock:
            self.calls.append((method, path, dict(headers), body))
        self.entered.set()
        if self.release is not None:
            self.release.wait(timeout=5.0)


def _upstream_handler(recorder):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, _format, *_args):
            return

        def do_GET(self):
            self._handle()

        def do_POST(self):
            self._handle()

        def _handle(self):
            raw_length = self.headers.get('Content-Length')
            length = int(raw_length) if raw_length is not None else 0
            body = self.rfile.read(length) if length else b''
            recorder.record(self.command, self.path, self.headers, body)
            self.send_response(recorder.status)
            for name, value in recorder.headers.items():
                self.send_header(name, value)
            self.send_header('Content-Length', str(len(recorder.body)))
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.write(recorder.body)

    return Handler


@contextmanager
def _fake_upstream():
    recorder = _UpstreamRecorder()
    server = ThreadingHTTPServer(
        ('127.0.0.1', 0),
        _upstream_handler(recorder),
    )
    server.daemon_threads = False
    server.block_on_close = True
    thread = threading.Thread(
        target=server.serve_forever,
        name='malbut-test-robot-web-upstream',
        daemon=False,
    )
    thread.start()
    try:
        yield server.server_address[1], recorder
    finally:
        if recorder.release is not None:
            recorder.release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2.0)
        assert not thread.is_alive()


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
        candidate.bind(('127.0.0.1', 0))
        return candidate.getsockname()[1]


def _wait_until(predicate, *, timeout_seconds=2.0) -> None:
    """Wait a bounded interval for one cross-thread proxy condition."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('bounded proxy condition was not reached')


@contextmanager
def _running_proxy(
    upstream_port,
    *,
    timeout_seconds=1.0,
    expected_preview_digest=None,
):
    proxy = CountingRobotWebProxy(
        _unused_loopback_port(),
        upstream_port,
        timeout_seconds=timeout_seconds,
        expected_preview_digest=expected_preview_digest,
    )
    proxy.start()
    try:
        yield proxy
    finally:
        proxy.close(timeout_seconds=2.0)


def _request(proxy, method, path, *, body=None, headers=None):
    connection = http.client.HTTPConnection(
        '127.0.0.1',
        int(proxy.origin.rsplit(':', 1)[1]),
        timeout=2.0,
    )
    try:
        connection.request(
            method,
            path,
            body=body,
            headers={} if headers is None else headers,
        )
        response = connection.getresponse()
        payload = response.read()
        return response.status, dict(response.getheaders()), payload
    finally:
        connection.close()


def _raw_length_request(proxy, path, declared_length):
    connection = http.client.HTTPConnection(
        '127.0.0.1',
        int(proxy.origin.rsplit(':', 1)[1]),
        timeout=2.0,
    )
    try:
        connection.putrequest('POST', path)
        connection.putheader('Content-Length', declared_length)
        connection.endheaders()
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def _truncated_body_request(proxy, path):
    connection = http.client.HTTPConnection(
        '127.0.0.1',
        int(proxy.origin.rsplit(':', 1)[1]),
        timeout=2.0,
    )
    try:
        connection.putrequest('POST', path)
        connection.putheader('Content-Length', '10')
        connection.putheader('Connection', 'close')
        connection.endheaders(message_body=b'x')
        connection.sock.shutdown(socket.SHUT_WR)
        response = connection.getresponse()
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def test_fixed_surface_forwards_and_counts_every_allowed_path_once() -> None:
    """Forward exactly the two reads and three command endpoints."""
    with _fake_upstream() as (upstream_port, recorder):
        with _running_proxy(upstream_port) as proxy:
            for path in ('/api/editor-config', '/api/robot/status'):
                status, _, payload = _request(proxy, 'GET', path)
                assert status == 200
                assert json.loads(payload) == {'ok': True}
            for path in (
                '/api/navigation/preview',
                '/api/navigation/start',
                '/api/navigation/cancel',
            ):
                status, _, payload = _request(
                    proxy,
                    'POST',
                    path,
                    body=b'{}',
                )
                assert status == 200
                assert json.loads(payload) == {'ok': True}

            assert proxy.snapshot() == RobotWebProxyCounts(
                bootstrap_count=1,
                status_count=1,
                preview_count=1,
                start_count=1,
                cancel_count=1,
            )
        assert [(call[0], call[1]) for call in recorder.calls] == [
            ('GET', '/api/editor-config'),
            ('GET', '/api/robot/status'),
            ('POST', '/api/navigation/preview'),
            ('POST', '/api/navigation/start'),
            ('POST', '/api/navigation/cancel'),
        ]


def test_cookie_csrf_and_rewritten_origin_reach_upstream() -> None:
    """Preserve session proof while pinning POST origin to upstream."""
    private_cookie = 'session=private-cookie-value'
    private_csrf = 'private-csrf-value'
    private_body = b'{"private":"payload-value"}'
    with _fake_upstream() as (upstream_port, recorder):
        with _running_proxy(upstream_port) as proxy:
            status, response_headers, _ = _request(
                proxy,
                'POST',
                '/api/navigation/start',
                body=private_body,
                headers={
                    'Content-Type': 'text/plain',
                    'Cookie': private_cookie,
                    'X-CSRF-Token': private_csrf,
                    'Origin': 'http://untrusted.invalid',
                },
            )

        assert status == 200
        _, _, headers, body = recorder.calls[0]
        assert headers['Cookie'] == private_cookie
        assert headers['X-CSRF-Token'] == private_csrf
        assert headers['Origin'] == f'http://127.0.0.1:{upstream_port}'
        assert headers['Content-Type'] == 'application/json'
        assert body == private_body
        assert response_headers['Set-Cookie'].startswith('upstream-session=')
        assert response_headers['Cache-Control'] == 'no-store'
        assert response_headers['X-Content-Type-Options'] == 'nosniff'
        assert 'X-Private-Upstream' not in response_headers


def test_get_forwards_cookie_but_not_write_only_headers() -> None:
    """Keep bootstrap session state without adding write-only headers."""
    with _fake_upstream() as (upstream_port, recorder):
        with _running_proxy(upstream_port) as proxy:
            status, _, _ = _request(
                proxy,
                'GET',
                '/api/editor-config',
                headers={
                    'Cookie': 'session=private-cookie',
                    'X-CSRF-Token': 'must-not-forward-on-get',
                    'Origin': 'http://must-not-forward.invalid',
                },
            )

        assert status == 200
        _, _, headers, body = recorder.calls[0]
        assert headers['Cookie'] == 'session=private-cookie'
        assert headers.get('X-CSRF-Token') is None
        assert headers.get('Origin') is None
        assert body == b''


@pytest.mark.parametrize(
    'method,path',
    (
        ('GET', '/api/navigation/start'),
        ('GET', '/api/robot/status?private=query'),
        ('POST', '/api/robot/status'),
        ('POST', '/api/navigation/start/private'),
    ),
)
def test_disallowed_pairs_never_reach_upstream(method, path) -> None:
    """Reject every path and method pairing outside the fixed surface."""
    with _fake_upstream() as (upstream_port, recorder):
        with _running_proxy(upstream_port) as proxy:
            status, _, payload = _request(
                proxy,
                method,
                path,
                body=b'{}' if method == 'POST' else None,
            )
            counts = proxy.snapshot()

        assert status == 404
        assert json.loads(payload) == {
            'error_code': 'PROXY_PATH_NOT_ALLOWED',
        }
        assert recorder.calls == []
        assert counts == RobotWebProxyCounts(0, 0, 0, 0, 0)


def test_empty_invalid_and_oversized_bodies_never_reach_upstream() -> None:
    """Reject invalid framing before counting or contacting Robot Web."""
    with _fake_upstream() as (upstream_port, recorder):
        with _running_proxy(upstream_port) as proxy:
            empty_status, _, empty_payload = _request(
                proxy,
                'POST',
                '/api/navigation/start',
                body=b'',
                headers={'Content-Length': '0'},
            )
            invalid_status, invalid_payload = _raw_length_request(
                proxy,
                '/api/navigation/start',
                'not-an-integer',
            )
            large_status, large_payload = _raw_length_request(
                proxy,
                '/api/navigation/start',
                '1000001',
            )
            truncated_status, truncated_payload = _truncated_body_request(
                proxy,
                '/api/navigation/start',
            )
            counts = proxy.snapshot()

        assert empty_status == 400
        assert json.loads(empty_payload) == {
            'error_code': 'PROXY_BODY_INVALID',
        }
        assert invalid_status == 400
        assert invalid_payload == {'error_code': 'PROXY_BODY_INVALID'}
        assert large_status == 400
        assert large_payload == {'error_code': 'PROXY_BODY_INVALID'}
        assert truncated_status == 400
        assert truncated_payload == {'error_code': 'PROXY_BODY_INVALID'}
        assert recorder.calls == []
        assert counts == RobotWebProxyCounts(0, 0, 0, 0, 0)


def test_unavailable_upstream_is_safe_and_counts_attempt() -> None:
    """Return one stable error while retaining only an aggregate attempt."""
    unavailable_port = _unused_loopback_port()
    with _running_proxy(unavailable_port, timeout_seconds=0.2) as proxy:
        status, _, payload = _request(proxy, 'GET', '/api/robot/status')
        counts = proxy.snapshot()

    assert status == 502
    assert json.loads(payload) == {
        'error_code': 'PROXY_UPSTREAM_UNAVAILABLE',
    }
    assert counts == RobotWebProxyCounts(0, 1, 0, 0, 0)
    assert str(unavailable_port).encode('ascii') not in payload


def test_repr_and_logs_never_contain_forwarded_private_content(
    caplog,
    capsys,
) -> None:
    """Keep cookie, CSRF value, and request body out of diagnostics."""
    private_values = (
        'private-cookie-value',
        'private-csrf-value',
        'private-payload-value',
    )
    with _fake_upstream() as (upstream_port, _recorder):
        with _running_proxy(upstream_port) as proxy:
            status, _, _ = _request(
                proxy,
                'POST',
                '/api/navigation/preview',
                body=b'{"value":"private-payload-value"}',
                headers={
                    'Cookie': 'session=private-cookie-value',
                    'X-CSRF-Token': 'private-csrf-value',
                },
            )
            rendered = repr(proxy) + repr(proxy.snapshot())

    assert status == 200
    captured = capsys.readouterr()
    observed_logs = caplog.text + captured.out + captured.err + rendered
    for private_value in private_values:
        assert private_value not in observed_logs
    assert str(upstream_port) not in rendered


def test_threads_are_non_daemon_and_close_cleanly() -> None:
    """Make both listener and handlers owned, joinable resources."""
    with _fake_upstream() as (upstream_port, _recorder):
        proxy = CountingRobotWebProxy(
            _unused_loopback_port(),
            upstream_port,
        )
        assert proxy._thread is None
        proxy.start()
        thread = proxy._thread
        server = proxy._server

        assert thread is not None
        assert thread.daemon is False
        assert server is not None
        assert server.daemon_threads is False
        # Handler ownership is tracked explicitly so server_close itself
        # cannot exceed the caller's bounded drain deadline.
        assert server.block_on_close is False
        assert proxy.close(timeout_seconds=1.0) is True
        assert thread.is_alive() is False


@pytest.mark.parametrize(
    'value',
    (0, -1, 61, True, float('nan'), float('inf'), '1'),
)
def test_close_timeout_is_finite_positive_and_bounded(value) -> None:
    """Reject close deadlines that cannot form a finite positive bound."""
    proxy = CountingRobotWebProxy(18080, 18081)
    with pytest.raises(ValueError, match='close timeout'):
        proxy.close(timeout_seconds=value)


def test_close_timeout_bounds_an_inflight_upstream_request() -> None:
    """A close deadline must include handler drain and upstream waiting."""
    with _fake_upstream() as (upstream_port, recorder):
        recorder.release = threading.Event()
        proxy = CountingRobotWebProxy(
            _unused_loopback_port(),
            upstream_port,
            timeout_seconds=5.0,
        )
        proxy.start()
        request_finished = threading.Event()
        request_errors = []

        def request():
            try:
                _request(proxy, 'GET', '/api/robot/status')
            except (
                ConnectionError,
                OSError,
                TimeoutError,
                http.client.HTTPException,
            ) as error:
                request_errors.append(error)
            finally:
                request_finished.set()

        request_thread = threading.Thread(target=request, daemon=False)
        request_thread.start()
        assert recorder.entered.wait(timeout=1.0)

        close_finished = threading.Event()
        close_result = []

        def close():
            close_result.append(proxy.close(timeout_seconds=0.05))
            close_finished.set()

        close_thread = threading.Thread(target=close, daemon=False)
        close_thread.start()
        completed_within_bound = close_finished.wait(timeout=0.75)
        recorder.release.set()
        close_thread.join(timeout=2.0)
        request_thread.join(timeout=2.0)

        assert completed_within_bound
        assert close_result == [False]
        assert not close_thread.is_alive()
        assert request_finished.is_set()
        assert all(
            isinstance(error, http.client.RemoteDisconnected)
            for error in request_errors
        )


def test_close_shuts_partial_post_downstream_and_joins_handler() -> None:
    """A stalled request body cannot outlive the bounded proxy shutdown."""
    proxy = CountingRobotWebProxy(
        _unused_loopback_port(),
        _unused_loopback_port(),
        timeout_seconds=5.0,
    )
    proxy.start()
    client = socket.create_connection(
        ('127.0.0.1', int(proxy.origin.rsplit(':', 1)[1])),
        timeout=1.0,
    )
    closed = False
    try:
        client.sendall(
            b'POST /api/navigation/start HTTP/1.1\r\n'
            b'Host: 127.0.0.1\r\n'
            b'Content-Length: 100\r\n'
            b'Connection: close\r\n\r\n'
            b'{'
        )

        def downstream_is_owned():
            with proxy._handler_condition:
                return bool(
                    proxy._active_handler_count == 1
                    and len(proxy._active_downstream_connections) == 1
                )

        _wait_until(downstream_is_owned)

        closed = proxy.close(timeout_seconds=2.0)

        assert closed is True
        with proxy._handler_condition:
            assert proxy._active_handler_count == 0
            assert proxy._active_downstream_connections == set()
            assert proxy._active_upstream_connections == set()
        assert proxy._thread is not None
        assert proxy._thread.is_alive() is False
    finally:
        client.close()
        if not closed:
            assert proxy.close(timeout_seconds=2.0) is True


def test_inflight_connections_are_tracked_then_removed() -> None:
    """Both sides of a forwarded request leave the ownership sets at EOF."""
    with _fake_upstream() as (upstream_port, recorder):
        recorder.release = threading.Event()
        proxy = CountingRobotWebProxy(
            _unused_loopback_port(),
            upstream_port,
            timeout_seconds=2.0,
        )
        proxy.start()
        results = []
        errors = []

        def request():
            try:
                results.append(_request(proxy, 'GET', '/api/robot/status'))
            except Exception as error:  # pragma: no cover - asserted below
                errors.append(error)

        request_thread = threading.Thread(target=request, daemon=False)
        request_thread.start()
        try:
            assert recorder.entered.wait(timeout=1.0)
            with proxy._handler_condition:
                assert proxy._active_handler_count == 1
                assert len(proxy._active_downstream_connections) == 1
                assert len(proxy._active_upstream_connections) == 1

            recorder.release.set()
            request_thread.join(timeout=2.0)
            assert not request_thread.is_alive()
            assert errors == []
            assert results[0][0] == 200

            def all_connections_released():
                with proxy._handler_condition:
                    return bool(
                        proxy._active_handler_count == 0
                        and not proxy._active_downstream_connections
                        and not proxy._active_upstream_connections
                    )

            _wait_until(all_connections_released)
            assert proxy.close(timeout_seconds=2.0) is True
        finally:
            recorder.release.set()
            request_thread.join(timeout=2.0)
            proxy.close(timeout_seconds=2.0)


def test_quiet_server_suppresses_handler_broken_pipe_traceback(
    monkeypatch,
    capsys,
) -> None:
    """A handler exception cannot disclose its message or source path."""
    proxy = CountingRobotWebProxy(
        _unused_loopback_port(),
        _unused_loopback_port(),
        timeout_seconds=1.0,
    )
    proxy.start()
    private_marker = '/private/source/path/private-token'
    handler = proxy._server.RequestHandlerClass

    def broken_pipe(_self, _status, _code):
        raise BrokenPipeError(private_marker)

    monkeypatch.setattr(handler, '_safe_error', broken_pipe)
    connection = http.client.HTTPConnection(
        '127.0.0.1',
        int(proxy.origin.rsplit(':', 1)[1]),
        timeout=1.0,
    )
    try:
        connection.request('GET', '/not-allowed')
        with pytest.raises(http.client.RemoteDisconnected):
            connection.getresponse()

        def handler_released():
            with proxy._handler_condition:
                return proxy._active_handler_count == 0

        _wait_until(handler_released)
        assert proxy.close(timeout_seconds=2.0) is True
    finally:
        connection.close()
        proxy.close(timeout_seconds=2.0)

    captured = capsys.readouterr()
    observed = captured.out + captured.err
    assert observed == ''
    assert private_marker not in observed
    assert 'Traceback' not in observed


def test_request_body_digest_uses_strict_canonical_json_object() -> None:
    """Whitespace and key order cannot change one preview binding digest."""
    first = b'{"room":"living", "options":{"speed":1,"quiet":true}}'
    reordered = (
        b'{ "options" : { "quiet" : true, "speed" : 1 }, '
        b'"room" : "living" }'
    )
    canonical = (
        b'{"options":{"quiet":true,"speed":1},"room":"living"}'
    )

    expected = hashlib.sha256(canonical).hexdigest()
    assert request_body_digest(first) == expected
    assert request_body_digest(reordered) == expected


@pytest.mark.parametrize(
    'body',
    (
        b'{"room":"living","room":"kitchen"}',
        b'{"outer":{"value":1,"value":2}}',
        b'[]',
        b'"text"',
        b'null',
        b'1',
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"value":1e999}',
        b'{"value":"\xff"}',
    ),
)
def test_request_body_digest_rejects_ambiguous_or_invalid_json(body) -> None:
    """Duplicate, non-object, non-finite, and invalid UTF-8 all fail."""
    with pytest.raises(ValueError, match='^request body is invalid$'):
        request_body_digest(body)


@pytest.mark.parametrize(
    'digest',
    (
        '',
        'a' * 63,
        'a' * 65,
        'A' * 64,
        'g' * 64,
        1,
        True,
    ),
)
def test_constructor_rejects_invalid_expected_preview_digest(digest) -> None:
    """Only an optional lowercase SHA-256 can arm preview verification."""
    with pytest.raises(ValueError, match='expected preview digest'):
        CountingRobotWebProxy(
            18080,
            18081,
            expected_preview_digest=digest,
        )


def test_preview_digest_mismatch_is_not_forwarded_or_counted() -> None:
    """A mismatched private preview fails before contacting Robot Web."""
    expected = request_body_digest(b'{"location":"living-room"}')
    with _fake_upstream() as (upstream_port, recorder):
        with _running_proxy(
            upstream_port,
            expected_preview_digest=expected,
        ) as proxy:
            status, _, payload = _request(
                proxy,
                'POST',
                '/api/navigation/preview',
                body=b'{"location":"different-room"}',
            )
            counts = proxy.snapshot()

    assert status == 502
    assert json.loads(payload) == {
        'error_code': 'PROXY_UPSTREAM_UNAVAILABLE',
    }
    assert recorder.calls == []
    assert counts == RobotWebProxyCounts(0, 0, 0, 0, 0, 0)


def test_exact_preview_is_verified_only_after_upstream_200() -> None:
    """A digest match forwards, but only HTTP 200 seals verification."""
    body = b'{ "location" : "living-room", "mode" : "single" }'
    expected = request_body_digest(
        b'{"mode":"single","location":"living-room"}'
    )
    with _fake_upstream() as (upstream_port, recorder):
        with _running_proxy(
            upstream_port,
            expected_preview_digest=expected,
        ) as proxy:
            recorder.status = 503
            rejected_status, _, _ = _request(
                proxy,
                'POST',
                '/api/navigation/preview',
                body=body,
            )
            after_rejection = proxy.snapshot()

            recorder.status = 200
            accepted_status, _, _ = _request(
                proxy,
                'POST',
                '/api/navigation/preview',
                body=body,
            )
            after_acceptance = proxy.snapshot()

    assert rejected_status == 503
    assert after_rejection.preview_count == 1
    assert after_rejection.verified_preview_count == 0
    assert accepted_status == 200
    assert after_acceptance.preview_count == 2
    assert after_acceptance.verified_preview_count == 1
    assert [call[3] for call in recorder.calls] == [body, body]


@pytest.mark.parametrize(
    'value,expected',
    (
        ('http://127.0.0.1:8765', True),
        ('http://127.0.0.2:1', True),
        ('http://localhost:8765', False),
        ('https://127.0.0.1:8765', False),
        ('http://192.0.2.1:8765', False),
        ('http://127.0.0.1:0', False),
        ('http://127.0.0.1:65536', False),
        ('http://127.0.0.1:not-a-port', False),
    ),
)
def test_loopback_origin_parser_is_strict(value, expected) -> None:
    """Accept only numeric HTTP loopback origins with a valid port."""
    assert is_literal_loopback_origin(value) is expected
