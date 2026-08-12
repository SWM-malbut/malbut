"""Offline speech-to-agent boundary for one trusted local voice session.

This module intentionally accepts text and bounded metadata only.  It does
not import an STT or TTS SDK, open an audio device, or define a ROS adapter.
"""

import hashlib
import json
import math
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Sequence, Tuple

from malbut_agent_server.orchestrator import (
    AgentOrchestrator,
    OrchestrationResult,
)
from malbut_agent_server.schemas import (
    MAX_ID_LENGTH,
    MAX_UTTERANCE_LENGTH,
    AgentRequest,
    RobotState,
    ValidationError,
    validate_conversation_id,
    validate_user_id,
)


SPEECH_SCHEMA_VERSION = 1
DEFAULT_MINIMUM_CONFIDENCE = 0.75
DEFAULT_EVENT_CACHE_SIZE = 256
MAX_SOURCE_LENGTH = 64
MAX_AUDIO_DURATION_MS = 30000
MIN_SAMPLE_RATE_HZ = 8000
MAX_SAMPLE_RATE_HZ = 48000
MAX_SEQUENCE = (1 << 63) - 1
CAPTURE_ORIGINS = frozenset({'microphone', 'self_echo', 'unknown'})
TTS_VOICES = frozenset({'default'})
TTS_STYLES = frozenset({'neutral'})


def _object(value: Any, field_name: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f'{field_name} must be an object')
    return value


def _reject_unknown(
    value: Dict[str, Any],
    allowed: set,
    field_name: str,
) -> None:
    unknown = set(value) - allowed
    if unknown:
        names = ', '.join(sorted(unknown))
        raise ValidationError(
            f'{field_name} contains unknown fields: {names}'
        )


def _string(value: Any, field_name: str, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValidationError(f'{field_name} must be a string')
    result = value.strip()
    if not result:
        raise ValidationError(f'{field_name} must not be empty')
    if len(result) > max_length:
        raise ValidationError(
            f'{field_name} must be at most {max_length} characters'
        )
    return result


def _identifier(value: Any, field_name: str) -> str:
    result = _string(value, field_name, MAX_ID_LENGTH)
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in result
    ):
        raise ValidationError(
            f'{field_name} must not contain control characters'
        )
    return result


def _source_label(value: Any) -> str:
    result = _string(value, 'source', MAX_SOURCE_LENGTH)
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in result
    ):
        raise ValidationError('source must not contain control characters')
    return result


def _integer(
    value: Any,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f'{field_name} must be an integer')
    if value < minimum or value > maximum:
        raise ValidationError(
            f'{field_name} must be between {minimum} and {maximum}'
        )
    return value


def _number(
    value: Any,
    field_name: str,
    minimum: float,
    maximum: float,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f'{field_name} must be a number')
    result = float(value)
    if (
        not math.isfinite(result)
        or result < minimum
        or result > maximum
    ):
        raise ValidationError(
            f'{field_name} must be between {minimum} and {maximum}'
        )
    return result


def _schema_version(value: Any) -> int:
    version = _integer(
        value,
        'schema_version',
        SPEECH_SCHEMA_VERSION,
        SPEECH_SCHEMA_VERSION,
    )
    return version


@dataclass(frozen=True)
class AudioMetadata:
    """Small non-content metadata allowed across the agent boundary."""

    duration_ms: int
    sample_rate_hz: int
    channel_count: int

    def __post_init__(self) -> None:
        """Enforce the same bounds for direct construction."""
        object.__setattr__(
            self,
            'duration_ms',
            _integer(
                self.duration_ms,
                'audio_metadata.duration_ms',
                1,
                MAX_AUDIO_DURATION_MS,
            ),
        )
        object.__setattr__(
            self,
            'sample_rate_hz',
            _integer(
                self.sample_rate_hz,
                'audio_metadata.sample_rate_hz',
                MIN_SAMPLE_RATE_HZ,
                MAX_SAMPLE_RATE_HZ,
            ),
        )
        object.__setattr__(
            self,
            'channel_count',
            _integer(
                self.channel_count,
                'audio_metadata.channel_count',
                1,
                2,
            ),
        )

    @classmethod
    def from_dict(cls, value: Any) -> 'AudioMetadata':
        """Validate metadata without accepting bytes, paths, or URIs."""
        body = _object(value, 'audio_metadata')
        _reject_unknown(
            body,
            {'duration_ms', 'sample_rate_hz', 'channel_count'},
            'audio_metadata',
        )
        return cls(
            duration_ms=_integer(
                body.get('duration_ms'),
                'audio_metadata.duration_ms',
                1,
                MAX_AUDIO_DURATION_MS,
            ),
            sample_rate_hz=_integer(
                body.get('sample_rate_hz'),
                'audio_metadata.sample_rate_hz',
                MIN_SAMPLE_RATE_HZ,
                MAX_SAMPLE_RATE_HZ,
            ),
            channel_count=_integer(
                body.get('channel_count'),
                'audio_metadata.channel_count',
                1,
                2,
            ),
        )

    def to_dict(self) -> Dict[str, int]:
        """Return only bounded non-content metadata."""
        return {
            'duration_ms': self.duration_ms,
            'sample_rate_hz': self.sample_rate_hz,
            'channel_count': self.channel_count,
        }


