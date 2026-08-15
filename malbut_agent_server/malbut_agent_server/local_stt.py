"""Temporary, local-only speech-to-text adapter for one PCM WAV.

The adapter deliberately keeps raw audio on the local machine and passes
only a final transcript plus bounded metadata into :mod:`speech`.  Model
loading is lazy, and model downloads are disabled unless a caller explicitly
opts in.
"""

import argparse
import math
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import wave
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Union,
)

from malbut_agent_server.schemas import MAX_UTTERANCE_LENGTH
from malbut_agent_server.speech import (
    SPEECH_SCHEMA_VERSION,
    AudioMetadata,
    SpeechTranscriptEvent,
    TrustedSpeechBinding,
)


MAX_WAV_BYTES = 6 * 1024 * 1024
MAX_AUDIO_DURATION_SECONDS = 30.0
MIN_SAMPLE_RATE_HZ = 8000
MAX_SAMPLE_RATE_HZ = 48000
PCM_SAMPLE_WIDTH_BYTES = 2
CAPTURE_SAMPLE_RATE_HZ = 16000
CAPTURE_CHANNEL_COUNT = 1
DEFAULT_CAPTURE_SECONDS = 5
DEFAULT_LANGUAGE = 'ko'
DEFAULT_MODEL = 'base'
DEFAULT_DEVICE = 'cpu'
DEFAULT_COMPUTE_TYPE = 'int8'
MAX_AUDIO_DEVICE_LENGTH = 128
ALLOWED_CONFIDENCE_BASES = frozenset(
    {
        'backend_reported_uncalibrated',
        'word_probability_mean_uncalibrated',
        'segment_avg_logprob_duration_weighted_exp_uncalibrated',
        'unavailable_uncalibrated',
    }
)

PathLike = Union[str, os.PathLike]
Runner = Callable[..., Any]
Which = Callable[[str], Optional[str]]


class LocalSTTError(RuntimeError):
    """Base class for stable, sanitized local STT failures."""

    code = 'local_stt_error'
    exit_code = 1
    default_message = 'local speech recognition failed'
    reason_messages: Dict[str, str] = {}

    def __init__(self, reason: Optional[str] = None) -> None:
        """Select only a pre-approved public message for the failure."""
        safe_reason = (
            reason
            if type(reason) is str and reason in self.reason_messages
            else None
        )
        message = self.reason_messages.get(
            safe_reason,
            self.default_message,
        )
        self.reason = safe_reason
        self.public_message = message
        super().__init__(message)

    def __repr__(self) -> str:
        """Avoid rendering paths, transcripts, or wrapped exceptions."""
        return '{}(code={!r})'.format(type(self).__name__, self.code)

    def to_dict(self) -> Dict[str, Any]:
        """Return a stable public error without implementation details."""
        return {
            'code': self.code,
            'message': self.public_message,
            'exit_code': self.exit_code,
        }

    def to_audit_dict(self) -> Dict[str, Any]:
        """Return the same content-free projection for local auditing."""
        return self.to_dict()


class AudioValidationError(LocalSTTError):
    """Raised when a WAV does not meet the bounded PCM16 contract."""

    code = 'invalid_audio'
    exit_code = 2
    default_message = 'audio input is not a supported PCM16 WAV'
    reason_messages = {
        'input': 'audio input is unavailable or unreadable',
        'size': 'audio file size must be at most 6 MiB',
        'container': 'audio input must be a RIFF PCM WAV',
        'pcm': 'audio input must use uncompressed PCM',
        'sample_width': 'audio input must use 16-bit samples',
        'sample_rate': 'audio sample rate must be between 8000 and 48000 Hz',
        'channel': 'audio channel count must be one or two',
        'duration': (
            'audio duration must be greater than zero '
            'and at most 30 seconds'
        ),
        'truncated': 'audio PCM payload is incomplete',
        'cleanup': 'private audio cleanup failed',
    }


class CaptureError(LocalSTTError):
    """Raised when a private one-shot microphone capture fails."""

    code = 'audio_capture_failed'
    exit_code = 3
    default_message = 'microphone capture failed'
    reason_messages = {
        'configuration': 'microphone capture configuration is invalid',
        'unavailable': 'the local arecord command is unavailable',
        'invalid_output': 'microphone capture produced invalid audio',
        'cleanup': 'private microphone audio cleanup failed',
    }


class BackendUnavailableError(LocalSTTError):
    """Raised when faster-whisper or a requested local model is absent."""

    code = 'stt_backend_unavailable'
    exit_code = 4
    default_message = 'the local speech recognition backend is unavailable'


class NoSpeechError(LocalSTTError):
    """Raised when inference produces no non-whitespace transcript."""

    code = 'no_speech_detected'
    exit_code = 5
    default_message = 'no speech was detected'


class TranscriptionError(LocalSTTError):
    """Raised when local inference returns an invalid or failed result."""

    code = 'transcription_failed'
    exit_code = 6
    default_message = 'local speech recognition failed'
    reason_messages = {
        'confidence': 'speech recognition confidence is invalid',
        'language': 'speech recognition language metadata is invalid',
        'result': 'speech recognition returned an invalid result',
        'text_length': 'speech recognition text exceeds the supported length',
    }


