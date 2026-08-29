"""Loopback-only Robot Web proxy that counts commands without payload logs."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import json
import math
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
import socket
import threading
import time
from typing import Optional
import re


_MAX_BODY_BYTES = 1_000_000
_MAX_TIMEOUT_SECONDS = 60.0
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_GET_PATHS = frozenset({
    '/api/editor-config',
    '/api/robot/status',
})
_POST_PATHS = frozenset({
    '/api/navigation/preview',
    '/api/navigation/start',
    '/api/navigation/cancel',
})
_FORWARDED_RESPONSE_HEADERS = frozenset({
    'cache-control',
    'content-type',
    'set-cookie',
    'x-content-type-options',
})


@dataclass(frozen=True, slots=True)
class RobotWebProxyCounts:
    """Aggregate request attempts with no cookies, tokens, or payloads."""

    bootstrap_count: int
    status_count: int
    preview_count: int
    start_count: int
    cancel_count: int
    verified_preview_count: int = 0


class CountingRobotWebProxy:
    """Forward a fixed Robot Web surface and count each command attempt."""

    def __init__(
        self,
        listen_port: int,
        upstream_port: int,
        *,
        timeout_seconds: float = 30.0,
        expected_preview_digest: Optional[str] = None,
    ) -> None:
        """Validate two distinct literal-loopback ports without binding."""
        for value in (listen_port, upstream_port):
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 1 <= value <= 65535
            ):
                raise ValueError('proxy port is invalid')
        if listen_port == upstream_port:
            raise ValueError('proxy and upstream ports must differ')
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0.0 < float(timeout_seconds) <= _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError('proxy timeout is invalid')
        if expected_preview_digest is not None and (
            type(expected_preview_digest) is not str
            or _SHA256.fullmatch(expected_preview_digest) is None
        ):
            raise ValueError('expected preview digest is invalid')
        self._listen_port = listen_port
        self._upstream_port = upstream_port
        self._timeout_seconds = float(timeout_seconds)
        self._expected_preview_digest = expected_preview_digest
        self._lock = threading.Lock()
        self._handler_condition = threading.Condition()
        self._active_handler_count = 0
        self._active_upstream_connections: set[
            http.client.HTTPConnection
        ] = set()
        self._active_downstream_connections: set[socket.socket] = set()
        self._counts = {
            '/api/editor-config': 0,
            '/api/robot/status': 0,
            '/api/navigation/preview': 0,
            '/api/navigation/start': 0,
            '/api/navigation/cancel': 0,
            'verified-preview': 0,
        }
        self._server: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    def __repr__(self) -> str:
        """Do not render ports or forwarded request state."""
        return (
            'CountingRobotWebProxy('
            f'started={self._server is not None!r})'
        )

    @property
    def origin(self) -> str:
        """Return the fixed local origin used only by the Agent child."""
        return f'http://127.0.0.1:{self._listen_port}'

    def start(self) -> None:
        """Bind loopback and start one non-daemon server thread."""
        if self._server is not None:
            raise RuntimeError('Robot Web proxy already started')
        owner = self

        class Handler(_RobotWebProxyHandler):
            proxy_owner = owner

        server = _QuietThreadingHTTPServer(
            ('127.0.0.1', self._listen_port),
            Handler,
        )
        server.daemon_threads = False
        server.block_on_close = False
        thread = threading.Thread(
            target=server.serve_forever,
            name='malbut-counting-robot-web-proxy',
            daemon=False,
        )
        self._server = server
        self._thread = thread
        thread.start()

    def snapshot(self) -> RobotWebProxyCounts:
        """Return aggregate counts without rendering the fixed paths."""
        with self._lock:
            return RobotWebProxyCounts(
                bootstrap_count=self._counts['/api/editor-config'],
                status_count=self._counts['/api/robot/status'],
                preview_count=self._counts['/api/navigation/preview'],
                start_count=self._counts['/api/navigation/start'],
                cancel_count=self._counts['/api/navigation/cancel'],
                verified_preview_count=self._counts['verified-preview'],
            )

    def close(self, timeout_seconds: float = 10.0) -> bool:
        """Drain handlers, close the listener, and join its owner thread."""
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(float(timeout_seconds))
            or not 0.0 < float(timeout_seconds) <= _MAX_TIMEOUT_SECONDS
        ):
            raise ValueError('proxy close timeout is invalid')
        server = self._server
        thread = self._thread
        if server is None:
            return True
        deadline = time.monotonic() + float(timeout_seconds)
        server.shutdown()
        server.server_close()
        with self._handler_condition:
            upstream = tuple(self._active_upstream_connections)
            downstream = tuple(self._active_downstream_connections)
        for connection in upstream:
            connection.close()
        for connection in downstream:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        if thread is not None:
            thread.join(timeout=max(0.0, deadline - time.monotonic()))
        with self._handler_condition:
            while self._active_handler_count:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    break
                self._handler_condition.wait(timeout=remaining)
            handlers_stopped = self._active_handler_count == 0
        return bool(
            (thread is None or not thread.is_alive()) and handlers_stopped
        )

    def _handler_started(self, connection: socket.socket) -> None:
        with self._handler_condition:
            self._active_handler_count += 1
            self._active_downstream_connections.add(connection)

    def _handler_stopped(self, connection: socket.socket) -> None:
        with self._handler_condition:
            self._active_handler_count -= 1
            self._active_downstream_connections.discard(connection)
            self._handler_condition.notify_all()

    def _increment(self, path: str) -> None:
        with self._lock:
            self._counts[path] += 1

    def _forward(
        self,
        method: str,
        path: str,
        body: bytes | None,
        incoming_headers,
    ) -> tuple[int, list[tuple[str, str]], bytes]:
        preview_verified = False
        if (
            path == '/api/navigation/preview'
            and self._expected_preview_digest is not None
        ):
            if body is None or request_body_digest(body) != (
                self._expected_preview_digest
            ):
                raise RuntimeError('proxy preview binding mismatch')
            preview_verified = True
        self._increment(path)
        headers = {'Accept': 'application/json'}
        cookie = incoming_headers.get('Cookie')
        if cookie:
            headers['Cookie'] = cookie
        if body is not None:
            headers.update({
                'Content-Type': 'application/json',
                'Origin': f'http://127.0.0.1:{self._upstream_port}',
            })
            csrf = incoming_headers.get('X-CSRF-Token')
            if csrf:
                headers['X-CSRF-Token'] = csrf
        connection = http.client.HTTPConnection(
            '127.0.0.1',
            self._upstream_port,
            timeout=self._timeout_seconds,
        )
        with self._handler_condition:
            self._active_upstream_connections.add(connection)
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            declared = response.getheader('Content-Length')
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError as error:
                    raise RuntimeError(
                        'proxy upstream length invalid'
                    ) from error
                if not 0 <= declared_size <= _MAX_BODY_BYTES:
                    raise RuntimeError('proxy upstream response too large')
            payload = response.read(_MAX_BODY_BYTES + 1)
            if len(payload) > _MAX_BODY_BYTES:
                raise RuntimeError('proxy upstream response too large')
            selected_headers = [
                (name, value)
                for name, value in response.getheaders()
                if name.lower() in _FORWARDED_RESPONSE_HEADERS
            ]
            if preview_verified and response.status == 200:
                self._increment('verified-preview')
            return response.status, selected_headers, payload
        finally:
            connection.close()
            with self._handler_condition:
                self._active_upstream_connections.discard(connection)
                self._handler_condition.notify_all()


class _QuietThreadingHTTPServer(ThreadingHTTPServer):
    """Suppress request tracebacks that can disclose installed host paths."""

    def handle_error(self, _request, _client_address) -> None:
        """Convert handler failures into connection-local failures only."""
        return


class _RobotWebProxyHandler(BaseHTTPRequestHandler):
    """Forward fixed paths while suppressing default request logging."""

    protocol_version = 'HTTP/1.1'
    proxy_owner: CountingRobotWebProxy

    def handle(self) -> None:
        """Account for every owned handler until its connection closes."""
        connection = self.connection
        connection.settimeout(self.proxy_owner._timeout_seconds)
        self.proxy_owner._handler_started(connection)
        try:
            super().handle()
        finally:
            self.proxy_owner._handler_stopped(connection)

    def log_message(self, _format: str, *_args) -> None:
        """Do not put paths, cookies, or bodies in process logs."""
        return

    def do_GET(self) -> None:
        if self.path not in _GET_PATHS:
            self._safe_error(404, 'PROXY_PATH_NOT_ALLOWED')
            return
        self._relay('GET', None)

    def do_POST(self) -> None:
        if self.path not in _POST_PATHS:
            self._safe_error(404, 'PROXY_PATH_NOT_ALLOWED')
            return
        raw_length = self.headers.get('Content-Length')
        try:
            length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            length = -1
        if not 1 <= length <= _MAX_BODY_BYTES:
            self._safe_error(400, 'PROXY_BODY_INVALID')
            return
        body = self.rfile.read(length)
        if len(body) != length:
            self._safe_error(400, 'PROXY_BODY_INVALID')
            return
        self._relay('POST', body)

    def _relay(self, method: str, body: bytes | None) -> None:
        try:
            status, headers, payload = self.proxy_owner._forward(
                method,
                self.path,
                body,
                self.headers,
            )
        except (
            ConnectionError,
            OSError,
            TimeoutError,
            http.client.HTTPException,
            RuntimeError,
        ):
            self._safe_error(502, 'PROXY_UPSTREAM_UNAVAILABLE')
            return
        self.send_response(status)
        for name, value in headers:
            if name.lower() != 'content-length':
                self.send_header(name, value)
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(payload)

    def _safe_error(self, status: int, code: str) -> None:
        payload = json.dumps(
            {'error_code': code},
            ensure_ascii=True,
            separators=(',', ':'),
        ).encode('ascii')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(payload)))
        self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(payload)


def is_literal_loopback_origin(value: str) -> bool:
    """Recognize only the proxy's numeric local HTTP origin shape."""
    try:
        scheme, rest = value.split('://', 1)
        host, raw_port = rest.rsplit(':', 1)
        return bool(
            scheme == 'http'
            and ip_address(host).is_loopback
            and raw_port.isascii()
            and raw_port.isdecimal()
            and 1 <= int(raw_port) <= 65535
        )
    except (ValueError, TypeError):
        return False


def request_body_digest(body: bytes) -> str:
    """Hash one strict JSON object without retaining its private fields."""
    if not isinstance(body, bytes) or not 1 <= len(body) <= _MAX_BODY_BYTES:
        raise ValueError('request body is invalid')

    def unique_object(pairs):
        value = {}
        for key, item in pairs:
            if type(key) is not str or key in value:
                raise ValueError('request body is invalid')
            value[key] = item
        return value

    def reject_constant(_value):
        raise ValueError('request body is invalid')

    try:
        value = json.loads(
            body.decode('utf-8', errors='strict'),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
        if type(value) is not dict:
            raise ValueError('request body is invalid')
        canonical = json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('ascii')
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ) as error:
        raise ValueError('request body is invalid') from error
    return hashlib.sha256(canonical).hexdigest()


__all__ = [
    'CountingRobotWebProxy',
    'RobotWebProxyCounts',
    'is_literal_loopback_origin',
    'request_body_digest',
]
