"""Real local HTTP connections exercise reuse, isolation and failure bounds."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import shutil
import socket
import ssl
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from malbut_agent_server.providers.base import ProviderError, close_providers
from malbut_agent_server.providers.http_pool import HTTPConnectionPool
from malbut_agent_server.providers.openai_responses import OpenAIResponsesProvider
from malbut_agent_server.providers.reliable import (
    ProviderFailureCode, ReliableProvider, classify_exception,
)


@pytest.fixture(autouse=True)
def direct_requests(monkeypatch):
    """Keep local tests independent of the developer's network settings."""
    monkeypatch.setattr('urllib.request.getproxies', lambda: {})


@contextmanager
def local_server(responder, *, tls_context=None):
    calls = []

    class Server(ThreadingHTTPServer):
        accepted = 0
        daemon_threads = True

        def get_request(self):
            request = super().get_request()
            self.accepted += 1
            return request

    class Handler(BaseHTTPRequestHandler):
        protocol_version = 'HTTP/1.1'

        def log_message(self, *args):
            pass

        def do_POST(self):
            request = SimpleNamespace(
                body=json.loads(self.rfile.read(int(self.headers['Content-Length']))),
                authorization=self.headers.get('Authorization'),
                path=self.path, port=self.client_address[1],
            )
            calls.append(request)
            try:
                responder(self, request)
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = Server(('127.0.0.1', 0), Handler)
    if tls_context is not None:
        server.socket = tls_context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01),
                              daemon=True)
    thread.start()
    scheme = 'https' if tls_context is not None else 'http'
    server.url = f'{scheme}://localhost:{server.server_port}/v1/responses'
    server.calls = calls
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def respond(handler, value, status=200, *, extra_headers=None):
    body = value if isinstance(value, bytes) else json.dumps(value).encode()
    handler.send_response(status)
    handler.send_header('Content-Length', str(len(body)))
    for name, value in (extra_headers or {}).items():
        handler.send_header(name, value)
    handler.end_headers()
    handler.wfile.write(body)
    handler.wfile.flush()


@pytest.fixture
def provider():
    adapter = OpenAIResponsesProvider(api_key='local-test-only', model='test-model')
    yield adapter
    adapter.close()


def test_sequential_calls_reuse_socket_with_fresh_headers_and_payload(provider):
    """A connection retains no prior authorization, image or Unicode text."""
    with local_server(lambda h, req: respond(h, req.body)) as server:
        for index in range(3):
            payload = {'text': f'새 질문 {index} 📦', 'image': f'frame-{index}'}
            assert provider.transport(server.url + '?trial=1',
                                      {'Authorization': f'Bearer test-{index}'},
                                      payload, 2) == payload
        assert server.accepted == 1
        assert [c.authorization for c in server.calls] == [f'Bearer test-{n}' for n in range(3)]
        assert all(c.path.endswith('?trial=1') for c in server.calls)


def test_concurrent_calls_never_share_an_inflight_connection(provider):
    """Two overlapping replies remain bound to their own request."""
    barrier = threading.Barrier(2)

    def reply(handler, request):
        barrier.wait(timeout=2)
        respond(handler, request.body)

    with local_server(reply) as server, ThreadPoolExecutor(2) as workers:
        futures = [workers.submit(provider.transport, server.url, {}, {'n': n}, 2)
                   for n in range(2)]
        assert [f.result(timeout=3) for f in futures] == [{'n': 0}, {'n': 1}]
        assert server.accepted == 2


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308, 401, 429, 500])
def test_status_errors_do_not_redirect_or_retry(provider, status):
    """No redirect or transient error creates another POST behind our back."""
    with local_server(lambda h, req: respond(h, {})) as sink:
        with local_server(lambda h, req: respond(
                h, {'error': {'code': 'rate_limit'}}, status,
                extra_headers={'Location': sink.url})) as origin:
            with pytest.raises(ProviderError, match=f'HTTP status {status}'):
                provider.transport(origin.url, {'Authorization': 'Bearer local'}, {}, 2)
            assert len(origin.calls) == 1
            assert not sink.calls