def _clone_public_error(error: LocalSTTError) -> LocalSTTError:
    """Clone an allowlisted error without its traceback or exception chain."""
    error_type = type(error)
    if error_type not in {
        LocalSTTError,
        AudioValidationError,
        CaptureError,
        BackendUnavailableError,
        NoSpeechError,
        TranscriptionError,
    }:
        return LocalSTTError()
    try:
        reason = error.reason
    except Exception:
        reason = None
    if error_type is AudioValidationError:
        return AudioValidationError(reason)
    if error_type is CaptureError:
        return CaptureError(reason)
    if error_type is BackendUnavailableError:
        return BackendUnavailableError(reason)
    if error_type is NoSpeechError:
        return NoSpeechError(reason)
    if error_type is TranscriptionError:
        return TranscriptionError(reason)
    return LocalSTTError()


def _raise_chain_free(error: LocalSTTError) -> None:
    """Raise a public error after explicitly severing generator context."""
    try:
        raise error
    except LocalSTTError:
        error.__cause__ = None
        error.__context__ = None
        error.__suppress_context__ = True
        raise


@dataclass(frozen=True)
class WavMetadata:
    """Content-free metadata from a validated PCM16 WAV."""

    duration_ms: int
    sample_rate_hz: int
    channel_count: int
    sample_width_bytes: int
    frame_count: int
    file_size_bytes: int

    def __post_init__(self) -> None:
        """Reject invalid metadata even when constructed directly."""
        integer_fields = (
            self.duration_ms,
            self.sample_rate_hz,
            self.channel_count,
            self.sample_width_bytes,
            self.frame_count,
            self.file_size_bytes,
        )
        if any(
            type(value) is not int
            for value in integer_fields
        ):
            raise AudioValidationError('input')
        if not 1 <= self.duration_ms <= 30000:
            raise AudioValidationError('duration')
        if not MIN_SAMPLE_RATE_HZ <= self.sample_rate_hz <= MAX_SAMPLE_RATE_HZ:
            raise AudioValidationError('sample_rate')
        if self.channel_count not in (1, 2):
            raise AudioValidationError('channel')
        if self.sample_width_bytes != PCM_SAMPLE_WIDTH_BYTES:
            raise AudioValidationError('sample_width')
        if self.frame_count <= 0:
            raise AudioValidationError('duration')
        if not 1 <= self.file_size_bytes <= MAX_WAV_BYTES:
            raise AudioValidationError('size')

    def to_dict(self) -> Dict[str, int]:
        """Return bounded metadata without a path or audio content."""
        return {
            'duration_ms': self.duration_ms,
            'sample_rate_hz': self.sample_rate_hz,
            'channel_count': self.channel_count,
            'sample_width_bytes': self.sample_width_bytes,
            'frame_count': self.frame_count,
            'file_size_bytes': self.file_size_bytes,
        }


@dataclass(frozen=True, repr=False)
class BackendTranscript:
    """Raw typed output expected from a local STT backend."""

    text: str
    confidence: float
    language: str
    confidence_basis: str = 'backend_reported_uncalibrated'
    model: Optional[str] = None

    def __repr__(self) -> str:
        """Represent measurements without exposing transcript content."""
        if type(self) is not BackendTranscript:
            return 'BackendTranscript(invalid)'
        text_chars = len(self.text) if type(self.text) is str else None
        confidence = (
            float(self.confidence)
            if _is_valid_confidence(self.confidence)
            else None
        )
        return (
            'BackendTranscript(text_chars={!r}, confidence={!r}, '
            'language={!r}, confidence_basis={!r}, model={!r})'
        ).format(
            text_chars,
            confidence,
            _safe_label(self.language, 'unknown', 16),
            _normalized_confidence_basis(self.confidence_basis),
            _safe_model_label(self.model),
        )


@dataclass(frozen=True, repr=False)
class LocalSTTResult:
    """Validated final transcript returned by the local adapter."""

    text: str
    confidence: float
    language: str
    audio_metadata: WavMetadata
    confidence_basis: str = 'backend_reported_uncalibrated'
    backend: str = 'unknown'
    model: Optional[str] = None

    def __post_init__(self) -> None:
        """Normalize every field that can appear in an audit projection."""
        if type(self.text) is not str:
            raise TranscriptionError('result')
        text = self.text.strip()
        if (
            not text
            or len(text) > MAX_UTTERANCE_LENGTH
            or _contains_control_character(text)
        ):
            raise TranscriptionError('result')
        if not _is_valid_confidence(self.confidence):
            raise TranscriptionError('confidence')
        language = _safe_label(self.language, '', 16)
        if not language:
            raise TranscriptionError('language')
        if type(self.audio_metadata) is not WavMetadata:
            raise TranscriptionError('result')
        object.__setattr__(self, 'text', text)
        object.__setattr__(self, 'confidence', float(self.confidence))
        object.__setattr__(self, 'language', language)
        object.__setattr__(
            self,
            'confidence_basis',
            _normalized_confidence_basis(self.confidence_basis),
        )
        object.__setattr__(
            self,
            'backend',
            _safe_label(self.backend, 'unknown'),
        )
        object.__setattr__(
            self,
            'model',
            _safe_model_label(self.model),
        )

    def __repr__(self) -> str:
        """Represent only the explicit content-free audit projection."""
        return 'LocalSTTResult({!r})'.format(self.to_audit_dict())

    def to_dict(self) -> Dict[str, Any]:
        """Return the explicit local result, including its transcript."""
        return {
            'text': self.text,
            'confidence': self.confidence,
            'language': self.language,
            'confidence_basis': self.confidence_basis,
            'backend': self.backend,
            'model': self.model,
            'audio_metadata': self.audio_metadata.to_dict(),
        }

    def to_audit_dict(self) -> Dict[str, Any]:
        """Return measurements without transcript, path, or audio bytes."""
        return {
            'status': 'transcribed',
            'text_chars': len(self.text),
            'confidence': self.confidence,
            'confidence_basis': self.confidence_basis,
            'language': self.language,
            'backend': self.backend,
            'model': self.model,
            'duration_ms': self.audio_metadata.duration_ms,
            'sample_rate_hz': self.audio_metadata.sample_rate_hz,
            'channel_count': self.audio_metadata.channel_count,
            'sample_width_bytes': self.audio_metadata.sample_width_bytes,
            'frame_count': self.audio_metadata.frame_count,
            'file_size_bytes': self.audio_metadata.file_size_bytes,
        }


