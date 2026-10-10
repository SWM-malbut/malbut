"""Stream cloud PCM through the existing cancellable local playback worker.

Only the selected backend sends text externally. No retries, local-model
fallback, audio files, or response/error bodies are logged here.

The key is read on every utterance (the owner can change it on the web,
SWM25-235). When it is missing, wrong or out of quota, Malbut cannot speak
the answer, so the prerecorded notice plays instead through the same player.
"""

import asyncio
import math
from pathlib import Path
import re
import wave

import numpy as np

from malbut_tts.managed_key import OpenAIKey
from malbut_tts.voice_style import VOICE_INSTRUCTIONS

NOTICE_PATH = Path(__file__).resolve().parent / 'assets' / 'notice_no_dialogue.wav'
NOTICE_TEXT = '지금은 대화를 할 수 없어요.'
# Failures that mean the key itself cannot be used, and how they are reported.
KEY_FAILURES = {
    'missing_api_key': 'missing',
    'authentication_failed': 'invalid',
    'permission_denied': 'invalid',
    'insufficient_quota': 'quota',
}


class ApiTtsError(RuntimeError):
    """Expose only a fixed diagnostic code, never provider response content."""

    def __init__(self, code):
        super().__init__('API TTS failed: ' + code)
        self.code = code


class _Cancelled(Exception):
    pass


def _error_code(error):
    if isinstance(error, (TimeoutError, asyncio.TimeoutError)):
        return 'timeout'
    if type(error).__name__ == 'APITimeoutError':
        return 'timeout'
    if type(error).__name__ == 'APIConnectionError':
        return 'connection_failed'
    status = getattr(error, 'status_code', None)
    if status == 401:
        return 'authentication_failed'
    if status == 403:
        return 'permission_denied'
    if status == 429:
        return 'insufficient_quota' if _insufficient_quota(error) else 'rate_limited'
    if isinstance(status, int):
        return 'invalid_request' if 400 <= status < 500 else 'provider_error'
    return 'stream_error'


def _insufficient_quota(error):
    """A 429 for an exhausted account, not a short rate limit (SDK error fields)."""
    body = getattr(error, 'body', None)
    fields = [getattr(error, 'code', None), getattr(error, 'type', None)]
    if isinstance(body, dict):
        fields += [body.get('code'), body.get('type')]
    return 'insufficient_quota' in fields


