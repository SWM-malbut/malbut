"""
Offline speech-to-agent boundary for one trusted local voice session.

This module intentionally accepts text and bounded metadata only.  It does
not import an STT or TTS SDK, open an audio device, or define a ROS adapter.
"""

import hashlib
import json
import math
import sqlite3
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, Optional, Protocol, Sequence, Tuple

from malbut_agent_server.confirmation import (
    AuthenticatedUIActor,
    ToolConfirmationResolution,
    ToolConfirmationRequest,
    ToolConfirmationResponseEvent,
    ToolConfirmationUIResponseEvent,
    build_monitor_room_confirmation,
    classify_confirmation_response,
)
from malbut_agent_server.conversation import (
    ConfirmationIntentDraft,
    ConfirmationIntentAlreadyTerminalError,
    ConfirmationIntentConflictError,
    ConfirmationIntentNotFoundError,
    ConfirmationReservedResponseIdError,
    ConversationChangedError,
    ConversationClockError,
    ConversationConflictError,
    ConversationNotFoundError,
    ConversationStateError,
)
from malbut_agent_server.orchestrator import (
    AgentOrchestrator,
    GazeboSimulationEvidenceBinding,
    OrchestrationCancelledError,
    OrchestrationResult,
)
from malbut_agent_server.monitor_room_target import TargetBinding
from malbut_agent_server.schemas import (
    MAX_ID_LENGTH,
    MAX_UTTERANCE_LENGTH,
    AgentRequest,
    RobotState,
    ValidationError,
    validate_conversation_id,
    validate_user_id,
)
from malbut_agent_server.trusted_result_tts import (
    TrustedResultTTSClaim,
    TrustedResultTTSConflictError,
    TrustedResultTTSError,
)


SPEECH_SCHEMA_VERSION = 1
DEFAULT_MINIMUM_CONFIDENCE = 0.75
DEFAULT_EVENT_CACHE_SIZE = 256
DEFAULT_MAX_SESSION_STATES = 1024
MAX_ACTIVE_CONFIRMATION_RESPONSE_CLAIMS = 256
MAX_SOURCE_LENGTH = 64
MAX_AUDIO_DURATION_MS = 30000
MIN_SAMPLE_RATE_HZ = 8000
MAX_SAMPLE_RATE_HZ = 48000
MAX_SEQUENCE = (1 << 63) - 1
CAPTURE_ORIGINS = frozenset({'microphone', 'self_echo', 'unknown'})
TTS_VOICES = frozenset({'default'})
TTS_STYLES = frozenset({'neutral'})


@dataclass(frozen=True)
class MonitorRoomTargetRequest:
    """Trusted server context passed to a semantic target resolver."""

    user_id: str
    speech_session_id: str
    source_utterance_id: str
    conversation_id: str
    conversation_session_instance_id: str
    conversation_generation: int
    conversation_revision: int
    conversation_ordinal: int
    agent_request_id: str
    turn_id: str
    decision_id: str
    location: str
    issued_at: float
    expires_at: float


class MonitorRoomTargetResolver(Protocol):
    """Resolve one server-bound room target without granting authority."""

    def resolve(self, request: MonitorRoomTargetRequest) -> TargetBinding:
        """Return one immutable target/effects binding."""


