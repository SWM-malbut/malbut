"""Offline cloud-stream contract tests; no credentials or network required."""

import asyncio
from threading import Event, Thread
import time
from types import SimpleNamespace

import numpy as np
import pytest

from malbut_tts.api_synthesis import ApiTtsError, OpenAISynthesizer
from malbut_tts.runtime import SpeechRuntime


class Client:
    def __init__(self, chunks, *, mime='audio/pcm', stalled=None, error=None):
        self.chunks = chunks
        self.headers = {'content-type': mime}
        self.stalled = stalled
        self.error = error
        self.entered = Event()
        self.iterating = Event()
        self.context_closed = False
        self.closed = False
        self.calls = []
        self.audio = SimpleNamespace(speech=SimpleNamespace(
            with_streaming_response=self))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self

    async def __aenter__(self):
        self.entered.set()
        if self.stalled == 'headers':
            await asyncio.Event().wait()
        if self.error is not None:
            raise self.error
        return self

    async def __aexit__(self, *args):
        self.context_closed = True

    async def close(self):
        self.closed = True

    async def iter_bytes(self, *, chunk_size):
        assert chunk_size == 4800
        self.iterating.set()
        for chunk in self.chunks:
            yield chunk
        if self.stalled == 'body':
            await asyncio.Event().wait()


def engine(client, **kwargs):
    options = []

    def factory(**kw):
        options.append(kw)
        return client

    return OpenAISynthesizer(
        api_key='private-test-key', client_factory=factory, **kwargs), options


def test_pcm_alignment_streaming_and_one_request():
    client = Client([b'\x00', b'\x80\xff\x7f\x00', b'\x00'])
    synth, options = engine(client)
    output = list(synth.generate('공개 시험 문장', Event()))
    np.testing.assert_array_equal(np.concatenate([x for x, _ in output]),
                                  [-1.0, 32767 / 32768, 0.0])
    assert all(x.dtype == np.float32 and x.ndim == 1 for x, _ in output)
    assert [rate for _, rate in output] == [24000]
    assert client.calls == [{'model': 'gpt-4o-mini-tts', 'voice': 'marin',
                             'input': '공개 시험 문장', 'response_format': 'pcm'}]
    assert options == [{'api_key': 'private-test-key',
                        'base_url': 'https://api.openai.com/v1',
                        'max_retries': 0, 'timeout': 8.0}]
    assert client.closed and client.context_closed


def test_first_pcm_yield_does_not_wait_for_complete_body():
    client = Client([b'\x00\x10' * 9600], stalled='body')
    synth, _ = engine(client)
    chunks = synth.generate('시험', Event())
    audio, rate = next(chunks)
    assert rate == 24000 and len(audio) == 9600
    assert not client.closed
    chunks.close()
    assert client.closed and client.context_closed


@pytest.mark.parametrize('chunks,mime,code', [
    ([], 'audio/pcm', 'empty_audio'),
    ([b'\x00'], 'audio/pcm', 'invalid_audio'),
    ([b'\x00\x00'], 'application/json', 'invalid_audio'),
    (['private body'], 'audio/pcm', 'invalid_audio'),
])
def test_invalid_stream_fails_closed(chunks, mime, code):
    client = Client(chunks, mime=mime)
    synth, _ = engine(client)
    with pytest.raises(ApiTtsError, match='^API TTS failed: ' + code + '$'):
        list(synth.generate('private text', Event()))
    assert client.closed and client.context_closed
    assert len(client.calls) == 1


def test_size_limit():
    client = Client([b'\x00\x00' * 3])
    synth, _ = engine(client)
    synth.max_audio_bytes = 4
    with pytest.raises(ApiTtsError, match='response_too_large'):
        list(synth.generate('시험', Event()))
    assert client.closed


@pytest.mark.parametrize('value', ['', '   ', None, 7, '가' * 4097])
def test_invalid_input_never_requests(value):
    client = Client([])
    synth, options = engine(client)
    with pytest.raises(ApiTtsError, match='invalid_request'):
        list(synth.generate(value, Event()))
    assert not options and not client.calls


@pytest.mark.parametrize('kwargs', [
    {'voice': ''}, {'voice': 'bad\nvoice'}, {'model': None},
    {'timeout_seconds': 0}, {'timeout_seconds': True},
    {'timeout_seconds': float('nan')}, {'timeout_seconds': 61},
])
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        OpenAISynthesizer(**kwargs)


def test_load_only_checks_key_without_network(monkeypatch):
    client = Client([])
    synth, options = engine(client)
    synth.load()
    assert not options
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    with pytest.raises(ApiTtsError, match='missing_api_key'):
        OpenAISynthesizer(client_factory=object).load()
    monkeypatch.setenv('OPENAI_API_KEY', 'environment-test-key')
    OpenAISynthesizer(client_factory=object).load()