def test_insufficient_quota_stays_nonretryable(provider):
    with local_server(lambda h, req: respond(
            h, {'error': {'code': 'insufficient_quota'}}, 429)) as server:
        with pytest.raises(ProviderError) as caught:
            provider.transport(server.url, {}, {}, 2)
        assert classify_exception(caught.value).code is ProviderFailureCode.QUOTA
        assert len(server.calls) == 1


def test_connection_lost_after_post_is_not_replayed(provider):
    """A server may have accepted an operation before disconnecting."""
    def reply(handler, request):
        if request.body.get('drop'):
            handler.close_connection = True
            handler.connection.shutdown(socket.SHUT_RDWR)
            handler.connection.close()
        else:
            respond(handler, {'ok': True})

    with local_server(reply) as server:
        assert provider.transport(server.url, {}, {}, 2) == {'ok': True}
        with pytest.raises(ProviderError, match='network error'):
            provider.transport(server.url, {}, {'drop': True}, 2)
        assert len(server.calls) == 2
        assert provider.transport(server.url, {}, {}, 2) == {'ok': True}
        assert len(server.calls) == 3
        assert server.accepted == 2


@pytest.mark.parametrize('response,expected', [
    (b'{"ok":true}', None),
    (b'{"big":"' + b'x' * 130 + b'"}', 'size limit'),
    (b'{"value":NaN}', 'not valid JSON'),
    (b'{"value":Infinity}', 'not valid JSON'),
    (b'\xff', 'not valid JSON'),
    (b'[]', 'must be an object'),
])
def test_response_bounds_and_json_contract(provider, monkeypatch, response, expected):
    monkeypatch.setattr('malbut_agent_server.providers.openai_responses.'
                        'MAX_PROVIDER_RESPONSE_BYTES', 128)
    with local_server(lambda h, req: respond(h, response)) as server:
        if expected is None:
            assert provider.transport(server.url, {}, {}, 2) == {'ok': True}
        else:
            with pytest.raises(ProviderError, match=expected):
                provider.transport(server.url, {}, {}, 2)
        assert len(server.calls) == 1


def test_oversized_body_discards_connection_before_next_request(provider, monkeypatch):
    monkeypatch.setattr('malbut_agent_server.providers.openai_responses.'
                        'MAX_PROVIDER_RESPONSE_BYTES', 64)
    with local_server(lambda h, req: respond(
            h, b'x' * 256 if req.body else b'{"ok":true}')) as server:
        with pytest.raises(ProviderError, match='size limit'):
            provider.transport(server.url, {}, {'big': True}, 2)
        assert provider.transport(server.url, {}, {}, 2) == {'ok': True}
        assert server.accepted == 2


def test_truncated_body_discards_connection(provider):
    def reply(handler, request):
        handler.send_response(200)
        handler.send_header('Content-Length', '50')
        handler.end_headers()
        handler.wfile.write(b'{"ok":true}')
        handler.wfile.flush()
        handler.close_connection = True

    with local_server(reply) as server:
        with pytest.raises(ProviderError, match='network error') as caught:
            provider.transport(server.url, {}, {}, 2)
        assert classify_exception(caught.value).code is ProviderFailureCode.NETWORK
        assert len(server.calls) == 1


def test_timeout_discards_socket_without_replaying(provider):
    release = threading.Event()

    def reply(handler, request):
        release.wait(timeout=2)
        respond(handler, {})

    with local_server(reply) as server:
        try:
            with pytest.raises(ProviderError) as caught:
                provider.transport(server.url, {}, {}, .05)
            assert classify_exception(caught.value).code is ProviderFailureCode.TIMEOUT
            assert len(server.calls) == 1
        finally:
            release.set()