class _MonitorRoomTargetFailure(RuntimeError):
    """Internal control flow for a content-free target rejection."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class _StoredConfirmationDelivery:
    """One durable pending prompt or terminal historical receipt."""

    request: Optional[ToolConfirmationRequest]
    terminal_code: Optional[str]


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
class TrustedResultTTSRequest:
    """
    One leased simulation-result notification for a TTS adapter.

    This is deliberately not a :class:`TTSRequest`.  Its private durable
    claim credentials and exact conversation lifecycle binding may only be
    consumed by the dedicated terminal method below.
    """

    schema_version: int
    request_id: str
    speech_session_id: str
    conversation_id: str
    terminal_request_id: str
    result_code: str
    template_key: str
    text: str
    claim_fence: int
    attempt_number: int
    claimed_at: float
    lease_expires_at: float
    user_id: str = field(repr=False)
    conversation_session_instance_id: str = field(repr=False)
    conversation_generation: int = field(repr=False)
    claim_request_id: str = field(repr=False)
    claim_token: str = field(repr=False)
    lease_seconds: int = field(repr=False)
    voice: str = 'default'
    style: str = 'neutral'
    interruptible: bool = True

    def __post_init__(self) -> None:
        """Reject forged identities, authority, and lease projections."""
        object.__setattr__(
            self,
            'schema_version',
            _schema_version(self.schema_version),
        )
        for name in (
            'request_id',
            'speech_session_id',
            'conversation_session_instance_id',
            'claim_request_id',
            'claim_token',
            'terminal_request_id',
            'result_code',
            'template_key',
        ):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            'user_id',
            validate_user_id(self.user_id),
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
        object.__setattr__(
            self,
            'conversation_generation',
            _integer(
                self.conversation_generation,
                'conversation_generation',
                1,
                MAX_SEQUENCE,
            ),
        )
        object.__setattr__(
            self,
            'claim_fence',
            _integer(self.claim_fence, 'claim_fence', 1, 5),
        )
        object.__setattr__(
            self,
            'lease_seconds',
            _integer(self.lease_seconds, 'lease_seconds', 1, 300),
        )
        object.__setattr__(
            self,
            'attempt_number',
            _integer(self.attempt_number, 'attempt_number', 1, 5),
        )
        object.__setattr__(
            self,
            'claimed_at',
            _number(
                self.claimed_at,
                'claimed_at',
                0.0,
                float(MAX_SEQUENCE),
            ),
        )
        object.__setattr__(
            self,
            'lease_expires_at',
            _number(
                self.lease_expires_at,
                'lease_expires_at',
                0.0,
                float(MAX_SEQUENCE),
            ),
        )
        if (
            self.attempt_number != self.claim_fence
            or self.lease_expires_at <= self.claimed_at
            or self.voice not in TTS_VOICES
            or self.style not in TTS_STYLES
            or self.interruptible is not True
        ):
            raise ValidationError(
                'trusted result TTS request is invalid'
            )

    @property
    def physical_audio_verified(self) -> bool:
        """Return false because an adapter ACK is not audible proof."""
        return False

    def to_dict(self) -> Dict[str, Any]:
        """Return the content-minimized adapter request projection."""
        return {
            'schema_version': self.schema_version,
            'request_id': self.request_id,
            'speech_session_id': self.speech_session_id,
            'conversation_id': self.conversation_id,
            'terminal_request_id': self.terminal_request_id,
            'result_code': self.result_code,
            'template_key': self.template_key,
            'text': self.text,
            'claim_fence': self.claim_fence,
            'attempt_number': self.attempt_number,
            'claimed_at': self.claimed_at,
            'lease_expires_at': self.lease_expires_at,
            'voice': self.voice,
            'style': self.style,
            'interruptible': self.interruptible,
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
            'execution_authorized': False,
            'physical_audio_verified': False,
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
        if self.reason not in {
            'barge_in',
            'confirmation_invalidated',
            'confirmation_resolved',
            'session_closed',
            'trusted_result_invalidated',
            'lease_expired',
        }:
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
    tts_cancel_request: Optional[TTSCancelRequest] = None
    confirmation_request: Optional[ToolConfirmationRequest] = None
    confirmation_resolution: Optional[
        ToolConfirmationResolution
    ] = None

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
            'tts_cancel_request': (
                self.tts_cancel_request.to_dict()
                if self.tts_cancel_request is not None
                else None
            ),
            'confirmation_request': (
                self.confirmation_request.to_dict()
                if self.confirmation_request is not None
                else None
            ),
            'confirmation_resolution': (
                self.confirmation_resolution.to_dict()
                if self.confirmation_resolution is not None
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


@dataclass(frozen=True)
class TrustedResultTTSControlResult:
    """Typed outcome for the durable notification delivery lane."""

    status: str
    code: str
    capture_epoch: int
    tts_request: Optional[TrustedResultTTSRequest] = None
    cancel_request: Optional[TTSCancelRequest] = None

    @property
    def physical_audio_verified(self) -> bool:
        """Return false for every claim, cancel, and terminal outcome."""
        return False

    def to_dict(self) -> Dict[str, Any]:
        """Return a non-authorizing scripted-adapter projection."""
        return {
            'status': self.status,
            'code': self.code,
            'capture_epoch': self.capture_epoch,
            'tts_request': (
                self.tts_request.to_dict()
                if self.tts_request is not None
                else None
            ),
            'cancel_request': (
                self.cancel_request.to_dict()
                if self.cancel_request is not None
                else None
            ),
            'simulation': True,
            'physical_authorized': False,
            'physical_effects': False,
            'execution_authorized': False,
            'physical_audio_verified': False,
        }


@dataclass
class _InFlightTranscript:
    """One transcript reserved while inference runs without the lock."""

    utterance_id: str
    fingerprint: str
    capture_epoch: int
    sequence: int
    request_id: str
    turn_id: str
    completed_result: Optional[SpeechPipelineResult] = None


@dataclass(frozen=True)
class _TrustedResultTTSClaimReservation:
    """Exact local lifecycle reserved before an unlocked store claim."""

    claim_request_id: str
    lease_seconds: int
    capture_epoch: int
    user_id: str
    conversation_id: str
    speech_session_id: str
    conversation_session_instance_id: str
    conversation_generation: int
    expected_active_request: Optional[TrustedResultTTSRequest] = None


@dataclass(frozen=True)
class _TrustedResultTTSTerminalReservation:
    """Exact delivery attempt reserved before an unlocked durable ACK."""

    request: TrustedResultTTSRequest
    was_active: bool
    capture_epoch: int
    was_terminal: bool = False
    was_cancel_terminal: bool = False


@dataclass
class _SpeechSessionState:
    binding: TrustedSpeechBinding
    conversation_session_instance_id: str
    conversation_generation: int
    lock: Any = field(
        default_factory=threading.RLock,
        repr=False,
        compare=False,
    )
    capture_epoch: int = 1
    last_final_sequence: int = 0
    closed: bool = False
    active_tts: Optional[TTSRequest] = None
    active_trusted_result_tts: Optional[
        TrustedResultTTSRequest
    ] = None
    terminal_pending_trusted_result_tts: Optional[
        TrustedResultTTSRequest
    ] = None
    terminal_pending_trusted_result_tts_cancel_result: Optional[
        TrustedResultTTSControlResult
    ] = None
    trusted_result_tts_claim_reservation: Optional[
        _TrustedResultTTSClaimReservation
    ] = None
    trusted_result_tts_terminal_reservation: Optional[
        _TrustedResultTTSTerminalReservation
    ] = None
    pending_confirmation: Optional[ToolConfirmationRequest] = None
    confirmation_response_cache: OrderedDict = field(
        default_factory=OrderedDict
    )
    confirmation_response_claims: OrderedDict = field(
        default_factory=OrderedDict
    )
    terminal_confirmations: OrderedDict = field(
        default_factory=OrderedDict
    )
    terminal_tts_ids: OrderedDict = field(
        default_factory=OrderedDict
    )
    terminal_trusted_result_tts_ids: OrderedDict = field(
        default_factory=OrderedDict
    )
    terminal_cancel_trusted_result_tts_ids: OrderedDict = field(
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
    in_flight: Optional[_InFlightTranscript] = None


class SpeechConversationCoordinator:
    """
    Correlate final STT text with agent turns and text-only TTS.

    TTS lane exclusion is process-local to this coordinator.  It does not
    provide multi-process speaker arbitration, start a background outbox
    drain, or open an audio device.
    """

    def __init__(
        self,
        orchestrator: AgentOrchestrator,
        minimum_confidence: float = DEFAULT_MINIMUM_CONFIDENCE,
        event_cache_size: int = DEFAULT_EVENT_CACHE_SIZE,
        max_session_states: int = DEFAULT_MAX_SESSION_STATES,
        clock=time.time,
        monitor_room_target_resolver: Optional[
            MonitorRoomTargetResolver
        ] = None,
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
        self.max_session_states = _integer(
            max_session_states,
            'max_session_states',
            1,
            4096,
        )
        if not callable(clock):
            raise TypeError('clock must be callable')
        if (
            monitor_room_target_resolver is not None
            and not callable(
                getattr(monitor_room_target_resolver, 'resolve', None)
            )
        ):
            raise TypeError(
                'monitor_room_target_resolver must provide resolve()'
            )
        self._clock = clock
        self._monitor_room_target_resolver = monitor_room_target_resolver
        self._sessions: Dict[str, _SpeechSessionState] = {}
        self._sessions_lock = threading.RLock()
        self._lease_lock = threading.Lock()

    def open_session(
        self,
        binding: TrustedSpeechBinding,
    ) -> SpeechControlResult:
        """Bind identity and idempotently create its conversation."""
        if not isinstance(binding, TrustedSpeechBinding):
            raise TypeError('binding must be a TrustedSpeechBinding')
        with self._lease_lock:
            existing = self._session_state(binding.speech_session_id)
            if existing is not None:
                with existing.lock:
                    if existing.binding != binding:
                        raise ValidationError(
                            'speech_session_id is already bound differently'
                        )
                    if existing.closed:
                        raise ValidationError('speech session is closed')
                    context_code = self._speech_session_context_code(
                        existing
                    )
                    if context_code is not None:
                        return self._tts_context_failure_locked(
                            existing,
                            context_code,
                        )
                    return SpeechControlResult(
                        status='ready',
                        code='session_already_open',
                        capture_epoch=existing.capture_epoch,
                    )
            with self._sessions_lock:
                states = list(self._sessions.values())
            for state in states:
                with state.lock:
                    if (
                        state.closed
                        or state.binding.user_id != binding.user_id
                        or state.binding.conversation_id
                        != binding.conversation_id
                    ):
                        continue
                    context_code = self._speech_session_context_code(
                        state
                    )
                    if context_code is not None:
                        return self._tts_context_failure_locked(
                            state,
                            context_code,
                        )
                    raise ValidationError(
                        'conversation already has an active speech '
                        'session'
                    )
            if len(states) >= self.max_session_states:
                raise ValidationError('speech session capacity reached')
            conversation_session = (
                self.orchestrator.conversation_store.create(
                    binding.user_id,
                    binding.conversation_id,
                )
            )
            state = _SpeechSessionState(
                binding=binding,
                conversation_session_instance_id=(
                    conversation_session.session_instance_id
                ),
                conversation_generation=(
                    conversation_session.generation
                ),
            )
            with self._sessions_lock:
                self._sessions[binding.speech_session_id] = state
            return SpeechControlResult(
                status='ready',
                code='session_opened',
                capture_epoch=state.capture_epoch,
            )

    def claim_trusted_result_tts(
        self,
        speech_session_id: str,
        claim_request_id: str,
        lease_seconds: int = 30,
    ) -> TrustedResultTTSControlResult:
        """Claim one durable notification without holding the speech lock."""
        normalized_session = _identifier(
            speech_session_id,
            'speech_session_id',
        )
        normalized_request = _identifier(
            claim_request_id,
            'claim_request_id',
        )
        normalized_lease = _integer(
            lease_seconds,
            'lease_seconds',
            1,
            300,
        )
        state = self._session_state(normalized_session)
        if state is None:
            return TrustedResultTTSControlResult(
                status='rejected',
                code='unknown_speech_session',
                capture_epoch=0,
            )
        expiry_cancel_replay = None
        with state.lock:
            pending = state.terminal_pending_trusted_result_tts
            cached_cancel = (
                state.terminal_pending_trusted_result_tts_cancel_result
            )
            if (
                not state.closed
                and pending is not None
                and cached_cancel is not None
                and pending.claim_request_id == normalized_request
            ):
                if pending.lease_seconds != normalized_lease:
                    return TrustedResultTTSControlResult(
                        status='rejected',
                        code='trusted_result_tts_claim_conflict',
                        capture_epoch=state.capture_epoch,
                    )
                expiry_cancel_replay = (pending, cached_cancel)
        if expiry_cancel_replay is not None:
            pending, cached_cancel = expiry_cancel_replay
            context_code = self._trusted_result_tts_request_context_code(
                pending
            )
            with state.lock:
                if (
                    state.closed
                    or state.terminal_pending_trusted_result_tts
                    is not pending
                    or state.terminal_pending_trusted_result_tts_cancel_result
                    is not cached_cancel
                ):
                    return TrustedResultTTSControlResult(
                        status='retryable',
                        code='trusted_result_tts_claim_superseded_retry_exact',
                        capture_epoch=state.capture_epoch,
                    )
                if context_code is not None:
                    if context_code == 'conversation_context_unavailable':
                        return TrustedResultTTSControlResult(
                            status='retryable',
                            code=context_code,
                            capture_epoch=state.capture_epoch,
                        )
                    return self._trusted_result_tts_context_failure_locked(
                        state,
                        context_code,
                        pending.claim_request_id,
                    )
                return cached_cancel
        with state.lock:
            if state.closed:
                return TrustedResultTTSControlResult(
                    status='rejected',
                    code='speech_session_closed',
                    capture_epoch=state.capture_epoch,
                )
            active = state.active_trusted_result_tts
            if state.trusted_result_tts_claim_reservation is not None:
                reservation = (
                    state.trusted_result_tts_claim_reservation
                )
                if (
                    reservation.claim_request_id
                    == normalized_request
                    and reservation.lease_seconds == normalized_lease
                ):
                    return TrustedResultTTSControlResult(
                        status='processing',
                        code='trusted_result_tts_claim_in_progress',
                        capture_epoch=state.capture_epoch,
                    )
                return TrustedResultTTSControlResult(
                    status='retryable',
                    code='trusted_result_tts_claim_reserved',
                    capture_epoch=state.capture_epoch,
                )
            if state.trusted_result_tts_terminal_reservation is not None:
                return TrustedResultTTSControlResult(
                    status='retryable',
                    code='trusted_result_tts_terminal_in_progress',
                    capture_epoch=state.capture_epoch,
                )
            if state.active_tts is not None:
                return TrustedResultTTSControlResult(
                    status='retryable',
                    code='normal_tts_playback_active',
                    capture_epoch=state.capture_epoch,
                )
            if state.pending_confirmation is not None:
                return TrustedResultTTSControlResult(
                    status='retryable',
                    code='confirmation_pending',
                    capture_epoch=state.capture_epoch,
                )
            if state.in_flight is not None:
                return TrustedResultTTSControlResult(
                    status='retryable',
                    code='inference_in_progress',
                    capture_epoch=state.capture_epoch,
                )
            if state.terminal_pending_trusted_result_tts is not None:
                return TrustedResultTTSControlResult(
                    status='retryable',
                    code='trusted_result_tts_terminal_pending',
                    capture_epoch=state.capture_epoch,
                )
            if active is not None and (
                active.claim_request_id != normalized_request
                or active.lease_seconds != normalized_lease
            ):
                return TrustedResultTTSControlResult(
                    status='retryable',
                    code='trusted_result_tts_playback_active',
                    capture_epoch=state.capture_epoch,
                )
            reservation = _TrustedResultTTSClaimReservation(
                claim_request_id=normalized_request,
                lease_seconds=normalized_lease,
                capture_epoch=state.capture_epoch,
                user_id=state.binding.user_id,
                conversation_id=state.binding.conversation_id,
                speech_session_id=state.binding.speech_session_id,
                conversation_session_instance_id=(
                    state.conversation_session_instance_id
                ),
                conversation_generation=state.conversation_generation,
                expected_active_request=active,
            )
            state.trusted_result_tts_claim_reservation = reservation

        context_code = self._trusted_result_tts_context_code(
            reservation
        )
        if context_code is not None:
            return self._finish_trusted_result_tts_claim_failure(
                state,
                reservation,
                context_code,
                transient=(
                    context_code == 'conversation_context_unavailable'
                ),
            )
        try:
            claim = (
                self.orchestrator.conversation_store
                .claim_trusted_result_tts(
                    reservation.user_id,
                    reservation.conversation_id,
                    reservation.speech_session_id,
                    reservation.claim_request_id,
                    lease_seconds=reservation.lease_seconds,
                )
            )
        except TrustedResultTTSConflictError:
            context_code = self._trusted_result_tts_context_code(
                reservation
            )
            return self._finish_trusted_result_tts_claim_failure(
                state,
                reservation,
                context_code or 'trusted_result_tts_claim_conflict',
                transient=False,
            )
        except (
            TrustedResultTTSError,
            ConversationClockError,
            sqlite3.Error,
        ):
            return self._finish_trusted_result_tts_claim_failure(
                state,
                reservation,
                'trusted_result_tts_store_unavailable',
                transient=True,
            )
        if claim is None:
            context_code = self._trusted_result_tts_context_code(
                reservation
            )
            if context_code is not None:
                return self._finish_trusted_result_tts_claim_failure(
                    state,
                    reservation,
                    context_code,
                    transient=(
                        context_code
                        == 'conversation_context_unavailable'
                    ),
                )
            return self._finish_empty_trusted_result_tts_claim(
                state,
                reservation,
            )
        try:
            request = self._trusted_result_tts_request(
                reservation,
                claim,
            )
        except (TrustedResultTTSError, TypeError, ValidationError):
            return self._finish_trusted_result_tts_claim_failure(
                state,
                reservation,
                'trusted_result_tts_claim_invalid',
                transient=True,
            )
        context_code = self._trusted_result_tts_request_context_code(
            request
        )
        if context_code is not None:
            return self._finish_trusted_result_tts_claim_failure(
                state,
                reservation,
                context_code,
                transient=(
                    context_code == 'conversation_context_unavailable'
                ),
            )
        with state.lock:
            if not self._trusted_result_tts_claim_cas_matches(
                state,
                reservation,
            ):
                if (
                    state.trusted_result_tts_claim_reservation
                    is reservation
                ):
                    state.trusted_result_tts_claim_reservation = None
                if state.closed:
                    return TrustedResultTTSControlResult(
                        status='rejected',
                        code='speech_session_closed',
                        capture_epoch=state.capture_epoch,
                    )
                return TrustedResultTTSControlResult(
                    status='retryable',
                    code='trusted_result_tts_claim_superseded_retry_exact',
                    capture_epoch=state.capture_epoch,
                )
            state.trusted_result_tts_claim_reservation = None
            if reservation.expected_active_request is None:
                state.active_trusted_result_tts = request
                code = 'trusted_result_tts_claimed'
            else:
                state.active_trusted_result_tts = request
                code = 'trusted_result_tts_claim_replayed'
            return TrustedResultTTSControlResult(
                status='ready',
                code=code,
                capture_epoch=state.capture_epoch,
                tts_request=request,
            )

    def mark_trusted_result_tts_terminal(
        self,
        speech_session_id: str,
        tts_request_id: str,
        terminal_request_id: str,
    ) -> TrustedResultTTSControlResult:
        """ACK one exact notification attempt through its private claim."""
        normalized_session = _identifier(
            speech_session_id,
            'speech_session_id',
        )
        normalized_tts = _identifier(
            tts_request_id,
            'tts_request_id',
        )
        normalized_terminal = _identifier(
            terminal_request_id,
            'terminal_request_id',
        )
        state = self._session_state(normalized_session)
        if state is None:
            return TrustedResultTTSControlResult(
                status='rejected',
                code='unknown_speech_session',
                capture_epoch=0,
            )
        terminal_key = (normalized_tts, normalized_terminal)
        with state.lock:
            if state.closed:
                return TrustedResultTTSControlResult(
                    status='rejected',
                    code='speech_session_closed',
                    capture_epoch=state.capture_epoch,
                )
            existing_reservation = (
                state.trusted_result_tts_terminal_reservation
            )
            if existing_reservation is not None:
                existing = existing_reservation.request
                if (
                    existing.request_id == normalized_tts
                    and existing.terminal_request_id
                    == normalized_terminal
                ):
                    return TrustedResultTTSControlResult(
                        status='processing',
                        code='trusted_result_tts_terminal_in_progress',
                        capture_epoch=state.capture_epoch,
                    )
                return TrustedResultTTSControlResult(
                    status='rejected',
                    code='stale_trusted_result_tts_terminal',
                    capture_epoch=state.capture_epoch,
                )
            request = state.terminal_trusted_result_tts_ids.get(
                terminal_key
            )
            was_terminal = request is not None
            was_cancel_terminal = False
            if request is None:
                request = state.terminal_cancel_trusted_result_tts_ids.get(
                    terminal_key
                )
                was_cancel_terminal = request is not None
            was_active = False
            if request is None:
                request = state.active_trusted_result_tts
                was_active = request is not None
            if request is None:
                request = state.terminal_pending_trusted_result_tts
            if (
                request is None
                or request.request_id != normalized_tts
                or request.terminal_request_id != normalized_terminal
            ):
                return TrustedResultTTSControlResult(
                    status='rejected',
                    code='stale_trusted_result_tts_terminal',
                    capture_epoch=state.capture_epoch,
                )
            reservation = _TrustedResultTTSTerminalReservation(
                request=request,
                was_active=was_active,
                capture_epoch=state.capture_epoch,
                was_terminal=was_terminal,
                was_cancel_terminal=was_cancel_terminal,
            )
            state.trusted_result_tts_terminal_reservation = reservation

        context_code = self._trusted_result_tts_request_context_code(
            request
        )
        if context_code is not None:
            return self._finish_trusted_result_tts_terminal_failure(
                state,
                reservation,
                context_code,
                transient=(
                    context_code == 'conversation_context_unavailable'
                ),
            )
        if reservation.was_cancel_terminal:
            with state.lock:
                if (
                    state.trusted_result_tts_terminal_reservation
                    is not reservation
                ):
                    return TrustedResultTTSControlResult(
                        status='retryable',
                        code='trusted_result_tts_terminal_superseded',
                        capture_epoch=state.capture_epoch,
                    )
                state.trusted_result_tts_terminal_reservation = None
                if state.closed:
                    return TrustedResultTTSControlResult(
                        status='rejected',
                        code='speech_session_closed',
                        capture_epoch=state.capture_epoch,
                    )
                return TrustedResultTTSControlResult(
                    status='ready',
                    code='trusted_result_tts_cancel_already_terminal',
                    capture_epoch=state.capture_epoch,
                )
        try:
            store = self.orchestrator.conversation_store
            store.acknowledge_trusted_result_tts(
                request.user_id,
                request.conversation_id,
                request.speech_session_id,
                event_id=request.request_id,
                claim_token=request.claim_token,
                claim_fence=request.claim_fence,
            )
        except TrustedResultTTSConflictError:
            context_code = self._trusted_result_tts_request_context_code(
                request
            )
            return self._finish_trusted_result_tts_terminal_failure(
                state,
                reservation,
                context_code or 'stale_trusted_result_tts_terminal',
                transient=False,
                discard_request=True,
            )
        except (
            TrustedResultTTSError,
            ConversationClockError,
            sqlite3.Error,
        ):
            return self._finish_trusted_result_tts_terminal_failure(
                state,
                reservation,
                'trusted_result_tts_store_unavailable',
                transient=True,
            )
        with state.lock:
            if (
                state.trusted_result_tts_terminal_reservation
                is not reservation
            ):
                return TrustedResultTTSControlResult(
                    status='retryable',
                    code='trusted_result_tts_terminal_superseded',
                    capture_epoch=state.capture_epoch,
                )
            state.trusted_result_tts_terminal_reservation = None
            if state.closed:
                return TrustedResultTTSControlResult(
                    status='rejected',
                    code='speech_session_closed',
                    capture_epoch=state.capture_epoch,
                )
            if reservation.was_terminal:
                return TrustedResultTTSControlResult(
                    status='ready',
                    code='trusted_result_tts_already_terminal',
                    capture_epoch=state.capture_epoch,
                )
            if state.active_trusted_result_tts is request:
                state.active_trusted_result_tts = None
                state.capture_epoch += 1
            elif state.terminal_pending_trusted_result_tts is request:
                state.terminal_pending_trusted_result_tts = None
                state.terminal_pending_trusted_result_tts_cancel_result = (
                    None
                )
            else:
                return TrustedResultTTSControlResult(
                    status='retryable',
                    code='trusted_result_tts_terminal_superseded',
                    capture_epoch=state.capture_epoch,
                )
            self._remember_terminal_trusted_result_tts(
                state,
                terminal_key,
                request,
            )
            return TrustedResultTTSControlResult(
                status='ready',
                code='trusted_result_tts_terminal',
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
        fingerprint = self._fingerprint(event.to_dict())
        state = self._session_state(event.speech_session_id)
        if state is None:
            return self._pipeline_result(
                'rejected',
                'unknown_speech_session',
                event.capture_epoch,
            )
        with state.lock:
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
        speech_context_code = self._speech_session_context_code(state)
        if speech_context_code is not None:
            with state.lock:
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
                return self._speech_context_failure_locked(
                    state,
                    event,
                    fingerprint,
                    speech_context_code,
                )
        confirmation_pending = False
        with state.lock:
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
            if state.trusted_result_tts_claim_reservation is not None:
                return SpeechPipelineResult(
                    status='retryable',
                    code='trusted_result_tts_claim_in_progress',
                    capture_epoch=state.capture_epoch,
                )
            if state.trusted_result_tts_terminal_reservation is not None:
                return SpeechPipelineResult(
                    status='retryable',
                    code='trusted_result_tts_terminal_in_progress',
                    capture_epoch=state.capture_epoch,
                )
            if state.active_trusted_result_tts is not None:
                return self._reject_final(
                    state,
                    event,
                    fingerprint,
                    'trusted_result_tts_playback_active',
                )
            if state.terminal_pending_trusted_result_tts is not None:
                return SpeechPipelineResult(
                    status='retryable',
                    code='trusted_result_tts_terminal_pending',
                    capture_epoch=state.capture_epoch,
                )
            if (
                state.active_tts is not None
                and state.pending_confirmation is not None
            ):
                return SpeechPipelineResult(
                    status='retryable',
                    code='confirmation_prompt_active',
                    capture_epoch=state.capture_epoch,
                    request_id=(
                        state.pending_confirmation.agent_request_id
                    ),
                    turn_id=state.pending_confirmation.turn_id,
                    confirmation_request=state.pending_confirmation,
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
            if state.pending_confirmation is not None:
                confirmation_pending = True
            else:
                request_id, turn_id = self._agent_ids(
                    state.binding,
                    event.utterance_id,
                )
                if state.in_flight is not None:
                    in_flight = state.in_flight
                    if in_flight.utterance_id == event.utterance_id:
                        if in_flight.fingerprint != fingerprint:
                            return self._pipeline_result(
                                'rejected',
                                'utterance_conflict',
                                state.capture_epoch,
                            )
                        return SpeechPipelineResult(
                            status='processing',
                            code='transcript_in_progress',
                            capture_epoch=state.capture_epoch,
                            request_id=in_flight.request_id,
                            turn_id=in_flight.turn_id,
                        )
                    return self._pipeline_result(
                        'retryable',
                        'inference_in_progress',
                        state.capture_epoch,
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
                reservation = _InFlightTranscript(
                    utterance_id=event.utterance_id,
                    fingerprint=fingerprint,
                    capture_epoch=state.capture_epoch,
                    sequence=event.sequence,
                    request_id=request_id,
                    turn_id=turn_id,
                )
                state.in_flight = reservation

        if confirmation_pending:
            return self._handle_confirmation_transcript(
                state,
                event,
                fingerprint,
            )

        try:
            self.orchestrator.handle(
                request,
                expected_session_instance_id=(
                    state.conversation_session_instance_id
                ),
                result_completion_guard=lambda result: self._completion_guard(
                    event.speech_session_id,
                    reservation,
                    result,
                    event,
                ),
            )
        except OrchestrationCancelledError:
            discarded = self._discard_changed_inference(
                event,
                reservation,
            )
            if discarded is None:
                return SpeechPipelineResult(
                    status='discarded',
                    code='inference_cancelled_before_commit',
                    capture_epoch=reservation.capture_epoch,
                    request_id=reservation.request_id,
                    turn_id=reservation.turn_id,
                )
            return discarded
        except _MonitorRoomTargetFailure as error:
            return self._monitor_room_target_failure_result(
                event,
                reservation,
                error.code,
            )
        except ConfirmationIntentAlreadyTerminalError:
            return self._confirmation_registration_terminal_result(
                event,
                reservation,
            )
        except (
            ConversationChangedError,
            ConversationConflictError,
            ConversationNotFoundError,
            ConversationStateError,
        ) as error:
            return self._conversation_failure_result(
                event,
                reservation,
                error,
            )
        except Exception:
            discarded = self._discard_changed_inference(
                event,
                reservation,
            )
            if discarded is not None:
                return discarded
            raise

        pipeline_result = reservation.completed_result
        if pipeline_result is None:
            raise RuntimeError(
                'speech completion guard returned no pipeline result'
            )
        return pipeline_result

    def handle_ui_confirmation_response(
        self,
        event: ToolConfirmationUIResponseEvent,
        actor: AuthenticatedUIActor,
    ) -> SpeechPipelineResult:
        """Record one authenticated UI intent without audio epoch coupling."""
        if not isinstance(event, ToolConfirmationUIResponseEvent):
            raise TypeError(
                'event must be a ToolConfirmationUIResponseEvent'
            )
        if not isinstance(actor, AuthenticatedUIActor):
            raise TypeError('actor must be an AuthenticatedUIActor')
        state = self._session_state(event.speech_session_id)
        if state is None:
            return self._pipeline_result(
                'rejected',
                'unknown_speech_session',
                0,
            )
        fingerprint = self._fingerprint(
            {
                'event': event.to_dict(),
                'actor': {
                    'user_id': actor.user_id,
                    'auth_session_id': actor.auth_session_id,
                    'authentication_method': (
                        actor.authentication_method
                    ),
                },
            }
        )
        with state.lock:
            cached = state.confirmation_response_cache.get(
                event.response_id
            )
            if cached is not None:
                cached_fingerprint, cached_result = cached
                if cached_fingerprint != fingerprint:
                    return self._pipeline_result(
                        'rejected',
                        'confirmation_response_conflict',
                        state.capture_epoch,
                    )
                state.confirmation_response_cache.move_to_end(
                    event.response_id
                )
                return cached_result
            if state.closed:
                return self._pipeline_result(
                    'rejected',
                    'speech_session_closed',
                    state.capture_epoch,
                )
            if actor.user_id != state.binding.user_id:
                return self._pipeline_result(
                    'rejected',
                    'confirmation_unavailable',
                    state.capture_epoch,
                )
            if (
                event.confirmation_request_id
                in state.terminal_confirmations
            ):
                return self._pipeline_result(
                    'rejected',
                    'confirmation_already_resolved',
                    state.capture_epoch,
                )
            pending = state.pending_confirmation
            if (
                pending is None
                or pending.confirmation_request_id
                != event.confirmation_request_id
            ):
                return self._pipeline_result(
                    'rejected',
                    'confirmation_unavailable',
                    state.capture_epoch,
                )
            claim_conflict = self._claim_confirmation_response_locked(
                state,
                event.response_id,
                fingerprint,
            )
            if claim_conflict is not None:
                return claim_conflict
            normalized = ToolConfirmationResponseEvent(
                response_id=event.response_id,
                speech_session_id=state.binding.speech_session_id,
                conversation_id=state.binding.conversation_id,
                confirmation_request_id=(
                    pending.confirmation_request_id
                ),
                decision_id=pending.decision_id,
                proposal_fingerprint=pending.proposal_fingerprint,
                capture_epoch=state.capture_epoch,
                disposition=event.disposition,
            )
        return self._resolve_confirmation_attempt(
            state,
            pending,
            normalized,
            actor.user_id,
            fingerprint,
            expected_capture_epoch=None,
            response_channel='ui_in_process',
            assurance_level='unverified_in_process_ui',
        )

    def expire_due_confirmations(self) -> Tuple[SpeechPipelineResult, ...]:
        """Expire or invalidate pending requests without a user utterance."""
        with self._sessions_lock:
            states = tuple(self._sessions.values())
        results = []
        for state in states:
            with state.lock:
                if state.closed or state.pending_confirmation is None:
                    continue
                pending = state.pending_confirmation
            current_time, context_code = (
                self._sample_confirmation_context(pending)
            )
            if context_code is not None:
                with state.lock:
                    superseded = self._confirmation_superseded_locked(
                        state,
                        pending,
                        expected_capture_epoch=None,
                    )
                    if superseded is not None:
                        continue
                    results.append(
                        self._confirmation_context_failure_locked(
                            state,
                            pending,
                            context_code,
                        )
                    )
                continue
            if current_time is None or current_time < pending.expires_at:
                continue
            with state.lock:
                superseded = self._confirmation_superseded_locked(
                    state,
                    pending,
                    expected_capture_epoch=None,
                )
                if superseded is not None:
                    continue
                response_id, fingerprint, _provenance = (
                    self.orchestrator.conversation_store
                    .confirmation_expiry_envelope(
                        pending.confirmation_request_id,
                        pending.proposal_fingerprint,
                    )
                )
                response = ToolConfirmationResponseEvent(
                    response_id=response_id,
                    speech_session_id=pending.speech_session_id,
                    conversation_id=pending.conversation_id,
                    confirmation_request_id=(
                        pending.confirmation_request_id
                    ),
                    decision_id=pending.decision_id,
                    proposal_fingerprint=pending.proposal_fingerprint,
                    capture_epoch=state.capture_epoch,
                    disposition='cancel',
                )
            results.append(
                self._resolve_confirmation_attempt(
                    state,
                    pending,
                    response,
                    pending.user_id,
                    fingerprint,
                    expected_capture_epoch=None,
                    response_channel='server_expiry',
                    assurance_level='server_clock',
                )
            )
        return tuple(results)

    def handle_barge_in(
        self,
        event: SpeechActivityEvent,
    ) -> SpeechControlResult:
        """Fence playback and return one idempotent TTS cancellation."""
        if not isinstance(event, SpeechActivityEvent):
            raise TypeError('event must be a SpeechActivityEvent')
        state = self._session_state(event.speech_session_id)
        if state is None:
            return SpeechControlResult(
                status='rejected',
                code='unknown_speech_session',
                capture_epoch=0,
            )
        with state.lock:
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
                active_trusted_result_tts = (
                    state.active_trusted_result_tts
                )
                if (
                    active_tts is not None
                    and active_trusted_result_tts is not None
                ):
                    raise RuntimeError('TTS delivery lanes overlap')
                state.capture_epoch += 1
                state.active_tts = None
                cancel_request = None
                code = 'capture_epoch_advanced'
                if active_trusted_result_tts is not None:
                    state.active_trusted_result_tts = None
                    state.terminal_pending_trusted_result_tts = (
                        active_trusted_result_tts
                    )
                    state.terminal_pending_trusted_result_tts_cancel_result = (
                        None
                    )
                    cancel_request = TTSCancelRequest(
                        request_id=self._cancel_request_id(
                            active_trusted_result_tts.request_id,
                            event.event_id,
                            'barge_in',
                        ),
                        speech_session_id=(
                            state.binding.speech_session_id
                        ),
                        tts_request_id=(
                            active_trusted_result_tts.request_id
                        ),
                        reason='barge_in',
                    )
                    code = 'trusted_result_tts_cancel_requested'
                elif active_tts is not None:
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
        state = self._session_state(normalized_session)
        if state is None:
            return SpeechControlResult(
                status='rejected',
                code='unknown_speech_session',
                capture_epoch=0,
            )
        with state.lock:
            context_required = not state.closed
        context_code = (
            self._speech_session_context_code(state)
            if context_required
            else None
        )
        with state.lock:
            # Preserve the established terminal replay behavior after an
            # explicit local close.  A still-open voice binding, however,
            # must revalidate its durable conversation generation before an
            # old playback acknowledgement can advance the capture epoch.
            if not state.closed and context_code is not None:
                return self._tts_context_failure_locked(
                    state,
                    context_code,
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
        state = self._session_state(normalized_session)
        if state is None:
            return SpeechControlResult(
                status='rejected',
                code='unknown_speech_session',
                capture_epoch=0,
            )
        with state.lock:
            if state.closed:
                if state.close_control_id is None:
                    result = SpeechControlResult(
                        status='closed',
                        code='session_already_closed_external',
                        capture_epoch=state.capture_epoch,
                    )
                    state.close_control_id = normalized_control
                    state.close_result = result
                    return result
                if state.close_control_id != normalized_control:
                    return SpeechControlResult(
                        status='rejected',
                        code='close_conflict',
                        capture_epoch=state.capture_epoch,
                    )
                if state.close_result is None:
                    raise RuntimeError('closed speech session has no result')
                return state.close_result
            context_code = self._speech_session_context_code(state)
            conversation_changed = (
                context_code == 'conversation_changed_during_inference'
            )
            conversation_unavailable = context_code is not None
            if context_code is None:
                try:
                    store = self.orchestrator.conversation_store
                    store.close_session_if_current(
                        state.binding.user_id,
                        state.binding.conversation_id,
                        expected_session_instance_id=(
                            state.conversation_session_instance_id
                        ),
                        expected_generation=(
                            state.conversation_generation
                        ),
                    )
                except ConversationChangedError:
                    conversation_changed = True
                    conversation_unavailable = True
                except (
                    ConversationNotFoundError,
                    ConversationStateError,
                ):
                    conversation_unavailable = True
            active_tts = state.active_tts
            active_trusted_result_tts = (
                state.active_trusted_result_tts
            )
            if (
                active_tts is not None
                and active_trusted_result_tts is not None
            ):
                raise RuntimeError('TTS delivery lanes overlap')
            cancel_request = None
            trusted_cancel = False
            if active_trusted_result_tts is not None:
                state.active_trusted_result_tts = None
                state.terminal_pending_trusted_result_tts = (
                    active_trusted_result_tts
                )
                state.terminal_pending_trusted_result_tts_cancel_result = (
                    None
                )
                cancel_request = TTSCancelRequest(
                    request_id=self._cancel_request_id(
                        active_trusted_result_tts.request_id,
                        normalized_control,
                        'session_closed',
                    ),
                    speech_session_id=state.binding.speech_session_id,
                    tts_request_id=active_trusted_result_tts.request_id,
                    reason='session_closed',
                )
                trusted_cancel = True
            elif active_tts is not None:
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
            state.active_trusted_result_tts = None
            state.terminal_pending_trusted_result_tts = None
            state.terminal_pending_trusted_result_tts_cancel_result = None
            state.pending_confirmation = None
            state.capture_epoch += 1
            state.closed = True
            if conversation_changed:
                code = 'session_closed_conversation_changed'
                if cancel_request is not None:
                    code += (
                        '_trusted_result_tts_cancel_requested'
                        if trusted_cancel
                        else '_tts_cancel_requested'
                    )
            elif conversation_unavailable:
                code = 'session_closed_conversation_unavailable'
                if cancel_request is not None:
                    code += (
                        '_trusted_result_tts_cancel_requested'
                        if trusted_cancel
                        else '_tts_cancel_requested'
                    )
            elif trusted_cancel:
                code = (
                    'session_closed_'
                    'trusted_result_tts_cancel_requested'
                )
            elif cancel_request is not None:
                code = 'session_closed_tts_cancel_requested'
            else:
                code = 'session_closed'
            result = SpeechControlResult(
                status='closed',
                code=code,
                capture_epoch=state.capture_epoch,
                cancel_request=cancel_request,
            )
            state.close_control_id = normalized_control
            state.close_result = result
            return result

    @contextmanager
    def _completion_guard(
        self,
        speech_session_id: str,
        reservation: _InFlightTranscript,
        result: OrchestrationResult,
        event: SpeechTranscriptEvent,
    ) -> Iterator[Optional[ConfirmationIntentDraft]]:
        """Resolve trusted targets unlocked, then commit one delivery state."""
        state = self._session_state(speech_session_id)
        if state is None:
            raise OrchestrationCancelledError(
                'speech inference was superseded before commit'
            )
        target_request = None
        with state.lock:
            expected_session_instance_id = (
                state.conversation_session_instance_id
            )
            self._require_current_completion_locked(
                state,
                reservation,
                result,
                expected_session_instance_id,
            )
            if (
                result.decision.type == 'tool_call'
                and result.decision.tool_name == 'monitor_room'
            ):
                target_request = self._target_resolution_request(
                    state,
                    reservation,
                    result,
                    event,
                )

        stored_delivery = None
        target = None
        if target_request is not None:
            stored_delivery = self._stored_monitor_room_confirmation(
                target_request,
                result,
            )
            if stored_delivery is None:
                target = self._resolve_monitor_room_target(target_request)
                self._require_target_matches_state_evidence(
                    result,
                    target,
                )
            elif stored_delivery.terminal_code is not None:
                raise ConfirmationIntentAlreadyTerminalError(
                    'confirmation intent is already terminal'
                )

        with state.lock:
            self._require_current_completion_locked(
                state,
                reservation,
                result,
                expected_session_instance_id,
            )
            confirmation_request = None
            status = 'responded'
            code = 'final_transcript_processed'
            tts_text = result.decision.message
            if (
                stored_delivery is not None
                and stored_delivery.request is not None
            ):
                self._require_target_matches_state_evidence(
                    result,
                    stored_delivery.request.target,
                )
                confirmation_request = stored_delivery.request
                status = 'awaiting_confirmation'
                code = 'tool_confirmation_required'
                tts_text = confirmation_request.message
            elif target is not None:
                self._require_target_matches_state_evidence(
                    result,
                    target,
                )
                try:
                    confirmation_request = build_monitor_room_confirmation(
                        state.binding.user_id,
                        state.binding.speech_session_id,
                        event.utterance_id,
                        result,
                        target,
                    )
                except (TypeError, ValidationError):
                    raise _MonitorRoomTargetFailure(
                        'monitor_room_target_resolution_failed'
                    ) from None
                status = 'awaiting_confirmation'
                code = 'tool_confirmation_required'
                tts_text = confirmation_request.message
            if confirmation_request is not None:
                self._require_target_matches_state_evidence(
                    result,
                    confirmation_request.target,
                )
            tts_request = TTSRequest(
                schema_version=SPEECH_SCHEMA_VERSION,
                request_id=self._tts_request_id(
                    reservation.request_id
                ),
                speech_session_id=state.binding.speech_session_id,
                conversation_id=state.binding.conversation_id,
                turn_id=reservation.turn_id,
                source_utterance_id=reservation.utterance_id,
                text=tts_text,
            )
            pipeline_result = SpeechPipelineResult(
                status=status,
                code=code,
                capture_epoch=state.capture_epoch,
                request_id=reservation.request_id,
                turn_id=reservation.turn_id,
                agent_result=result,
                tts_request=tts_request,
                confirmation_request=confirmation_request,
            )
            if confirmation_request is not None:
                self._require_target_matches_state_evidence(
                    result,
                    confirmation_request.target,
                )
            yield (
                confirmation_request.to_intent_draft()
                if confirmation_request is not None
                else None
            )
            state.last_final_sequence = reservation.sequence
            state.active_tts = tts_request
            state.pending_confirmation = confirmation_request
            state.in_flight = None
            reservation.completed_result = self._remember_transcript(
                state,
                event,
                reservation.fingerprint,
                pipeline_result,
            )

    @staticmethod
    def _require_current_completion_locked(
        state: _SpeechSessionState,
        reservation: _InFlightTranscript,
        result: OrchestrationResult,
        expected_session_instance_id: str,
    ) -> None:
        """Fence a result before and after unlocked target resolution."""
        if (
            state.in_flight is not reservation
            or state.closed
            or state.capture_epoch != reservation.capture_epoch
            or state.conversation_session_instance_id
            != expected_session_instance_id
            or result.conversation_generation
            != state.conversation_generation
            or result.conversation_session_instance_id
            != expected_session_instance_id
            or result.request_id != reservation.request_id
            or result.turn_id != reservation.turn_id
        ):
            raise OrchestrationCancelledError(
                'speech inference was superseded before commit'
            )

    @staticmethod
    def _target_resolution_request(
        state: _SpeechSessionState,
        reservation: _InFlightTranscript,
        result: OrchestrationResult,
        event: SpeechTranscriptEvent,
    ) -> MonitorRoomTargetRequest:
        """Snapshot only trusted identities and one canonical location."""
        location = result.decision.arguments.get('location')
        session_instance_id = result.conversation_session_instance_id
        if not isinstance(location, str) or session_instance_id is None:
            raise _MonitorRoomTargetFailure(
                'monitor_room_target_resolution_failed'
            )
        return MonitorRoomTargetRequest(
            user_id=state.binding.user_id,
            speech_session_id=state.binding.speech_session_id,
            source_utterance_id=event.utterance_id,
            conversation_id=state.binding.conversation_id,
            conversation_session_instance_id=session_instance_id,
            conversation_generation=result.conversation_generation,
            conversation_revision=result.conversation_revision,
            conversation_ordinal=result.conversation_ordinal,
            agent_request_id=reservation.request_id,
            turn_id=reservation.turn_id,
            decision_id=result.decision_id,
            location=location,
            issued_at=result.issued_at,
            expires_at=result.expires_at,
        )

    def _resolve_monitor_room_target(
        self,
        request: MonitorRoomTargetRequest,
    ) -> TargetBinding:
        """Invoke one trusted resolver without exposing its failure details."""
        resolver = self._monitor_room_target_resolver
        if resolver is None:
            raise _MonitorRoomTargetFailure(
                'monitor_room_target_resolver_unavailable'
            )
        try:
            target = resolver.resolve(request)
        except Exception:
            raise _MonitorRoomTargetFailure(
                'monitor_room_target_resolution_failed'
            ) from None
        if not isinstance(target, TargetBinding):
            raise _MonitorRoomTargetFailure(
                'monitor_room_target_resolution_failed'
            )
        return target

    def _stored_monitor_room_confirmation(
        self,
        request: MonitorRoomTargetRequest,
        result: OrchestrationResult,
    ) -> Optional[_StoredConfirmationDelivery]:
        """Replay durable target evidence without a fresh network lookup."""
        confirmation_request_id = f'confirm-{result.decision_id}'
        try:
            record = (
                self.orchestrator.conversation_store
                .refresh_confirmation_intent(
                    request.user_id,
                    confirmation_request_id,
                )
            )
        except ConfirmationIntentNotFoundError:
            return None
        except (ConversationClockError, ConversationStateError):
            raise _MonitorRoomTargetFailure(
                'monitor_room_confirmation_replay_unavailable'
            ) from None
        expected = (
            record.schema_version == 3
            and record.confirmation_request_id
            == confirmation_request_id
            and record.agent_request_id == request.agent_request_id
            and record.user_id == request.user_id
            and record.speech_session_id == request.speech_session_id
            and record.source_utterance_id
            == request.source_utterance_id
            and record.conversation_id == request.conversation_id
            and record.session_instance_id
            == request.conversation_session_instance_id
            and record.generation == request.conversation_generation
            and record.revision == request.conversation_revision
            and record.ordinal == request.conversation_ordinal
            and record.turn_id == request.turn_id
            and record.decision_id == request.decision_id
            and record.tool_name == 'monitor_room'
            and record.issued_at == request.issued_at
            and record.expires_at == request.expires_at
            and record.risk_level == 'L3'
        )
        if not expected:
            raise _MonitorRoomTargetFailure(
                'monitor_room_confirmation_replay_conflict'
            )
        if record.state != 'pending':
            if record.state not in {'resolved', 'invalidated'}:
                raise _MonitorRoomTargetFailure(
                    'monitor_room_confirmation_replay_conflict'
                )
            return _StoredConfirmationDelivery(
                request=None,
                terminal_code=(
                    record.result_code
                    or 'confirmation_already_resolved'
                ),
            )
        try:
            target = record.reconstruct_target_binding()
            self._require_target_matches_state_evidence(
                result,
                target,
            )
            confirmation = build_monitor_room_confirmation(
                request.user_id,
                request.speech_session_id,
                request.source_utterance_id,
                result,
                target,
            )
        except (TypeError, ValidationError):
            raise _MonitorRoomTargetFailure(
                'monitor_room_confirmation_replay_conflict'
            ) from None
        if (
            confirmation.proposal_fingerprint
            != record.proposal_fingerprint
            or confirmation.message != record.confirmation_message
        ):
            raise _MonitorRoomTargetFailure(
                'monitor_room_confirmation_replay_conflict'
            )
        return _StoredConfirmationDelivery(
            request=confirmation,
            terminal_code=None,
        )

    @staticmethod
    def _require_target_matches_state_evidence(
        result: OrchestrationResult,
        target: TargetBinding,
    ) -> None:
        """Bind semantic resolution to the same fresh robot map/device."""
        evidence = result.current_monitor_room_evidence()
        if evidence is None or time.time() >= result.expires_at:
            raise _MonitorRoomTargetFailure(
                'monitor_room_state_evidence_unavailable'
            )
        if (
            target.device_id != evidence.device_id
            or target.map_id != evidence.map_id
            or target.map_revision != evidence.map_revision
        ):
            raise _MonitorRoomTargetFailure(
                'monitor_room_state_target_mismatch'
            )
        if isinstance(evidence, GazeboSimulationEvidenceBinding):
            if (
                not target.effects.gazebo_simulation_navigation
                or target.room_id != evidence.room_id
                or target.geometry_digest != evidence.geometry_digest
                or target.source_arguments_digest
                != evidence.source_arguments_digest
                or target.binding_digest
                != evidence.target_binding_digest
                or target.effects_digest != evidence.effects_digest
            ):
                raise _MonitorRoomTargetFailure(
                    'monitor_room_state_target_mismatch'
                )
        elif target.effects.gazebo_simulation_navigation:
            raise _MonitorRoomTargetFailure(
                'monitor_room_state_target_mismatch'
            )

    def _handle_confirmation_transcript(
        self,
        state: _SpeechSessionState,
        event: SpeechTranscriptEvent,
        transcript_fingerprint: str,
    ) -> SpeechPipelineResult:
        """Route a voice response locally without holding the state lock."""
        disposition = classify_confirmation_response(event.text)
        with state.lock:
            pending = state.pending_confirmation
            if (
                pending is None
                or state.closed
                or event.capture_epoch != state.capture_epoch
            ):
                return self._pipeline_result(
                    'rejected',
                    'confirmation_unavailable',
                    state.capture_epoch,
                )
            response = ToolConfirmationResponseEvent(
                response_id=self._confirmation_response_id(
                    pending.confirmation_request_id,
                    event.utterance_id,
                ),
                speech_session_id=state.binding.speech_session_id,
                conversation_id=state.binding.conversation_id,
                confirmation_request_id=pending.confirmation_request_id,
                decision_id=pending.decision_id,
                proposal_fingerprint=pending.proposal_fingerprint,
                capture_epoch=state.capture_epoch,
                disposition=disposition or 'cancel',
            )
            response_fingerprint = self._fingerprint(
                {
                    'event': response.to_dict(),
                    'trusted_user_id': state.binding.user_id,
                    'voice_utterance_id': event.utterance_id,
                }
            )
            if disposition is not None:
                claim_conflict = self._claim_confirmation_response_locked(
                    state,
                    response.response_id,
                    response_fingerprint,
                )
                if claim_conflict is not None:
                    return claim_conflict
            expected_epoch = state.capture_epoch

        if disposition is None:
            current_time, context_code = (
                self._sample_confirmation_context(pending)
            )
            with state.lock:
                superseded = self._confirmation_superseded_locked(
                    state,
                    pending,
                    expected_epoch,
                )
                if superseded is not None:
                    return superseded
                if context_code is not None:
                    result = self._confirmation_context_failure_locked(
                        state,
                        pending,
                        context_code,
                    )
                    if result.status != 'retryable':
                        state.last_final_sequence = event.sequence
                        return self._remember_transcript(
                            state,
                            event,
                            transcript_fingerprint,
                            result,
                        )
                    return result
                if current_time is None:
                    raise RuntimeError('confirmation time is missing')
                if current_time < pending.expires_at:
                    state.last_final_sequence = event.sequence
                    return self._remember_transcript(
                        state,
                        event,
                        transcript_fingerprint,
                        SpeechPipelineResult(
                            status='clarification',
                            code='confirmation_response_unrecognized',
                            capture_epoch=state.capture_epoch,
                            request_id=pending.agent_request_id,
                            turn_id=pending.turn_id,
                            confirmation_request=pending,
                        ),
                    )
                claim_conflict = self._claim_confirmation_response_locked(
                    state,
                    response.response_id,
                    response_fingerprint,
                )
                if claim_conflict is not None:
                    return claim_conflict
            result = self._resolve_confirmation_attempt(
                state,
                pending,
                response,
                state.binding.user_id,
                response_fingerprint,
                expected_capture_epoch=expected_epoch,
                voice_event=event,
                transcript_fingerprint=transcript_fingerprint,
                response_channel='voice',
                assurance_level='local_speech_binding',
            )
        else:
            result = self._resolve_confirmation_attempt(
                state,
                pending,
                response,
                state.binding.user_id,
                response_fingerprint,
                expected_capture_epoch=expected_epoch,
                voice_event=event,
                transcript_fingerprint=transcript_fingerprint,
                response_channel='voice',
                assurance_level='local_speech_binding',
            )
        return result

    def _resolve_confirmation_attempt(
        self,
        state: _SpeechSessionState,
        pending: ToolConfirmationRequest,
        event: ToolConfirmationResponseEvent,
        trusted_user_id: str,
        response_fingerprint: str,
        *,
        expected_capture_epoch: Optional[int],
        response_channel: str,
        assurance_level: str,
        voice_event: Optional[SpeechTranscriptEvent] = None,
        transcript_fingerprint: Optional[str] = None,
    ) -> SpeechPipelineResult:
        """Linearize speech state and durable intent under one lock order."""
        with state.lock:
            cached = state.confirmation_response_cache.get(
                event.response_id
            )
            if cached is not None:
                cached_fingerprint, cached_result = cached
                if cached_fingerprint != response_fingerprint:
                    return self._pipeline_result(
                        'rejected',
                        'confirmation_response_conflict',
                        state.capture_epoch,
                    )
                state.confirmation_response_cache.move_to_end(
                    event.response_id
                )
                return self._remember_voice_confirmation_result_locked(
                    state,
                    voice_event,
                    transcript_fingerprint,
                    cached_result,
                )
            if trusted_user_id != state.binding.user_id:
                return self._pipeline_result(
                    'rejected',
                    'confirmation_unavailable',
                    state.capture_epoch,
                )
            superseded = self._confirmation_superseded_locked(
                state,
                pending,
                expected_capture_epoch,
            )
            if superseded is not None:
                return superseded
            return self._resolve_confirmation_attempt_locked(
                state,
                pending,
                event,
                trusted_user_id,
                response_fingerprint,
                expected_capture_epoch=expected_capture_epoch,
                response_channel=response_channel,
                assurance_level=assurance_level,
                voice_event=voice_event,
                transcript_fingerprint=transcript_fingerprint,
            )

    def _resolve_confirmation_attempt_locked(
        self,
        state: _SpeechSessionState,
        pending: ToolConfirmationRequest,
        event: ToolConfirmationResponseEvent,
        trusted_user_id: str,
        response_fingerprint: str,
        *,
        expected_capture_epoch: Optional[int],
        response_channel: str,
        assurance_level: str,
        voice_event: Optional[SpeechTranscriptEvent] = None,
        transcript_fingerprint: Optional[str] = None,
    ) -> SpeechPipelineResult:
        """Persist and mirror one intent while ``state.lock`` is held."""
        provenance_ref = self._confirmation_provenance_ref(
            response_channel,
            response_fingerprint,
        )
        try:
            durable = (
                self.orchestrator.conversation_store
                .resolve_confirmation_intent(
                    user_id=trusted_user_id,
                    confirmation_request_id=(
                        pending.confirmation_request_id
                    ),
                    proposal_fingerprint=pending.proposal_fingerprint,
                    response_id=event.response_id,
                    response_fingerprint=response_fingerprint,
                    requested_disposition=event.disposition,
                    response_channel=response_channel,
                    assurance_level=assurance_level,
                    provenance_ref=provenance_ref,
                )
            )
        except ConfirmationIntentAlreadyTerminalError:
            try:
                durable = (
                    self.orchestrator.conversation_store
                    .get_confirmation_intent(
                        trusted_user_id,
                        pending.confirmation_request_id,
                    )
                )
            except Exception:
                with state.lock:
                    return SpeechPipelineResult(
                        status='retryable',
                        code='confirmation_persistence_unavailable',
                        capture_epoch=state.capture_epoch,
                        request_id=pending.agent_request_id,
                        turn_id=pending.turn_id,
                        confirmation_request=pending,
                    )
        except ConfirmationIntentConflictError:
            with state.lock:
                return self._pipeline_result(
                    'rejected',
                    'confirmation_response_conflict',
                    state.capture_epoch,
                )
        except ConfirmationReservedResponseIdError:
            with state.lock:
                return self._pipeline_result(
                    'rejected',
                    'confirmation_response_id_reserved',
                    state.capture_epoch,
                )
        except ConfirmationIntentNotFoundError:
            with state.lock:
                result = self._confirmation_context_failure_locked(
                    state,
                    pending,
                    'confirmation_conversation_not_found',
                )
                return self._remember_voice_confirmation_result_locked(
                    state,
                    voice_event,
                    transcript_fingerprint,
                    result,
                )
        except ConversationClockError:
            with state.lock:
                return SpeechPipelineResult(
                    status='retryable',
                    code=(
                        'confirmation_persistence_unavailable'
                        if state.closed
                        else 'confirmation_time_unavailable'
                    ),
                    capture_epoch=state.capture_epoch,
                    request_id=pending.agent_request_id,
                    turn_id=pending.turn_id,
                    confirmation_request=pending,
                )
        except Exception:
            with state.lock:
                return SpeechPipelineResult(
                    status='retryable',
                    code='confirmation_persistence_unavailable',
                    capture_epoch=state.capture_epoch,
                    request_id=pending.agent_request_id,
                    turn_id=pending.turn_id,
                    confirmation_request=pending,
                )
        with state.lock:
            cached = state.confirmation_response_cache.get(
                event.response_id
            )
            if cached is not None:
                cached_fingerprint, cached_result = cached
                if cached_fingerprint != response_fingerprint:
                    return self._pipeline_result(
                        'rejected',
                        'confirmation_response_conflict',
                        state.capture_epoch,
                    )
                state.confirmation_response_cache.move_to_end(
                    event.response_id
                )
                return self._remember_voice_confirmation_result_locked(
                    state,
                    voice_event,
                    transcript_fingerprint,
                    cached_result,
                )
            if trusted_user_id != state.binding.user_id:
                return self._pipeline_result(
                    'rejected',
                    'confirmation_unavailable',
                    state.capture_epoch,
                )
            superseded = self._confirmation_superseded_locked(
                state,
                pending,
                expected_capture_epoch,
            )
            if superseded is not None:
                return superseded
            if durable.state == 'invalidated':
                result = self._confirmation_context_failure_locked(
                    state,
                    pending,
                    durable.result_code
                    or 'confirmation_conversation_changed',
                )
                self._remember(
                    state.confirmation_response_cache,
                    event.response_id,
                    response_fingerprint,
                    result,
                )
                return self._remember_voice_confirmation_result_locked(
                    state,
                    voice_event,
                    transcript_fingerprint,
                    result,
                )
            if (
                durable.state != 'resolved'
                or durable.disposition is None
                or durable.result_code is None
                or durable.confirmation_result_id is None
                or durable.response_id is None
                or durable.resolved_at is None
            ):
                raise RuntimeError(
                    'durable confirmation result is incomplete'
                )
            resolution = ToolConfirmationResolution(
                confirmation_result_id=(
                    durable.confirmation_result_id
                ),
                response_id=durable.response_id,
                confirmation_request_id=(
                    pending.confirmation_request_id
                ),
                decision_id=pending.decision_id,
                proposal_fingerprint=pending.proposal_fingerprint,
                disposition=durable.disposition,
                code=durable.result_code,
                resolved_at=durable.resolved_at,
            )
            cancel_request = self._finish_confirmation_tts_locked(
                state,
                durable.response_id,
                'confirmation_resolved',
            )
            state.pending_confirmation = None
            result = SpeechPipelineResult(
                status=(
                    'expired'
                    if resolution.disposition == 'expired'
                    else 'recorded'
                ),
                code=resolution.code,
                capture_epoch=state.capture_epoch,
                request_id=pending.agent_request_id,
                turn_id=pending.turn_id,
                tts_cancel_request=cancel_request,
                confirmation_request=pending,
                confirmation_resolution=resolution,
            )
            self._remember_terminal_confirmation(
                state,
                pending.confirmation_request_id,
                result,
            )
            self._remember(
                state.confirmation_response_cache,
                event.response_id,
                response_fingerprint,
                result,
            )
            return self._remember_voice_confirmation_result_locked(
                state,
                voice_event,
                transcript_fingerprint,
                result,
            )

    def _remember_voice_confirmation_result_locked(
        self,
        state: _SpeechSessionState,
        event: Optional[SpeechTranscriptEvent],
        fingerprint: Optional[str],
        result: SpeechPipelineResult,
    ) -> SpeechPipelineResult:
        """Commit voice ordering and its result in the same state lock."""
        if event is None:
            if fingerprint is not None:
                raise RuntimeError('voice fingerprint has no event')
            return result
        if fingerprint is None:
            raise RuntimeError('voice event has no fingerprint')
        if result.status == 'retryable':
            return result
        cached = state.transcript_cache.get(event.utterance_id)
        if cached is not None:
            cached_fingerprint, cached_result = cached
            if cached_fingerprint != fingerprint:
                return self._pipeline_result(
                    'rejected',
                    'utterance_conflict',
                    state.capture_epoch,
                )
            return cached_result
        state.last_final_sequence = event.sequence
        return self._remember_transcript(
            state,
            event,
            fingerprint,
            result,
        )

    def _sample_confirmation_context(
        self,
        pending: ToolConfirmationRequest,
    ) -> Tuple[Optional[float], Optional[str]]:
        """Read server time and conversation state without a speech lock."""
        context_code = self._conversation_context_code(pending)
        if context_code is not None:
            return None, context_code
        try:
            current_time = self._now()
        except ValidationError:
            return None, 'confirmation_time_unavailable'
        return current_time, None

    def _conversation_context_code(
        self,
        pending: ToolConfirmationRequest,
    ) -> Optional[str]:
        """Return a content-free reason when the proposal context changed."""
        try:
            session = self.orchestrator.conversation_store.get(
                pending.user_id,
                pending.conversation_id,
            )
        except ConversationNotFoundError:
            return 'confirmation_conversation_not_found'
        except ConversationStateError:
            return 'confirmation_conversation_inactive'
        except Exception:
            return 'confirmation_context_unavailable'
        if session.status != 'active':
            return 'confirmation_conversation_inactive'
        if (
            session.session_instance_id
            != pending.conversation_session_instance_id
            or session.generation != pending.conversation_generation
            or session.revision != pending.conversation_revision
        ):
            return 'confirmation_conversation_changed'
        return None

    def _speech_session_context_code(
        self,
        state: _SpeechSessionState,
    ) -> Optional[str]:
        """Validate the private lifecycle before any transcript replay."""
        try:
            session = self.orchestrator.conversation_store.get(
                state.binding.user_id,
                state.binding.conversation_id,
            )
        except ConversationNotFoundError:
            return 'conversation_not_found'
        except ConversationStateError:
            return 'conversation_inactive'
        except Exception:
            return 'conversation_context_unavailable'
        if session.status != 'active':
            return 'conversation_inactive'
        if (
            session.session_instance_id
            != state.conversation_session_instance_id
            or session.generation != state.conversation_generation
        ):
            return 'conversation_changed_during_inference'
        return None

    def _trusted_result_tts_context_code(
        self,
        reservation: _TrustedResultTTSClaimReservation,
    ) -> Optional[str]:
        """Read and compare a reserved durable lifecycle without its lock."""
        return self._trusted_result_tts_binding_context_code(
            reservation.user_id,
            reservation.conversation_id,
            reservation.conversation_session_instance_id,
            reservation.conversation_generation,
        )

    def _trusted_result_tts_request_context_code(
        self,
        request: TrustedResultTTSRequest,
    ) -> Optional[str]:
        """Read and compare one claimed notification's durable lifecycle."""
        return self._trusted_result_tts_binding_context_code(
            request.user_id,
            request.conversation_id,
            request.conversation_session_instance_id,
            request.conversation_generation,
        )

    def _trusted_result_tts_binding_context_code(
        self,
        user_id: str,
        conversation_id: str,
        session_instance_id: str,
        generation: int,
    ) -> Optional[str]:
        """Return a content-free exact conversation binding failure."""
        try:
            session = self.orchestrator.conversation_store.get(
                user_id,
                conversation_id,
            )
        except ConversationNotFoundError:
            return 'conversation_not_found'
        except ConversationStateError:
            return 'conversation_inactive'
        except Exception:
            return 'conversation_context_unavailable'
        if session.status != 'active':
            return 'conversation_inactive'
        if (
            session.session_instance_id != session_instance_id
            or session.generation != generation
        ):
            return 'conversation_changed_during_trusted_result_tts'
        return None

    @staticmethod
    def _trusted_result_tts_request(
        reservation: _TrustedResultTTSClaimReservation,
        claim: TrustedResultTTSClaim,
    ) -> TrustedResultTTSRequest:
        """Bind one storage claim to the exact reserved speech lifecycle."""
        if not isinstance(claim, TrustedResultTTSClaim):
            raise TypeError('trusted result TTS claim is invalid')
        if (
            claim.claim_request_id != reservation.claim_request_id
            or claim.speech_session_id != reservation.speech_session_id
            or claim.lease_expires_at
            != claim.claimed_at + reservation.lease_seconds
        ):
            raise TrustedResultTTSError(
                'trusted result TTS claim binding changed'
            )
        terminal_request_id = (
            SpeechConversationCoordinator
            ._trusted_result_tts_terminal_request_id(
                claim.event_id,
                claim.claim_request_id,
                claim.claim_fence,
            )
        )
        return TrustedResultTTSRequest(
            schema_version=SPEECH_SCHEMA_VERSION,
            request_id=claim.event_id,
            speech_session_id=reservation.speech_session_id,
            conversation_id=reservation.conversation_id,
            terminal_request_id=terminal_request_id,
            result_code=claim.result_code,
            template_key=claim.template_key,
            text=claim.message,
            claim_fence=claim.claim_fence,
            attempt_number=claim.attempt_number,
            claimed_at=claim.claimed_at,
            lease_expires_at=claim.lease_expires_at,
            user_id=reservation.user_id,
            conversation_session_instance_id=(
                reservation.conversation_session_instance_id
            ),
            conversation_generation=(
                reservation.conversation_generation
            ),
            claim_request_id=claim.claim_request_id,
            claim_token=claim.claim_token,
            lease_seconds=reservation.lease_seconds,
        )

    @staticmethod
    def _trusted_result_tts_claim_cas_matches(
        state: _SpeechSessionState,
        reservation: _TrustedResultTTSClaimReservation,
    ) -> bool:
        """Check all local owners before publishing an unlocked claim."""
        if (
            state.trusted_result_tts_claim_reservation is not reservation
            or state.closed
            or state.capture_epoch != reservation.capture_epoch
            or state.binding.user_id != reservation.user_id
            or state.binding.conversation_id != reservation.conversation_id
            or state.binding.speech_session_id
            != reservation.speech_session_id
            or state.conversation_session_instance_id
            != reservation.conversation_session_instance_id
            or state.conversation_generation
            != reservation.conversation_generation
            or state.active_tts is not None
            or state.pending_confirmation is not None
            or state.in_flight is not None
            or state.terminal_pending_trusted_result_tts is not None
            or state.trusted_result_tts_terminal_reservation is not None
        ):
            return False
        if reservation.expected_active_request is None:
            return state.active_trusted_result_tts is None
        return (
            state.active_trusted_result_tts
            is reservation.expected_active_request
        )

    def _finish_empty_trusted_result_tts_claim(
        self,
        state: _SpeechSessionState,
        reservation: _TrustedResultTTSClaimReservation,
    ) -> TrustedResultTTSControlResult:
        """Cancel an uncertain active delivery before reporting no work."""
        with state.lock:
            if state.trusted_result_tts_claim_reservation is not reservation:
                return TrustedResultTTSControlResult(
                    status='retryable',
                    code='trusted_result_tts_claim_superseded_retry_exact',
                    capture_epoch=state.capture_epoch,
                )
            state.trusted_result_tts_claim_reservation = None
            if state.closed:
                return TrustedResultTTSControlResult(
                    status='rejected',
                    code='speech_session_closed',
                    capture_epoch=state.capture_epoch,
                )
            active = reservation.expected_active_request
            if (
                active is not None
                and state.active_trusted_result_tts is active
            ):
                cancel_request = self._cancel_trusted_result_tts_locked(
                    state,
                    active,
                    reservation.claim_request_id,
                    retain_terminal=True,
                    reason='lease_expired',
                )
                result = TrustedResultTTSControlResult(
                    status='cancel_pending',
                    code=(
                        'trusted_result_tts_'
                        'claim_expired_cancel_requested'
                    ),
                    capture_epoch=state.capture_epoch,
                    cancel_request=cancel_request,
                )
                state.terminal_pending_trusted_result_tts_cancel_result = (
                    result
                )
                return result
            if active is not None:
                return TrustedResultTTSControlResult(
                    status='retryable',
                    code='trusted_result_tts_claim_superseded_retry_exact',
                    capture_epoch=state.capture_epoch,
                )
            return TrustedResultTTSControlResult(
                status='empty',
                code='trusted_result_tts_unavailable',
                capture_epoch=state.capture_epoch,
            )

    def _finish_trusted_result_tts_claim_failure(
        self,
        state: _SpeechSessionState,
        reservation: _TrustedResultTTSClaimReservation,
        code: str,
        *,
        transient: bool,
    ) -> TrustedResultTTSControlResult:
        """Release one claim reservation and fence stale local playback."""
        with state.lock:
            if state.trusted_result_tts_claim_reservation is not reservation:
                return TrustedResultTTSControlResult(
                    status='retryable',
                    code='trusted_result_tts_claim_superseded_retry_exact',
                    capture_epoch=state.capture_epoch,
                )
            state.trusted_result_tts_claim_reservation = None
            if state.closed:
                return TrustedResultTTSControlResult(
                    status='rejected',
                    code='speech_session_closed',
                    capture_epoch=state.capture_epoch,
                )
            if transient:
                return TrustedResultTTSControlResult(
                    status='retryable',
                    code=code,
                    capture_epoch=state.capture_epoch,
                )
            if code in {
                'conversation_not_found',
                'conversation_inactive',
                'conversation_changed_during_trusted_result_tts',
            }:
                return self._trusted_result_tts_context_failure_locked(
                    state,
                    code,
                    reservation.claim_request_id,
                )
            cancel_request = None
            active = reservation.expected_active_request
            if (
                active is not None
                and state.active_trusted_result_tts is active
            ):
                cancel_request = self._cancel_trusted_result_tts_locked(
                    state,
                    active,
                    reservation.claim_request_id,
                    retain_terminal=True,
                    reason='trusted_result_invalidated',
                )
                result = TrustedResultTTSControlResult(
                    status='cancel_pending',
                    code=(
                        'trusted_result_tts_'
                        'claim_invalidated_cancel_requested'
                    ),
                    capture_epoch=state.capture_epoch,
                    cancel_request=cancel_request,
                )
                state.terminal_pending_trusted_result_tts_cancel_result = (
                    result
                )
                return result
            return TrustedResultTTSControlResult(
                status='rejected',
                code=code,
                capture_epoch=state.capture_epoch,
                cancel_request=cancel_request,
            )

    def _finish_trusted_result_tts_terminal_failure(
        self,
        state: _SpeechSessionState,
        reservation: _TrustedResultTTSTerminalReservation,
        code: str,
        *,
        transient: bool,
        discard_request: bool = False,
    ) -> TrustedResultTTSControlResult:
        """Release one ACK reservation while preserving safe retry state."""
        with state.lock:
            if (
                state.trusted_result_tts_terminal_reservation
                is not reservation
            ):
                return TrustedResultTTSControlResult(
                    status='retryable',
                    code='trusted_result_tts_terminal_superseded',
                    capture_epoch=state.capture_epoch,
                )
            state.trusted_result_tts_terminal_reservation = None
            if state.closed:
                return TrustedResultTTSControlResult(
                    status='rejected',
                    code='speech_session_closed',
                    capture_epoch=state.capture_epoch,
                )
            if transient:
                return TrustedResultTTSControlResult(
                    status='retryable',
                    code=code,
                    capture_epoch=state.capture_epoch,
                )
            if code in {
                'conversation_not_found',
                'conversation_inactive',
                'conversation_changed_during_trusted_result_tts',
            }:
                return self._trusted_result_tts_context_failure_locked(
                    state,
                    code,
                    reservation.request.terminal_request_id,
                )
            cancel_request = None
            request = reservation.request
            if discard_request:
                if state.active_trusted_result_tts is request:
                    cancel_request = self._cancel_trusted_result_tts_locked(
                        state,
                        request,
                        request.terminal_request_id,
                        retain_terminal=True,
                        reason='trusted_result_invalidated',
                    )
                elif state.terminal_pending_trusted_result_tts is request:
                    state.terminal_pending_trusted_result_tts = None
                    state.terminal_pending_trusted_result_tts_cancel_result = (
                        None
                    )
                    terminal_key = (
                        request.request_id,
                        request.terminal_request_id,
                    )
                    self._remember_terminal_cancel_trusted_result_tts(
                        state,
                        terminal_key,
                        request,
                    )
                    return TrustedResultTTSControlResult(
                        status='ready',
                        code='trusted_result_tts_cancel_terminal',
                        capture_epoch=state.capture_epoch,
                    )
            return TrustedResultTTSControlResult(
                status='rejected',
                code=code,
                capture_epoch=state.capture_epoch,
                cancel_request=cancel_request,
            )

    def _trusted_result_tts_context_failure_locked(
        self,
        state: _SpeechSessionState,
        code: str,
        control_id: str,
    ) -> TrustedResultTTSControlResult:
        """Fence notification delivery after durable lifecycle change."""
        cancel_request = None
        active = state.active_trusted_result_tts
        if active is not None:
            cancel_request = self._cancel_trusted_result_tts_locked(
                state,
                active,
                control_id,
                retain_terminal=False,
            )
        state.terminal_pending_trusted_result_tts = None
        state.terminal_pending_trusted_result_tts_cancel_result = None
        state.active_tts = None
        state.pending_confirmation = None
        state.in_flight = None
        if cancel_request is None:
            state.capture_epoch += 1
        state.closed = True
        return TrustedResultTTSControlResult(
            status='rejected',
            code=code,
            capture_epoch=state.capture_epoch,
            cancel_request=cancel_request,
        )

    def _cancel_trusted_result_tts_locked(
        self,
        state: _SpeechSessionState,
        request: TrustedResultTTSRequest,
        control_id: str,
        *,
        retain_terminal: bool,
        reason: str = 'session_closed',
    ) -> TTSCancelRequest:
        """Move or discard one notification after issuing one cancel."""
        cancel_request = TTSCancelRequest(
            request_id=self._cancel_request_id(
                request.request_id,
                control_id,
                reason,
            ),
            speech_session_id=state.binding.speech_session_id,
            tts_request_id=request.request_id,
            reason=reason,
        )
        if state.active_trusted_result_tts is request:
            state.active_trusted_result_tts = None
        state.terminal_pending_trusted_result_tts = (
            request if retain_terminal else None
        )
        state.terminal_pending_trusted_result_tts_cancel_result = None
        state.capture_epoch += 1
        return cancel_request

    def _tts_context_failure_locked(
        self,
        state: _SpeechSessionState,
        code: str,
    ) -> SpeechControlResult:
        """Reject a stale terminal ACK and fence its active playback once."""
        if code == 'conversation_context_unavailable':
            return SpeechControlResult(
                status='retryable',
                code=code,
                capture_epoch=state.capture_epoch,
            )
        active_tts = state.active_tts
        active_trusted_result_tts = state.active_trusted_result_tts
        if (
            active_tts is not None
            and active_trusted_result_tts is not None
        ):
            raise RuntimeError('TTS delivery lanes overlap')
        cancel_request = None
        capture_advanced = False
        if active_trusted_result_tts is not None:
            cancel_request = self._cancel_trusted_result_tts_locked(
                state,
                active_trusted_result_tts,
                active_trusted_result_tts.terminal_request_id,
                retain_terminal=False,
            )
            capture_advanced = True
        elif active_tts is not None:
            cancel_request = TTSCancelRequest(
                request_id=self._cancel_request_id(
                    active_tts.request_id,
                    active_tts.request_id,
                    'session_closed',
                ),
                speech_session_id=state.binding.speech_session_id,
                tts_request_id=active_tts.request_id,
                reason='session_closed',
            )
            self._remember_terminal_tts(state, active_tts.request_id)
        state.active_tts = None
        state.active_trusted_result_tts = None
        state.terminal_pending_trusted_result_tts = None
        state.terminal_pending_trusted_result_tts_cancel_result = None
        state.pending_confirmation = None
        state.in_flight = None
        if not capture_advanced:
            state.capture_epoch += 1
        state.closed = True
        return SpeechControlResult(
            status='rejected',
            code=code,
            capture_epoch=state.capture_epoch,
            cancel_request=cancel_request,
        )

    def _speech_context_failure_locked(
        self,
        state: _SpeechSessionState,
        event: SpeechTranscriptEvent,
        fingerprint: str,
        code: str,
    ) -> SpeechPipelineResult:
        """Fail closed before cached TTS can cross a lifecycle boundary."""
        if code == 'conversation_context_unavailable':
            return SpeechPipelineResult(
                status='retryable',
                code=code,
                capture_epoch=state.capture_epoch,
            )
        active_tts = state.active_tts
        active_trusted_result_tts = state.active_trusted_result_tts
        if (
            active_tts is not None
            and active_trusted_result_tts is not None
        ):
            raise RuntimeError('TTS delivery lanes overlap')
        cancel_request = None
        capture_advanced = False
        if active_trusted_result_tts is not None:
            cancel_request = self._cancel_trusted_result_tts_locked(
                state,
                active_trusted_result_tts,
                event.utterance_id,
                retain_terminal=False,
            )
            capture_advanced = True
        elif active_tts is not None:
            cancel_request = TTSCancelRequest(
                request_id=self._cancel_request_id(
                    active_tts.request_id,
                    event.utterance_id,
                    'session_closed',
                ),
                speech_session_id=state.binding.speech_session_id,
                tts_request_id=active_tts.request_id,
                reason='session_closed',
            )
            self._remember_terminal_tts(state, active_tts.request_id)
        state.active_tts = None
        state.active_trusted_result_tts = None
        state.terminal_pending_trusted_result_tts = None
        state.terminal_pending_trusted_result_tts_cancel_result = None
        state.pending_confirmation = None
        state.in_flight = None
        if not capture_advanced:
            state.capture_epoch += 1
        state.closed = True
        result = SpeechPipelineResult(
            status='rejected',
            code=code,
            capture_epoch=state.capture_epoch,
            tts_cancel_request=cancel_request,
        )
        if event.is_final:
            return self._remember_transcript(
                state,
                event,
                fingerprint,
                result,
            )
        return result

    def _confirmation_superseded_locked(
        self,
        state: _SpeechSessionState,
        pending: ToolConfirmationRequest,
        expected_capture_epoch: Optional[int],
    ) -> Optional[SpeechPipelineResult]:
        """Check state identity after every unlocked collaborator call."""
        if state.closed:
            return self._pipeline_result(
                'rejected',
                'speech_session_closed',
                state.capture_epoch,
            )
        if (
            state.pending_confirmation is not pending
            or pending.confirmation_request_id
            in state.terminal_confirmations
        ):
            return self._pipeline_result(
                'rejected',
                'confirmation_already_resolved',
                state.capture_epoch,
            )
        if (
            expected_capture_epoch is not None
            and state.capture_epoch != expected_capture_epoch
        ):
            return self._pipeline_result(
                'rejected',
                'confirmation_unavailable',
                state.capture_epoch,
            )
        return None

    def _confirmation_context_failure_locked(
        self,
        state: _SpeechSessionState,
        pending: ToolConfirmationRequest,
        code: str,
    ) -> SpeechPipelineResult:
        """Fail closed on stale context and retain transient retries."""
        if code in {
            'confirmation_time_unavailable',
            'confirmation_context_unavailable',
        }:
            return SpeechPipelineResult(
                status='retryable',
                code=code,
                capture_epoch=state.capture_epoch,
                request_id=pending.agent_request_id,
                turn_id=pending.turn_id,
                confirmation_request=pending,
            )
        cancel_request = self._finish_confirmation_tts_locked(
            state,
            pending.confirmation_request_id,
            'confirmation_invalidated',
        )
        state.pending_confirmation = None
        result = SpeechPipelineResult(
            status='rejected',
            code=code,
            capture_epoch=state.capture_epoch,
            request_id=pending.agent_request_id,
            turn_id=pending.turn_id,
            tts_cancel_request=cancel_request,
            confirmation_request=pending,
        )
        self._remember_terminal_confirmation(
            state,
            pending.confirmation_request_id,
            result,
        )
        return result

    def _finish_confirmation_tts_locked(
        self,
        state: _SpeechSessionState,
        control_id: str,
        reason: str,
    ) -> Optional[TTSCancelRequest]:
        """Cancel an active confirmation prompt and advance its epoch."""
        active_tts = state.active_tts
        if active_tts is None:
            return None
        cancel_request = TTSCancelRequest(
            request_id=self._cancel_request_id(
                active_tts.request_id,
                control_id,
                reason,
            ),
            speech_session_id=state.binding.speech_session_id,
            tts_request_id=active_tts.request_id,
            reason=reason,
        )
        self._remember_terminal_tts(state, active_tts.request_id)
        state.active_tts = None
        state.capture_epoch += 1
        return cancel_request

    def _claim_confirmation_response_locked(
        self,
        state: _SpeechSessionState,
        response_id: str,
        fingerprint: str,
    ) -> Optional[SpeechPipelineResult]:
        """Reserve an id even when the same response must retry later."""
        existing = state.confirmation_response_claims.get(response_id)
        if existing is not None and existing != fingerprint:
            return self._pipeline_result(
                'rejected',
                'confirmation_response_conflict',
                state.capture_epoch,
            )
        if existing is None and (
            len(state.confirmation_response_claims)
            >= MAX_ACTIVE_CONFIRMATION_RESPONSE_CLAIMS
        ):
            return self._pipeline_result(
                'rejected',
                'confirmation_response_capacity_reached',
                state.capture_epoch,
            )
        state.confirmation_response_claims[response_id] = fingerprint
        return None

    def _conversation_failure_result(
        self,
        event: SpeechTranscriptEvent,
        reservation: _InFlightTranscript,
        error: Exception,
    ) -> SpeechPipelineResult:
        """Convert conversation lifecycle races into a typed rejection."""
        state = self._session_state(event.speech_session_id)
        if state is None:
            discarded = self._discard_if_superseded(
                None,
                event,
                reservation,
            )
            if discarded is None:
                raise RuntimeError('missing speech session was not discarded')
            return discarded
        with state.lock:
            discarded = self._discard_if_superseded(
                state,
                event,
                reservation,
            )
            if discarded is not None:
                return discarded
            if isinstance(error, ConversationNotFoundError):
                code = 'conversation_not_found'
                terminal = True
            elif isinstance(error, ConversationStateError):
                code = 'conversation_inactive'
                terminal = True
            elif isinstance(error, ConversationChangedError):
                code = 'conversation_changed_during_inference'
                terminal = True
            else:
                code = 'conversation_conflict'
                terminal = False
            state.in_flight = None
            result = SpeechPipelineResult(
                status='rejected' if terminal else 'retryable',
                code=code,
                capture_epoch=(
                    state.capture_epoch + 1
                    if terminal
                    else state.capture_epoch
                ),
                request_id=reservation.request_id,
                turn_id=reservation.turn_id,
            )
            if not terminal:
                return result
            state.active_tts = None
            state.capture_epoch += 1
            state.closed = True
            return self._remember_transcript(
                state,
                event,
                reservation.fingerprint,
                result,
            )

    def _confirmation_registration_terminal_result(
        self,
        event: SpeechTranscriptEvent,
        reservation: _InFlightTranscript,
    ) -> SpeechPipelineResult:
        """Do not republish a prompt whose durable row is terminal."""
        state = self._session_state(event.speech_session_id)
        if state is None:
            return SpeechPipelineResult(
                status='discarded',
                code='speech_session_removed_during_inference',
                capture_epoch=reservation.capture_epoch,
                request_id=reservation.request_id,
                turn_id=reservation.turn_id,
            )
        with state.lock:
            discarded = self._discard_if_superseded(
                state,
                event,
                reservation,
            )
            if discarded is not None:
                return discarded
            state.in_flight = None
            state.last_final_sequence = event.sequence
            result = SpeechPipelineResult(
                status='rejected',
                code='confirmation_already_resolved',
                capture_epoch=state.capture_epoch,
                request_id=reservation.request_id,
                turn_id=reservation.turn_id,
            )
            return self._remember_transcript(
                state,
                event,
                reservation.fingerprint,
                result,
            )

    def _monitor_room_target_failure_result(
        self,
        event: SpeechTranscriptEvent,
        reservation: _InFlightTranscript,
        code: str,
    ) -> SpeechPipelineResult:
        """Release one failed target lookup without producing TTS or intent."""
        state = self._session_state(event.speech_session_id)
        if state is None:
            return SpeechPipelineResult(
                status='discarded',
                code='speech_session_removed_during_inference',
                capture_epoch=reservation.capture_epoch,
                request_id=reservation.request_id,
                turn_id=reservation.turn_id,
            )
        with state.lock:
            discarded = self._discard_if_superseded(
                state,
                event,
                reservation,
            )
            if discarded is not None:
                return discarded
            state.in_flight = None
            state.last_final_sequence = event.sequence
            result = SpeechPipelineResult(
                status='rejected',
                code=code,
                capture_epoch=state.capture_epoch,
                request_id=reservation.request_id,
                turn_id=reservation.turn_id,
            )
            return self._remember_transcript(
                state,
                event,
                reservation.fingerprint,
                result,
            )

    def _discard_changed_inference(
        self,
        event: SpeechTranscriptEvent,
        reservation: _InFlightTranscript,
    ) -> Optional[SpeechPipelineResult]:
        """Release a failed reservation or discard it after control change."""
        state = self._session_state(event.speech_session_id)
        if state is None:
            return self._discard_if_superseded(
                None,
                event,
                reservation,
            )
        with state.lock:
            discarded = self._discard_if_superseded(
                state,
                event,
                reservation,
            )
            if discarded is not None:
                return discarded
            state.in_flight = None
            return None

    def _session_state(
        self,
        speech_session_id: str,
    ) -> Optional[_SpeechSessionState]:
        """Return one stable state object under the registry lock."""
        with self._sessions_lock:
            return self._sessions.get(speech_session_id)

    def is_speech_session_bound_to_user(
        self,
        speech_session_id: str,
        user_id: str,
    ) -> bool:
        """Return whether one stable local binding belongs to this user."""
        normalized_session = _identifier(
            speech_session_id,
            'speech_session_id',
        )
        normalized_user = validate_user_id(user_id)
        state = self._session_state(normalized_session)
        if state is None:
            return False
        with state.lock:
            return state.binding.user_id == normalized_user

    def _discard_if_superseded(
        self,
        state: Optional[_SpeechSessionState],
        event: SpeechTranscriptEvent,
        reservation: _InFlightTranscript,
    ) -> Optional[SpeechPipelineResult]:
        """Discard a late inference result after close or epoch advance."""
        if state is None:
            return SpeechPipelineResult(
                status='discarded',
                code='speech_session_removed_during_inference',
                capture_epoch=reservation.capture_epoch,
                request_id=reservation.request_id,
                turn_id=reservation.turn_id,
            )
        if state.in_flight is not reservation:
            return SpeechPipelineResult(
                status='discarded',
                code='inference_reservation_lost',
                capture_epoch=state.capture_epoch,
                request_id=reservation.request_id,
                turn_id=reservation.turn_id,
            )
        if state.closed:
            code = 'speech_session_closed_during_inference'
        elif state.capture_epoch != reservation.capture_epoch:
            code = 'capture_epoch_changed_during_inference'
        else:
            return None
        state.in_flight = None
        result = SpeechPipelineResult(
            status='discarded',
            code=code,
            capture_epoch=state.capture_epoch,
            request_id=reservation.request_id,
            turn_id=reservation.turn_id,
        )
        return self._remember_transcript(
            state,
            event,
            reservation.fingerprint,
            result,
        )

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

    def _remember_terminal_trusted_result_tts(
        self,
        state: _SpeechSessionState,
        terminal_key: Tuple[str, str],
        request: TrustedResultTTSRequest,
    ) -> None:
        """Bound exact notification terminal replay independently."""
        state.terminal_trusted_result_tts_ids[terminal_key] = request
        state.terminal_trusted_result_tts_ids.move_to_end(terminal_key)
        while (
            len(state.terminal_trusted_result_tts_ids)
            > self.event_cache_size
        ):
            state.terminal_trusted_result_tts_ids.popitem(last=False)

    def _remember_terminal_cancel_trusted_result_tts(
        self,
        state: _SpeechSessionState,
        terminal_key: Tuple[str, str],
        request: TrustedResultTTSRequest,
    ) -> None:
        """Cache a local cancel terminal without implying durable ACK."""
        state.terminal_cancel_trusted_result_tts_ids[
            terminal_key
        ] = request
        state.terminal_cancel_trusted_result_tts_ids.move_to_end(
            terminal_key
        )
        while (
            len(state.terminal_cancel_trusted_result_tts_ids)
            > self.event_cache_size
        ):
            state.terminal_cancel_trusted_result_tts_ids.popitem(
                last=False
            )

    def _remember_terminal_confirmation(
        self,
        state: _SpeechSessionState,
        confirmation_request_id: str,
        result: SpeechPipelineResult,
    ) -> None:
        state.terminal_confirmations[confirmation_request_id] = result
        state.terminal_confirmations.move_to_end(confirmation_request_id)
        state.confirmation_response_claims.clear()
        while len(state.terminal_confirmations) > self.event_cache_size:
            state.terminal_confirmations.popitem(last=False)

    def _now(self) -> float:
        invalid = False
        try:
            value = float(self._clock())
        except (OverflowError, TypeError, ValueError):
            invalid = True
            value = 0.0
        except Exception:
            invalid = True
            value = 0.0
        if invalid or not math.isfinite(value) or value < 0:
            raise ValidationError('speech clock is invalid') from None
        return value

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
    def _trusted_result_tts_terminal_request_id(
        event_id: str,
        claim_request_id: str,
        claim_fence: int,
    ) -> str:
        """Return one public, non-secret terminal id per claim attempt."""
        digest = hashlib.sha256(
            (
                'trusted-result-tts-terminal-v1\0'
                f'{event_id}\0{claim_request_id}\0{claim_fence}'
            ).encode('utf-8')
        ).hexdigest()[:40]
        return f'trusted-result-tts-terminal-{digest}'

    @staticmethod
    def _confirmation_response_id(
        confirmation_request_id: str,
        utterance_id: str,
    ) -> str:
        digest = hashlib.sha256(
            (
                'confirmation-response-v1\0'
                f'{confirmation_request_id}\0{utterance_id}'
            ).encode('utf-8')
        ).hexdigest()[:40]
        return f'confirmation-response-{digest}'

    @staticmethod
    def _confirmation_expiry_response_id(
        confirmation_request_id: str,
    ) -> str:
        digest = hashlib.sha256(
            (
                'confirmation-expiry-v1\0'
                f'{confirmation_request_id}'
            ).encode('utf-8')
        ).hexdigest()[:40]
        return f'confirmation-expiry-{digest}'

    @staticmethod
    def _confirmation_provenance_ref(
        response_channel: str,
        response_fingerprint: str,
    ) -> str:
        """Return an internal content-free correlation digest."""
        return hashlib.sha256(
            (
                'confirmation-provenance-v1\0'
                f'{response_channel}\0{response_fingerprint}'
            ).encode('utf-8')
        ).hexdigest()

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