class SpeechToTextBackend(Protocol):
    """Minimal injectable interface for one local transcription backend."""

    def transcribe(
        self,
        wav_path: Path,
        *,
        language: str,
    ) -> BackendTranscript:
        """Transcribe one already validated local WAV."""
        ...


def _safe_model_label(value: Any) -> Optional[str]:
    """Return a bounded model label without exposing a local path."""
    if value is None:
        return None
    if type(value) is not str:
        return 'custom'
    label = value.strip()
    if not label:
        return None
    if (
        len(label) > 128
        or os.path.isabs(label)
        or label.startswith(('.', '~'))
        or '/' in label
        or '\\' in label
        or any(
            not (
                character.isalnum()
                or character in {'-', '_', '.'}
            )
            for character in label
        )
    ):
        return 'custom'
    return label


def _safe_label(value: Any, fallback: str, max_length: int = 64) -> str:
    """Normalize a short, non-path diagnostic label."""
    if type(value) is not str:
        return fallback
    label = value.strip()
    if (
        not label
        or len(label) > max_length
        or os.path.isabs(label)
        or any(
            not (
                character.isalnum()
                or character in {'-', '_', '.'}
            )
            for character in label
        )
    ):
        return fallback
    return label


def _contains_control_character(value: str) -> bool:
    """Return whether text contains a terminal or transport control."""
    return any(
        ord(character) < 32 or ord(character) == 127
        for character in value
    )


def _is_valid_confidence(value: Any) -> bool:
    """Return whether a value is one finite probability."""
    return (
        type(value) in (int, float)
        and math.isfinite(float(value))
        and 0.0 <= float(value) <= 1.0
    )


def _normalized_confidence_basis(value: Any) -> str:
    """Map untrusted confidence provenance to a closed public label."""
    if type(value) is str and value in ALLOWED_CONFIDENCE_BASES:
        return value
    return 'backend_reported_uncalibrated'


def _validated_path(value: PathLike) -> Path:
    """Convert a caller value to a path without reflecting it in errors."""
    if not isinstance(value, (str, os.PathLike)):
        raise AudioValidationError('input')
    invalid_path = False
    try:
        result = Path(value)
    except Exception:
        invalid_path = True
        result = Path('.')
    if invalid_path:
        raise AudioValidationError('input')
    return result


def _close_fd(file_descriptor: int) -> None:
    """Close a local descriptor without surfacing cleanup diagnostics."""
    try:
        os.close(file_descriptor)
    except OSError:
        pass


def _paths_are_absent(paths: Sequence[PathLike]) -> bool:
    """Verify that no private file or directory remains on disk."""
    verification_failed = False
    try:
        any_remaining = any(
            os.path.lexists(os.fspath(path))
            for path in paths
        )
    except (OSError, TypeError, ValueError):
        verification_failed = True
        any_remaining = True
    return not verification_failed and not any_remaining


def _cleanup_private_temp(
    temp_context: tempfile.TemporaryDirectory,
    temp_directory: PathLike,
    private_path: PathLike,
) -> bool:
    """Delete private audio, retry once, and verify absence."""
    try:
        temp_context.cleanup()
    except Exception:
        pass
    if _paths_are_absent((private_path, temp_directory)):
        return True

    try:
        shutil.rmtree(os.fspath(temp_directory))
    except Exception:
        pass
    if _paths_are_absent((private_path, temp_directory)):
        return True

    try:
        temp_context.cleanup()
    except Exception:
        pass
    return _paths_are_absent((private_path, temp_directory))


def _open_regular_audio_fd(
    path: PathLike,
) -> Tuple[int, os.stat_result]:
    """Open one non-symlink regular file and bind metadata to that fd."""
    wav_path = _validated_path(path)
    flags = os.O_RDONLY
    for option in ('O_CLOEXEC', 'O_NOFOLLOW', 'O_NONBLOCK'):
        flags |= getattr(os, option, 0)

    open_failed = False
    try:
        file_descriptor = os.open(str(wav_path), flags)
    except (OSError, TypeError, ValueError):
        open_failed = True
        file_descriptor = -1
    if open_failed:
        raise AudioValidationError('input')

    stat_failed = False
    try:
        file_status = os.fstat(file_descriptor)
    except OSError:
        stat_failed = True
        file_status = None
    if stat_failed or file_status is None:
        _close_fd(file_descriptor)
        raise AudioValidationError('input')
    if not stat.S_ISREG(file_status.st_mode) or file_status.st_size <= 0:
        _close_fd(file_descriptor)
        raise AudioValidationError('input')
    if file_status.st_size > MAX_WAV_BYTES:
        _close_fd(file_descriptor)
        raise AudioValidationError('size')
    return file_descriptor, file_status