@dataclass(frozen=True)
class SpeechTranscriptEvent:
    """One untrusted STT event from a trusted local transport."""

    schema_version: int
    utterance_id: str
    speech_session_id: str
    conversation_id: str
    speaker_id: str
    source: str
    sequence: int
    capture_epoch: int
    source_timestamp_ns: int
    text: str
    confidence: float
    is_final: bool
    capture_origin: str
    audio_metadata: AudioMetadata

    def __post_init__(self) -> None:
        """Validate and normalize direct typed construction."""
        object.__setattr__(
            self,
            'schema_version',
            _schema_version(self.schema_version),
        )
        for name in (
            'utterance_id',
            'speech_session_id',
            'speaker_id',
        ):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            'conversation_id',
            validate_conversation_id(self.conversation_id),
        )
        object.__setattr__(
            self,
            'source',
            _source_label(self.source),
        )
        object.__setattr__(
            self,
            'sequence',
            _integer(self.sequence, 'sequence', 1, MAX_SEQUENCE),
        )
        object.__setattr__(
            self,
            'capture_epoch',
            _integer(
                self.capture_epoch,
                'capture_epoch',
                1,
                MAX_SEQUENCE,
            ),
        )
        object.__setattr__(
            self,
            'source_timestamp_ns',
            _integer(
                self.source_timestamp_ns,
                'source_timestamp_ns',
                0,
                MAX_SEQUENCE,
            ),
        )
        object.__setattr__(
            self,
            'text',
            _string(self.text, 'text', MAX_UTTERANCE_LENGTH),
        )
        object.__setattr__(
            self,
            'confidence',
            _number(self.confidence, 'confidence', 0.0, 1.0),
        )
        if not isinstance(self.is_final, bool):
            raise ValidationError('is_final must be a boolean')
        normalized_origin = _string(
            self.capture_origin,
            'capture_origin',
            32,
        )
        if normalized_origin not in CAPTURE_ORIGINS:
            raise ValidationError('capture_origin is unsupported')
        object.__setattr__(
            self,
            'capture_origin',
            normalized_origin,
        )
        if not isinstance(self.audio_metadata, AudioMetadata):
            raise ValidationError(
                'audio_metadata must be an AudioMetadata'
            )

    @classmethod
    def from_dict(cls, value: Any) -> 'SpeechTranscriptEvent':
        """Validate one strict transcript envelope."""
        body = _object(value, 'speech transcript')
        allowed = {
            'schema_version',
            'utterance_id',
            'speech_session_id',
            'conversation_id',
            'speaker_id',
            'source',
            'sequence',
            'capture_epoch',
            'source_timestamp_ns',
            'text',
            'confidence',
            'is_final',
            'capture_origin',
            'audio_metadata',
        }
        _reject_unknown(body, allowed, 'speech transcript')
        is_final = body.get('is_final')
        if not isinstance(is_final, bool):
            raise ValidationError('is_final must be a boolean')
        capture_origin = _string(
            body.get('capture_origin'),
            'capture_origin',
            32,
        )
        if capture_origin not in CAPTURE_ORIGINS:
            raise ValidationError('capture_origin is unsupported')
        return cls(
            schema_version=_schema_version(body.get('schema_version')),
            utterance_id=_identifier(
                body.get('utterance_id'),
                'utterance_id',
            ),
            speech_session_id=_identifier(
                body.get('speech_session_id'),
                'speech_session_id',
            ),
            conversation_id=validate_conversation_id(
                body.get('conversation_id')
            ),
            speaker_id=_identifier(
                body.get('speaker_id'),
                'speaker_id',
            ),
            source=_source_label(body.get('source')),
            sequence=_integer(
                body.get('sequence'),
                'sequence',
                1,
                MAX_SEQUENCE,
            ),
            capture_epoch=_integer(
                body.get('capture_epoch'),
                'capture_epoch',
                1,
                MAX_SEQUENCE,
            ),
            source_timestamp_ns=_integer(
                body.get('source_timestamp_ns'),
                'source_timestamp_ns',
                0,
                MAX_SEQUENCE,
            ),
            text=_string(
                body.get('text'),
                'text',
                MAX_UTTERANCE_LENGTH,
            ),
            confidence=_number(
                body.get('confidence'),
                'confidence',
                0.0,
                1.0,
            ),
            is_final=is_final,
            capture_origin=capture_origin,
            audio_metadata=AudioMetadata.from_dict(
                body.get('audio_metadata')
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the transport object; callers must not log this value."""
        return {
            'schema_version': self.schema_version,
            'utterance_id': self.utterance_id,
            'speech_session_id': self.speech_session_id,
            'conversation_id': self.conversation_id,
            'speaker_id': self.speaker_id,
            'source': self.source,
            'sequence': self.sequence,
            'capture_epoch': self.capture_epoch,
            'source_timestamp_ns': self.source_timestamp_ns,
            'text': self.text,
            'confidence': self.confidence,
            'is_final': self.is_final,
            'capture_origin': self.capture_origin,
            'audio_metadata': self.audio_metadata.to_dict(),
        }

    def to_audit_dict(self) -> Dict[str, Any]:
        """Return content-free measurements suitable for local audit logs."""
        return {
            'schema_version': self.schema_version,
            'utterance_id': self.utterance_id,
            'speech_session_id': self.speech_session_id,
            'conversation_id': self.conversation_id,
            'source': self.source,
            'sequence': self.sequence,
            'capture_epoch': self.capture_epoch,
            'source_timestamp_ns': self.source_timestamp_ns,
            'text_chars': len(self.text),
            'confidence': self.confidence,
            'is_final': self.is_final,
            'capture_origin': self.capture_origin,
            'audio_metadata': self.audio_metadata.to_dict(),
        }


@dataclass(frozen=True)
class TrustedSpeechBinding:
    """Server-owned identity and conversation binding for voice input."""

    user_id: str
    speaker_id: str
    speech_session_id: str
    conversation_id: str
    source: str

    def __post_init__(self) -> None:
        """Validate and normalize server-owned binding fields."""
        object.__setattr__(
            self,
            'user_id',
            validate_user_id(self.user_id),
        )
        object.__setattr__(
            self,
            'speaker_id',
            _identifier(self.speaker_id, 'speaker_id'),
        )
        object.__setattr__(
            self,
            'speech_session_id',
            _identifier(
                self.speech_session_id,
                'speech_session_id',
            ),
        )
        object.__setattr__(
            self,
            'conversation_id',
            validate_conversation_id(self.conversation_id),
        )
        object.__setattr__(
            self,
            'source',
            _source_label(self.source),
        )

    @classmethod
    def from_dict(cls, value: Any) -> 'TrustedSpeechBinding':
        """Create a binding only from a trusted local configuration path."""
        body = _object(value, 'speech binding')
        _reject_unknown(
            body,
            {
                'user_id',
                'speaker_id',
                'speech_session_id',
                'conversation_id',
                'source',
            },
            'speech binding',
        )
        return cls(
            user_id=validate_user_id(body.get('user_id')),
            speaker_id=_identifier(
                body.get('speaker_id'),
                'speaker_id',
            ),
            speech_session_id=_identifier(
                body.get('speech_session_id'),
                'speech_session_id',
            ),
            conversation_id=validate_conversation_id(
                body.get('conversation_id')
            ),
            source=_source_label(body.get('source')),
        )

    def to_dict(self) -> Dict[str, str]:
        """Return the trusted binding for local diagnostics."""
        return {
            'user_id': self.user_id,
            'speaker_id': self.speaker_id,
            'speech_session_id': self.speech_session_id,
            'conversation_id': self.conversation_id,
            'source': self.source,
        }


@dataclass(frozen=True)
class SpeechActivityEvent:
    """Trusted VAD signal used to fence a barge-in capture epoch."""

    schema_version: int
    event_id: str
    speech_session_id: str
    speaker_id: str
    source: str
    capture_epoch: int
    source_timestamp_ns: int

    def __post_init__(self) -> None:
        """Validate and normalize direct activity construction."""
        object.__setattr__(
            self,
            'schema_version',
            _schema_version(self.schema_version),
        )
        for name in ('event_id', 'speech_session_id', 'speaker_id'):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            'source',
            _source_label(self.source),
        )
        object.__setattr__(
            self,
            'capture_epoch',
            _integer(
                self.capture_epoch,
                'capture_epoch',
                1,
                MAX_SEQUENCE,
            ),
        )
        object.__setattr__(
            self,
            'source_timestamp_ns',
            _integer(
                self.source_timestamp_ns,
                'source_timestamp_ns',
                0,
                MAX_SEQUENCE,
            ),
        )

    @classmethod
    def from_dict(cls, value: Any) -> 'SpeechActivityEvent':
        """Validate one user-speech-started event."""
        body = _object(value, 'speech activity')
        _reject_unknown(
            body,
            {
                'schema_version',
                'event_id',
                'speech_session_id',
                'speaker_id',
                'source',
                'capture_epoch',
                'source_timestamp_ns',
            },
            'speech activity',
        )
        return cls(
            schema_version=_schema_version(body.get('schema_version')),
            event_id=_identifier(body.get('event_id'), 'event_id'),
            speech_session_id=_identifier(
                body.get('speech_session_id'),
                'speech_session_id',
            ),
            speaker_id=_identifier(
                body.get('speaker_id'),
                'speaker_id',
            ),
            source=_source_label(body.get('source')),
            capture_epoch=_integer(
                body.get('capture_epoch'),
                'capture_epoch',
                1,
                MAX_SEQUENCE,
            ),
            source_timestamp_ns=_integer(
                body.get('source_timestamp_ns'),
                'source_timestamp_ns',
                0,
                MAX_SEQUENCE,
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the validated activity event."""
        return {
            'schema_version': self.schema_version,
            'event_id': self.event_id,
            'speech_session_id': self.speech_session_id,
            'speaker_id': self.speaker_id,
            'source': self.source,
            'capture_epoch': self.capture_epoch,
            'source_timestamp_ns': self.source_timestamp_ns,
        }


@dataclass(frozen=True)
class TTSRequest:
    """Text-only, interruptible synthesis request for a downstream adapter."""

    schema_version: int
    request_id: str
    speech_session_id: str
    conversation_id: str
    turn_id: str
    source_utterance_id: str
    text: str
    voice: str = 'default'
    style: str = 'neutral'
    interruptible: bool = True

    def __post_init__(self) -> None:
        """Prevent an invalid request even when constructed directly."""
        object.__setattr__(
            self,
            'schema_version',
            _schema_version(self.schema_version),
        )
        for name in (
            'request_id',
            'speech_session_id',
            'turn_id',
            'source_utterance_id',
        ):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            'conversation_id',
            validate_conversation_id(self.conversation_id),
        )
        object.__setattr__(
            self,
            'text',
            _string(self.text, 'text', MAX_UTTERANCE_LENGTH),
        )
        if self.voice not in TTS_VOICES:
            raise ValidationError('voice is unsupported')
        if self.style not in TTS_STYLES:
            raise ValidationError('style is unsupported')
        if self.interruptible is not True:
            raise ValidationError('TTS requests must be interruptible')

    def to_dict(self) -> Dict[str, Any]:
        """Return the text-only downstream synthesis request."""
        return {
            'schema_version': self.schema_version,
            'request_id': self.request_id,
            'speech_session_id': self.speech_session_id,
            'conversation_id': self.conversation_id,
            'turn_id': self.turn_id,
            'source_utterance_id': self.source_utterance_id,
            'text': self.text,
            'voice': self.voice,
            'style': self.style,
            'interruptible': self.interruptible,
        }


@dataclass(frozen=True)
class TTSCancelRequest:
    """Idempotent request to interrupt one active synthesis request."""

    request_id: str
    speech_session_id: str
    tts_request_id: str
    reason: str

    def __post_init__(self) -> None:
        """Validate cancellation identifiers and its closed reason set."""
        for name in (
            'request_id',
            'speech_session_id',
            'tts_request_id',
        ):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), name),
            )
        if self.reason not in {'barge_in', 'session_closed'}:
            raise ValidationError('TTS cancel reason is unsupported')

    def to_dict(self) -> Dict[str, str]:
        """Return the downstream cancellation request."""
        return {
            'request_id': self.request_id,
            'speech_session_id': self.speech_session_id,
            'tts_request_id': self.tts_request_id,
            'reason': self.reason,
        }


