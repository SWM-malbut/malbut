"""Exercise persistent cloud HTTP/TLS without internet, ROS, or robot movement."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shutil
import socket
import ssl
import subprocess
from threading import Event, Thread
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from malbut_bringup import cloud_sync
from malbut_bringup.cloud_sync import CloudClient, CloudError, CloudSync
from malbut_bringup.web_panel import PanelData


TOKEN = 'hc1.11111111-1111-4111-8111-111111111111.' + 'a' * 64
API = '/api/device/v1/robot/'


@contextmanager
def server(*, tls=None):
    """Use real HTTP framing and keep-alive with controlled failure responses."""
    requests = []
    disconnected = Event()
    waiting = Event()

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        accepted = 0

        def get_request(self):
            connection, address = super().get_request()
            self.accepted += 1
            return connection, address

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def setup(self):
            super().setup()
            self.connection.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

        def log_message(self, *_args):
            pass

        def do_POST(self):
            self.do_GET()

        def do_PUT(self):
            self.do_GET()

        def do_GET(self):
            body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            requests.append((self.path, self.command, self.headers.get('Authorization'), body))
            mode = self.path.rsplit('/', 1)[-1]
            if mode == 'drop':
                self.close_connection = True
                return
            if mode in ('timeout', 'slow-body', 'closing-body'):
                if mode != 'timeout':
                    self.send_response(200)
                    self.send_header('Content-Length', 100)
                    if mode == 'closing-body':
                        self.send_header('Connection', 'close')
                    self.end_headers()
                    self.wfile.write(b'{')
                    self.wfile.flush()
                waiting.set()
                time.sleep(.15)
                self.close_connection = True
                return
            status = 302 if mode == 'redirect' else 401 if mode == 'unauthorized' else 200
            raw = json.dumps({'path': self.path, 'body': body.decode()}).encode()
            if mode in ('truncated', 'chunk-truncated'):
                raw = b'{}'
            elif mode == 'invalid':
                raw = b'invalid ' + TOKEN.encode()
            elif mode == 'array':
                raw = b'[]'
            elif mode == 'oversized':
                raw = b'x' * (256 * 1024 + 1)
            self.send_response(status)
            if mode == 'redirect':
                self.send_header('Location', API + 'redirect-target')
            if mode == 'chunk-truncated':
                self.send_header('Transfer-Encoding', 'chunked')
                raw = b'2\r\n{}\r\n'  # Missing terminating zero-sized chunk.
            else:
                self.send_header('Content-Length', len(raw) + (7 if mode == 'truncated' else 0))
            if mode == 'close':
                self.send_header('Connection', 'close')
            self.end_headers()
            try:
                self.wfile.write(raw)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return
            if mode in ('stale', 'truncated', 'chunk-truncated', 'close'):
                self.close_connection = True
                self.connection.shutdown(socket.SHUT_WR)
                disconnected.set()

    httpd = Server(('127.0.0.1', 0), Handler)
    if tls is not None:
        httpd.socket = tls.wrap_socket(httpd.socket, server_side=True)
    thread = Thread(target=httpd.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
    thread.start()
    scheme = 'https' if tls is not None else 'http'
    try:
        yield SimpleNamespace(url=f'{scheme}://localhost:{httpd.server_port}',
                              server=httpd, requests=requests, disconnected=disconnected,
                              waiting=waiting)
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=2)


def test_sequential_and_concurrent_requests_share_connection_without_mixing_bodies():
    """State uploads and command polls use one connection, including concurrent calls."""
    with server() as backend:
        client = CloudClient(backend.url, TOKEN)
        try:
            def request(number):
                path = API + str(number)
                result = client.request(path, 'POST', {'sequence': number})
                assert result == {'path': path, 'body': json.dumps(
                    {'sequence': number}, separators=(',', ':'))}

            request(0)
            request(1)
            with ThreadPoolExecutor(max_workers=4) as workers:
                list(workers.map(request, range(2, 14)))
            assert backend.server.accepted == 1
            assert len(backend.requests) == 14
            assert all(item[2] == 'Bearer ' + TOKEN for item in backend.requests)
            assert client.opener._connection.sock is not None
        finally:
            client.close()
        assert client.opener._connection is None
        client.close()
        with pytest.raises(CloudError, match='request failed'):
            client.request(API + 'after-close')
        assert len(backend.requests) == 14


def test_server_connection_close_reconnects_on_next_request():
    with server() as backend:
        client = CloudClient(backend.url, TOKEN)
        try:
            client.request(API + 'close')
            client.request(API + 'next')
            assert backend.server.accepted == 2
        finally:
            client.close()


@pytest.mark.parametrize('method', ['GET', 'POST'])
def test_stale_connection_is_not_replayed_and_next_poll_reconnects(method):
    """GET /commands can claim work too; transport must never replay any method."""
    with server() as backend:
        client = CloudClient(backend.url, TOKEN)
        try:
            client.request(API + 'stale')
            assert backend.disconnected.wait(1)
            with pytest.raises(CloudError, match='request failed'):
                client.request(API + 'commands', method)
            assert len(backend.requests) == 1
            assert backend.server.accepted == 1
            client.request(API + 'next')
            assert backend.server.accepted == 2
        finally:
            client.close()


def test_lost_post_response_never_duplicates_a_received_command():
    with server() as backend:
        client = CloudClient(backend.url, TOKEN)
        try:
            with pytest.raises(CloudError, match='request failed'):
                client.request(API + 'drop', 'POST', {'completed': True})
            assert len(backend.requests) == 1
            assert backend.requests[0][3] == b'{"completed":true}'
            client.request(API + 'next')
            assert backend.server.accepted == 2
        finally:
            client.close()


@pytest.mark.parametrize('mode, diagnostic, status', [
    ('truncated', 'request failed', None),
    ('chunk-truncated', 'request failed', None),
    ('invalid', 'not valid JSON', None),
    ('array', 'JSON object', None),
    ('oversized', 'size limit', None),
    ('unauthorized', 'HTTP 401', 401),
    ('redirect', 'HTTP 302', 302),
])
def test_invalid_response_discards_connection_and_does_not_expose_credentials(
        mode, diagnostic, status):
    with server() as backend:
        client = CloudClient(backend.url, TOKEN)
        try:
            with pytest.raises(CloudError, match=diagnostic) as result:
                client.request(API + mode)
            assert result.value.status == status
            assert TOKEN not in str(result.value)
            assert len(backend.requests) == 1  # Redirect target was never contacted.
            client.request(API + 'next')
            assert backend.server.accepted == 2
        finally:
            client.close()


def test_socket_timeout_is_bounded_reported_and_discards_connection():
    with server() as backend:
        client = CloudClient(backend.url, TOKEN, timeout=.05)
        try:
            started = time.monotonic()
            with pytest.raises(CloudError) as result:
                client.request(API + 'timeout')
            assert result.value.timeout
            assert time.monotonic() - started < 1
            client.request(API + 'next')
            assert backend.server.accepted == 2
        finally:
            client.close()


@pytest.mark.parametrize('mode', ['timeout', 'slow-body', 'closing-body'])
def test_shutdown_interrupts_inflight_response_without_reopening_connection(mode):
    with server() as backend:
        client = CloudClient(backend.url, TOKEN, timeout=8)
        with ThreadPoolExecutor(max_workers=1) as workers:
            pending = workers.submit(client.request, API + mode)
            assert backend.waiting.wait(1)
            started = time.monotonic()
            client.close()
            assert time.monotonic() - started < .5
            with pytest.raises(CloudError) as result:
                pending.result(timeout=1)
            assert TOKEN not in str(result.value)
            assert backend.server.accepted == 1
            assert client.opener._connection is None


def test_request_size_and_path_validation_do_not_send_oversized_payloads():
    with server() as backend:
        client = CloudClient(backend.url, TOKEN)
        try:
            with pytest.raises(CloudError, match='size limit'):
                client.request(API + 'state', 'POST', {'data': 'x' * (64 * 1024)})
            with pytest.raises(ValueError, match='API path'):
                client.request('//other.example/api/device/v1/robot/state')
            assert not backend.requests and backend.server.accepted == 0
        finally:
            client.close()


@pytest.fixture
def certificate(tmp_path):
    """Generate a temporary loopback-only certificate; no private key is committed."""
    openssl = shutil.which('openssl')
    if openssl is None:
        pytest.skip('openssl is needed to generate the local TLS test certificate')
    key, cert = tmp_path / 'server.key', tmp_path / 'server.crt'
    subprocess.run([openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-keyout', str(key), '-out', str(cert), '-days', '1',
                    '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost'],
                   check=True, capture_output=True, timeout=10)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    return cert, context


def test_https_verifies_trust_and_hostname_then_reuses_authenticated_connection(
        certificate, monkeypatch):
    cert, tls = certificate
    with server(tls=tls) as backend:
        untrusted = CloudClient(backend.url, TOKEN)
        try:
            with pytest.raises(CloudError, match='request failed'):
                untrusted.request(API + 'untrusted')
            assert not backend.requests
        finally:
            untrusted.close()
        monkeypatch.setenv('SSL_CERT_FILE', str(cert))
        wrong_host = CloudClient(backend.url.replace('localhost', '127.0.0.1'), TOKEN)
        try:
            with pytest.raises(CloudError, match='request failed'):
                wrong_host.request(API + 'wrong-host')
            assert not backend.requests
        finally:
            wrong_host.close()
        client = CloudClient(backend.url, TOKEN)
        try:
            for index in range(4):
                client.request(API + str(index))
            assert backend.server.accepted == 1
            assert len(backend.requests) == 4
        finally:
            client.close()


def test_existing_proxy_routing_retains_no_redirect_policy(monkeypatch):
    opener = Mock()

    @contextmanager
    def opened(request, timeout):
        assert request.get_header('Authorization') == 'Bearer ' + TOKEN
        assert timeout == 8
        yield SimpleNamespace(read=lambda _limit: b'{}')

    opener.open = Mock(side_effect=opened)
    build = Mock(return_value=opener)
    monkeypatch.setattr(cloud_sync, 'getproxies', lambda: {'https': 'http://proxy.example'})
    monkeypatch.setattr(cloud_sync, 'proxy_bypass', lambda _host: False)
    monkeypatch.setattr(cloud_sync, 'build_opener', build)
    client = CloudClient('https://robot.example', TOKEN)
    assert isinstance(build.call_args.args[0], cloud_sync.NoRedirect)
    assert client.request(API + 'state', 'POST', {}) == {}
    client.close()
    opener.close.assert_called_once()


def test_sync_shutdown_releases_transport_even_without_starting_worker():
    client = Mock()
    sync = CloudSync(SimpleNamespace(data=PanelData(), catalog=Mock(), node=Mock()), client)
    sync.close()
    assert sync.stop_event.is_set()
    client.close.assert_called_once()