def _riff_data_region(
    source: Any,
    file_size: int,
) -> Tuple[int, int]:
    """Locate bounded PCM bytes, including a streaming WAV sentinel.

    Some streaming WAV producers write ``0xffffffff`` for both the RIFF
    length and the final ``data`` chunk length because the size was unknown
    when the response header was emitted.  Once such a response is a closed,
    bounded regular file, the actual payload length is the bytes through EOF.
    Other chunks must still have concrete, in-bounds lengths.
    """
    source.seek(0)
    header = source.read(12)
    if (
        len(header) != 12
        or header[0:4] != b'RIFF'
        or header[8:12] != b'WAVE'
    ):
        raise AudioValidationError('container')

    declared_riff_size = int.from_bytes(header[4:8], 'little')
    if (
        declared_riff_size != 0xFFFFFFFF
        and declared_riff_size + 8 > file_size
    ):
        raise AudioValidationError('truncated')

    offset = 12
    while offset + 8 <= file_size:
        source.seek(offset)
        chunk_header = source.read(8)
        if len(chunk_header) != 8:
            raise AudioValidationError('truncated')
        chunk_name = chunk_header[0:4]
        declared_chunk_size = int.from_bytes(
            chunk_header[4:8],
            'little',
        )
        payload_offset = offset + 8

        if chunk_name == b'data':
            payload_size = declared_chunk_size
            if payload_size == 0xFFFFFFFF:
                payload_size = file_size - payload_offset
            if payload_size < 0 or payload_offset + payload_size > file_size:
                raise AudioValidationError('truncated')
            return payload_offset, payload_size

        if declared_chunk_size == 0xFFFFFFFF:
            raise AudioValidationError('container')
        next_offset = (
            payload_offset
            + declared_chunk_size
            + (declared_chunk_size & 1)
        )
        if next_offset > file_size:
            raise AudioValidationError('truncated')
        offset = next_offset

    raise AudioValidationError('container')


def validate_pcm_wav(path: PathLike) -> WavMetadata:
    """Validate one bounded PCM16 WAV using one opened file descriptor.

    Accepted files are regular RIFF/WAVE files of at most six MiB, between
    one millisecond and 30 seconds, with one or two channels and a sample
    rate from 8 kHz through 48 kHz. Symbolic links are rejected where the
    operating system provides ``O_NOFOLLOW``.
    """
    file_descriptor, file_status = _open_regular_audio_fd(path)
    validation_error: Optional[LocalSTTError] = None
    try:
        with os.fdopen(file_descriptor, 'rb', closefd=True) as source:
            file_descriptor = -1
            data_offset, data_size = _riff_data_region(
                source,
                file_status.st_size,
            )
            source.seek(0)
            with wave.open(source, 'rb') as reader:
                compression = reader.getcomptype()
                sample_width = reader.getsampwidth()
                channel_count = reader.getnchannels()
                sample_rate = reader.getframerate()

                if compression != 'NONE':
                    raise AudioValidationError('pcm')
                if sample_width != PCM_SAMPLE_WIDTH_BYTES:
                    raise AudioValidationError('sample_width')
                if channel_count not in (1, 2):
                    raise AudioValidationError('channel')
                if not MIN_SAMPLE_RATE_HZ <= sample_rate <= MAX_SAMPLE_RATE_HZ:
                    raise AudioValidationError('sample_rate')

                frame_size = channel_count * sample_width
                if data_size % frame_size != 0:
                    raise AudioValidationError('truncated')
                frame_count = data_size // frame_size
                if (
                    frame_count <= 0
                    or frame_count * 1000 < sample_rate
                    or frame_count
                    > sample_rate * MAX_AUDIO_DURATION_SECONDS
                ):
                    raise AudioValidationError('duration')

            source.seek(data_offset)
            remaining = data_size
            while remaining:
                frames = source.read(min(remaining, 64 * 1024))
                if not frames:
                    raise AudioValidationError('truncated')
                remaining -= len(frames)
            if os.fstat(source.fileno()).st_size != file_status.st_size:
                raise AudioValidationError('input')
    except AudioValidationError as error:
        validation_error = _clone_public_error(error)
    except wave.Error:
        validation_error = AudioValidationError('pcm')
    except (
        EOFError,
        OSError,
        OverflowError,
        RuntimeError,
        ValueError,
    ):
        validation_error = AudioValidationError('input')
    finally:
        if file_descriptor >= 0:
            _close_fd(file_descriptor)
    if validation_error is not None:
        raise validation_error

    duration_ms = int(round(frame_count * 1000.0 / sample_rate))
    return WavMetadata(
        duration_ms=duration_ms,
        sample_rate_hz=sample_rate,
        channel_count=channel_count,
        sample_width_bytes=sample_width,
        frame_count=frame_count,
        file_size_bytes=file_status.st_size,
    )