@dataclass(frozen=True)
class SpeechPipelineResult:
    """Terminal coordinator result for one transcript event."""

    status: str
    code: str
    capture_epoch: int
    request_id: Optional[str] = None
    turn_id: Optional[str] = None
    agent_result: Optional[OrchestrationResult] = None
    tts_request: Optional[TTSRequest] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return a public result without the original transcript."""
        return {
            'status': self.status,
            'code': self.code,
            'capture_epoch': self.capture_epoch,
            'request_id': self.request_id,
            'turn_id': self.turn_id,
            'agent': (
                self.agent_result.to_dict()
                if self.agent_result is not None
                else None
            ),
            'tts_request': (
                self.tts_request.to_dict()
                if self.tts_request is not None
                else None
            ),
        }


@dataclass(frozen=True)
class SpeechControlResult:
    """Result of barge-in, playback completion, or session close."""

    status: str
    code: str
    capture_epoch: int
    cancel_request: Optional[TTSCancelRequest] = None

    def to_dict(self) -> Dict[str, Any]:
        """Return control state without any transcript or audio content."""
        return {
            'status': self.status,
            'code': self.code,
            'capture_epoch': self.capture_epoch,
            'cancel_request': (
                self.cancel_request.to_dict()
                if self.cancel_request is not None
                else None
            ),
        }


@dataclass
class _SpeechSessionState:
    binding: TrustedSpeechBinding
    capture_epoch: int = 1
    last_final_sequence: int = 0
    closed: bool = False
    active_tts: Optional[TTSRequest] = None
    terminal_tts_ids: OrderedDict = field(
        default_factory=OrderedDict
    )
    transcript_cache: OrderedDict = field(
        default_factory=OrderedDict
    )
    activity_cache: OrderedDict = field(
        default_factory=OrderedDict
    )
    close_control_id: Optional[str] = None
    close_result: Optional[SpeechControlResult] = None


class SpeechConversationCoordinator:
    """Correlate final STT text with agent turns and text-only TTS."""

    def __init__(
        self,
        orchestrator: AgentOrchestrator,
        minimum_confidence: float = DEFAULT_MINIMUM_CONFIDENCE,
        event_cache_size: int = DEFAULT_EVENT_CACHE_SIZE,
    ) -> None:
        """Create a deterministic coordinator with bounded event caches."""
        if not isinstance(orchestrator, AgentOrchestrator):
            raise TypeError('orchestrator must be an AgentOrchestrator')
        self.orchestrator = orchestrator
        self.minimum_confidence = _number(
            minimum_confidence,
            'minimum_confidence',
            0.0,
            1.0,
        )
        self.event_cache_size = _integer(
            event_cache_size,
            'event_cache_size',
            1,
            4096,
        )
        self._sessions: Dict[str, _SpeechSessionState] = {}
        self._lock = threading.RLock()

    def open_session(
        self,
        binding: TrustedSpeechBinding,
    ) -> SpeechControlResult:
        """Bind identity and idempotently create its conversation."""
        if not isinstance(binding, TrustedSpeechBinding):
            raise TypeError('binding must be a TrustedSpeechBinding')
        with self._lock:
            existing = self._sessions.get(binding.speech_session_id)
            if existing is not None:
                if existing.binding != binding:
                    raise ValidationError(
                        'speech_session_id is already bound differently'
                    )
                if existing.closed:
                    raise ValidationError('speech session is closed')
                return SpeechControlResult(
                    status='ready',
                    code='session_already_open',
                    capture_epoch=existing.capture_epoch,
                )
            for state in self._sessions.values():
                if (
                    not state.closed
                    and state.binding.user_id == binding.user_id
                    and state.binding.conversation_id
                    == binding.conversation_id
                ):
                    raise ValidationError(
                        'conversation already has an active speech session'
                    )
            self.orchestrator.conversation_store.create(
                binding.user_id,
                binding.conversation_id,
            )
            state = _SpeechSessionState(binding=binding)
            self._sessions[binding.speech_session_id] = state
            return SpeechControlResult(
                status='ready',
                code='session_opened',
                capture_epoch=state.capture_epoch,
            )

    def handle_transcript(
        self,
        event: SpeechTranscriptEvent,
        *,
        robot_state: Optional[RobotState] = None,
        available_tools: Sequence[str] = (),
    ) -> SpeechPipelineResult:
        """Process only one trusted, final, current microphone transcript."""
        if not isinstance(event, SpeechTranscriptEvent):
            raise TypeError('event must be a SpeechTranscriptEvent')
        if robot_state is not None and not isinstance(
            robot_state, RobotState
        ):
            raise TypeError('robot_state must be a RobotState or None')
        with self._lock:
            state = self._sessions.get(event.speech_session_id)
            if state is None:
                return self._pipeline_result(
                    'rejected',
                    'unknown_speech_session',
                    event.capture_epoch,
                )
            mismatch = self._binding_mismatch(state.binding, event)
            if mismatch is not None:
                return self._pipeline_result(
                    'rejected',
                    mismatch,
                    state.capture_epoch,
                )
            if state.closed:
                return self._pipeline_result(
                    'rejected',
                    'speech_session_closed',
                    state.capture_epoch,
                )
            fingerprint = self._fingerprint(event.to_dict())
            cached = state.transcript_cache.get(event.utterance_id)
            if cached is not None:
                cached_fingerprint, cached_result = cached
                if cached_fingerprint != fingerprint:
                    return self._pipeline_result(
                        'rejected',
                        'utterance_conflict',
                        state.capture_epoch,
                    )
                state.transcript_cache.move_to_end(event.utterance_id)
                return cached_result
            if not event.is_final:
                return self._pipeline_result(
                    'ignored',
                    'partial_transcript',
                    state.capture_epoch,
                )
            if event.capture_origin == 'self_echo':
                return self._reject_final(
                    state,
                    event,
                    fingerprint,
                    'self_echo',
                )
            if event.capture_origin != 'microphone':
                return self._reject_final(
                    state,
                    event,
                    fingerprint,
                    'unknown_capture_origin',
                )
            if event.capture_epoch != state.capture_epoch:
                return self._reject_final(
                    state,
                    event,
                    fingerprint,
                    'stale_capture_epoch',
                )
            if state.active_tts is not None:
                return self._reject_final(
                    state,
                    event,
                    fingerprint,
                    'tts_playback_active',
                )
            if event.sequence <= state.last_final_sequence:
                return self._reject_final(
                    state,
                    event,
                    fingerprint,
                    'stale_transcript',
                )
            if event.confidence < self.minimum_confidence:
                return self._reject_final(
                    state,
                    event,
                    fingerprint,
                    'low_confidence',
                )

            request_id, turn_id = self._agent_ids(
                state.binding,
                event.utterance_id,
            )
            request = AgentRequest.from_dict(
                {
                    'request_id': request_id,
                    'user_id': state.binding.user_id,
                    'conversation_id': state.binding.conversation_id,
                    'turn_id': turn_id,
                    'utterance': event.text,
                    'robot_state': (
                        robot_state.to_dict()
                        if robot_state is not None
                        else {}
                    ),
                    'available_tools': list(available_tools),
                }
            )
            result = self.orchestrator.handle(request)
            tts_request = TTSRequest(
                schema_version=SPEECH_SCHEMA_VERSION,
                request_id=self._tts_request_id(request_id),
                speech_session_id=state.binding.speech_session_id,
                conversation_id=state.binding.conversation_id,
                turn_id=turn_id,
                source_utterance_id=event.utterance_id,
                text=result.decision.message,
            )
            pipeline_result = SpeechPipelineResult(
                status='responded',
                code='final_transcript_processed',
                capture_epoch=state.capture_epoch,
                request_id=request_id,
                turn_id=turn_id,
                agent_result=result,
                tts_request=tts_request,
            )
            state.last_final_sequence = event.sequence
            state.active_tts = tts_request
            return self._remember_transcript(
                state,
                event,
                fingerprint,
                pipeline_result,
            )

    def handle_barge_in(
        self,
        event: SpeechActivityEvent,
    ) -> SpeechControlResult:
        """Fence playback and return one idempotent TTS cancellation."""
        if not isinstance(event, SpeechActivityEvent):
            raise TypeError('event must be a SpeechActivityEvent')
        with self._lock:
            state = self._sessions.get(event.speech_session_id)
            if state is None:
                return SpeechControlResult(
                    status='rejected',
                    code='unknown_speech_session',
                    capture_epoch=0,
                )
            if state.closed:
                return SpeechControlResult(
                    status='rejected',
                    code='speech_session_closed',
                    capture_epoch=state.capture_epoch,
                )
            fingerprint = self._fingerprint(event.to_dict())
            cached = state.activity_cache.get(event.event_id)
            if cached is not None:
                cached_fingerprint, cached_result = cached
                if cached_fingerprint != fingerprint:
                    return SpeechControlResult(
                        status='rejected',
                        code='activity_conflict',
                        capture_epoch=state.capture_epoch,
                    )
                state.activity_cache.move_to_end(event.event_id)
                return cached_result
            if (
                event.speaker_id != state.binding.speaker_id
                or event.source != state.binding.source
            ):
                result = SpeechControlResult(
                    status='rejected',
                    code='speech_binding_mismatch',
                    capture_epoch=state.capture_epoch,
                )
            elif event.capture_epoch != state.capture_epoch:
                result = SpeechControlResult(
                    status='rejected',
                    code='stale_activity_epoch',
                    capture_epoch=state.capture_epoch,
                )
            else:
                active_tts = state.active_tts
                state.capture_epoch += 1
                state.active_tts = None
                cancel_request = None
                code = 'capture_epoch_advanced'
                if active_tts is not None:
                    cancel_request = TTSCancelRequest(
                        request_id=self._cancel_request_id(
                            active_tts.request_id,
                            event.event_id,
                            'barge_in',
                        ),
                        speech_session_id=state.binding.speech_session_id,
                        tts_request_id=active_tts.request_id,
                        reason='barge_in',
                    )
                    self._remember_terminal_tts(
                        state,
                        active_tts.request_id,
                    )
                    code = 'tts_cancel_requested'
                result = SpeechControlResult(
                    status='ready',
                    code=code,
                    capture_epoch=state.capture_epoch,
                    cancel_request=cancel_request,
                )
            self._remember(
                state.activity_cache,
                event.event_id,
                fingerprint,
                result,
            )
            return result

    def mark_tts_terminal(
        self,
        speech_session_id: str,
        tts_request_id: str,
    ) -> SpeechControlResult:
        """Fence echo after a downstream TTS terminal event."""
        normalized_session = _identifier(
            speech_session_id,
            'speech_session_id',
        )
        normalized_request = _identifier(
            tts_request_id,
            'tts_request_id',
        )
        with self._lock:
            state = self._sessions.get(normalized_session)
            if state is None:
                return SpeechControlResult(
                    status='rejected',
                    code='unknown_speech_session',
                    capture_epoch=0,
                )
            if normalized_request in state.terminal_tts_ids:
                return SpeechControlResult(
                    status='ready',
                    code='tts_already_terminal',
                    capture_epoch=state.capture_epoch,
                )
            if (
                state.active_tts is None
                or state.active_tts.request_id != normalized_request
            ):
                return SpeechControlResult(
                    status='rejected',
                    code='stale_tts_result',
                    capture_epoch=state.capture_epoch,
                )
            self._remember_terminal_tts(state, normalized_request)
            state.active_tts = None
            state.capture_epoch += 1
            return SpeechControlResult(
                status='ready',
                code='tts_terminal',
                capture_epoch=state.capture_epoch,
            )

    def close_session(
        self,
        speech_session_id: str,
        control_id: str,
    ) -> SpeechControlResult:
        """Close only the voice binding and cancel any active TTS."""
        normalized_session = _identifier(
            speech_session_id,
            'speech_session_id',
        )
        normalized_control = _identifier(control_id, 'control_id')
        with self._lock:
            state = self._sessions.get(normalized_session)
            if state is None:
                return SpeechControlResult(
                    status='rejected',
                    code='unknown_speech_session',
                    capture_epoch=0,
                )
            if state.closed:
                if state.close_control_id != normalized_control:
                    return SpeechControlResult(
                        status='rejected',
                        code='close_conflict',
                        capture_epoch=state.capture_epoch,
                    )
                if state.close_result is None:
                    raise RuntimeError('closed speech session has no result')
                return state.close_result
            self.orchestrator.conversation_store.close_session(
                state.binding.user_id,
                state.binding.conversation_id,
            )
            active_tts = state.active_tts
            cancel_request = None
            if active_tts is not None:
                cancel_request = TTSCancelRequest(
                    request_id=self._cancel_request_id(
                        active_tts.request_id,
                        normalized_control,
                        'session_closed',
                    ),
                    speech_session_id=state.binding.speech_session_id,
                    tts_request_id=active_tts.request_id,
                    reason='session_closed',
                )
                self._remember_terminal_tts(
                    state,
                    active_tts.request_id,
                )
            state.active_tts = None
            state.capture_epoch += 1
            state.closed = True
            result = SpeechControlResult(
                status='closed',
                code=(
                    'session_closed_tts_cancel_requested'
                    if cancel_request is not None
                    else 'session_closed'
                ),
                capture_epoch=state.capture_epoch,
                cancel_request=cancel_request,
            )
            state.close_control_id = normalized_control
            state.close_result = result
            return result

    @staticmethod
    def _binding_mismatch(
        binding: TrustedSpeechBinding,
        event: SpeechTranscriptEvent,
    ) -> Optional[str]:
        if event.conversation_id != binding.conversation_id:
            return 'conversation_mismatch'
        if event.speaker_id != binding.speaker_id:
            return 'speaker_mismatch'
        if event.source != binding.source:
            return 'source_mismatch'
        return None

    def _reject_final(
        self,
        state: _SpeechSessionState,
        event: SpeechTranscriptEvent,
        fingerprint: str,
        code: str,
    ) -> SpeechPipelineResult:
        return self._remember_transcript(
            state,
            event,
            fingerprint,
            self._pipeline_result(
                'rejected',
                code,
                state.capture_epoch,
            ),
        )

    def _remember_transcript(
        self,
        state: _SpeechSessionState,
        event: SpeechTranscriptEvent,
        fingerprint: str,
        result: SpeechPipelineResult,
    ) -> SpeechPipelineResult:
        self._remember(
            state.transcript_cache,
            event.utterance_id,
            fingerprint,
            result,
        )
        return result

    def _remember(
        self,
        cache: OrderedDict,
        key: str,
        fingerprint: str,
        result: Any,
    ) -> None:
        cache[key] = (fingerprint, result)
        cache.move_to_end(key)
        while len(cache) > self.event_cache_size:
            cache.popitem(last=False)

    def _remember_terminal_tts(
        self,
        state: _SpeechSessionState,
        tts_request_id: str,
    ) -> None:
        state.terminal_tts_ids[tts_request_id] = None
        state.terminal_tts_ids.move_to_end(tts_request_id)
        while len(state.terminal_tts_ids) > self.event_cache_size:
            state.terminal_tts_ids.popitem(last=False)

    @staticmethod
    def _pipeline_result(
        status: str,
        code: str,
        capture_epoch: int,
    ) -> SpeechPipelineResult:
        return SpeechPipelineResult(
            status=status,
            code=code,
            capture_epoch=capture_epoch,
        )

    @staticmethod
    def _fingerprint(value: Dict[str, Any]) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _agent_ids(
        binding: TrustedSpeechBinding,
        utterance_id: str,
    ) -> Tuple[str, str]:
        digest = hashlib.sha256(
            (
                'speech-turn-v1\0'
                f'{binding.user_id}\0'
                f'{binding.speech_session_id}\0'
                f'{binding.conversation_id}\0'
                f'{utterance_id}'
            ).encode('utf-8')
        ).hexdigest()[:40]
        return (
            f'speech-request-{digest}',
            f'speech-turn-{digest}',
        )

    @staticmethod
    def _tts_request_id(request_id: str) -> str:
        digest = hashlib.sha256(
            f'tts-v1\0{request_id}'.encode('utf-8')
        ).hexdigest()[:40]
        return f'speech-tts-{digest}'

    @staticmethod
    def _cancel_request_id(
        tts_request_id: str,
        control_id: str,
        reason: str,
    ) -> str:
        digest = hashlib.sha256(
            (
                f'tts-cancel-v1\0{tts_request_id}\0'
                f'{control_id}\0{reason}'
            ).encode('utf-8')
        ).hexdigest()[:40]
        return f'speech-cancel-{digest}'
