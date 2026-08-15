"""Offline tests for the temporary local speech-to-text adapter."""

import builtins
import json
import math
import os
import subprocess
import sys
import tempfile
import types
import wave
from contextlib import contextmanager
from pathlib import Path

import pytest

from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.local_stt import (
    AudioValidationError,
    BackendTranscript,
    BackendUnavailableError,
    CaptureError,
    FasterWhisperBackend,
    LocalSTTError,
    LocalSTTResult,
    NoSpeechError,
    TranscriptionError,
    WavMetadata,
    build_transcript_event,
    capture_microphone,
    main,
    transcribe_wav,
    validate_pcm_wav,
)
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.safety import SafetyPolicy
from malbut_agent_server.schemas import MAX_UTTERANCE_LENGTH
from malbut_agent_server.speech import (
    SpeechConversationCoordinator,
    SpeechTranscriptEvent,
    TrustedSpeechBinding,
)


def _write_wav(
    path: Path,
    *,
    duration_ms: int = 500,
    sample_rate_hz: int = 16000,
    channel_count: int = 1,
    sample_width_bytes: int = 2,
) -> Path:
    """Write deterministic silence using only the standard library."""
    frame_count = max(0, sample_rate_hz * duration_ms // 1000)
    frame = b'\x00' * sample_width_bytes * channel_count
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(channel_count)
        output.setsampwidth(sample_width_bytes)
        output.setframerate(sample_rate_hz)
        output.writeframes(frame * frame_count)
    return path


def _mark_wav_as_streaming(path: Path) -> Path:
    """Replace finite RIFF/data lengths with streaming sentinels."""
    payload = bytearray(path.read_bytes())
    data_offset = payload.index(b'data', 12)
    payload[4:8] = b'\xff\xff\xff\xff'
    payload[data_offset + 4:data_offset + 8] = b'\xff\xff\xff\xff'
    path.write_bytes(payload)
    return path


class FakeBackend:
    """Return one deterministic transcript without loading a model."""

    def __init__(
        self,
        transcript: BackendTranscript = None,
        error: Exception = None,
    ) -> None:
        """Store a result or injected failure and record calls."""
        self.transcript = transcript or BackendTranscript(
            text='안녕하세요 말벗',
            confidence=0.91,
            language='ko',
        )
        self.error = error
        self.calls = []
        self.prepare_calls = 0

    def prepare(self) -> None:
        """Mimic eager CLI preparation without loading anything."""
        self.prepare_calls += 1

    def transcribe(
        self,
        wav_path: Path,
        *,
        language: str,
    ) -> BackendTranscript:
        """Record the boundary call, then return or raise locally."""
        self.calls.append((wav_path, language))
        if self.error is not None:
            raise self.error
        return self.transcript


def _binding() -> TrustedSpeechBinding:
    """Build the trusted identity owned by the speech coordinator."""
    return TrustedSpeechBinding.from_dict(
        {
            'user_id': 'voice-user',
            'speaker_id': 'trusted-speaker',
            'speech_session_id': 'speech-session-1',
            'conversation_id': 'voice-conversation-1',
            'source': 'local-stt',
        }
    )


def _assert_chain_free_public_error(
    error: LocalSTTError,
    *private_values: object,
) -> None:
    """Assert public failures retain no hidden exception or private value."""
    assert error.__cause__ is None
    assert error.__context__ is None
    rendered = ' '.join(
        (
            str(error),
            repr(error),
            json.dumps(error.to_dict(), ensure_ascii=False),
            json.dumps(error.to_audit_dict(), ensure_ascii=False),
        )
    )
    for private_value in private_values:
        assert str(private_value) not in rendered


def test_validate_pcm_wav_returns_bounded_metadata(tmp_path: Path) -> None:
    """A supported local PCM WAV yields content-free metadata."""
    wav_path = _write_wav(tmp_path / 'voice.wav', duration_ms=625)

    metadata = validate_pcm_wav(wav_path)

    assert metadata.duration_ms == 625
    assert metadata.sample_rate_hz == 16000
    assert metadata.channel_count == 1
    assert metadata.sample_width_bytes == 2
    assert metadata.frame_count == 10000
    assert not hasattr(metadata, 'path')
    assert not hasattr(metadata, 'audio')


def test_validate_pcm_wav_accepts_bounded_streaming_size_sentinels(
    tmp_path: Path,
) -> None:
    """A closed streaming WAV derives duration from its actual PCM bytes."""
    wav_path = _mark_wav_as_streaming(
        _write_wav(
            tmp_path / 'streaming.wav',
            duration_ms=625,
            sample_rate_hz=24000,
        )
    )

    metadata = validate_pcm_wav(wav_path)

    assert metadata.duration_ms == 625
    assert metadata.frame_count == 15000
    assert metadata.sample_rate_hz == 24000


def test_streaming_size_sentinel_cannot_bypass_duration_limit(
    tmp_path: Path,
) -> None:
    """Actual PCM length still enforces the 30-second upper boundary."""
    wav_path = _mark_wav_as_streaming(
        _write_wav(
            tmp_path / 'streaming-too-long.wav',
            duration_ms=30001,
            sample_rate_hz=8000,
        )
    )

    with pytest.raises(AudioValidationError, match='duration'):
        validate_pcm_wav(wav_path)


def test_streaming_size_sentinel_rejects_partial_pcm_frame(
    tmp_path: Path,
) -> None:
    """EOF-derived payloads must end on a complete PCM frame."""
    wav_path = _mark_wav_as_streaming(
        _write_wav(tmp_path / 'streaming-partial.wav')
    )
    with wav_path.open('ab') as output:
        output.write(b'\x00')

    with pytest.raises(AudioValidationError, match='incomplete'):
        validate_pcm_wav(wav_path)


def test_public_errors_share_one_catchable_base() -> None:
    """Callers can handle all stable adapter failures uniformly."""
    for error_type in (
        AudioValidationError,
        CaptureError,
        BackendUnavailableError,
        NoSpeechError,
        TranscriptionError,
    ):
        assert issubclass(error_type, LocalSTTError)


@pytest.mark.parametrize(
    ('duration_ms', 'sample_rate_hz', 'channel_count'),
    [
        (1, 8000, 1),
        (30000, 8000, 1),
        (10, 48000, 2),
    ],
)
def test_validate_pcm_wav_accepts_contract_boundaries(
    tmp_path: Path,
    duration_ms: int,
    sample_rate_hz: int,
    channel_count: int,
) -> None:
    """Inclusive duration, rate, and channel limits remain usable."""
    wav_path = _write_wav(
        tmp_path / 'boundary.wav',
        duration_ms=duration_ms,
        sample_rate_hz=sample_rate_hz,
        channel_count=channel_count,
    )

    metadata = validate_pcm_wav(wav_path)

    assert metadata.duration_ms == duration_ms
    assert metadata.sample_rate_hz == sample_rate_hz
    assert metadata.channel_count == channel_count


@pytest.mark.parametrize(
    ('overrides', 'message'),
    [
        ({'duration_ms': 0}, 'duration'),
        ({'duration_ms': 30001}, 'duration'),
        ({'sample_rate_hz': 7999}, 'sample rate'),
        ({'sample_rate_hz': 48001}, 'sample rate'),
        ({'channel_count': 3}, 'channel'),
        ({'sample_width_bytes': 1}, '16-bit'),
    ],
)
def test_validate_pcm_wav_rejects_out_of_contract_audio(
    tmp_path: Path,
    overrides: dict,
    message: str,
) -> None:
    """Duration and PCM shape limits fail closed before inference."""
    parameters = {
        'duration_ms': 500,
        'sample_rate_hz': 16000,
        'channel_count': 1,
        'sample_width_bytes': 2,
    }
    parameters.update(overrides)
    wav_path = _write_wav(tmp_path / 'unsupported.wav', **parameters)

    with pytest.raises(AudioValidationError, match=message):
        validate_pcm_wav(wav_path)


def test_validate_pcm_wav_rejects_malformed_and_oversized_files(
    tmp_path: Path,
) -> None:
    """Malformed containers and files above six MiB never reach a model."""
    malformed = tmp_path / 'not-a-wave.wav'
    malformed.write_bytes(b'private spoken words')
    oversized = _write_wav(tmp_path / 'oversized.wav')
    with oversized.open('ab') as output:
        output.truncate((6 * 1024 * 1024) + 1)

    with pytest.raises(AudioValidationError):
        validate_pcm_wav(malformed)
    with pytest.raises(AudioValidationError, match='size'):
        validate_pcm_wav(oversized)


def test_missing_private_wav_error_has_no_exception_chain(
    tmp_path: Path,
) -> None:
    """A missing input path cannot survive through an exception context."""
    private_path = tmp_path / 'PRIVATE-person-name-secret-voice.wav'

    with pytest.raises(AudioValidationError) as failure:
        validate_pcm_wav(private_path)

    _assert_chain_free_public_error(failure.value, private_path, 'PRIVATE')


def test_malicious_pathlike_hook_is_a_chain_free_validation_error() -> None:
    """A hostile filesystem coercion hook cannot leak its private detail."""
    private_detail = 'PRIVATE fspath hook /private/speaker/voice.wav'

    class ExplodingPathLike(os.PathLike):
        """Raise a deliberately sensitive error during path coercion."""

        def __fspath__(self):
            """Simulate an adversarial path implementation."""
            raise RuntimeError(private_detail)

    with pytest.raises(AudioValidationError) as failure:
        validate_pcm_wav(ExplodingPathLike())

    assert type(failure.value) is AudioValidationError
    assert failure.value.exit_code == 2
    _assert_chain_free_public_error(
        failure.value,
        private_detail,
        '/private/speaker/voice.wav',
    )


def test_validate_pcm_wav_rejects_non_pcm_format(tmp_path: Path) -> None:
    """A WAV header using the IEEE-float format tag is not accepted."""
    wav_path = _write_wav(tmp_path / 'float-tag.wav')
    payload = bytearray(wav_path.read_bytes())
    payload[20:22] = (3).to_bytes(2, 'little')
    wav_path.write_bytes(payload)

    with pytest.raises(AudioValidationError, match='PCM'):
        validate_pcm_wav(wav_path)


def test_truncated_20_byte_riff_junk_is_sanitized_in_api_and_cli(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """A minimal RIFF/JUNK truncation stays a chain-free exit-2 error."""
    wav_path = tmp_path / 'PRIVATE-malformed-speaker-path.wav'
    malformed = b'RIFF\x0c\x00\x00\x00WAVEJUNK\x00\x00\x00\x00'
    assert len(malformed) == 20
    wav_path.write_bytes(malformed)

    with pytest.raises(AudioValidationError) as failure:
        validate_pcm_wav(wav_path)

    assert type(failure.value) is AudioValidationError
    assert failure.value.exit_code == 2
    _assert_chain_free_public_error(
        failure.value,
        wav_path,
        'PRIVATE',
        'JUNK',
    )

    exit_code = main(
        ['--wav', str(wav_path)],
        backend_factory=lambda **_kwargs: FakeBackend(),
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.out == ''
    assert str(wav_path) not in captured.err
    assert 'PRIVATE' not in captured.err
    assert 'JUNK' not in captured.err
    assert 'Traceback' not in captured.err


def test_validate_pcm_wav_rejects_symlink(tmp_path: Path) -> None:
    """Validation does not follow a path that can be retargeted."""
    private_target = _write_wav(
        tmp_path / 'PRIVATE-real-speaker.wav'
    )
    symlink = tmp_path / 'voice.wav'
    symlink.symlink_to(private_target)

    with pytest.raises(AudioValidationError) as failure:
        validate_pcm_wav(symlink)

    _assert_chain_free_public_error(
        failure.value,
        private_target,
        symlink,
        'PRIVATE',
    )


def test_validate_pcm_wav_opens_with_no_follow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final kernel open explicitly refuses symbolic links."""
    wav_path = _write_wav(tmp_path / 'voice.wav')
    original_open = os.open
    observed_flags = []

    def recording_open(path, flags, *args, **kwargs):
        observed_flags.append(flags)
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(
        'malbut_agent_server.local_stt.os.open',
        recording_open,
    )

    validate_pcm_wav(wav_path)

    assert observed_flags
    assert any(flags & os.O_NOFOLLOW for flags in observed_flags)


def test_validate_pcm_wav_rejects_actual_duration_below_one_ms(
    tmp_path: Path,
) -> None:
    """Rounding cannot turn a sub-millisecond sample into valid audio."""
    wav_path = tmp_path / 'one-sample.wav'
    with wave.open(str(wav_path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b'\x00\x00')

    with pytest.raises(AudioValidationError, match='duration'):
        validate_pcm_wav(wav_path)


def test_transcribe_wav_calls_injected_backend_after_validation(
    tmp_path: Path,
) -> None:
    """The coordinator returns normalized text and validated metadata."""
    wav_path = _write_wav(tmp_path / 'voice.wav')
    backend = FakeBackend(
        BackendTranscript(
            text='  오늘 날씨 알려 줘  ',
            confidence=0.83,
            language='ko',
        )
    )

    result = transcribe_wav(wav_path, backend, language='ko')

    assert result.text == '오늘 날씨 알려 줘'
    assert result.confidence == pytest.approx(0.83)
    assert result.language == 'ko'
    assert result.audio_metadata.duration_ms == 500
    assert len(backend.calls) == 1
    snapshot_path, requested_language = backend.calls[0]
    assert snapshot_path != wav_path
    assert not snapshot_path.exists()
    assert requested_language == 'ko'


def test_transcribe_uses_private_snapshot_and_survives_original_swap(
    tmp_path: Path,
) -> None:
    """Inference consumes the checked bytes, not a replaceable input path."""
    original = _write_wav(
        tmp_path / 'PRIVATE-original-speaker.wav',
        duration_ms=500,
    )
    original_bytes = original.read_bytes()
    replacement = _write_wav(
        tmp_path / 'replacement.wav',
        duration_ms=900,
    )
    replacement_bytes = replacement.read_bytes()
    observed = {}

    class ReplacingBackend:
        """Replace the caller file while inspecting the private snapshot."""

        backend_name = 'snapshot-fixture'

        def transcribe(
            self,
            wav_path: Path,
            *,
            language: str,
        ) -> BackendTranscript:
            observed['path'] = Path(wav_path)
            observed['parent'] = Path(wav_path).parent
            observed['mode'] = os.stat(wav_path).st_mode & 0o777
            observed['parent_mode'] = (
                os.stat(Path(wav_path).parent).st_mode & 0o777
            )
            observed['bytes_before_swap'] = Path(wav_path).read_bytes()
            os.replace(replacement, original)
            observed['bytes_after_swap'] = Path(wav_path).read_bytes()
            return BackendTranscript(
                text='검사된 음성',
                confidence=0.9,
                language=language,
            )

    result = transcribe_wav(original, ReplacingBackend())

    assert observed['path'] != original
    assert observed['mode'] == 0o600
    assert observed['parent_mode'] == 0o700
    assert observed['bytes_before_swap'] == original_bytes
    assert observed['bytes_after_swap'] == original_bytes
    assert not observed['path'].exists()
    assert not observed['parent'].exists()
    assert original.read_bytes() == replacement_bytes
    assert result.audio_metadata.duration_ms == 500
    assert result.text == '검사된 음성'


def test_transcribe_removes_snapshot_after_backend_failure(
    tmp_path: Path,
) -> None:
    """A backend failure deletes only the private snapshot."""
    original = _write_wav(
        tmp_path / 'PRIVATE-original-speaker.wav'
    )
    original_bytes = original.read_bytes()
    private_detail = 'PRIVATE backend failure /private/runtime'
    observed = {}

    class FailingBackend:
        """Record the snapshot and raise an adversarial raw exception."""

        def transcribe(
            self,
            wav_path: Path,
            *,
            language: str,
        ) -> BackendTranscript:
            del language
            observed['path'] = Path(wav_path)
            observed['parent'] = Path(wav_path).parent
            observed['mode'] = os.stat(wav_path).st_mode & 0o777
            observed['parent_mode'] = (
                os.stat(Path(wav_path).parent).st_mode & 0o777
            )
            observed['bytes'] = Path(wav_path).read_bytes()
            raise RuntimeError(private_detail)

    with pytest.raises(TranscriptionError) as failure:
        transcribe_wav(original, FailingBackend())

    _assert_chain_free_public_error(
        failure.value,
        private_detail,
        original,
        observed['path'],
    )
    assert observed['path'] != original
    assert observed['mode'] == 0o600
    assert observed['parent_mode'] == 0o700
    assert observed['bytes'] == original_bytes
    assert not observed['path'].exists()
    assert not observed['parent'].exists()
    assert original.read_bytes() == original_bytes


def test_transcribe_removes_snapshot_after_keyboard_interrupt(
    tmp_path: Path,
) -> None:
    """Ctrl-C unwinds the private snapshot while preserving the input."""
    original = _write_wav(
        tmp_path / 'PRIVATE-original-speaker.wav'
    )
    original_bytes = original.read_bytes()
    observed = {}

    class InterruptingBackend:
        """Interrupt inference after observing its private snapshot."""

        def transcribe(
            self,
            wav_path: Path,
            *,
            language: str,
        ) -> BackendTranscript:
            del language
            observed['path'] = Path(wav_path)
            observed['parent'] = Path(wav_path).parent
            observed['mode'] = os.stat(wav_path).st_mode & 0o777
            observed['parent_mode'] = (
                os.stat(Path(wav_path).parent).st_mode & 0o777
            )
            observed['bytes'] = Path(wav_path).read_bytes()
            raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        transcribe_wav(original, InterruptingBackend())

    assert observed['path'] != original
    assert observed['mode'] == 0o600
    assert observed['parent_mode'] == 0o700
    assert observed['bytes'] == original_bytes
    assert not observed['path'].exists()
    assert not observed['parent'].exists()
    assert original.read_bytes() == original_bytes


def test_transcribe_uses_open_fd_when_source_becomes_symlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path swap after kernel open cannot redirect snapshot contents."""
    source = _write_wav(
        tmp_path / 'source.wav',
        duration_ms=500,
    )
    source_bytes = source.read_bytes()
    private_target = _write_wav(
        tmp_path / 'PRIVATE-symlink-target.wav',
        duration_ms=900,
    )
    original_open = os.open
    state = {'swapped': False}
    observed = {}

    def swapping_open(path, flags, *args, **kwargs):
        descriptor = original_open(path, flags, *args, **kwargs)
        if Path(path) == source and not state['swapped']:
            source.unlink()
            source.symlink_to(private_target)
            state['swapped'] = True
        return descriptor

    class InspectingBackend:
        """Retain only assertions about the already-copied snapshot."""

        def transcribe(
            self,
            wav_path: Path,
            *,
            language: str,
        ) -> BackendTranscript:
            observed['path'] = Path(wav_path)
            observed['bytes'] = Path(wav_path).read_bytes()
            return BackendTranscript(
                text='원본 파일 디스크립터',
                confidence=0.9,
                language=language,
            )

    monkeypatch.setattr(
        'malbut_agent_server.local_stt.os.open',
        swapping_open,
    )

    result = transcribe_wav(source, InspectingBackend())

    assert state['swapped'] is True
    assert source.is_symlink()
    assert observed['bytes'] == source_bytes
    assert observed['bytes'] != private_target.read_bytes()
    assert observed['path'] != source
    assert not observed['path'].exists()
    assert result.audio_metadata.duration_ms == 500


def test_source_growth_beyond_limit_cannot_reach_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A file enlarged during snapshot copy is rejected at six MiB."""
    source = _write_wav(tmp_path / 'growing-source.wav')
    original_read = os.read
    state = {'grown': False}
    backend = FakeBackend()

    def grow_after_first_read(file_descriptor: int, size: int) -> bytes:
        chunk = original_read(file_descriptor, size)
        if chunk and not state['grown']:
            with source.open('ab') as output:
                output.truncate((6 * 1024 * 1024) + 1)
            state['grown'] = True
        return chunk

    monkeypatch.setattr(
        'malbut_agent_server.local_stt.os.read',
        grow_after_first_read,
    )

    with pytest.raises(AudioValidationError, match='size') as failure:
        transcribe_wav(source, backend)

    _assert_chain_free_public_error(failure.value, source)
    assert state['grown'] is True
    assert source.stat().st_size == (6 * 1024 * 1024) + 1
    assert backend.calls == []


def test_snapshot_cleanup_noop_fails_closed_and_leaves_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A surviving private snapshot prevents a successful transcript."""
    original = _write_wav(tmp_path / 'original.wav')
    original_bytes = original.read_bytes()
    backend = FakeBackend()
    leftover_roots = []
    snapshot_path = None
    original_rmtree = tempfile.TemporaryDirectory._rmtree

    def noop_temp_rmtree(_class, path, ignore_errors=False):
        del ignore_errors
        leftover_roots.append(Path(path))

    def noop_shutil_rmtree(path, *args, **kwargs):
        del args, kwargs
        leftover_roots.append(Path(path))

    monkeypatch.setattr(
        tempfile.TemporaryDirectory,
        '_rmtree',
        classmethod(noop_temp_rmtree),
    )
    monkeypatch.setattr(
        'malbut_agent_server.local_stt.shutil.rmtree',
        noop_shutil_rmtree,
    )

    try:
        with pytest.raises(AudioValidationError) as failure:
            transcribe_wav(original, backend)

        assert type(failure.value) is AudioValidationError
        _assert_chain_free_public_error(failure.value, original)
        assert len(backend.calls) == 1
        snapshot_path = backend.calls[0][0]
        assert snapshot_path.exists()
        assert snapshot_path.parent.exists()
        assert original.read_bytes() == original_bytes
    finally:
        monkeypatch.undo()
        for root in set(leftover_roots):
            if root.exists():
                original_rmtree(str(root), ignore_errors=True)

    assert snapshot_path is not None
    assert not snapshot_path.exists()
    assert not snapshot_path.parent.exists()


def test_microphone_cleanup_oserror_fails_closed_and_is_manual_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A surviving microphone WAV becomes a chain-free capture failure."""
    private_detail = 'PRIVATE cleanup failure /private/capture'
    leftover_roots = []
    observed = {}
    original_rmtree = tempfile.TemporaryDirectory._rmtree

    def failing_temp_rmtree(_class, path, ignore_errors=False):
        del ignore_errors
        leftover_roots.append(Path(path))
        raise OSError(private_detail)

    def failing_shutil_rmtree(path, *args, **kwargs):
        del args, kwargs
        leftover_roots.append(Path(path))
        raise OSError(private_detail)

    def fake_runner(argv, **_kwargs):
        observed['path'] = Path(argv[-1])
        _write_wav(observed['path'])
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(
        tempfile.TemporaryDirectory,
        '_rmtree',
        classmethod(failing_temp_rmtree),
    )
    monkeypatch.setattr(
        'malbut_agent_server.local_stt.shutil.rmtree',
        failing_shutil_rmtree,
    )

    try:
        with pytest.raises(CaptureError) as failure:
            with capture_microphone(
                runner=fake_runner,
                which=lambda _name: '/usr/bin/arecord',
                temp_root=tmp_path,
            ) as captured_path:
                assert captured_path.exists()

        assert type(failure.value) is CaptureError
        _assert_chain_free_public_error(
            failure.value,
            private_detail,
            observed['path'],
        )
        assert observed['path'].exists()
        assert observed['path'].parent.exists()
    finally:
        monkeypatch.undo()
        for root in set(leftover_roots):
            if root.exists():
                original_rmtree(str(root), ignore_errors=True)

    assert not observed['path'].exists()
    assert not observed['path'].parent.exists()


@pytest.mark.parametrize(
    'pending_error',
    [KeyboardInterrupt(), RuntimeError('expected downstream body error')],
)
def test_pending_body_error_precedes_cleanup_failure_without_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    pending_error: BaseException,
) -> None:
    """Cleanup failure never replaces or chains a pending body exception."""
    private_detail = 'PRIVATE cleanup precedence /private/capture'
    leftover_roots = []
    observed = {}
    original_rmtree = tempfile.TemporaryDirectory._rmtree

    def blocked_temp_rmtree(_class, path, ignore_errors=False):
        del ignore_errors
        leftover_roots.append(Path(path))

    def blocked_shutil_rmtree(path, *args, **kwargs):
        del args, kwargs
        leftover_roots.append(Path(path))
        raise OSError(private_detail)

    def fake_runner(argv, **_kwargs):
        observed['path'] = Path(argv[-1])
        _write_wav(observed['path'])
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(
        tempfile.TemporaryDirectory,
        '_rmtree',
        classmethod(blocked_temp_rmtree),
    )
    monkeypatch.setattr(
        'malbut_agent_server.local_stt.shutil.rmtree',
        blocked_shutil_rmtree,
    )

    try:
        with pytest.raises(type(pending_error)) as failure:
            with capture_microphone(
                runner=fake_runner,
                which=lambda _name: '/usr/bin/arecord',
                temp_root=tmp_path,
            ):
                raise pending_error

        assert failure.value is pending_error
        assert failure.value.__cause__ is None
        assert failure.value.__context__ is None
        assert private_detail not in str(failure.value)
        assert private_detail not in repr(failure.value)
        assert observed['path'].exists()
    finally:
        monkeypatch.undo()
        for root in set(leftover_roots):
            if root.exists():
                original_rmtree(str(root), ignore_errors=True)

    assert not observed['path'].exists()
    assert not observed['path'].parent.exists()


@pytest.mark.parametrize(
    'transcript',
    [
        BackendTranscript(text='', confidence=0.9, language='ko'),
        BackendTranscript(text='   ', confidence=0.9, language='ko'),
    ],
)
def test_transcribe_wav_rejects_empty_speech(
    tmp_path: Path,
    transcript: BackendTranscript,
) -> None:
    """Silence or whitespace is a distinct non-successful outcome."""
    wav_path = _write_wav(tmp_path / 'silence.wav')

    with pytest.raises(NoSpeechError):
        transcribe_wav(wav_path, FakeBackend(transcript))


@pytest.mark.parametrize(
    'text',
    [
        '가' * (MAX_UTTERANCE_LENGTH + 1),
        '안녕\x00숨은 제어문자',
    ],
)
def test_transcribe_wav_rejects_unsafe_transcript_text(
    tmp_path: Path,
    text: str,
) -> None:
    """Oversized text and control characters stop at the STT boundary."""
    wav_path = _write_wav(tmp_path / 'voice.wav')
    backend = FakeBackend(
        BackendTranscript(text=text, confidence=0.9, language='ko')
    )

    with pytest.raises(TranscriptionError):
        transcribe_wav(wav_path, backend)


def test_backend_transcript_subclass_cannot_run_secret_access_hook(
    tmp_path: Path,
) -> None:
    """A result subclass is rejected before its text property is touched."""
    private_detail = 'PRIVATE subclass hook /private/transcript'

    class ExplodingTranscript(BackendTranscript):
        """Raise if an untrusted subclass field is ever inspected."""

        def __getattribute__(self, name):
            """Make text access an observable unsafe operation."""
            if name == 'text':
                raise RuntimeError(private_detail)
            return super().__getattribute__(name)

    transcript = ExplodingTranscript(
        text='안녕',
        confidence=0.9,
        language='ko',
    )
    wav_path = _write_wav(tmp_path / 'voice.wav')

    with pytest.raises(TranscriptionError) as failure:
        transcribe_wav(wav_path, FakeBackend(transcript))

    _assert_chain_free_public_error(
        failure.value,
        private_detail,
        wav_path,
    )


def test_primitive_subclasses_cannot_execute_in_validation_or_repr(
    tmp_path: Path,
) -> None:
    """Hostile str and float hooks are neither called nor rendered."""
    private_detail = 'PRIVATE primitive hook /private/value'

    class HostileString(str):
        """A string subclass whose common hooks disclose private data."""

        def strip(self, _characters=None):
            """Fail if a validator treats this subclass as a plain string."""
            raise RuntimeError(private_detail)

        def __len__(self):
            """Fail if debug rendering measures the hostile value."""
            raise RuntimeError(private_detail)

        def __hash__(self):
            """Fail if allowlist membership hashes the hostile value."""
            raise RuntimeError(private_detail)

        def __repr__(self):
            """Return a deliberately sensitive debug representation."""
            return private_detail

    class HostileFloat(float):
        """A float subclass whose conversion and repr disclose data."""

        def __float__(self):
            """Fail if validation converts this subclass to float."""
            raise RuntimeError(private_detail)

        def __repr__(self):
            """Return a deliberately sensitive debug representation."""
            return private_detail

    hostile_string = HostileString('ordinary')
    hostile_float = HostileFloat(0.9)
    hostile = BackendTranscript(
        text=hostile_string,
        confidence=hostile_float,
        language=hostile_string,
        confidence_basis=hostile_string,
        model=hostile_string,
    )

    rendered = repr(hostile)
    assert private_detail not in rendered
    assert '/private/' not in rendered

    wav_path = _write_wav(tmp_path / 'voice.wav')
    for transcript in (
        BackendTranscript(
            text=hostile_string,
            confidence=0.9,
            language='ko',
        ),
        BackendTranscript(
            text='안녕',
            confidence=hostile_float,
            language='ko',
        ),
        BackendTranscript(
            text='안녕',
            confidence=0.9,
            language=hostile_string,
        ),
    ):
        with pytest.raises(TranscriptionError) as failure:
            transcribe_wav(wav_path, FakeBackend(transcript))
        _assert_chain_free_public_error(
            failure.value,
            private_detail,
            wav_path,
        )

    normalized = transcribe_wav(
        wav_path,
        FakeBackend(
            BackendTranscript(
                text='안녕',
                confidence=0.9,
                language='ko',
                confidence_basis=hostile_string,
                model=hostile_string,
            )
        ),
    )
    normalized_rendered = ' '.join(
        (
            repr(normalized),
            json.dumps(normalized.to_dict(), ensure_ascii=False),
            json.dumps(normalized.to_audit_dict(), ensure_ascii=False),
        )
    )
    assert private_detail not in normalized_rendered
    assert normalized.confidence_basis == 'backend_reported_uncalibrated'
    assert normalized.model == 'custom'


@pytest.mark.parametrize('confidence', [-0.01, 1.01, math.nan, math.inf])
def test_transcribe_wav_rejects_invalid_backend_confidence(
    tmp_path: Path,
    confidence: float,
) -> None:
    """Untrusted model metadata must be finite and within zero to one."""
    wav_path = _write_wav(tmp_path / 'voice.wav')
    backend = FakeBackend(
        BackendTranscript(
            text='안녕',
            confidence=confidence,
            language='ko',
        )
    )

    with pytest.raises(TranscriptionError, match='confidence'):
        transcribe_wav(wav_path, backend)


def test_transcribe_wav_resanitizes_typed_and_unknown_backend_errors(
    tmp_path: Path,
) -> None:
    """Even typed backend errors cross as fresh chain-free failures."""
    wav_path = _write_wav(tmp_path / 'private-name.wav')
    private_detail = 'PRIVATE typed error /private/backend/model'
    unavailable = BackendUnavailableError('model is unavailable')
    unavailable.private_detail = private_detail
    with pytest.raises(BackendUnavailableError) as known:
        transcribe_wav(wav_path, FakeBackend(error=unavailable))
    assert known.value is not unavailable
    assert not hasattr(known.value, 'private_detail')
    _assert_chain_free_public_error(
        known.value,
        private_detail,
        wav_path,
    )

    secret = 'raw transcript and /private/model/path'
    with pytest.raises(TranscriptionError) as unknown:
        transcribe_wav(wav_path, FakeBackend(error=RuntimeError(secret)))
    _assert_chain_free_public_error(unknown.value, secret, wav_path)


def test_local_result_audit_excludes_text_path_and_audio(
    tmp_path: Path,
) -> None:
    """The explicit audit projection contains measurements, not content."""
    wav_path = _write_wav(tmp_path / 'private-speaker.wav')
    result = transcribe_wav(wav_path, FakeBackend())

    audit = result.to_audit_dict()
    rendered = json.dumps(audit, ensure_ascii=False)

    assert result.text not in rendered
    assert str(wav_path) not in rendered
    assert 'audio' not in rendered.lower()
    assert result.text not in repr(result)
    assert str(wav_path) not in repr(result)
    assert result.text not in repr(FakeBackend().transcript)
    assert audit['text_chars'] == len(result.text)
    assert audit['duration_ms'] == 500
    assert audit['sample_rate_hz'] == 16000


def test_backend_transcript_repr_sanitizes_all_untrusted_labels() -> None:
    """Backend metadata and text cannot escape through debug rendering."""
    private_token = 'PRIVATE_LABEL_9f3a'
    transcript = BackendTranscript(
        text='spoken {}'.format(private_token),
        confidence=0.5,
        language='../{}/language'.format(private_token),
        confidence_basis='{}/secret-basis'.format(private_token),
        model='/private/{}/model'.format(private_token),
    )

    rendered = repr(transcript)

    assert private_token not in rendered
    assert '/private/' not in rendered
    assert '../' not in rendered
    assert 'text_chars=' in rendered


def test_direct_local_result_sanitizes_metadata_labels() -> None:
    """Direct dataclass construction cannot poison audit-safe labels."""
    private_token = 'PRIVATE_RESULT_LABEL_7c2b'
    metadata = WavMetadata(
        duration_ms=500,
        sample_rate_hz=16000,
        channel_count=1,
        sample_width_bytes=2,
        frame_count=8000,
        file_size_bytes=16044,
    )
    with pytest.raises(TranscriptionError) as invalid_language:
        LocalSTTResult(
            text='안녕',
            confidence=0.5,
            language='../{}/language'.format(private_token),
            audio_metadata=metadata,
        )
    _assert_chain_free_public_error(
        invalid_language.value,
        private_token,
    )

    result = LocalSTTResult(
        text='안녕',
        confidence=0.5,
        language='ko',
        audio_metadata=metadata,
        confidence_basis='{}/basis'.format(private_token),
        backend='../{}/backend'.format(private_token),
        model='/private/{}/model'.format(private_token),
    )

    rendered = ' '.join(
        (
            repr(result),
            json.dumps(result.to_dict(), ensure_ascii=False),
            json.dumps(result.to_audit_dict(), ensure_ascii=False),
        )
    )

    assert private_token not in rendered
    assert '/private/' not in rendered
    assert '../' not in rendered
    for label in (
        result.language,
        result.confidence_basis,
        result.backend,
        result.model,
    ):
        if label is not None:
            assert private_token not in label
            assert '/' not in label
            assert '\\' not in label


def test_malicious_backend_class_name_and_model_path_are_sanitized(
    tmp_path: Path,
) -> None:
    """Fallback backend metadata is safe even for adversarial classes."""
    private_token = 'PRIVATE_BACKEND_LABEL_41de'

    def malicious_transcribe(
        _self,
        _wav_path: Path,
        *,
        language: str,
    ) -> BackendTranscript:
        return BackendTranscript(
            text='안녕',
            confidence=0.9,
            language=language,
            model=None,
        )

    malicious_type = type(
        '../{}/backend'.format(private_token),
        (),
        {
            'model': '/private/{}/model'.format(private_token),
            'transcribe': malicious_transcribe,
        },
    )
    result = transcribe_wav(
        _write_wav(tmp_path / 'voice.wav'),
        malicious_type(),
    )
    rendered = ' '.join(
        (
            repr(result),
            json.dumps(result.to_dict(), ensure_ascii=False),
            json.dumps(result.to_audit_dict(), ensure_ascii=False),
        )
    )

    assert private_token not in rendered
    assert '/private/' not in rendered
    assert '../' not in rendered
    assert '/' not in result.backend
    assert result.model is None or '/' not in result.model


def test_capture_microphone_uses_argv_and_cleans_temporary_wav(
    tmp_path: Path,
) -> None:
    """Capture uses an injected process runner and removes the recording."""
    observed = {}

    def fake_which(command: str) -> str:
        assert command == 'arecord'
        return '/usr/bin/arecord'

    def fake_runner(argv, **kwargs):
        observed['argv'] = argv
        observed['kwargs'] = kwargs
        observed['precreated'] = Path(argv[-1]).is_file()
        observed['initial_mode'] = os.stat(argv[-1]).st_mode & 0o777
        _write_wav(Path(argv[-1]), duration_ms=250)
        return subprocess.CompletedProcess(argv, 0, stdout=b'', stderr=b'')

    with capture_microphone(
        seconds=1,
        audio_device='plughw:1,0;not-a-command',
        runner=fake_runner,
        which=fake_which,
        temp_root=tmp_path,
    ) as wav_path:
        assert wav_path.exists()
        assert wav_path.parent != tmp_path
        assert os.stat(wav_path).st_mode & 0o777 == 0o600
        assert validate_pcm_wav(wav_path).duration_ms == 250
        captured_path = wav_path

    assert not captured_path.exists()
    assert observed['argv'][0] == '/usr/bin/arecord'
    assert 'plughw:1,0;not-a-command' in observed['argv']
    assert observed['precreated'] is True
    assert observed['initial_mode'] == 0o600
    assert observed['kwargs'].get('shell', False) is False
    assert observed['kwargs']['check'] is True
    assert observed['argv'] == [
        '/usr/bin/arecord',
        '--quiet',
        '--file-type',
        'wav',
        '--format',
        'S16_LE',
        '--channels',
        '1',
        '--rate',
        '16000',
        '--duration',
        '1',
        '--device',
        'plughw:1,0;not-a-command',
        str(captured_path),
    ]


@pytest.mark.parametrize(
    'seconds',
    [False, True, 0, 0.5, 1.01, 5.0, 30.01, math.nan, math.inf],
)
def test_capture_microphone_rejects_out_of_range_duration(
    tmp_path: Path,
    seconds: float,
) -> None:
    """Capture duration is a strict one-through-thirty second boundary."""
    with pytest.raises(CaptureError):
        with capture_microphone(
            seconds=seconds,
            which=lambda _name: pytest.fail('recorder lookup must not run'),
            temp_root=tmp_path,
        ):
            pytest.fail('capture unexpectedly yielded')


def test_capture_microphone_cleans_up_when_consumer_fails(
    tmp_path: Path,
) -> None:
    """The temporary recording is deleted even after a downstream error."""
    observed = {}

    def fake_runner(argv, **_kwargs):
        observed['path'] = Path(argv[-1])
        _write_wav(observed['path'])
        return subprocess.CompletedProcess(argv, 0)

    with pytest.raises(RuntimeError, match='downstream'):
        with capture_microphone(
            runner=fake_runner,
            which=lambda _name: '/usr/bin/arecord',
            temp_root=tmp_path,
        ):
            raise RuntimeError('downstream failure')

    assert not observed['path'].exists()


def test_capture_microphone_reports_missing_or_failed_recorder(
    tmp_path: Path,
) -> None:
    """Unavailable and failed local capture commands become typed errors."""
    with pytest.raises(CaptureError):
        with capture_microphone(
            which=lambda _name: None,
            temp_root=tmp_path,
        ):
            pytest.fail('capture unexpectedly yielded')

    def failed_runner(argv, **_kwargs):
        raise subprocess.CalledProcessError(
            1,
            argv,
            stderr=b'sensitive ALSA details',
        )

    with pytest.raises(CaptureError) as failed:
        with capture_microphone(
            runner=failed_runner,
            which=lambda _name: '/usr/bin/arecord',
            temp_root=tmp_path,
        ):
            pytest.fail('capture unexpectedly yielded')
    _assert_chain_free_public_error(
        failed.value,
        'sensitive ALSA details',
    )


def test_capture_sanitizes_injected_runner_runtime_error_and_cleans_up(
    tmp_path: Path,
) -> None:
    """An arbitrary runner exception becomes a fresh cleaned capture error."""
    observed = {}

    def failing_runner(argv, **_kwargs):
        observed['path'] = Path(argv[-1])
        observed['parent'] = observed['path'].parent
        private_detail = (
            'PRIVATE runner failure {}'.format(observed['path'])
        )
        observed['private_detail'] = private_detail
        raise RuntimeError(private_detail)

    with pytest.raises(CaptureError) as failure:
        with capture_microphone(
            runner=failing_runner,
            which=lambda _name: '/usr/bin/arecord',
            temp_root=tmp_path,
        ):
            pytest.fail('capture unexpectedly yielded')

    assert type(failure.value) is CaptureError
    _assert_chain_free_public_error(
        failure.value,
        observed['private_detail'],
        observed['path'],
    )
    assert not observed['path'].exists()
    assert not observed['parent'].exists()


def test_capture_microphone_turns_timeout_into_error_and_cleans_up(
    tmp_path: Path,
) -> None:
    """A stuck recorder is bounded and leaves no temporary recording."""
    observed = {}

    def timed_out_runner(argv, **kwargs):
        observed['path'] = Path(argv[-1])
        observed['parent'] = observed['path'].parent
        observed['timeout'] = kwargs.get('timeout')
        raise subprocess.TimeoutExpired(argv, timeout=kwargs.get('timeout'))

    with pytest.raises(CaptureError) as failure:
        with capture_microphone(
            seconds=1,
            runner=timed_out_runner,
            which=lambda _name: '/usr/bin/arecord',
            temp_root=tmp_path,
        ):
            pytest.fail('capture unexpectedly yielded')

    _assert_chain_free_public_error(
        failure.value,
        'TimeoutExpired',
        observed['path'],
    )
    assert observed['timeout'] is not None
    assert not observed['path'].exists()
    assert not observed['parent'].exists()


def test_build_transcript_event_matches_existing_speech_contract(
    tmp_path: Path,
) -> None:
    """An unverified adapter produces a final event of unknown origin."""
    result = transcribe_wav(
        _write_wav(tmp_path / 'voice.wav'),
        FakeBackend(),
    )

    event = build_transcript_event(
        result,
        _binding(),
        utterance_id='utterance-local-1',
        sequence=7,
        capture_epoch=2,
        timestamp_ns=123456789,
    )

    assert isinstance(event, SpeechTranscriptEvent)
    assert event.text == result.text
    assert event.confidence == result.confidence
    assert event.is_final is True
    assert event.capture_origin == 'unknown'
    assert event.sequence == 7
    assert event.capture_epoch == 2
    assert event.source_timestamp_ns == 123456789
    assert event.audio_metadata.duration_ms == 500
    assert event.speech_session_id == 'speech-session-1'
    assert event.conversation_id == 'voice-conversation-1'
    assert event.speaker_id == 'trusted-speaker'


def test_result_builder_rejects_boundary_type_subclasses() -> None:
    """Subclass hooks cannot cross result metadata or identity boundaries."""

    class DerivedWavMetadata(WavMetadata):
        """Represent an untrusted metadata subclass."""

    class DerivedLocalSTTResult(LocalSTTResult):
        """Represent an untrusted final-result subclass."""

    class DerivedTrustedSpeechBinding(TrustedSpeechBinding):
        """Represent an untrusted identity-binding subclass."""

    metadata = WavMetadata(
        duration_ms=500,
        sample_rate_hz=16000,
        channel_count=1,
        sample_width_bytes=2,
        frame_count=8000,
        file_size_bytes=16044,
    )
    derived_metadata = DerivedWavMetadata(**metadata.to_dict())
    with pytest.raises(TranscriptionError) as invalid_metadata:
        LocalSTTResult(
            text='안녕',
            confidence=0.9,
            language='ko',
            audio_metadata=derived_metadata,
        )
    _assert_chain_free_public_error(invalid_metadata.value)

    derived_result = DerivedLocalSTTResult(
        text='안녕',
        confidence=0.9,
        language='ko',
        audio_metadata=metadata,
    )
    binding = _binding()
    with pytest.raises(TypeError, match='LocalSTTResult'):
        build_transcript_event(
            derived_result,
            binding,
            utterance_id='subclass-result',
            sequence=1,
        )

    derived_binding = DerivedTrustedSpeechBinding.from_dict(
        binding.to_dict()
    )
    result = LocalSTTResult(
        text='안녕',
        confidence=0.9,
        language='ko',
        audio_metadata=metadata,
    )
    with pytest.raises(TypeError, match='TrustedSpeechBinding'):
        build_transcript_event(
            result,
            derived_binding,
            utterance_id='subclass-binding',
            sequence=1,
        )


def test_default_origin_event_is_rejected_by_speech_coordinator(
    tmp_path: Path,
) -> None:
    """STT text cannot impersonate trusted microphone provenance."""
    memory_store = SQLiteMemoryStore(':memory:')
    conversation_store = SQLiteConversationStore(':memory:')
    orchestrator = AgentOrchestrator(
        provider=MockProvider(),
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(),
        trusted_robot_state=True,
    )
    coordinator = SpeechConversationCoordinator(orchestrator)
    binding = _binding()
    coordinator.open_session(binding)
    result = transcribe_wav(
        _write_wav(tmp_path / 'voice.wav'),
        FakeBackend(),
    )
    event = build_transcript_event(
        result,
        binding,
        utterance_id='utterance-unverified-1',
        sequence=1,
        capture_epoch=1,
        timestamp_ns=123456789,
    )
    try:
        outcome = coordinator.handle_transcript(event)
        assert outcome.status == 'rejected'
        assert outcome.code == 'unknown_capture_origin'
        assert outcome.tts_request is None
    finally:
        conversation_store.close()
        memory_store.close()


def test_built_event_is_accepted_by_offline_speech_coordinator(
    tmp_path: Path,
) -> None:
    """The adapter output can drive one existing agent turn end to end."""
    memory_store = SQLiteMemoryStore(':memory:')
    conversation_store = SQLiteConversationStore(':memory:')
    orchestrator = AgentOrchestrator(
        provider=MockProvider(),
        memory_store=memory_store,
        conversation_store=conversation_store,
        safety_policy=SafetyPolicy(),
        trusted_robot_state=True,
    )
    coordinator = SpeechConversationCoordinator(orchestrator)
    binding = _binding()
    coordinator.open_session(binding)
    result = transcribe_wav(
        _write_wav(tmp_path / 'voice.wav'),
        FakeBackend(),
    )
    event = build_transcript_event(
        result,
        binding,
        utterance_id='utterance-local-1',
        sequence=1,
        capture_epoch=1,
        timestamp_ns=123456789,
        capture_origin='microphone',
    )
    try:
        outcome = coordinator.handle_transcript(event)
        assert outcome.status == 'responded'
        assert outcome.tts_request is not None
    finally:
        conversation_store.close()
        memory_store.close()


def test_faster_whisper_backend_loads_lazily_and_disables_download(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Creating the backend is cheap; the first call loads local files only."""
    observed = {'loads': [], 'calls': []}

    class FakeSegment:
        """Small faster-whisper segment fixture."""

        text = ' 로컬 음성 인식 '
        avg_logprob = math.log(0.8)

    class FakeWhisperModel:
        """Record lazy model construction and inference options."""

        def __init__(self, model, **kwargs) -> None:
            observed['loads'].append((model, kwargs))

        def transcribe(self, path, **kwargs):
            observed['calls'].append((path, kwargs))
            return iter([FakeSegment()]), types.SimpleNamespace(language='ko')

    monkeypatch.setitem(
        sys.modules,
        'faster_whisper',
        types.SimpleNamespace(WhisperModel=FakeWhisperModel),
    )
    backend = FasterWhisperBackend(
        model='tiny',
        device='cpu',
        compute_type='int8',
        allow_model_download=False,
    )
    assert observed['loads'] == []

    backend.prepare()
    assert len(observed['loads']) == 1

    wav_path = _write_wav(tmp_path / 'voice.wav')
    transcript = backend.transcribe(wav_path, language='ko')

    assert transcript.text == '로컬 음성 인식'
    assert observed['loads'][0][0] == 'tiny'
    assert observed['loads'][0][1]['device'] == 'cpu'
    assert observed['loads'][0][1]['compute_type'] == 'int8'
    assert observed['loads'][0][1]['local_files_only'] is True
    assert observed['calls'][0][0] == str(wav_path)
    assert observed['calls'][0][1]['language'] == 'ko'
    assert observed['calls'][0][1]['beam_size'] == 1
    assert observed['calls'][0][1]['condition_on_previous_text'] is False


def test_faster_whisper_missing_dependency_is_a_typed_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The optional runtime is imported lazily with an actionable failure."""
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == 'faster_whisper':
            raise ImportError('sensitive interpreter detail')
        return original_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, 'faster_whisper', raising=False)
    monkeypatch.setattr(builtins, '__import__', blocked_import)
    backend = FasterWhisperBackend()

    with pytest.raises(BackendUnavailableError) as error:
        backend.prepare()
    _assert_chain_free_public_error(
        error.value,
        'sensitive interpreter detail',
    )


def test_faster_whisper_native_import_oserror_is_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A native dependency load failure cannot disclose its library path."""
    private_detail = 'PRIVATE native library /private/lib/cudnn.so'
    original_import = builtins.__import__

    def blocked_import(name, *args, **kwargs):
        if name == 'faster_whisper':
            raise OSError(private_detail)
        return original_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, 'faster_whisper', raising=False)
    monkeypatch.setattr(builtins, '__import__', blocked_import)
    backend = FasterWhisperBackend()

    with pytest.raises(BackendUnavailableError) as failure:
        backend.prepare()

    assert type(failure.value) is BackendUnavailableError
    _assert_chain_free_public_error(failure.value, private_detail)


def test_faster_whisper_import_keyboard_interrupt_passes_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model preparation never converts an operator interrupt to an error."""
    original_import = builtins.__import__

    def interrupted_import(name, *args, **kwargs):
        if name == 'faster_whisper':
            raise KeyboardInterrupt()
        return original_import(name, *args, **kwargs)

    monkeypatch.delitem(sys.modules, 'faster_whisper', raising=False)
    monkeypatch.setattr(builtins, '__import__', interrupted_import)

    with pytest.raises(KeyboardInterrupt):
        FasterWhisperBackend().prepare()


def test_faster_whisper_model_load_failure_has_no_private_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model-loader internals are removed from the public exception graph."""
    private_detail = 'PRIVATE loader failure /private/model/cache'

    class FailingWhisperModel:
        """Fail while loading a model with deliberately private detail."""

        def __init__(self, *_args, **_kwargs) -> None:
            raise RuntimeError(private_detail)

    monkeypatch.setitem(
        sys.modules,
        'faster_whisper',
        types.SimpleNamespace(WhisperModel=FailingWhisperModel),
    )
    backend = FasterWhisperBackend()

    with pytest.raises(BackendUnavailableError) as failure:
        backend.prepare()

    _assert_chain_free_public_error(failure.value, private_detail)


def test_faster_whisper_inference_failure_has_no_private_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Inference internals and the local WAV path leave no error chain."""
    private_detail = 'PRIVATE inference failure /private/runtime/cache'

    class FailingWhisperModel:
        """Load successfully and fail only when inference starts."""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def transcribe(self, _path, **_kwargs):
            raise RuntimeError(private_detail)

    monkeypatch.setitem(
        sys.modules,
        'faster_whisper',
        types.SimpleNamespace(WhisperModel=FailingWhisperModel),
    )
    backend = FasterWhisperBackend()
    wav_path = _write_wav(tmp_path / 'PRIVATE-secret-speaker.wav')

    with pytest.raises(TranscriptionError) as failure:
        backend.transcribe(wav_path, language='ko')

    _assert_chain_free_public_error(
        failure.value,
        private_detail,
        wav_path,
    )


@pytest.mark.parametrize(
    'configuration',
    [
        {'model': 'large-v3'},
        {'model': '../private-model'},
        {'device': 'tpu'},
        {'compute_type': 'float32'},
        {'allow_model_download': 'yes'},
    ],
)
def test_faster_whisper_rejects_unsupported_configuration(
    configuration: dict,
) -> None:
    """Programmatic callers cannot bypass the bounded CLI choices."""
    with pytest.raises(BackendUnavailableError):
        FasterWhisperBackend(**configuration)


def test_faster_whisper_prefers_mean_word_probability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reported word probabilities form the primary confidence heuristic."""

    class FakeWhisperModel:
        """Return two words with deterministic probabilities."""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def transcribe(self, _path, **_kwargs):
            words = [
                types.SimpleNamespace(probability=0.6),
                types.SimpleNamespace(probability=0.8),
            ]
            segment = types.SimpleNamespace(
                text=' 안녕 말벗 ',
                words=words,
                avg_logprob=math.log(0.1),
            )
            return iter([segment]), types.SimpleNamespace(language='ko')

    monkeypatch.setitem(
        sys.modules,
        'faster_whisper',
        types.SimpleNamespace(WhisperModel=FakeWhisperModel),
    )
    backend = FasterWhisperBackend(model='tiny')

    transcript = backend.transcribe(
        _write_wav(tmp_path / 'voice.wav'),
        language='ko',
    )

    assert transcript.confidence == pytest.approx(0.7)
    assert 'word' in transcript.confidence_basis


def test_faster_whisper_falls_back_to_weighted_segment_logprob(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Segment log probabilities are exponentiated and duration-weighted."""

    class FakeWhisperModel:
        """Return segments without word-level probabilities."""

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def transcribe(self, _path, **_kwargs):
            segments = [
                types.SimpleNamespace(
                    text=' 가 ',
                    words=None,
                    avg_logprob=math.log(0.25),
                    start=0.0,
                    end=1.0,
                ),
                types.SimpleNamespace(
                    text=' 나다라 ',
                    words=None,
                    avg_logprob=math.log(0.81),
                    start=1.0,
                    end=4.0,
                ),
            ]
            return iter(segments), types.SimpleNamespace(language='ko')

    monkeypatch.setitem(
        sys.modules,
        'faster_whisper',
        types.SimpleNamespace(WhisperModel=FakeWhisperModel),
    )
    backend = FasterWhisperBackend(model='tiny')

    transcript = backend.transcribe(
        _write_wav(tmp_path / 'voice.wav'),
        language='ko',
    )

    assert transcript.confidence == pytest.approx(
        ((0.25 * 1) + (0.81 * 3)) / 4
    )
    assert 'segment' in transcript.confidence_basis


def test_cli_wav_prints_only_transcript(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """The happy CLI path emits one transcript line and no diagnostics."""
    wav_path = _write_wav(tmp_path / 'voice.wav')
    backend = FakeBackend()
    monkeypatch.setattr(
        'malbut_agent_server.local_stt.FasterWhisperBackend',
        lambda **_kwargs: backend,
    )

    exit_code = main(['--wav', str(wav_path), '--language', 'ko'])
    captured = capsys.readouterr()

    assert exit_code == 0
    assert captured.out == '안녕하세요 말벗\n'
    assert captured.err == ''
    assert len(backend.calls) == 1
    snapshot_path, requested_language = backend.calls[0]
    assert snapshot_path != wav_path
    assert not snapshot_path.exists()
    assert requested_language == 'ko'


def test_cli_validates_wav_before_preparing_model(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
) -> None:
    """Bad input fails without importing or loading the optional model."""
    invalid_wav = tmp_path / 'invalid-private-input.wav'
    invalid_wav.write_bytes(b'not a WAV')
    backend = FakeBackend()

    exit_code = main(
        ['--wav', str(invalid_wav)],
        backend_factory=lambda **_kwargs: backend,
    )

    assert exit_code == 2
    assert backend.prepare_calls == 0
    assert backend.calls == []
    assert str(invalid_wav) not in capsys.readouterr().err


def test_cli_prepares_model_before_opening_microphone(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """A missing model fails before collecting private microphone audio."""
    order = []

    class OrderedBackend(FakeBackend):
        """Record explicit model preparation and inference."""

        def prepare(self) -> None:
            order.append('prepare')

        def transcribe(self, wav_path: Path, *, language: str):
            order.append('transcribe')
            return super().transcribe(wav_path, language=language)

    backend = OrderedBackend()

    @contextmanager
    def fake_capture(**_kwargs):
        order.append('capture')
        yield _write_wav(tmp_path / 'microphone.wav')

    monkeypatch.setattr(
        'malbut_agent_server.local_stt.FasterWhisperBackend',
        lambda **_kwargs: backend,
    )
    monkeypatch.setattr(
        'malbut_agent_server.local_stt.capture_microphone',
        fake_capture,
    )

    assert main(['--microphone']) == 0
    assert order == ['prepare', 'capture', 'transcribe']
    assert capsys.readouterr().out == '안녕하세요 말벗\n'


@pytest.mark.parametrize('seconds', [1, 30])
def test_cli_accepts_integer_capture_boundaries(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,
    seconds: int,
) -> None:
    """Both inclusive integer capture boundaries reach arecord exactly."""
    observed = {}

    def fake_runner(argv, **_kwargs):
        observed['argv'] = argv
        _write_wav(Path(argv[-1]))
        return subprocess.CompletedProcess(argv, 0)

    exit_code = main(
        ['--microphone', '--seconds', str(seconds)],
        backend_factory=lambda **_kwargs: FakeBackend(),
        runner=fake_runner,
        which=lambda _name: '/usr/bin/arecord',
    )

    assert exit_code == 0
    duration_index = observed['argv'].index('--duration') + 1
    assert observed['argv'][duration_index] == str(seconds)
    assert capsys.readouterr().out == '안녕하세요 말벗\n'


def test_cli_keyboard_interrupt_returns_130_and_releases_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    """Ctrl-C has shell-standard status and still exits capture context."""
    state = {'cleaned': False}
    backend = FakeBackend(error=KeyboardInterrupt())

    @contextmanager
    def fake_capture(**_kwargs):
        wav_path = _write_wav(tmp_path / 'microphone.wav')
        try:
            yield wav_path
        finally:
            wav_path.unlink(missing_ok=True)
            state['cleaned'] = True

    monkeypatch.setattr(
        'malbut_agent_server.local_stt.FasterWhisperBackend',
        lambda **_kwargs: backend,
    )
    monkeypatch.setattr(
        'malbut_agent_server.local_stt.capture_microphone',
        fake_capture,
    )

    assert main(['--microphone']) == 130
    captured = capsys.readouterr()
    assert captured.out == ''
    assert state['cleaned'] is True
    assert not (tmp_path / 'microphone.wav').exists()


@pytest.mark.parametrize(
    ('error_type', 'exit_code'),
    [
        (AudioValidationError, 2),
        (CaptureError, 3),
        (BackendUnavailableError, 4),
        (NoSpeechError, 5),
        (TranscriptionError, 6),
    ],
)
def test_cli_maps_typed_errors_without_disclosing_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    error_type: type,
    exit_code: int,
) -> None:
    """Every public error has a stable sanitized process exit status."""
    wav_path = _write_wav(tmp_path / 'do-not-log-this-path.wav')
    sensitive = 'spoken secret and /private/model/path'

    def fail(*_args, **_kwargs):
        raise error_type(sensitive)

    monkeypatch.setattr(
        'malbut_agent_server.local_stt.transcribe_wav',
        fail,
    )
    monkeypatch.setattr(
        'malbut_agent_server.local_stt.FasterWhisperBackend',
        lambda **_kwargs: FakeBackend(),
    )

    actual = main(['--wav', str(wav_path)])
    captured = capsys.readouterr()

    assert actual == exit_code
    assert captured.out == ''
    assert sensitive not in captured.err
    assert str(wav_path) not in captured.err
    assert 'error' in captured.err.lower()


@pytest.mark.parametrize(
    ('source_args', 'option', 'private_value'),
    [
        (
            ['--wav', 'voice.wav'],
            '--language',
            '../PRIVATE-language/value',
        ),
        (
            ['--microphone'],
            '--audio-device',
            'A' * 129 + 'PRIVATE-device',
        ),
        (
            ['--microphone'],
            '--audio-device',
            'plughw:1,0\nPRIVATE-device',
        ),
    ],
)
def test_cli_rejects_private_language_and_device_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
    source_args: list,
    option: str,
    private_value: str,
) -> None:
    """Unsafe labels fail in argparse before model or capture setup."""
    state = {'factory_calls': 0}

    def forbidden_factory(**_kwargs):
        state['factory_calls'] += 1
        pytest.fail('backend factory must not run')

    def forbidden_capture(**_kwargs):
        pytest.fail('microphone capture must not run')

    monkeypatch.setattr(
        'malbut_agent_server.local_stt.capture_microphone',
        forbidden_capture,
    )

    with pytest.raises(SystemExit) as failure:
        main(
            source_args + [option, private_value],
            backend_factory=forbidden_factory,
        )

    assert failure.value.code == 2
    assert state['factory_calls'] == 0
    captured = capsys.readouterr()
    assert captured.out == ''
    assert private_value not in captured.err
    assert 'PRIVATE' not in captured.err
    assert 'invalid arguments' in captured.err


def test_cli_preserves_safe_punctuated_audio_device_as_one_argv(
    capsys: pytest.CaptureFixture,
) -> None:
    """A bounded ALSA device label remains one non-shell argument."""
    audio_device = 'plughw:1,0;not-a-command'
    observed = {}

    def fake_runner(argv, **kwargs):
        observed['argv'] = argv
        observed['shell'] = kwargs.get('shell')
        _write_wav(Path(argv[-1]))
        return subprocess.CompletedProcess(argv, 0)

    exit_code = main(
        ['--microphone', '--audio-device', audio_device],
        backend_factory=lambda **_kwargs: FakeBackend(),
        runner=fake_runner,
        which=lambda _name: '/usr/bin/arecord',
    )

    assert exit_code == 0
    assert observed['argv'].count(audio_device) == 1
    device_index = observed['argv'].index('--device') + 1
    assert observed['argv'][device_index] == audio_device
    assert observed['shell'] is False
    assert capsys.readouterr().out == '안녕하세요 말벗\n'


@pytest.mark.parametrize('seconds', ['0', '31', '1.01', '5.0', 'true'])
def test_cli_rejects_invalid_capture_seconds(
    seconds: str,
    capsys: pytest.CaptureFixture,
) -> None:
    """CLI capture duration accepts only integer text from one to thirty."""
    with pytest.raises(SystemExit) as failure:
        main(['--microphone', '--seconds', seconds])

    assert failure.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ''
    assert seconds not in captured.err
    assert 'invalid arguments' in captured.err


def test_cli_requires_exactly_one_audio_source() -> None:
    """Argument parsing rejects missing and conflicting input modes."""
    with pytest.raises(SystemExit) as missing:
        main([])
    assert missing.value.code == 2

    with pytest.raises(SystemExit) as conflicting:
        main(['--wav', 'voice.wav', '--microphone'])
    assert conflicting.value.code == 2