@contextmanager
def _private_wav_snapshot(
    path: PathLike,
) -> Iterator[Tuple[Path, WavMetadata]]:
    """Copy one opened input into a bounded private immutable work path."""
    temp_failed = False
    try:
        temp_context = tempfile.TemporaryDirectory(
            prefix='malbut-stt-input-',
        )
    except Exception:
        temp_failed = True
        temp_context = None
    if temp_failed or temp_context is None:
        raise AudioValidationError('input')

    temp_directory = Path(temp_context.name)
    snapshot_path = temp_directory / 'input.wav'
    source_fd = -1
    output_fd = -1
    setup_error: Optional[LocalSTTError] = None
    setup_pending: Optional[BaseException] = None
    setup_traceback = None
    try:
        os.chmod(temp_directory, 0o700)
        source_fd, _ = _open_regular_audio_fd(path)
        output_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        output_flags |= getattr(os, 'O_CLOEXEC', 0)
        try:
            output_fd = os.open(str(snapshot_path), output_flags, 0o600)
            os.fchmod(output_fd, 0o600)
            copied_bytes = 0
            while True:
                chunk = os.read(source_fd, 64 * 1024)
                if not chunk:
                    break
                copied_bytes += len(chunk)
                if copied_bytes > MAX_WAV_BYTES:
                    setup_error = AudioValidationError('size')
                    break
                remaining = memoryview(chunk)
                while remaining:
                    written = os.write(output_fd, remaining)
                    if written <= 0:
                        raise OSError('short private WAV write')
                    remaining = remaining[written:]
        except Exception:
            setup_error = AudioValidationError('input')
    except LocalSTTError as error:
        setup_error = _clone_public_error(error)
    except Exception:
        setup_error = AudioValidationError('input')
    except BaseException as error:
        setup_pending = error
        setup_traceback = error.__traceback__
    finally:
        if source_fd >= 0:
            _close_fd(source_fd)
        if output_fd >= 0:
            _close_fd(output_fd)
    if setup_error is not None or setup_pending is not None:
        cleanup_succeeded = _cleanup_private_temp(
            temp_context,
            temp_directory,
            snapshot_path,
        )
        if not cleanup_succeeded:
            if setup_pending is not None:
                raise setup_pending.with_traceback(setup_traceback)
            _raise_chain_free(AudioValidationError('cleanup'))
        if setup_pending is not None:
            raise setup_pending.with_traceback(setup_traceback)
        raise setup_error

    validation_error: Optional[LocalSTTError] = None
    validation_pending: Optional[BaseException] = None
    validation_traceback = None
    try:
        metadata = validate_pcm_wav(snapshot_path)
    except LocalSTTError as error:
        validation_error = _clone_public_error(error)
        metadata = None
    except Exception:
        validation_error = AudioValidationError('input')
        metadata = None
    except BaseException as error:
        validation_pending = error
        validation_traceback = error.__traceback__
        metadata = None
    if (
        validation_error is not None
        or validation_pending is not None
        or metadata is None
    ):
        cleanup_succeeded = _cleanup_private_temp(
            temp_context,
            temp_directory,
            snapshot_path,
        )
        if not cleanup_succeeded:
            if validation_pending is not None:
                raise validation_pending.with_traceback(
                    validation_traceback
                )
            _raise_chain_free(AudioValidationError('cleanup'))
        if validation_pending is not None:
            raise validation_pending.with_traceback(validation_traceback)
        raise validation_error or AudioValidationError('input')

    pending_error: Optional[BaseException] = None
    pending_traceback = None
    try:
        yield snapshot_path, metadata
    except BaseException as error:
        pending_error = error
        pending_traceback = error.__traceback__

    cleanup_succeeded = _cleanup_private_temp(
        temp_context,
        temp_directory,
        snapshot_path,
    )
    if not cleanup_succeeded:
        if pending_error is not None:
            raise pending_error.with_traceback(pending_traceback)
        _raise_chain_free(AudioValidationError('cleanup'))
    if pending_error is not None:
        raise pending_error.with_traceback(pending_traceback)


def _confidence_from_segments(segments: Sequence[Any]) -> Tuple[float, str]:
    """Estimate an explicitly uncalibrated confidence from model output."""
    word_probabilities: List[float] = []
    for segment in segments:
        words = getattr(segment, 'words', None) or ()
        for word in words:
            probability = getattr(word, 'probability', None)
            if (
                type(probability) in (int, float)
                and math.isfinite(float(probability))
                and 0.0 <= float(probability) <= 1.0
            ):
                word_probabilities.append(float(probability))
    if word_probabilities:
        return (
            sum(word_probabilities) / len(word_probabilities),
            'word_probability_mean_uncalibrated',
        )

    scored_segments: List[Tuple[float, float]] = []
    for segment in segments:
        avg_logprob = getattr(segment, 'avg_logprob', None)
        if (
            type(avg_logprob) in (int, float)
            and math.isfinite(float(avg_logprob))
        ):
            start = getattr(segment, 'start', None)
            end = getattr(segment, 'end', None)
            if (
                type(start) in (int, float)
                and type(end) in (int, float)
                and math.isfinite(float(start))
                and math.isfinite(float(end))
                and float(end) > float(start)
            ):
                weight = float(end) - float(start)
            else:
                weight = 1.0
            log_probability = float(avg_logprob)
            probability = (
                1.0
                if log_probability >= 0.0
                else math.exp(log_probability)
            )
            scored_segments.append((probability, weight))
    if scored_segments:
        total_weight = sum(weight for _, weight in scored_segments)
        confidence = sum(
            probability * weight
            for probability, weight in scored_segments
        ) / total_weight
        return (
            confidence,
            'segment_avg_logprob_duration_weighted_exp_uncalibrated',
        )
    return 0.0, 'unavailable_uncalibrated'


