"""Optional short-utterance recognition without another resident model."""

import io
import json
import math
import urllib.request
import uuid
import wave


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _transport(headers, body, timeout):
    request = urllib.request.Request(
        'https://api.openai.com/v1/audio/transcriptions', data=body, headers=headers)
    with urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout) as response:
        data = response.read(65537)
    if len(data) > 65536:
        raise ValueError('transcription response exceeds limit')
    return json.loads(data)


class InterruptionTranscriber:
    """Use the authorized API only for captured interruptions, with local fallback.

    No network connection, model, or thread is kept alive during normal listening.
    The pipeline owns cancellation by generation and never publishes stale results.
    """

    def __init__(self, key_provider, fallback, *, model='gpt-transcribe',
                 timeout_s=5.0, transport=_transport, report=lambda _: None):
        if not callable(key_provider) or not callable(transport):
            raise ValueError('key provider and transport must be callable')
        if (type(timeout_s) not in (int, float) or not math.isfinite(timeout_s)
                or not 0 < timeout_s <= 10):
            raise ValueError('interruption ASR timeout must be between zero and ten seconds')
        if not isinstance(model, str) or not model.strip() or len(model) > 100:
            raise ValueError('interruption ASR model must be a bounded nonblank string')
        self.key_provider, self.fallback = key_provider, fallback
        self.model, self.timeout_s = model, timeout_s
        self.transport, self.report = transport, report

    def transcribe(self, pcm, sample_rate):
        if sample_rate != 16000 or len(pcm) % 2:
            raise ValueError('interruption ASR requires mono 16 kHz PCM16')
        if not any(pcm):
            return ''
        # Retain the established local path for long explanations. Uploads are
        # bounded independently of the normal collector's minute-long buffer.
        if len(pcm) > 20 * 32000:
            return self.fallback.transcribe(pcm, sample_rate)
        try:
            key = self.key_provider()
            if not isinstance(key, str) or not key.strip():
                raise ValueError('API key unavailable')
            with io.BytesIO() as buffer:
                with wave.open(buffer, 'wb') as wav:
                    wav.setparams((1, 2, sample_rate, 0, 'NONE', 'not compressed'))
                    wav.writeframes(pcm)
                audio = buffer.getvalue()
            boundary = uuid.uuid4().hex
            fields = [('model', self.model), ('languages[]', 'ko')]
            body = b''.join(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
                for name, value in fields)
            body += (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                     'filename="utterance.wav"\r\nContent-Type: audio/wav\r\n\r\n').encode()
            body += audio + f'\r\n--{boundary}--\r\n'.encode()
            result = self.transport({'Authorization': 'Bearer ' + key,
                'Content-Type': 'multipart/form-data; boundary=' + boundary}, body, self.timeout_s)
            text = result.get('text')
            if not isinstance(text, str) or len(text) > 16000:
                raise ValueError('Invalid transcription response')
            self.report('interruption_asr:api')
            return text.strip()
        except Exception as error:
            # Never log credentials, HTTP response bodies, or request headers.
            self.report('interruption_asr:local_fallback:' + type(error).__name__)
            return self.fallback.transcribe(pcm, sample_rate)