def test_proxy_requests_keep_existing_urllib_path(provider, monkeypatch):
    """Configured proxies are not silently bypassed by direct connections."""
    seen = []
    monkeypatch.setattr('urllib.request.getproxies', lambda: {'https': 'http://proxy.test:3128'})
    monkeypatch.setattr('urllib.request.proxy_bypass', lambda host: False)
    monkeypatch.setattr(provider, '_urllib_transport', lambda *args: seen.append(args) or {})
    assert provider.transport('https://api.openai.com/v1/responses', {}, {'n': 1}, 2) == {}
    assert len(seen) == 1
    provider.close()
    with pytest.raises(ProviderError):
        provider.transport('https://api.openai.com/v1/responses', {}, {}, 2)
    assert len(seen) == 1


def test_proxy_bypass_allows_direct_connection(provider, monkeypatch):
    monkeypatch.setattr('urllib.request.getproxies', lambda: {'http': 'http://proxy.test:3128'})
    monkeypatch.setattr('urllib.request.proxy_bypass', lambda host: True)
    with local_server(lambda h, req: respond(h, {})) as server:
        provider.transport(server.url, {}, {}, 2)
        provider.transport(server.url, {}, {}, 2)
        assert server.accepted == 1


def test_real_http_proxy_preserves_absolute_target(provider, monkeypatch):
    with local_server(lambda h, req: respond(h, {'via': 'proxy'})) as proxy:
        monkeypatch.setattr('urllib.request.getproxies', lambda: {'http': proxy.url})
        monkeypatch.setattr('urllib.request.proxy_bypass', lambda host: False)
        target = 'http://unresolved.invalid/v1/responses'
        assert provider.transport(target, {}, {}, 2) == {'via': 'proxy'}
        assert [c.path for c in proxy.calls] == [target]


@pytest.mark.parametrize('size', [64, 65])
def test_response_byte_limit_boundary(provider, monkeypatch, size):
    monkeypatch.setattr('malbut_agent_server.providers.openai_responses.'
                        'MAX_PROVIDER_RESPONSE_BYTES', 64)
    body = b'{"x":"' + b'a' * (size - 8) + b'"}'
    with local_server(lambda h, req: respond(h, body)) as server:
        if size == 64:
            assert provider.transport(server.url, {}, {}, 2) == json.loads(body)
        else:
            with pytest.raises(ProviderError, match='size limit'):
                provider.transport(server.url, {}, {}, 2)


def test_chunked_response_can_reuse_connection(provider):
    def reply(handler, request):
        handler.send_response(200)
        handler.send_header('Transfer-Encoding', 'chunked')
        handler.end_headers()
        handler.wfile.write(b'7\r\n{"n":1}\r\n0\r\n\r\n')
        handler.wfile.flush()

    with local_server(reply) as server:
        assert provider.transport(server.url, {}, {}, 2) == {'n': 1}
        assert provider.transport(server.url, {}, {}, 2) == {'n': 1}
        assert server.accepted == 1


def test_runtime_shutdown_closes_all_owned_providers_after_workers(tmp_path):
    """Shutdown releases foreground, background and auxiliary idle sockets."""
    from malbut_agent_server.config import Settings
    from malbut_agent_server.factory import build_orchestrator

    runtime = build_orchestrator(Settings(database_path=str(tmp_path / 'agent.sqlite3')))
    seen = []
    def owned(name):
        return SimpleNamespace(close=lambda: seen.append(name))

    runtime.provider = owned('foreground')
    runtime.memory_source_reviewer = SimpleNamespace(provider=owned('review'))
    runtime.automatic_memory_extractor = SimpleNamespace(provider=owned('extract'))
    runtime.speech_addressee = SimpleNamespace(provider=owned('addressee'))
    runtime.companion_provider = owned('companion')
    stop = runtime.stop_background_memory

    def stop_workers():
        seen.append('workers')
        stop()

    runtime.stop_background_memory = stop_workers
    runtime.close()
    runtime.close()
    assert seen == ['workers', 'foreground', 'review', 'extract', 'addressee', 'companion']