class FasterWhisperBackend:
    """Lazy local faster-whisper backend with opt-in model downloads."""

    backend_name = 'faster-whisper'

    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        device: str = DEFAULT_DEVICE,
        compute_type: str = DEFAULT_COMPUTE_TYPE,
        allow_model_download: bool = False,
    ) -> None:
        """Store configuration without importing or loading a model."""
        if (
            type(model) is not str
            or model not in {'tiny', 'base', 'small'}
        ):
            raise BackendUnavailableError()
        if (
            type(device) is not str
            or device not in {'auto', 'cpu', 'cuda'}
        ):
            raise BackendUnavailableError()
        if (
            type(compute_type) is not str
            or compute_type not in {
                'default',
                'int8',
                'float16',
                'int8_float16',
            }
        ):
            raise BackendUnavailableError()
        if type(allow_model_download) is not bool:
            raise BackendUnavailableError()
        self.model = model
        self.device = device
        self.compute_type = compute_type
        self.allow_model_download = allow_model_download
        self._loaded_model: Any = None
        self._load_lock = threading.Lock()

    def prepare(self) -> None:
        """Import faster-whisper and load the configured model once."""
        if self._loaded_model is not None:
            return
        with self._load_lock:
            if self._loaded_model is not None:
                return
            import_failed = False
            try:
                from faster_whisper import WhisperModel
            except Exception:
                import_failed = True
                WhisperModel = None
            if import_failed or WhisperModel is None:
                raise BackendUnavailableError()

            load_failed = False
            try:
                loaded_model = WhisperModel(
                    self.model,
                    device=self.device,
                    compute_type=self.compute_type,
                    local_files_only=not self.allow_model_download,
                )
            except Exception:
                load_failed = True
                loaded_model = None
            if load_failed or loaded_model is None:
                raise BackendUnavailableError()
            self._loaded_model = loaded_model

    def transcribe(
        self,
        wav_path: Path,
        *,
        language: str,
    ) -> BackendTranscript:
        """Transcribe one validated WAV without logging its path or text."""
        self.prepare()
        transcription_error: Optional[LocalSTTError] = None
        try:
            output, info = self._loaded_model.transcribe(
                str(wav_path),
                language=language,
                word_timestamps=True,
                vad_filter=True,
                beam_size=1,
                condition_on_previous_text=False,
            )
            segments = list(output)
            text_parts = []
            for segment in segments:
                value = getattr(segment, 'text', '')
                if type(value) is str and value.strip():
                    text_parts.append(value.strip())
            text = ' '.join(text_parts)
            confidence, basis = _confidence_from_segments(segments)
            detected_language = getattr(info, 'language', language)
        except LocalSTTError as error:
            transcription_error = _clone_public_error(error)
        except Exception:
            transcription_error = TranscriptionError()
        if transcription_error is not None:
            raise transcription_error

        return BackendTranscript(
            text=text,
            confidence=confidence,
            language=detected_language,
            confidence_basis=basis,
            model=self.model,
        )


def _result_backend_label(backend: SpeechToTextBackend) -> str:
    """Derive a safe backend name without serializing the object."""
    try:
        value = getattr(backend, 'backend_name', None)
    except Exception:
        value = None
    return _safe_label(value, 'custom')


def _result_model_label(
    transcript: BackendTranscript,
    backend: SpeechToTextBackend,
) -> Optional[str]:
    """Derive a safe model label without exposing a local model path."""
    model = transcript.model
    if model is None:
        try:
            model = getattr(backend, 'model', None)
        except Exception:
            model = None
    return _safe_model_label(model)


def transcribe_wav(
    path: PathLike,
    backend: SpeechToTextBackend,
    *,
    language: str = DEFAULT_LANGUAGE,
) -> LocalSTTResult:
    """Snapshot, validate, and transcribe exactly one local WAV."""
    requested_language = _safe_label(language, '', 16)
    if not requested_language:
        raise TranscriptionError('language')

    backend_error: Optional[LocalSTTError] = None
    with _private_wav_snapshot(path) as snapshot:
        wav_path, metadata = snapshot
        try:
            transcript = backend.transcribe(
                wav_path,
                language=requested_language,
            )
        except LocalSTTError as error:
            backend_error = _clone_public_error(error)
            transcript = None
        except Exception:
            backend_error = TranscriptionError()
            transcript = None
    if backend_error is not None:
        raise backend_error

    if type(transcript) is not BackendTranscript:
        raise TranscriptionError('result')
    if type(transcript.text) is not str:
        raise TranscriptionError('result')
    text = transcript.text.strip()
    if not text:
        raise NoSpeechError()
    if len(text) > MAX_UTTERANCE_LENGTH:
        raise TranscriptionError('text_length')
    if _contains_control_character(text):
        raise TranscriptionError('result')

    confidence = transcript.confidence
    if not _is_valid_confidence(confidence):
        raise TranscriptionError('confidence')

    detected_language = _safe_label(transcript.language, '', 16)
    if not detected_language:
        raise TranscriptionError('language')
    return LocalSTTResult(
        text=text,
        confidence=float(confidence),
        language=detected_language,
        audio_metadata=metadata,
        confidence_basis=_normalized_confidence_basis(
            transcript.confidence_basis
        ),
        backend=_result_backend_label(backend),
        model=_result_model_label(transcript, backend),
    )


def _capture_seconds(value: Any) -> int:
    """Validate an exact whole-second arecord duration."""
    if (
        type(value) is not int
        or value < 1
        or value > int(MAX_AUDIO_DURATION_SECONDS)
    ):
        raise CaptureError('configuration')
    return value


def _audio_device_label(value: Any) -> str:
    """Return one bounded argv-safe ALSA label or an empty sentinel."""
    if type(value) is not str:
        return ''
    label = value.strip()
    if (
        not label
        or len(label) > MAX_AUDIO_DEVICE_LENGTH
        or _contains_control_character(label)
    ):
        return ''
    return label