class OpenAISynthesizer:
    """Adapt streamed signed 16-bit, 24 kHz PCM to mono float32 chunks.

    An async transport is driven by the existing single synthesis worker.
    Each network await polls the request's cancellation event, including
    before headers arrive; no detached producer or watchdog thread survives
    generator close. One answer is one HTTP request, not one per sentence.
    """

    sentence_streaming = False
    synthesis_streaming = True
    sample_rate = 24000
    chunk_bytes = 4800  # 100 ms of PCM, not 100 ms of network latency.
    startup_buffer_bytes = 19200  # 400 ms to absorb the first network burst gap.
    max_audio_bytes = 24000 * 2 * 300

    def __init__(self, *, model='gpt-4o-mini-tts', voice='shimmer',
                 timeout_seconds=8.0, api_key=None, client_factory=None,
                 notice_path=NOTICE_PATH, instructions=VOICE_INSTRUCTIONS):
        for value in (model, voice):
            if (not isinstance(value, str)
                    or re.fullmatch(r'[a-zA-Z0-9_.:-]{1,128}', value) is None):
                raise ValueError('API TTS model and voice must be identifiers')
        if (type(timeout_seconds) not in (int, float)
                or not math.isfinite(timeout_seconds)
                or not 0.1 <= timeout_seconds <= 60):
            raise ValueError('API TTS timeout must be 0.1..60 seconds')
        self.model = model
        self.voice = voice
        if not isinstance(instructions, str) or len(instructions) > 1000:
            raise ValueError('API TTS voice instructions must be at most 1000 characters')
        self.instructions = instructions
        self.timeout_seconds = float(timeout_seconds)
        # A fixed key string (tests, smoke), or the key the owner manages on the web.
        self.key = OpenAIKey() if api_key is None else api_key
        self._client_factory = client_factory
        self._notice_path = notice_path
        self._notice = None

    def _report(self, state, code=None):
        report = getattr(self.key, 'report', None)
        if report is not None:
            report(state, code)

    def _ready(self):
        """The key to use now; also checks the SDK, without a paid request."""
        key = self.key if isinstance(self.key, str) else self.key.current()
        if not isinstance(key, str) or not key.strip():
            self._report('missing', 'missing_api_key')
            raise ApiTtsError('missing_api_key')
        if self._client_factory is None:
            try:
                from openai import AsyncOpenAI
            except ImportError:
                raise ApiTtsError('dependency_missing') from None
            self._client_factory = AsyncOpenAI
        return key.strip()

    def load(self):
        """Check configuration/dependencies without making a paid request."""
        self._ready()

    def _notice_audio(self):
        """The bundled notice as mono float32, or None when it is missing or unusable."""
        if self._notice is None and self._notice_path is not None:
            try:
                with wave.open(str(self._notice_path), 'rb') as file:
                    rate = file.getframerate()
                    if (file.getnchannels() != 1 or file.getsampwidth() != 2
                            or not 8000 <= rate <= 48000
                            or not 0 < file.getnframes() <= rate * 30):
                        return None
                    frames = file.readframes(file.getnframes())
            except (OSError, EOFError, wave.Error):
                return None
            if not frames or len(frames) % 2:
                return None
            pcm = np.frombuffer(frames, dtype='<i2')
            self._notice = (pcm.astype(np.float32) / 32768.0, rate)
        return self._notice

    async def _wait(self, awaitable, cancel_event):
        task = asyncio.ensure_future(awaitable)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.timeout_seconds
        try:
            while True:
                if cancel_event.is_set():
                    raise _Cancelled()
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError()
                done, _ = await asyncio.wait(
                    (task,), timeout=min(0.05, remaining),
                )
                if done:
                    if cancel_event.is_set():
                        raise _Cancelled()
                    return task.result()
        finally:
            if not task.done():
                task.cancel()
            # Retrieve exceptions even when cancellation races a result.
            await asyncio.gather(task, return_exceptions=True)

    def generate(self, text, cancel_event):
        """Yield PCM; a substituted notice returns a failure marker after its audio."""
        if cancel_event.is_set():
            return
        substituted = text != NOTICE_TEXT or self._notice_path is None
        if not substituted:
            notice = self._notice_audio()
            if notice is None:
                raise ApiTtsError('notice_unavailable')
        else:
            stream = self._stream(text, cancel_event)
            started = False
            try:
                for chunk in stream:
                    started = True
                    yield chunk
                return
            except ApiTtsError as error:
                notice = None
                if not started and error.code in KEY_FAILURES:
                    notice = self._notice_audio()
                if notice is None or cancel_event.is_set():
                    raise
            finally:
                stream.close()
        audio, rate = notice
        step = rate // 10
        for start in range(0, len(audio), step):
            if cancel_event.is_set():
                return
            yield audio[start:start + step], rate
        return 'notice_substituted' if substituted else None

    def _stream(self, text, cancel_event):
        """Yield audio as it arrives; failed/partial responses are never retried."""
        if cancel_event.is_set():
            return
        if not isinstance(text, str) or not text.strip() or len(text) > 4096:
            raise ApiTtsError('invalid_request')
        api_key = self._ready()
        loop = asyncio.new_event_loop()
        client = None
        context = None
        entered = False
        failure = None
        try:
            # Explicit official endpoint: environment overrides cannot silently
            # redirect this backend's text or credentials to another provider.
            client = self._client_factory(
                api_key=api_key, base_url='https://api.openai.com/v1',
                max_retries=0, timeout=self.timeout_seconds,
            )
            context = client.audio.speech.with_streaming_response.create(
                model=self.model, voice=self.voice, input=text,
                response_format='pcm',
                **({'instructions': self.instructions}
                   if self.instructions and self.model not in {'tts-1', 'tts-1-hd'} else {}),
            )
            response = loop.run_until_complete(
                self._wait(context.__aenter__(), cancel_event))
            entered = True
            content_type = response.headers.get('content-type', '').split(';')[0]
            if content_type not in ('audio/pcm', 'application/octet-stream'):
                raise ApiTtsError('invalid_audio')
            self._report('ok')
            stream = response.iter_bytes(chunk_size=self.chunk_bytes).__aiter__()
            carry = b''
            startup = bytearray()
            primed = False
            total = 0
            while not cancel_event.is_set():
                try:
                    data = loop.run_until_complete(
                        self._wait(stream.__anext__(), cancel_event))
                except StopAsyncIteration:
                    break
                if not isinstance(data, bytes):
                    raise ApiTtsError('invalid_audio')
                total += len(data)
                if total > self.max_audio_bytes:
                    raise ApiTtsError('response_too_large')
                data = carry + data
                boundary = len(data) - len(data) % 2
                carry = data[boundary:]
                if boundary and not cancel_event.is_set():
                    if not primed:
                        startup.extend(data[:boundary])
                        if len(startup) < self.startup_buffer_bytes:
                            continue
                        data = bytes(startup)
                        boundary = len(data)
                        startup.clear()
                        primed = True
                    pcm = np.frombuffer(data[:boundary], dtype='<i2')
                    yield pcm.astype(np.float32) / 32768.0, self.sample_rate
            if not cancel_event.is_set():
                if carry:
                    raise ApiTtsError('invalid_audio')
                if total == 0:
                    raise ApiTtsError('empty_audio')
                if startup:
                    pcm = np.frombuffer(startup, dtype='<i2')
                    yield pcm.astype(np.float32) / 32768.0, self.sample_rate
        except _Cancelled:
            return
        except ApiTtsError:
            raise
        except Exception as error:
            failure = _error_code(error)
        finally:
            try:
                if entered:
                    loop.run_until_complete(asyncio.wait_for(
                        context.__aexit__(None, None, None), timeout=1.0))
            except Exception:
                failure = failure or 'cleanup_failed'
            try:
                if client is not None:
                    loop.run_until_complete(asyncio.wait_for(
                        client.close(), timeout=1.0))
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                failure = failure or 'cleanup_failed'
            finally:
                loop.close()
            if failure is not None:
                if failure in KEY_FAILURES:
                    self._report(KEY_FAILURES[failure], failure)
                raise ApiTtsError(failure) from None
