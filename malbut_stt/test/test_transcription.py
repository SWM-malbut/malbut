"""Check WAV encoding and the actual SDK wire request without network I/O."""

import io
from types import SimpleNamespace
import wave

import pytest

from malbut_stt.transcription import OpenAITranscriber


def test_completed_wav_and_korean_hint_preserve_raw_response():
    """The transcription request contains audio, not Agent instructions."""
    calls = []

    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text='  원문\n그대로 ')

    client = SimpleNamespace(audio=SimpleNamespace(
        transcriptions=SimpleNamespace(create=create),
    ))
    pcm = b'\x01\x00' * 320
    result = OpenAITranscriber(client).transcribe(pcm, 16000)
    assert result == '  원문\n그대로 '
    assert len(calls) == 1
    request = calls[0]
    assert request['model'] == 'gpt-transcribe'
    assert request['extra_body'] == {'languages': ['ko']}
    assert 'language' not in request
    assert 'stream' not in request
    name, data, content_type = request['file']
    assert name == 'utterance.wav' and content_type == 'audio/wav'
    with wave.open(io.BytesIO(data), 'rb') as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 16000
        assert wav.readframes(320) == pcm


@pytest.mark.parametrize('status', [200, 503])
def test_real_sdk_multipart_and_no_automatic_retries(status):
    """Exercise the installed SDK through a local transport, with a fake key."""
    openai = pytest.importorskip('openai')
    httpx = pytest.importorskip('httpx2')
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            status, json={'text': '안녕', 'languages': [{'code': 'ko'}]},
        )

    with openai.OpenAI(
        api_key='test-only-not-a-real-key',
        base_url='https://api.openai.com/v1',
        timeout=30.0, max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(handle)),
    ) as client:
        transcriber = OpenAITranscriber(client)
        if status == 200:
            assert transcriber.transcribe(bytes(640), 16000) == '안녕'
        else:
            with pytest.raises(openai.APIStatusError):
                transcriber.transcribe(bytes(640), 16000)
    assert len(requests) == 1
    request = requests[0]
    assert str(request.url) == 'https://api.openai.com/v1/audio/transcriptions'
    body = request.content
    assert b'gpt-transcribe' in body
    assert b'name="languages[]"' in body
    assert b'name="language"' not in body
    assert b'RIFF' in body
