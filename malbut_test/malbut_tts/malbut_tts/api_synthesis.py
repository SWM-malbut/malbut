"""Stream cloud PCM through the existing cancellable local playback worker.

Only the selected backend sends text externally. No retries, local-model
fallback, audio files, or response/error bodies are logged here.
"""

import asyncio
import math
import os
import re

import numpy as np


class ApiTtsError(RuntimeError):
    """Expose only a fixed diagnostic code, never provider response content."""

    def __init__(self, code):
        super().__init__('API TTS failed: ' + code)


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
        return 'rate_limited'
    if isinstance(status, int):
        return 'invalid_request' if 400 <= status < 500 else 'provider_error'
    return 'stream_error'


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

    def __init__(self, *, model='gpt-4o-mini-tts', voice='marin',
                 timeout_seconds=8.0, api_key=None, client_factory=None):
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
        self.timeout_seconds = float(timeout_seconds)
        self._api_key = api_key
        self._client_factory = client_factory

    def load(self):
        """Check configuration/dependencies without making a paid request."""
        key = self._api_key
        if key is None:
            key = os.environ.get('OPENAI_API_KEY', '')
        if not isinstance(key, str) or not key.strip():
            raise ApiTtsError('missing_api_key')
        self._api_key = key.strip()
        if self._client_factory is None:
            try:
                from openai import AsyncOpenAI
            except ImportError:
                raise ApiTtsError('dependency_missing') from None
            self._client_factory = AsyncOpenAI

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
        """Yield audio as it arrives; failed/partial responses are never retried."""
        if cancel_event.is_set():
            return
        if not isinstance(text, str) or not text.strip() or len(text) > 4096:
            raise ApiTtsError('invalid_request')
        self.load()
        loop = asyncio.new_event_loop()
        client = None
        context = None
        entered = False
        failure = None
        try:
            # Explicit official endpoint: environment overrides cannot silently
            # redirect this backend's text or credentials to another provider.
            client = self._client_factory(
                api_key=self._api_key, base_url='https://api.openai.com/v1',
                max_retries=0, timeout=self.timeout_seconds,
            )
            context = client.audio.speech.with_streaming_response.create(
                model=self.model, voice=self.voice, input=text,
                response_format='pcm',
            )
            response = loop.run_until_complete(
                self._wait(context.__aenter__(), cancel_event))
            entered = True
            content_type = response.headers.get('content-type', '').split(';')[0]
            if content_type not in ('audio/pcm', 'application/octet-stream'):
                raise ApiTtsError('invalid_audio')
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
                raise ApiTtsError(failure) from None
