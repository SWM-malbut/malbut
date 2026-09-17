"""Exercise the actual optional OpenAI SDK over an offline mock transport."""

import asyncio
from threading import Event, Thread
import time

import numpy as np
import pytest

from malbut_tts.api_synthesis import ApiTtsError, OpenAISynthesizer

httpx = pytest.importorskip('httpx')
openai = pytest.importorskip('openai')


class _PCMStream(httpx.AsyncByteStream):
    def __init__(self, *, block_body=False):
        self.closed = False
        self.block_body = block_body
        self.body_started = Event()

    async def __aiter__(self):
        # The real adapter retains 400 ms initially; keep that policy enabled
        # while testing actual SDK streaming and mid-body cancellation.
        yield b'\x01\x00' * 9600
        self.body_started.set()
        if self.block_body:
            await asyncio.sleep(60)
        yield b'\xff\xff' * 1200

    async def aclose(self):
        self.closed = True


@pytest.mark.parametrize('case', [
    'success', 'cancel_headers', 'cancel_body', 'rate_limit',
])
def test_real_async_sdk_stream_and_cleanup_without_network(case):
    cancel = Event()
    stream = _PCMStream(block_body=case == 'cancel_body')
    request_started = Event()
    calls, clients = [], []

    async def handler(request):
        calls.append(request)
        request_started.set()
        if case == 'cancel_headers':
            await asyncio.sleep(60)
        if case == 'rate_limit':
            return httpx.Response(
                429, json={'error': {'message': 'PRIVATE_RESPONSE_SENTINEL'}})
        return httpx.Response(
            200, headers={'content-type': 'audio/pcm'}, stream=stream)

    def client_factory(**kwargs):
        assert kwargs['base_url'] == 'https://api.openai.com/v1'
        assert kwargs['max_retries'] == 0
        client = openai.AsyncOpenAI(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            **kwargs)
        clients.append(client)
        return client

    synthesizer = OpenAISynthesizer(
        api_key='offline-test-key', client_factory=client_factory,
        timeout_seconds=2)
    thread = None
    if case in ('cancel_headers', 'cancel_body'):
        started = request_started if case == 'cancel_headers' else stream.body_started

        def do_cancel():
            started.wait(3)
            cancel.set()

        thread = Thread(target=do_cancel)
        thread.start()

    began = time.monotonic()
    try:
        if case == 'rate_limit':
            with pytest.raises(ApiTtsError, match='^API TTS failed: rate_limited$'):
                list(synthesizer.generate('안녕하세요.', cancel))
        else:
            chunks = list(synthesizer.generate('안녕하세요.', cancel))
            if case == 'success':
                assert len(chunks) == 2
                assert sum(len(chunk) for chunk, _ in chunks) == 10800
                assert all(rate == 24000 for _, rate in chunks)
                assert all(chunk.dtype == np.float32 for chunk, _ in chunks)
                np.testing.assert_allclose(chunks[0][0], 1 / 32768)
                np.testing.assert_allclose(chunks[1][0], -1 / 32768)
            elif case == 'cancel_headers':
                assert chunks == []
            else:
                assert len(chunks) == 1
            if case in ('cancel_headers', 'cancel_body'):
                assert cancel.is_set()
                assert time.monotonic() - began < 1.5
    finally:
        cancel.set()
        request_started.set()
        stream.body_started.set()
        if thread is not None:
            thread.join(3)
            assert not thread.is_alive()

    assert len(calls) == 1
    assert calls[0].url == 'https://api.openai.com/v1/audio/speech'
    assert len(clients) == 1 and clients[0].is_closed()
    if case in ('success', 'cancel_body'):
        assert stream.closed