@pytest.mark.parametrize('status,code', [
    (401, 'authentication_failed'), (403, 'permission_denied'),
    (429, 'rate_limited'), (500, 'provider_error'), (400, 'invalid_request'),
])
def test_errors_are_sanitized_and_not_retried(status, code):
    error = RuntimeError('secret key and private response body')
    error.status_code = status
    client = Client([], error=error)
    synth, _ = engine(client)
    with pytest.raises(ApiTtsError) as caught:
        list(synth.generate('private input', Event()))
    assert str(caught.value) == 'API TTS failed: ' + code
    assert len(client.calls) == 1 and client.closed
    assert caught.value.__suppress_context__


@pytest.mark.parametrize('stage', ['headers', 'body'])
def test_cancellation_interrupts_stalled_network(stage):
    client = Client([], stalled=stage)
    synth, _ = engine(client)
    cancel = Event()
    errors = []

    def run():
        try:
            assert list(synth.generate('시험', cancel)) == []
        except BaseException as error:
            errors.append(error)

    thread = Thread(target=run)
    thread.start()
    assert (client.entered if stage == 'headers' else client.iterating).wait(1)
    started = time.monotonic()
    cancel.set()
    thread.join(1)
    assert not thread.is_alive() and not errors
    assert time.monotonic() - started < 1
    assert client.closed


def test_pre_cancelled_request_does_not_load_or_connect():
    client = Client([])
    synth, options = engine(client)
    cancel = Event()
    cancel.set()
    assert list(synth.generate('시험', cancel)) == []
    assert options == []


def test_cancel_after_first_chunk_discards_remaining_pcm():
    client = Client([b'\x00\x10' * 9600, b'\x00\x20' * 2400])
    synth, _ = engine(client)
    cancel = Event()
    chunks = synth.generate('시험', cancel)
    assert len(next(chunks)[0]) == 9600
    cancel.set()
    assert list(chunks) == []
    assert client.closed and client.context_closed


def test_initial_buffer_retains_all_samples_across_many_chunks():
    client = Client([b'\x00\x10' * 2400] * 5)
    synth, _ = engine(client)
    output = list(synth.generate('시험', Event()))
    assert [len(pcm) for pcm, _ in output] == [9600, 2400]
    np.testing.assert_array_equal(np.concatenate([pcm for pcm, _ in output]),
                                  np.full(12000, 0.125, dtype=np.float32))


@pytest.mark.parametrize('resource', ['context', 'client'])
def test_cleanup_errors_sanitized(resource):
    class BrokenClient(Client):
        async def __aexit__(self, *args):
            self.context_closed = True
            if resource == 'context':
                raise RuntimeError('private provider detail')

        async def close(self):
            self.closed = True
            if resource == 'client':
                raise RuntimeError('private provider detail')

    client = BrokenClient([b'\x00\x00'])
    synth, _ = engine(client)
    with pytest.raises(ApiTtsError, match='^API TTS failed: cleanup_failed$'):
        list(synth.generate('시험', Event()))
    assert client.closed and client.context_closed


@pytest.mark.parametrize('stage', ['headers', 'body'])
def test_timeout_closes_and_sanitizes(stage):
    client = Client([], stalled=stage)
    synth, _ = engine(client, timeout_seconds=0.1)
    with pytest.raises(ApiTtsError, match='^API TTS failed: timeout$'):
        list(synth.generate('시험', Event()))
    assert client.closed and len(client.calls) == 1


def test_partial_failure_does_not_finish_and_next_request_recovers():
    first = Client([b'\x00\x10', b'\x00'])
    second = Client([b'\x00\x10'])
    clients = iter([first, second])
    synth = OpenAISynthesizer(api_key='test', client_factory=lambda **kw: next(clients))
    statuses = []
    done = Event()
    players = []

    class Player:
        def __init__(self, **kwargs):
            self.finished = False
            self.closed = False
            players.append(self)

        def write(self, audio, rate):
            pass

        def finish(self):
            self.finished = True

        def close(self):
            self.closed = True

    def status(pid, state):
        statuses.append((pid, state))
        if len(statuses) == 2:
            done.set()

    runtime = SpeechRuntime(synth, Player, status)
    try:
        one = runtime.submit('하나')
        two = runtime.submit('둘')
        assert done.wait(2)
        assert statuses == [(one, 'failed'), (two, 'finished')]
        assert [p.finished for p in players] == [False, True]
        assert all(p.closed for p in players)
    finally:
        runtime.close()