@contextmanager
def capture_microphone(
    seconds: int = DEFAULT_CAPTURE_SECONDS,
    audio_device: Optional[str] = None,
    *,
    runner: Runner = subprocess.run,
    which: Which = shutil.which,
    temp_root: Optional[PathLike] = None,
) -> Iterator[Path]:
    """Capture one private mono WAV with arecord and delete it afterward."""
    duration = _capture_seconds(seconds)
    if audio_device is not None:
        normalized_device = _audio_device_label(audio_device)
        if not normalized_device:
            raise CaptureError('configuration')
    else:
        normalized_device = None

    recorder_lookup_failed = False
    try:
        recorder = which('arecord')
    except Exception:
        recorder_lookup_failed = True
        recorder = None
    if (
        recorder_lookup_failed
        or type(recorder) is not str
        or not recorder
    ):
        raise CaptureError('unavailable')

    command = [
        recorder,
        '--quiet',
        '--file-type',
        'wav',
        '--format',
        'S16_LE',
        '--channels',
        str(CAPTURE_CHANNEL_COUNT),
        '--rate',
        str(CAPTURE_SAMPLE_RATE_HZ),
        '--duration',
        str(duration),
    ]
    if normalized_device is not None:
        command.extend(['--device', normalized_device])

    temp_creation_failed = False
    try:
        temp_context = tempfile.TemporaryDirectory(
            prefix='malbut-stt-',
            dir=temp_root,
        )
    except Exception:
        temp_creation_failed = True
        temp_context = None
    if temp_creation_failed or temp_context is None:
        raise CaptureError()

    temp_directory = Path(temp_context.name)
    wav_path = temp_directory / 'capture.wav'
    command.append(str(wav_path))
    setup_failed = False
    setup_pending: Optional[BaseException] = None
    setup_traceback = None
    descriptor = -1
    try:
        os.chmod(temp_directory, 0o700)
        output_flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        output_flags |= getattr(os, 'O_CLOEXEC', 0)
        descriptor = os.open(
            str(wav_path),
            output_flags,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
    except Exception:
        setup_failed = True
    except BaseException as error:
        setup_pending = error
        setup_traceback = error.__traceback__
    finally:
        if descriptor >= 0:
            _close_fd(descriptor)
    if setup_failed or setup_pending is not None:
        cleanup_succeeded = _cleanup_private_temp(
            temp_context,
            temp_directory,
            wav_path,
        )
        if not cleanup_succeeded:
            if setup_pending is not None:
                raise setup_pending.with_traceback(setup_traceback)
            _raise_chain_free(CaptureError('cleanup'))
        if setup_pending is not None:
            raise setup_pending.with_traceback(setup_traceback)
        raise CaptureError()

    runner_error: Optional[LocalSTTError] = None
    pending_error: Optional[BaseException] = None
    pending_traceback = None
    try:
        runner(
            command,
            check=True,
            shell=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=duration + 10,
        )
    except LocalSTTError:
        runner_error = CaptureError()
    except (
        OSError,
        subprocess.SubprocessError,
        ValueError,
    ):
        runner_error = CaptureError()
    except Exception:
        runner_error = CaptureError()
    except BaseException as error:
        pending_error = error
        pending_traceback = error.__traceback__
    if runner_error is not None or pending_error is not None:
        cleanup_succeeded = _cleanup_private_temp(
            temp_context,
            temp_directory,
            wav_path,
        )
        if not cleanup_succeeded:
            if pending_error is not None:
                raise pending_error.with_traceback(pending_traceback)
            _raise_chain_free(CaptureError('cleanup'))
        if pending_error is not None:
            raise pending_error.with_traceback(pending_traceback)
        raise runner_error or CaptureError()

    invalid_output = False
    validation_pending: Optional[BaseException] = None
    validation_traceback = None
    try:
        os.chmod(wav_path, 0o600)
        validate_pcm_wav(wav_path)
    except Exception:
        invalid_output = True
    except BaseException as error:
        validation_pending = error
        validation_traceback = error.__traceback__
    if invalid_output or validation_pending is not None:
        cleanup_succeeded = _cleanup_private_temp(
            temp_context,
            temp_directory,
            wav_path,
        )
        if not cleanup_succeeded:
            if validation_pending is not None:
                raise validation_pending.with_traceback(
                    validation_traceback
                )
            _raise_chain_free(CaptureError('cleanup'))
        if validation_pending is not None:
            raise validation_pending.with_traceback(validation_traceback)
        raise CaptureError('invalid_output')

    pending_error = None
    pending_traceback = None
    try:
        yield wav_path
    except BaseException as error:
        pending_error = error
        pending_traceback = error.__traceback__

    cleanup_succeeded = _cleanup_private_temp(
        temp_context,
        temp_directory,
        wav_path,
    )
    if not cleanup_succeeded:
        if pending_error is not None:
            raise pending_error.with_traceback(pending_traceback)
        _raise_chain_free(CaptureError('cleanup'))
    if pending_error is not None:
        raise pending_error.with_traceback(pending_traceback)


def build_transcript_event(
    result: LocalSTTResult,
    binding: TrustedSpeechBinding,
    *,
    utterance_id: str,
    sequence: int,
    capture_epoch: int = 1,
    timestamp_ns: Optional[int] = None,
    capture_origin: str = 'unknown',
) -> SpeechTranscriptEvent:
    """Build the existing text-only final transcript boundary event."""
    if type(result) is not LocalSTTResult:
        raise TypeError('result must be a LocalSTTResult')
    if type(binding) is not TrustedSpeechBinding:
        raise TypeError('binding must be a TrustedSpeechBinding')
    if (
        type(capture_origin) is not str
        or capture_origin not in {'unknown', 'microphone'}
    ):
        raise TranscriptionError('result')
    source_timestamp_ns = (
        time.time_ns()
        if timestamp_ns is None
        else timestamp_ns
    )
    return SpeechTranscriptEvent(
        schema_version=SPEECH_SCHEMA_VERSION,
        utterance_id=utterance_id,
        speech_session_id=binding.speech_session_id,
        conversation_id=binding.conversation_id,
        speaker_id=binding.speaker_id,
        source=binding.source,
        sequence=sequence,
        capture_epoch=capture_epoch,
        source_timestamp_ns=source_timestamp_ns,
        text=result.text,
        confidence=result.confidence,
        is_final=True,
        capture_origin=capture_origin,
        audio_metadata=AudioMetadata(
            duration_ms=result.audio_metadata.duration_ms,
            sample_rate_hz=result.audio_metadata.sample_rate_hz,
            channel_count=result.audio_metadata.channel_count,
        ),
    )


class _SanitizedArgumentParser(argparse.ArgumentParser):
    """Suppress caller-provided values in command-line parse errors."""

    def error(self, message: str) -> None:
        """Exit with a fixed diagnostic that cannot echo private input."""
        del message
        self.exit(2, '{}: error: invalid arguments\n'.format(self.prog))


def _parse_capture_seconds(value: str) -> int:
    """Parse one CLI integer in the closed microphone duration range."""
    parse_failed = False
    try:
        result = int(value, 10)
    except (TypeError, ValueError):
        parse_failed = True
        result = 0
    if parse_failed or not 1 <= result <= 30:
        raise argparse.ArgumentTypeError('invalid capture duration')
    return result


def _parse_language(value: str) -> str:
    """Parse a bounded language code before any model or microphone use."""
    language = _safe_label(value, '', 16)
    if not language:
        raise argparse.ArgumentTypeError('invalid language')
    return language


def _parse_audio_device(value: str) -> str:
    """Parse a bounded ALSA device label without shell interpretation."""
    device = _audio_device_label(value)
    if not device:
        raise argparse.ArgumentTypeError('invalid audio device')
    return device


def _argument_parser() -> argparse.ArgumentParser:
    """Create the one-shot local STT command-line parser."""
    parser = _SanitizedArgumentParser(
        prog='malbut-stt',
        description=(
            'Transcribe one bounded local PCM16 WAV with faster-whisper.'
        ),
    )
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--wav', type=Path, help='local PCM16 WAV input')
    source.add_argument(
        '--microphone',
        action='store_true',
        help='capture one temporary WAV with arecord',
    )
    parser.add_argument(
        '--seconds',
        type=_parse_capture_seconds,
        default=DEFAULT_CAPTURE_SECONDS,
        help='microphone capture duration (default: 5)',
    )
    parser.add_argument(
        '--language',
        type=_parse_language,
        default=DEFAULT_LANGUAGE,
    )
    parser.add_argument(
        '--model',
        choices=('tiny', 'base', 'small'),
        default=DEFAULT_MODEL,
    )
    parser.add_argument(
        '--device',
        choices=('auto', 'cpu', 'cuda'),
        default=DEFAULT_DEVICE,
    )
    parser.add_argument(
        '--compute-type',
        choices=('default', 'int8', 'float16', 'int8_float16'),
        default=DEFAULT_COMPUTE_TYPE,
    )
    parser.add_argument('--audio-device', type=_parse_audio_device)
    parser.add_argument(
        '--allow-model-download',
        action='store_true',
        help='explicitly allow faster-whisper to download a missing model',
    )
    return parser


def main(
    argv: Optional[Sequence[str]] = None,
    *,
    backend_factory: Optional[Callable[..., SpeechToTextBackend]] = None,
    runner: Runner = subprocess.run,
    which: Which = shutil.which,
) -> int:
    """Run one local transcription and print only its final text."""
    arguments = _argument_parser().parse_args(argv)
    factory = (
        FasterWhisperBackend
        if backend_factory is None
        else backend_factory
    )
    try:
        factory_error: Optional[LocalSTTError] = None
        try:
            backend = factory(
                model=arguments.model,
                device=arguments.device,
                compute_type=arguments.compute_type,
                allow_model_download=arguments.allow_model_download,
            )
        except LocalSTTError as error:
            factory_error = _clone_public_error(error)
            backend = None
        except Exception:
            factory_error = BackendUnavailableError()
            backend = None
        if factory_error is not None:
            raise factory_error

        if arguments.wav is not None:
            result = transcribe_wav(
                arguments.wav,
                backend,
                language=arguments.language,
            )
        else:
            prepare_lookup_failed = False
            try:
                prepare = getattr(backend, 'prepare', None)
            except Exception:
                prepare_lookup_failed = True
                prepare = None
            if prepare_lookup_failed:
                raise BackendUnavailableError()
            if callable(prepare):
                prepare_error: Optional[LocalSTTError] = None
                try:
                    prepare()
                except LocalSTTError as error:
                    prepare_error = _clone_public_error(error)
                except Exception:
                    prepare_error = BackendUnavailableError()
                if prepare_error is not None:
                    raise prepare_error
            with capture_microphone(
                seconds=arguments.seconds,
                audio_device=arguments.audio_device,
                runner=runner,
                which=which,
            ) as captured_wav:
                result = transcribe_wav(
                    captured_wav,
                    backend,
                    language=arguments.language,
                )
    except LocalSTTError as error:
        print(
            'error [{}]: {}'.format(error.code, error.public_message),
            file=sys.stderr,
        )
        return error.exit_code
    except KeyboardInterrupt:
        return 130

    print(result.text)
    return 0


__all__ = [
    'AudioValidationError',
    'BackendTranscript',
    'BackendUnavailableError',
    'CaptureError',
    'FasterWhisperBackend',
    'LocalSTTError',
    'LocalSTTResult',
    'NoSpeechError',
    'SpeechToTextBackend',
    'TranscriptionError',
    'WavMetadata',
    'build_transcript_event',
    'capture_microphone',
    'main',
    'transcribe_wav',
    'validate_pcm_wav',
]


if __name__ == '__main__':
    raise SystemExit(main())
