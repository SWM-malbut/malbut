"""No credentials, remote calls, paid inference or dataset evaluation."""

import asyncio
import base64
from dataclasses import replace
import io
import json
from unittest.mock import patch

from PIL import Image
import pytest

from malbut_agent_server.adapters.outbound.ollama_cloud_fall import (
    ENDPOINT, MAX_RESPONSE_BYTES, OllamaCloudFallProvider, build_payload, parse_reply,
)
from malbut_agent_server.domain.fall_monitoring import (
    CloudFallRequest, FrameWindow, RgbFrame, SensorSummary, VideoAssessment,
)
from malbut_agent_server.ports.cloud_fall import CloudFallProviderError


def jpeg():
    image = Image.new('RGB', (640, 400), 'gray')
    exif = Image.Exif()
    exif[270] = 'private image metadata'
    output = io.BytesIO()
    image.save(output, 'JPEG', exif=exif)
    return output.getvalue()


def request():
    return CloudFallRequest(
        'private-request', 'incident', 'private-device', 'private-boot',
        'private-incident', 'private-track', 1,
        FrameWindow((RgbFrame(90, jpeg()), RgbFrame(100, jpeg())), 90, 100, False),
        SensorSummary(99, floor_distance_m=0.2))


def response(content=None, **extra):
    if content is None:
        content = json.dumps({'assessment': 'suspected_fall', 'explanation': '바닥에 누워 있음'})
    return json.dumps(dict(message={'role': 'assistant', 'content': content},
                           done=True, done_reason='stop', **extra)).encode()


def test_ordered_rgb_contract_preserves_aspect_and_strips_metadata_and_ids():
    body = build_payload(request(), model='gemma4:31b')
    payload = json.loads(body)
    assert payload['model'] == 'gemma4:31b'
    assert payload['stream'] is False and payload['think'] is False
    assert 'format' not in payload and 'tools' not in payload
    assert b'private-' not in body
    message = payload['messages'][1]
    assert '"offset_s":0' in message['content'] and '"offset_s":10' in message['content']
    assert '"floor_distance_m":0.2' in message['content']
    assert '"audio_included":false' in message['content']
    for value in message['images']:
        with Image.open(io.BytesIO(base64.b64decode(value))) as image:
            assert image.size == (640, 400)
            assert not image.getexif()


@pytest.mark.parametrize('frames', [
    (), (RgbFrame(100, jpeg()), RgbFrame(90, jpeg())), (RgbFrame(100, b'\xff\xd8bad\xff\xd9'),),
])
def test_invalid_images_never_reach_http(frames):
    req = replace(request(), window=FrameWindow(frames, 90, 100, False))
    with pytest.raises(CloudFallProviderError, match='cloud_input_invalid'):
        build_payload(req, model='gemma4:31b')


def test_only_complete_outer_code_fence_is_removed():
    value = '{"assessment":"observed_fall","explanation":"넘어짐"}'
    for content in (value, '```json\n' + value + '\n```', '```\n' + value + '\n```'):
        assert parse_reply(response(content)).assessment is VideoAssessment.OBSERVED_FALL
    with pytest.raises(CloudFallProviderError):
        parse_reply(response('Here is the result: ' + value))


@pytest.mark.parametrize('content', [
    '{"assessment":"normal_activity","assessment":"observed_fall","explanation":"x"}',
    '{"assessment":"normal_activity","explanation":"x","confidence":NaN}',
    '{"assessment":"normal_activity","explanation":"x","notify":true}',
    '{"assessment":"normal_activity","explanation":" "}',
    '{"assessment":"confirmed_fall","explanation":"x"}',
    '[]', 'null', '```json\n{}\n```\nextra',
])
def test_invalid_model_content_never_becomes_normal(content):
    with pytest.raises(CloudFallProviderError, match='cloud_invalid_response'):
        parse_reply(response(content))


@pytest.mark.parametrize('body', [
    b'null', b'[]', b'{"message":null,"done":true}', b'{"message":{},"done":false}', b'not JSON',
])
def test_malformed_provider_envelope_is_sanitized(body):
    with pytest.raises(CloudFallProviderError, match='cloud_invalid_response'):
        parse_reply(body)


class Response:
    def __init__(self, status=200, chunks=None, wait=None):
        self.status = status
        self.content_type = 'application/json'
        self.content_length = None
        self.content = self
        self.chunks = chunks if chunks is not None else [response()]
        self.wait = wait
        self.entered = asyncio.Event()
        self.closed = False

    async def __aenter__(self):
        self.entered.set()
        return self

    async def __aexit__(self, *args):
        self.closed = True

    async def iter_chunked(self, size):
        if self.wait:
            await self.wait.wait()
        for chunk in self.chunks:
            yield chunk


class Session:
    def __init__(self, response, **kwargs):
        self.response, self.settings, self.calls = response, kwargs, []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        self.closed = True

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_http_security_and_cancellation_close_connection():
    aiohttp = pytest.importorskip('aiohttp')

    async def run():
        reply = Response(wait=asyncio.Event())
        sessions = []

        def factory(**kwargs):
            session = Session(reply, **kwargs)
            sessions.append(session)
            return session

        provider = OllamaCloudFallProvider(model='gemma4:31b', api_key='secret-token')
        assert 'secret-token' not in repr(provider)
        with patch.object(aiohttp, 'ClientSession', factory):
            task = asyncio.create_task(provider.analyze(request()))
            await reply.entered.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        assert reply.closed and sessions[0].closed
        assert sessions[0].settings['trust_env'] is False
        assert sessions[0].settings['timeout'].total == 20
        url, options = sessions[0].calls[0]
        assert url == ENDPOINT and options['allow_redirects'] is False
        assert options['headers']['Authorization'] == 'Bearer secret-token'
    asyncio.run(run())


@pytest.mark.parametrize('status,code', [
    (401, 'cloud_auth_required'), (403, 'cloud_auth_required'),
    (402, 'cloud_payment_required'), (429, 'cloud_quota_exhausted'),
])
def test_auth_quota_payment_stop_without_retry(status, code):
    aiohttp = pytest.importorskip('aiohttp')
    session = Session(Response(status=status))
    provider = OllamaCloudFallProvider(model='gemma4:31b', api_key='secret-token')
    with patch.object(aiohttp, 'ClientSession', return_value=session):
        for _ in range(2):
            with pytest.raises(CloudFallProviderError, match=code):
                asyncio.run(provider.analyze(request()))
    assert len(session.calls) == 1


@pytest.mark.parametrize('reply,code', [
    (Response(status=302), 'cloud_http_error'),
    (Response(status=500), 'cloud_http_error'),
    (Response(chunks=[b'x' * 4096] * (MAX_RESPONSE_BYTES // 4096 + 1)),
     'cloud_invalid_response'),
])
def test_redirect_errors_and_oversize_replies_fail_closed(reply, code):
    aiohttp = pytest.importorskip('aiohttp')
    session = Session(reply)
    provider = OllamaCloudFallProvider(model='gemma4:31b', api_key='secret-token')
    with patch.object(aiohttp, 'ClientSession', return_value=session):
        with pytest.raises(CloudFallProviderError, match=code):
            asyncio.run(provider.analyze(request()))
    assert session.closed and reply.closed


@pytest.mark.parametrize('options', [
    dict(model='gemma4:31b-cloud', api_key='key'), dict(model='gemma4:31b', api_key='key\n'),
    dict(model='gemma4:31b', api_key='key', timeout_s=21),
])
def test_configuration_does_not_silently_select_local_model(options):
    with pytest.raises(ValueError):
        OllamaCloudFallProvider(**options)