def test_close_wakes_pool_waiter_and_does_not_reopen_after_active_return():
    """Pool bounds and shutdown hold while another POST is still running."""
    arrived, release = threading.Event(), threading.Event()
    pool = HTTPConnectionPool(max_connections=1)

    def reply(handler, request):
        arrived.set()
        release.wait(timeout=2)
        respond(handler, {})

    with local_server(reply) as server, ThreadPoolExecutor(2) as workers:
        first = workers.submit(pool.post, server.url, {}, b'{}', 2, 64)
        assert arrived.wait(timeout=1)
        second = workers.submit(pool.post, server.url, {}, b'{}', 2, 64)
        pool.close()
        try:
            with pytest.raises(OSError, match='closed'):
                second.result(timeout=1)
        finally:
            release.set()
        assert first.result(timeout=2) == b'{}'
        assert len(server.calls) == 1
        assert pool._count == 0
        pool.close()
        with pytest.raises(OSError, match='closed'):
            pool.post(server.url, {}, b'{}', 1, 64)


def test_expired_idle_socket_is_replaced_without_extra_post(provider):
    provider._http_pool._idle_timeout = .01
    with local_server(lambda h, req: respond(h, {})) as server:
        provider.transport(server.url, {}, {}, 2)
        time.sleep(.02)
        provider.transport(server.url, {}, {}, 2)
        assert len(server.calls) == 2
        assert server.accepted == 2


def test_pool_capacity_wait_is_bounded_without_sending_an_extra_post():
    arrived, release = threading.Event(), threading.Event()
    pool = HTTPConnectionPool(max_connections=1)

    def reply(handler, request):
        arrived.set()
        release.wait(timeout=2)
        respond(handler, {})

    with local_server(reply) as server, ThreadPoolExecutor(1) as workers:
        first = workers.submit(pool.post, server.url, {}, b'{}', 2, 64)
        assert arrived.wait(timeout=1)
        try:
            with pytest.raises(TimeoutError, match='lease timed out'):
                pool.post(server.url, {}, b'{}', .03, 64)
            assert len(server.calls) == 1
        finally:
            release.set()
            first.result(timeout=2)
            pool.close()


def test_tls_verification_remains_enabled_and_trusted_tls_reuses(provider, tmp_path, monkeypatch):
    """TLS rejects untrusted certificates before receiving any POST."""
    executable = shutil.which('openssl')
    if executable is None:
        pytest.skip('local TLS certificate fixture requires openssl')
    cert, key = tmp_path / 'cert.pem', tmp_path / 'key.pem'
    subprocess.run([executable, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
                    '-days', '1', '-subj', '/CN=localhost', '-addext', 'subjectAltName=DNS:localhost',
                    '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    with local_server(lambda h, req: respond(h, {}), tls_context=context) as server:
        with pytest.raises(ProviderError, match='network error'):
            provider.transport(server.url, {}, {}, 2)
        assert not server.calls
        trust = ssl.create_default_context(cafile=str(cert))
        monkeypatch.setattr(ssl, '_create_default_https_context', lambda: trust)
        provider.transport(server.url, {}, {}, 2)
        provider.transport(server.url, {}, {}, 2)
        assert len(server.calls) == 2
        assert server.accepted == 1


def test_close_propagates_through_reliability_and_preserves_injected_ownership(provider):
    ReliableProvider([provider, provider]).close()
    with pytest.raises(ProviderError, match='network error'):
        provider.transport('https://api.openai.com/v1/responses', {}, {}, 1)
    injected = SimpleNamespace(close=lambda: pytest.fail('caller-owned transport was closed'))
    adapter = OpenAIResponsesProvider(api_key='test', model='test', transport=injected)
    adapter.close()
    seen = []
    owned = SimpleNamespace(close=lambda: seen.append('closed'))
    close_providers(owned, owned, None)
    assert seen == ['closed']
