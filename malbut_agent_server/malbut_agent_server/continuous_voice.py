"""Device-free continuous wake-to-agent session orchestration.

This module owns no microphone, wake-word model, TTS engine, network client,
or Tool executor.  Trusted local composition code injects those boundaries.
One accepted wake event may produce at most one final transcript.  The final
text is then passed through the existing speech coordinator, orchestrator,
and local safety policy.

Safe non-action decisions are delivered to ``SpeechOutput``.  A locally
approved Tool proposal is never executed here: it is returned as a typed
``ToolConfirmationRequest`` for a separate mission/confirmation boundary.
"""

import copy
import hashlib
import json
import math
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    Dict,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

from malbut_agent_server.local_stt import (
    LocalSTTResult,
    build_transcript_event,
)
from malbut_agent_server.schemas import RobotState, ValidationError
from malbut_agent_server.speech import (
    SpeechActivityEvent,
    SpeechControlResult,
    SpeechConversationCoordinator,
    SpeechPipelineResult,
    TTSCancelRequest,
    TTSRequest,
    TrustedSpeechBinding,
)


AWAITING_WAKE = 'awaiting_wake'
AWAITING_CONFIRMATION = 'awaiting_confirmation'
LISTENING = 'listening'
PROCESSING = 'processing'
SPEAKING = 'speaking'
MISSION_WAIT = 'mission_wait'
CLOSED = 'closed'
VOICE_STATES = frozenset(
    {
        AWAITING_WAKE,
        AWAITING_CONFIRMATION,
        LISTENING,
        PROCESSING,
        SPEAKING,
        MISSION_WAIT,
        CLOSED,
    }
)
NON_ACTION_DECISIONS = frozenset(
    {'message', 'refusal', 'clarification'}
)
OUTPUT_TERMINAL_STATUSES = frozenset(
    {'completed', 'cancelled', 'failed'}
)
MAX_ID_LENGTH = 128
MAX_SOURCE_LENGTH = 64
MAX_WAKE_CACHE_SIZE = 4096
DEFAULT_WAKE_CACHE_SIZE = 256
MISSION_TERMINAL_OUTCOMES = frozenset(
    {'succeeded', 'failed', 'cancelled', 'denied'}
)


class ContinuousVoiceError(RuntimeError):
    """Fixed, content-free failure at the continuous voice boundary."""

    def __init__(self) -> None:
        """Avoid reflecting adapter errors, transcripts, or local paths."""
        super().__init__('continuous voice session failed')


def _identifier(value: Any, field_name: str) -> str:
    if type(value) is not str:
        raise ValidationError(f'{field_name} must be a string')
    normalized = value.strip()
    if not normalized or len(normalized) > MAX_ID_LENGTH:
        raise ValidationError(f'{field_name} is invalid')
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in normalized
    ):
        raise ValidationError(
            f'{field_name} must not contain control characters'
        )
    return normalized


def _source(value: Any) -> str:
    normalized = _identifier(value, 'source')
    if len(normalized) > MAX_SOURCE_LENGTH:
        raise ValidationError('source is invalid')
    return normalized


def _bounded_integer(
    value: Any,
    field_name: str,
    minimum: int,
    maximum: int,
) -> int:
    if type(value) is not int or value < minimum or value > maximum:
        raise ValidationError(f'{field_name} is invalid')
    return value


def _confidence(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or value < 0
        or value > 1
    ):
        raise ValidationError('wake confidence is invalid')
    return float(value)


def _safe_clock_ns(clock_ns: Callable[[], int]) -> int:
    failed = False
    try:
        value = clock_ns()
    except Exception:
        failed = True
        value = None
    if (
        failed
        or type(value) is not int
        or value < 0
        or value > (1 << 63) - 1
    ):
        raise ContinuousVoiceError()
    return value


def _freeze_json(value: Any) -> Any:
    """Return a recursively immutable view of one decoded JSON value."""
    if type(value) is dict:
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    """Return plain JSON containers from an immutable internal snapshot."""
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class WakeWordEvent:
    """Content-free detection emitted by a trusted local wake adapter."""

    event_id: str
    source: str
    source_sequence: int
    source_timestamp_ns: int
    confidence: float

    def __post_init__(self) -> None:
        """Validate direct construction without accepting audio or phrases."""
        object.__setattr__(
            self,
            'event_id',
            _identifier(self.event_id, 'event_id'),
        )
        object.__setattr__(self, 'source', _source(self.source))
        object.__setattr__(
            self,
            'source_sequence',
            _bounded_integer(
                self.source_sequence,
                'source_sequence',
                1,
                (1 << 63) - 1,
            ),
        )
        object.__setattr__(
            self,
            'source_timestamp_ns',
            _bounded_integer(
                self.source_timestamp_ns,
                'source_timestamp_ns',
                0,
                (1 << 63) - 1,
            ),
        )
        object.__setattr__(self, 'confidence', _confidence(self.confidence))

    def to_dict(self) -> Dict[str, Any]:
        """Return the bounded content-free wake event."""
        return {
            'event_id': self.event_id,
            'source': self.source,
            'source_sequence': self.source_sequence,
            'source_timestamp_ns': self.source_timestamp_ns,
            'confidence': self.confidence,
        }

    def to_audit_dict(self) -> Dict[str, Any]:
        """Return the same event because it contains no audio or phrase."""
        return self.to_dict()


class WakeWordSource(Protocol):
    """Injected, trusted source of content-free wake detections."""

    def wait_for_wake(
        self,
        stop_event: threading.Event,
    ) -> Optional[WakeWordEvent]:
        """Return one detection, or ``None`` when no wake is available.

        Implementations must return promptly after ``stop_event`` is set.
        A Python thread cannot safely force-terminate a blocking device call.
        """
        ...


class TranscriptSource(Protocol):
    """Injected source that captures exactly one validated final result."""

    def capture_final(
        self,
        wake_event: WakeWordEvent,
        stop_event: threading.Event,
    ) -> LocalSTTResult:
        """Return one already validated final local STT result.

        Implementations must return promptly after ``stop_event`` is set.
        """
        ...


@dataclass(frozen=True)
class SpeechOutputResult:
    """One terminal result from a text-to-speech or no-audio output sink."""

    request_id: str
    status: str

    def __post_init__(self) -> None:
        """Require a terminal status tied to the exact TTS request."""
        object.__setattr__(
            self,
            'request_id',
            _identifier(self.request_id, 'request_id'),
        )
        if self.status not in OUTPUT_TERMINAL_STATUSES:
            raise ValidationError('speech output status is not terminal')

    def to_audit_dict(self) -> Dict[str, str]:
        """Return terminal metadata without output text or audio."""
        return {
            'request_id': self.request_id,
            'status': self.status,
        }


class SpeechOutput(Protocol):
    """Injected output boundary for final safe TTS and cancellation."""

    def play(
        self,
        request: TTSRequest,
        stop_event: threading.Event,
    ) -> SpeechOutputResult:
        """Deliver one request and return only after it becomes terminal.

        ``stop_event`` is scoped to this request.  An implementation must not
        start output when it is already set and must stop promptly when set.
        """
        ...

    def cancel(self, request: TTSCancelRequest) -> None:
        """Request idempotent cancellation and return promptly."""
        ...


@dataclass(frozen=True, repr=False)
class ToolConfirmationRequest:
    """Non-executing handoff for a separate mission confirmation layer.

    Possession of this object is not authorization.  In particular, it is
    not a ``tool_call_id`` and must never be passed directly to a robot or
    Tool adapter.
    """

    request_id: str
    decision_id: str
    user_id: str
    conversation_id: str
    turn_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    issued_at: float
    expires_at: float
    arguments_digest: str = field(init=False)
    _arguments_json: str = field(init=False, repr=False, compare=True)

    def __post_init__(self) -> None:
        """Freeze a bounded JSON copy of the exact proposal identity."""
        for field_name in (
            'request_id',
            'decision_id',
            'user_id',
            'conversation_id',
            'turn_id',
        ):
            object.__setattr__(
                self,
                field_name,
                _identifier(getattr(self, field_name), field_name),
            )
        tool_name = _identifier(self.tool_name, 'tool_name')
        if len(tool_name) > MAX_SOURCE_LENGTH:
            raise ValidationError('tool_name is invalid')
        object.__setattr__(self, 'tool_name', tool_name)
        if not isinstance(self.arguments, Mapping):
            raise ValidationError('arguments must be an object')
        try:
            encoded = json.dumps(
                _thaw_json(self.arguments),
                ensure_ascii=False,
                sort_keys=True,
                separators=(',', ':'),
                allow_nan=False,
            ).encode('utf-8')
        except (TypeError, ValueError) as error:
            raise ValidationError(
                'arguments must contain JSON values'
            ) from error
        if len(encoded) > 16384:
            raise ValidationError('arguments are too large')
        canonical_json = encoded.decode('utf-8')
        snapshot = json.loads(canonical_json)
        object.__setattr__(self, '_arguments_json', canonical_json)
        object.__setattr__(self, 'arguments', _freeze_json(snapshot))
        object.__setattr__(
            self,
            'arguments_digest',
            hashlib.sha256(encoded).hexdigest(),
        )
        if (
            isinstance(self.issued_at, bool)
            or not isinstance(self.issued_at, (int, float))
            or not math.isfinite(float(self.issued_at))
            or isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, (int, float))
            or not math.isfinite(float(self.expires_at))
            or float(self.expires_at) <= float(self.issued_at)
        ):
            raise ValidationError('proposal lifetime is invalid')
        object.__setattr__(self, 'issued_at', float(self.issued_at))
        object.__setattr__(self, 'expires_at', float(self.expires_at))

    def __repr__(self) -> str:
        """Exclude argument values from diagnostics."""
        return 'ToolConfirmationRequest({!r})'.format(self.to_audit_dict())

    def arguments_dict(self) -> Dict[str, Any]:
        """Return a detached mutable copy for a later validation boundary."""
        return json.loads(self._arguments_json)

    def to_audit_dict(self) -> Dict[str, Any]:
        """Return identifiers and argument shape, never argument values."""
        return {
            'status': 'confirmation_required',
            'request_id': self.request_id,
            'decision_id': self.decision_id,
            'conversation_id': self.conversation_id,
            'turn_id': self.turn_id,
            'tool_name': self.tool_name,
            'argument_names': sorted(self.arguments),
            'arguments_digest': self.arguments_digest,
            'issued_at': self.issued_at,
            'expires_at': self.expires_at,
            'authorized': False,
            'tool_call_id': None,
        }


@dataclass(frozen=True, repr=False)
class VoiceCycleResult:
    """One bounded wake cycle result with a content-free representation."""

    status: str
    code: str
    state: str
    wake_event_id: Optional[str] = None
    pipeline_result: Optional[SpeechPipelineResult] = None
    output_result: Optional[SpeechOutputResult] = None
    tts_terminal_result: Optional[SpeechControlResult] = None
    confirmation_request: Optional[ToolConfirmationRequest] = None
    cached: bool = False

    def __post_init__(self) -> None:
        """Reject malformed adapter-facing outcome objects."""
        if type(self.status) is not str or not self.status:
            raise ValidationError('cycle status is invalid')
        if type(self.code) is not str or not self.code:
            raise ValidationError('cycle code is invalid')
        if self.state not in VOICE_STATES:
            raise ValidationError('cycle state is invalid')
        if self.wake_event_id is not None:
            object.__setattr__(
                self,
                'wake_event_id',
                _identifier(self.wake_event_id, 'wake_event_id'),
            )
        if (
            self.pipeline_result is not None
            and type(self.pipeline_result) is not SpeechPipelineResult
        ):
            raise ValidationError('pipeline_result is invalid')
        if (
            self.output_result is not None
            and type(self.output_result) is not SpeechOutputResult
        ):
            raise ValidationError('output_result is invalid')
        if (
            self.tts_terminal_result is not None
            and type(self.tts_terminal_result) is not SpeechControlResult
        ):
            raise ValidationError('tts_terminal_result is invalid')
        if (
            self.confirmation_request is not None
            and type(self.confirmation_request)
            is not ToolConfirmationRequest
        ):
            raise ValidationError('confirmation_request is invalid')
        if type(self.cached) is not bool:
            raise ValidationError('cached is invalid')

    def __repr__(self) -> str:
        """Represent only content-free lifecycle metadata."""
        return 'VoiceCycleResult({!r})'.format(self.to_audit_dict())

    def to_audit_dict(self) -> Dict[str, Any]:
        """Project no transcript, response text, or Tool argument values."""
        pipeline = self.pipeline_result
        return {
            'status': self.status,
            'code': self.code,
            'state': self.state,
            'wake_event_id': self.wake_event_id,
            'capture_epoch': (
                pipeline.capture_epoch if pipeline is not None else None
            ),
            'request_id': (
                pipeline.request_id if pipeline is not None else None
            ),
            'turn_id': pipeline.turn_id if pipeline is not None else None,
            'decision_type': (
                pipeline.agent_result.decision.type
                if pipeline is not None
                and pipeline.agent_result is not None
                else None
            ),
            'output_status': (
                self.output_result.status
                if self.output_result is not None
                else None
            ),
            'tts_code': (
                self.tts_terminal_result.code
                if self.tts_terminal_result is not None
                else None
            ),
            'confirmation_required': self.confirmation_request is not None,
            'cached': self.cached,
        }


class ContinuousVoiceSession:
    """Run repeated wake-gated final transcripts in one conversation."""

    def __init__(
        self,
        coordinator: SpeechConversationCoordinator,
        binding: TrustedSpeechBinding,
        wake_source: WakeWordSource,
        transcript_source: TranscriptSource,
        speech_output: SpeechOutput,
        *,
        robot_state: Optional[RobotState] = None,
        available_tools: Sequence[str] = (),
        wake_cache_size: int = DEFAULT_WAKE_CACHE_SIZE,
        clock_ns: Optional[Callable[[], int]] = None,
    ) -> None:
        """Open one voice session around trusted injected local adapters."""
        if type(coordinator) is not SpeechConversationCoordinator:
            raise TypeError(
                'coordinator must be a SpeechConversationCoordinator'
            )
        if type(binding) is not TrustedSpeechBinding:
            raise TypeError('binding must be a TrustedSpeechBinding')
        for owner, method_name in (
            (wake_source, 'wait_for_wake'),
            (transcript_source, 'capture_final'),
            (speech_output, 'play'),
            (speech_output, 'cancel'),
        ):
            try:
                method = getattr(owner, method_name)
            except Exception as error:
                raise TypeError('voice adapter is invalid') from error
            if not callable(method):
                raise TypeError('voice adapter is invalid')
        if robot_state is not None and type(robot_state) is not RobotState:
            raise TypeError('robot_state must be a RobotState or None')
        if (
            type(wake_cache_size) is not int
            or wake_cache_size < 1
            or wake_cache_size > MAX_WAKE_CACHE_SIZE
        ):
            raise ValueError('wake_cache_size is invalid')
        if isinstance(available_tools, (str, bytes)):
            raise TypeError('available_tools must be a sequence')
        tools: Tuple[str, ...] = tuple(available_tools)
        if any(type(name) is not str for name in tools):
            raise TypeError('available_tools must contain strings')
        selected_clock = time.time_ns if clock_ns is None else clock_ns
        if not callable(selected_clock):
            raise TypeError('clock_ns must be callable')

        self.coordinator = coordinator
        self.binding = binding
        self.wake_source = wake_source
        self.transcript_source = transcript_source
        self.speech_output = speech_output
        self.robot_state = robot_state
        self.available_tools = tools
        self.wake_cache_size = wake_cache_size
        self._clock_ns = selected_clock
        self._state_lock = threading.RLock()
        self._cycle_lock = threading.Lock()
        self._close_lock = threading.Lock()
        self._cancel_lock = threading.RLock()
        self._stop_event = threading.Event()
        self._wake_cache: OrderedDict[
            str,
            Tuple[str, VoiceCycleResult],
        ] = OrderedDict()
        self._wake_source_sequences: OrderedDict[str, int] = OrderedDict()
        self._next_sequence = 1
        self._cancel_delivery_states: OrderedDict[
            str,
            Tuple[str, Optional[int]],
        ] = OrderedDict()
        self._cancelled_tts_ids: OrderedDict[str, str] = OrderedDict()
        self._tts_stop_events: Dict[str, threading.Event] = {}
        self._pending_confirmation_request: Optional[
            ToolConfirmationRequest
        ] = None
        self._pending_confirmation_fingerprint: Optional[str] = None
        self._active_mission_request: Optional[
            ToolConfirmationRequest
        ] = None
        self._active_mission_fingerprint: Optional[str] = None
        self._terminal_mission_requests: OrderedDict[
            str,
            Tuple[str, str],
        ] = OrderedDict()
        self._state = AWAITING_WAKE
        self._close_result: Optional[SpeechControlResult] = None
        self._close_control_id = self._stable_id(
            'continuous-voice-close',
            binding.speech_session_id,
            binding.conversation_id,
        )

        opened = coordinator.open_session(binding)
        if (
            type(opened) is not SpeechControlResult
            or opened.status != 'ready'
            or opened.code not in {'session_opened', 'session_already_open'}
        ):
            self._state = CLOSED
            self._stop_event.set()
            raise ContinuousVoiceError()
        self._capture_epoch = opened.capture_epoch

    @property
    def state(self) -> str:
        """Return the current small lifecycle state."""
        with self._state_lock:
            return self._state

    @property
    def capture_epoch(self) -> int:
        """Return the coordinator epoch for the next wake capture."""
        with self._state_lock:
            return self._capture_epoch

    @property
    def pending_confirmation_request(
        self,
    ) -> Optional[ToolConfirmationRequest]:
        """Return the exact immutable request awaiting trusted confirmation."""
        with self._state_lock:
            return self._pending_confirmation_request

    @property
    def active_mission_request(
        self,
    ) -> Optional[ToolConfirmationRequest]:
        """Return the exact request accepted into the mission wait state."""
        with self._state_lock:
            return self._active_mission_request

    def run_once(self) -> VoiceCycleResult:
        """Consume at most one wake event and one final transcript."""
        if self.state == CLOSED:
            return self._result('closed', 'session_closed')
        blocked = self._blocked_cycle_result()
        if blocked is not None:
            return blocked
        if not self._cycle_lock.acquire(blocking=False):
            if self.state == CLOSED:
                return self._result('closed', 'session_closed')
            blocked = self._blocked_cycle_result()
            if blocked is not None:
                return blocked
            return self._result('retryable', 'cycle_in_progress')
        try:
            if self.state == CLOSED:
                return self._result('closed', 'session_closed')
            blocked = self._blocked_cycle_result()
            if blocked is not None:
                return blocked
            try:
                wake_event = self.wake_source.wait_for_wake(
                    self._stop_event
                )
            except Exception:
                return self._adapter_failure('wake_source_failed')
            if wake_event is None:
                if self.state == CLOSED:
                    return self._result('closed', 'session_closed')
                return self._result('idle', 'awaiting_wake')
            if type(wake_event) is not WakeWordEvent:
                return self._adapter_failure('invalid_wake_event')

            fingerprint = self._fingerprint(wake_event.to_dict())
            with self._state_lock:
                if self._state == CLOSED:
                    return self._result(
                        'closed',
                        'session_closed',
                        wake_event.event_id,
                    )
                cached = self._wake_cache.get(wake_event.event_id)
                if cached is not None:
                    cached_fingerprint, cached_result = cached
                    if cached_fingerprint != fingerprint:
                        return self._result(
                            'rejected',
                            'wake_event_conflict',
                            wake_event.event_id,
                        )
                    self._wake_cache.move_to_end(wake_event.event_id)
                    return replace(cached_result, cached=True)
                last_source_sequence = self._wake_source_sequences.get(
                    wake_event.source
                )
                if (
                    last_source_sequence is not None
                    and wake_event.source_sequence <= last_source_sequence
                ):
                    result = VoiceCycleResult(
                        status='rejected',
                        code='stale_wake_sequence',
                        state=self._state,
                        wake_event_id=wake_event.event_id,
                    )
                    self._wake_cache[wake_event.event_id] = (
                        fingerprint,
                        result,
                    )
                    self._wake_cache.move_to_end(wake_event.event_id)
                    while len(self._wake_cache) > self.wake_cache_size:
                        self._wake_cache.popitem(last=False)
                    return result
                if (
                    last_source_sequence is None
                    and len(self._wake_source_sequences)
                    >= self.wake_cache_size
                ):
                    result = VoiceCycleResult(
                        status='rejected',
                        code='wake_source_capacity_reached',
                        state=self._state,
                        wake_event_id=wake_event.event_id,
                    )
                    self._wake_cache[wake_event.event_id] = (
                        fingerprint,
                        result,
                    )
                    self._wake_cache.move_to_end(wake_event.event_id)
                    while len(self._wake_cache) > self.wake_cache_size:
                        self._wake_cache.popitem(last=False)
                    return result
                self._wake_source_sequences[wake_event.source] = (
                    wake_event.source_sequence
                )
                self._wake_source_sequences.move_to_end(wake_event.source)
                sequence = self._next_sequence
                self._next_sequence += 1
                capture_epoch = self._capture_epoch
                self._state = LISTENING

            try:
                transcript = self.transcript_source.capture_final(
                    wake_event,
                    self._stop_event,
                )
            except Exception:
                result = self._adapter_failure(
                    'transcript_source_failed',
                    wake_event.event_id,
                )
                return self._remember_wake(
                    wake_event,
                    fingerprint,
                    result,
                )
            if type(transcript) is not LocalSTTResult:
                result = self._adapter_failure(
                    'invalid_transcript_result',
                    wake_event.event_id,
                )
                return self._remember_wake(
                    wake_event,
                    fingerprint,
                    result,
                )
            if self.state == CLOSED:
                return self._result(
                    'closed',
                    'session_closed',
                    wake_event.event_id,
                )

            self._set_open_state(PROCESSING)
            try:
                event = build_transcript_event(
                    transcript,
                    self.binding,
                    utterance_id=self._utterance_id(wake_event.event_id),
                    sequence=sequence,
                    capture_epoch=capture_epoch,
                    timestamp_ns=_safe_clock_ns(self._clock_ns),
                    capture_origin='microphone',
                )
                pipeline = self.coordinator.handle_transcript(
                    event,
                    robot_state=self.robot_state,
                    available_tools=self.available_tools,
                )
            except Exception:
                result = self._adapter_failure(
                    'processing_failed',
                    wake_event.event_id,
                )
                return self._remember_wake(
                    wake_event,
                    fingerprint,
                    result,
                )
            if type(pipeline) is not SpeechPipelineResult:
                result = self._adapter_failure(
                    'invalid_pipeline_result',
                    wake_event.event_id,
                )
                return self._remember_wake(
                    wake_event,
                    fingerprint,
                    result,
                )
            with self._state_lock:
                self._capture_epoch = max(
                    self._capture_epoch,
                    pipeline.capture_epoch,
                )

            if pipeline.status != 'responded':
                self._set_open_state(AWAITING_WAKE)
                result = VoiceCycleResult(
                    status=pipeline.status,
                    code=pipeline.code,
                    state=self.state,
                    wake_event_id=wake_event.event_id,
                    pipeline_result=pipeline,
                )
                return self._remember_wake(
                    wake_event,
                    fingerprint,
                    result,
                )

            result = self._responded_result(wake_event, pipeline)
            return self._remember_wake(
                wake_event,
                fingerprint,
                result,
            )
        finally:
            self._cycle_lock.release()

    def accept_confirmation(
        self,
        request: ToolConfirmationRequest,
    ) -> SpeechControlResult:
        """Atomically accept only the exact pending trusted confirmation."""
        if type(request) is not ToolConfirmationRequest:
            raise TypeError(
                'request must be a ToolConfirmationRequest'
            )
        fingerprint = self._confirmation_fingerprint(request)
        with self._state_lock:
            if self._state == CLOSED:
                return self._control_result('rejected', 'session_closed')
            replay_code = self._terminal_request_code_locked(
                request,
                fingerprint,
            )
            if replay_code is not None:
                return self._control_result('rejected', replay_code)
            if self._state == MISSION_WAIT:
                if self._active_mission_fingerprint == fingerprint:
                    code = 'confirmation_already_accepted'
                else:
                    code = 'mission_request_mismatch'
                return self._control_result('rejected', code)
            pending = self._pending_confirmation_request
            if (
                self._state != AWAITING_CONFIRMATION
                or pending is None
                or self._pending_confirmation_fingerprint is None
            ):
                return self._control_result(
                    'rejected',
                    'no_pending_confirmation',
                )
            if (
                request is not pending
                or self._pending_confirmation_fingerprint != fingerprint
            ):
                return self._control_result(
                    'rejected',
                    'confirmation_mismatch',
                )
            if time.time() >= pending.expires_at:
                self._terminalize_mission_locked(
                    pending,
                    self._pending_confirmation_fingerprint,
                    'expired',
                )
                return self._control_result(
                    'rejected',
                    'confirmation_expired',
                )
            self._active_mission_request = pending
            self._active_mission_fingerprint = fingerprint
            self._pending_confirmation_request = None
            self._pending_confirmation_fingerprint = None
            self._state = MISSION_WAIT
            return self._control_result('ready', 'mission_wait')

    def complete_mission(
        self,
        request: ToolConfirmationRequest,
        *,
        outcome: str,
    ) -> SpeechControlResult:
        """Terminally resolve one exact pending or active mission request."""
        if type(request) is not ToolConfirmationRequest:
            raise TypeError(
                'request must be a ToolConfirmationRequest'
            )
        if (
            type(outcome) is not str
            or outcome not in MISSION_TERMINAL_OUTCOMES
        ):
            raise ValueError('mission outcome is invalid')
        fingerprint = self._confirmation_fingerprint(request)
        with self._state_lock:
            if self._state == CLOSED:
                return self._control_result('rejected', 'session_closed')
            replay_code = self._terminal_request_code_locked(
                request,
                fingerprint,
            )
            if replay_code is not None:
                return self._control_result('rejected', replay_code)
            if self._state == AWAITING_CONFIRMATION:
                if outcome not in {'denied', 'cancelled'}:
                    return self._control_result(
                        'rejected',
                        'confirmation_not_accepted',
                    )
                expected_fingerprint = (
                    self._pending_confirmation_fingerprint
                )
                expected_request = self._pending_confirmation_request
                mismatch_code = 'confirmation_mismatch'
            elif self._state == MISSION_WAIT:
                if outcome == 'denied':
                    return self._control_result(
                        'rejected',
                        'mission_outcome_invalid_for_state',
                    )
                expected_fingerprint = self._active_mission_fingerprint
                expected_request = self._active_mission_request
                mismatch_code = 'mission_request_mismatch'
            else:
                return self._control_result(
                    'rejected',
                    'no_active_mission',
                )
            if (
                expected_request is None
                or expected_fingerprint is None
                or request is not expected_request
                or expected_fingerprint != fingerprint
            ):
                return self._control_result('rejected', mismatch_code)
            self._terminalize_mission_locked(
                expected_request,
                expected_fingerprint,
                outcome,
            )
            return self._control_result('ready', f'mission_{outcome}')

    def handle_barge_in(
        self,
        event: SpeechActivityEvent,
    ) -> SpeechControlResult:
        """Fence active output and forward one trusted cancel request."""
        if type(event) is not SpeechActivityEvent:
            raise TypeError('event must be a SpeechActivityEvent')
        result = self.coordinator.handle_barge_in(event)
        with self._state_lock:
            self._capture_epoch = result.capture_epoch
        if result.cancel_request is not None:
            if not self._deliver_cancel_once(result.cancel_request):
                self.close()
                return SpeechControlResult(
                    status='failed',
                    code='tts_cancel_delivery_failed',
                    capture_epoch=result.capture_epoch,
                    cancel_request=result.cancel_request,
                )
        return result

    def close(self) -> SpeechControlResult:
        """Idempotently stop sources, close the session, and cancel output."""
        with self._close_lock:
            with self._state_lock:
                if self._close_result is not None:
                    return self._close_result
                if (
                    self._pending_confirmation_request is not None
                    and self._pending_confirmation_fingerprint is not None
                ):
                    self._terminalize_mission_locked(
                        self._pending_confirmation_request,
                        self._pending_confirmation_fingerprint,
                        'session_closed',
                    )
                elif (
                    self._active_mission_request is not None
                    and self._active_mission_fingerprint is not None
                ):
                    self._terminalize_mission_locked(
                        self._active_mission_request,
                        self._active_mission_fingerprint,
                        'session_closed',
                    )
                self._state = CLOSED
                self._stop_event.set()
                with self._cancel_lock:
                    for stop_event in self._tts_stop_events.values():
                        stop_event.set()
            result = self.coordinator.close_session(
                self.binding.speech_session_id,
                self._close_control_id,
            )
            with self._state_lock:
                self._capture_epoch = result.capture_epoch
                self._close_result = result
        if (
            result.cancel_request is not None
            and not self._deliver_cancel_once(result.cancel_request)
        ):
            result = SpeechControlResult(
                status='failed',
                code='session_closed_tts_cancel_failed',
                capture_epoch=result.capture_epoch,
                cancel_request=result.cancel_request,
            )
            with self._close_lock:
                self._close_result = result
        return result

    def __enter__(self) -> 'ContinuousVoiceSession':
        """Return this already-open session."""
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> bool:
        """Close without suppressing an active body exception."""
        del exc_type, exc, traceback
        self.close()
        return False

    def _responded_result(
        self,
        wake_event: WakeWordEvent,
        pipeline: SpeechPipelineResult,
    ) -> VoiceCycleResult:
        agent_result = pipeline.agent_result
        tts_request = pipeline.tts_request
        if agent_result is None or tts_request is None:
            return self._adapter_failure(
                'invalid_responded_result',
                wake_event.event_id,
            )
        decision = agent_result.decision
        if decision.type == 'tool_call':
            if self._response_was_superseded(
                pipeline,
                tts_request.request_id,
            ):
                return self._terminalize_superseded_response(
                    wake_event,
                    pipeline,
                    include_output_result=False,
                )
            confirmation = self._confirmation_request(pipeline)
            terminal = self.coordinator.mark_tts_terminal(
                self.binding.speech_session_id,
                tts_request.request_id,
            )
            with self._state_lock:
                self._capture_epoch = max(
                    self._capture_epoch,
                    terminal.capture_epoch,
                )
            if (
                terminal.code != 'tts_terminal'
                or self.state == CLOSED
            ):
                return self._terminalize_superseded_response(
                    wake_event,
                    pipeline,
                    include_output_result=False,
                )
            if confirmation is None:
                self._set_open_state(AWAITING_WAKE)
                return VoiceCycleResult(
                    status='rejected',
                    code='tool_proposal_not_confirmable',
                    state=self.state,
                    wake_event_id=wake_event.event_id,
                    pipeline_result=pipeline,
                    tts_terminal_result=terminal,
                )
            confirmation_fingerprint = self._confirmation_fingerprint(
                confirmation
            )
            with self._state_lock:
                superseded = self._state == CLOSED
                if not superseded:
                    if (
                        self._pending_confirmation_request is not None
                        or self._active_mission_request is not None
                    ):
                        raise ContinuousVoiceError()
                    self._pending_confirmation_request = confirmation
                    self._pending_confirmation_fingerprint = (
                        confirmation_fingerprint
                    )
                    self._state = AWAITING_CONFIRMATION
            if superseded:
                return self._terminalize_superseded_response(
                    wake_event,
                    pipeline,
                    include_output_result=False,
                )
            return VoiceCycleResult(
                status='confirmation_required',
                code='confirmation_required',
                state=AWAITING_CONFIRMATION,
                wake_event_id=wake_event.event_id,
                pipeline_result=pipeline,
                tts_terminal_result=terminal,
                confirmation_request=confirmation,
            )
        if decision.type not in NON_ACTION_DECISIONS:
            self.close()
            return self._adapter_failure(
                'unsupported_decision_type',
                wake_event.event_id,
            )

        output_stop_event = self._prepare_output(
            pipeline,
            tts_request.request_id,
        )
        if output_stop_event is None:
            return self._terminalize_superseded_response(
                wake_event,
                pipeline,
                include_output_result=True,
            )
        try:
            output_result = self.speech_output.play(
                tts_request,
                output_stop_event,
            )
        except Exception:
            self.close()
            self._finish_output_tracking(tts_request.request_id)
            return VoiceCycleResult(
                status='failed',
                code='speech_output_failed',
                state=self.state,
                wake_event_id=wake_event.event_id,
                pipeline_result=pipeline,
            )
        if (
            type(output_result) is not SpeechOutputResult
            or output_result.request_id != tts_request.request_id
        ):
            self.close()
            self._finish_output_tracking(tts_request.request_id)
            return VoiceCycleResult(
                status='failed',
                code='invalid_speech_output_result',
                state=self.state,
                wake_event_id=wake_event.event_id,
                pipeline_result=pipeline,
            )
        superseded = self._response_was_superseded(
            pipeline,
            tts_request.request_id,
        )
        cancel_status = self._tts_cancel_status(tts_request.request_id)
        terminal = self.coordinator.mark_tts_terminal(
            self.binding.speech_session_id,
            tts_request.request_id,
        )
        with self._state_lock:
            self._capture_epoch = max(
                self._capture_epoch,
                terminal.capture_epoch,
            )
        self._finish_output_tracking(tts_request.request_id)
        self._set_open_state(AWAITING_WAKE)
        status = 'responded'
        code = decision.type
        if cancel_status == 'failed':
            status = 'failed'
            code = 'tts_cancel_delivery_failed'
        elif superseded:
            status = 'cancelled'
            code = 'speech_output_cancelled'
        elif output_result.status == 'failed':
            status = 'failed'
            code = 'speech_output_failed'
        elif output_result.status == 'cancelled':
            status = 'cancelled'
            code = 'speech_output_cancelled'
        return VoiceCycleResult(
            status=status,
            code=code,
            state=self.state,
            wake_event_id=wake_event.event_id,
            pipeline_result=pipeline,
            output_result=output_result,
            tts_terminal_result=terminal,
        )

    def _confirmation_request(
        self,
        pipeline: SpeechPipelineResult,
    ) -> Optional[ToolConfirmationRequest]:
        agent_result = pipeline.agent_result
        if agent_result is None:
            return None
        decision = agent_result.decision
        if (
            decision.type != 'tool_call'
            or decision.tool_name is None
            or not agent_result.safety.allowed
            or not agent_result.state_trusted
            or time.time() >= agent_result.expires_at
            or pipeline.request_id is None
            or pipeline.turn_id is None
        ):
            return None
        return ToolConfirmationRequest(
            request_id=self._stable_id(
                'mission-confirmation',
                agent_result.decision_id,
            ),
            decision_id=agent_result.decision_id,
            user_id=self.binding.user_id,
            conversation_id=self.binding.conversation_id,
            turn_id=pipeline.turn_id,
            tool_name=decision.tool_name,
            arguments=copy.deepcopy(decision.arguments),
            issued_at=agent_result.issued_at,
            expires_at=agent_result.expires_at,
        )

    def _adapter_failure(
        self,
        code: str,
        wake_event_id: Optional[str] = None,
    ) -> VoiceCycleResult:
        self._set_open_state(AWAITING_WAKE)
        return self._result('failed', code, wake_event_id)

    def _blocked_cycle_result(self) -> Optional[VoiceCycleResult]:
        """Return no-input state while confirmation or mission is pending."""
        with self._state_lock:
            if self._state == AWAITING_CONFIRMATION:
                request = self._pending_confirmation_request
                fingerprint = self._pending_confirmation_fingerprint
                if request is None or fingerprint is None:
                    raise ContinuousVoiceError()
                if time.time() >= request.expires_at:
                    self._terminalize_mission_locked(
                        request,
                        fingerprint,
                        'expired',
                    )
                    return VoiceCycleResult(
                        status='rejected',
                        code='confirmation_expired',
                        state=AWAITING_WAKE,
                    )
                return VoiceCycleResult(
                    status='busy',
                    code='confirmation_pending',
                    state=AWAITING_CONFIRMATION,
                    confirmation_request=request,
                )
            if self._state == MISSION_WAIT:
                if (
                    self._active_mission_request is None
                    or self._active_mission_fingerprint is None
                ):
                    raise ContinuousVoiceError()
                return VoiceCycleResult(
                    status='busy',
                    code='mission_in_progress',
                    state=MISSION_WAIT,
                )
            return None

    @staticmethod
    def _confirmation_fingerprint(
        request: ToolConfirmationRequest,
    ) -> str:
        return ContinuousVoiceSession._fingerprint(
            {
                'request_id': request.request_id,
                'decision_id': request.decision_id,
                'user_id': request.user_id,
                'conversation_id': request.conversation_id,
                'turn_id': request.turn_id,
                'tool_name': request.tool_name,
                'arguments': request.arguments_dict(),
                'arguments_digest': request.arguments_digest,
                'issued_at': request.issued_at,
                'expires_at': request.expires_at,
            }
        )

    def _terminal_request_code_locked(
        self,
        request: ToolConfirmationRequest,
        fingerprint: str,
    ) -> Optional[str]:
        terminal = self._terminal_mission_requests.get(request.request_id)
        if terminal is None:
            return None
        terminal_fingerprint, _outcome = terminal
        self._terminal_mission_requests.move_to_end(request.request_id)
        if terminal_fingerprint != fingerprint:
            return 'mission_request_conflict'
        return 'mission_terminal_replay'

    def _terminalize_mission_locked(
        self,
        request: ToolConfirmationRequest,
        fingerprint: str,
        outcome: str,
    ) -> None:
        self._terminal_mission_requests[request.request_id] = (
            fingerprint,
            outcome,
        )
        self._terminal_mission_requests.move_to_end(request.request_id)
        while len(self._terminal_mission_requests) > self.wake_cache_size:
            self._terminal_mission_requests.popitem(last=False)
        for wake_event_id, (_wake_fingerprint, cycle) in list(
            self._wake_cache.items()
        ):
            cached_request = cycle.confirmation_request
            if (
                cached_request is not None
                and cached_request.request_id == request.request_id
            ):
                del self._wake_cache[wake_event_id]
        self._pending_confirmation_request = None
        self._pending_confirmation_fingerprint = None
        self._active_mission_request = None
        self._active_mission_fingerprint = None
        self._state = AWAITING_WAKE

    def _control_result(
        self,
        status: str,
        code: str,
    ) -> SpeechControlResult:
        return SpeechControlResult(
            status=status,
            code=code,
            capture_epoch=self._capture_epoch,
        )

    def _prepare_output(
        self,
        pipeline: SpeechPipelineResult,
        tts_request_id: str,
    ) -> Optional[threading.Event]:
        """Atomically fence a terminal response before entering its adapter."""
        with self._state_lock:
            if (
                self._state == CLOSED
                or self._stop_event.is_set()
                or self._capture_epoch != pipeline.capture_epoch
            ):
                return None
            with self._cancel_lock:
                if tts_request_id in self._cancelled_tts_ids:
                    return None
                stop_event = threading.Event()
                self._tts_stop_events[tts_request_id] = stop_event
            self._state = SPEAKING
            return stop_event

    def _response_was_superseded(
        self,
        pipeline: SpeechPipelineResult,
        tts_request_id: str,
    ) -> bool:
        """Return whether close or barge-in fenced this exact response."""
        with self._state_lock:
            if (
                self._state == CLOSED
                or self._capture_epoch != pipeline.capture_epoch
            ):
                return True
            with self._cancel_lock:
                return tts_request_id in self._cancelled_tts_ids

    def _terminalize_superseded_response(
        self,
        wake_event: WakeWordEvent,
        pipeline: SpeechPipelineResult,
        *,
        include_output_result: bool,
    ) -> VoiceCycleResult:
        """Acknowledge coordinator terminal state without starting output."""
        tts_request = pipeline.tts_request
        if tts_request is None:
            return self._adapter_failure(
                'invalid_responded_result',
                wake_event.event_id,
            )
        cancel_status = self._tts_cancel_status(tts_request.request_id)
        terminal = self.coordinator.mark_tts_terminal(
            self.binding.speech_session_id,
            tts_request.request_id,
        )
        with self._state_lock:
            self._capture_epoch = max(
                self._capture_epoch,
                terminal.capture_epoch,
            )
        self._finish_output_tracking(tts_request.request_id)
        self._set_open_state(AWAITING_WAKE)
        failed = cancel_status == 'failed'
        output_result = None
        if include_output_result:
            output_result = SpeechOutputResult(
                request_id=tts_request.request_id,
                status='failed' if failed else 'cancelled',
            )
        return VoiceCycleResult(
            status='failed' if failed else 'cancelled',
            code=(
                'tts_cancel_delivery_failed'
                if failed
                else 'speech_output_cancelled'
            ),
            state=self.state,
            wake_event_id=wake_event.event_id,
            pipeline_result=pipeline,
            output_result=output_result,
            tts_terminal_result=terminal,
        )

    def _result(
        self,
        status: str,
        code: str,
        wake_event_id: Optional[str] = None,
    ) -> VoiceCycleResult:
        return VoiceCycleResult(
            status=status,
            code=code,
            state=self.state,
            wake_event_id=wake_event_id,
        )

    def _set_open_state(self, state: str) -> None:
        with self._state_lock:
            if self._state != CLOSED:
                self._state = state

    def _remember_wake(
        self,
        wake_event: WakeWordEvent,
        fingerprint: str,
        result: VoiceCycleResult,
    ) -> VoiceCycleResult:
        with self._state_lock:
            self._wake_cache[wake_event.event_id] = (
                fingerprint,
                result,
            )
            self._wake_cache.move_to_end(wake_event.event_id)
            while len(self._wake_cache) > self.wake_cache_size:
                self._wake_cache.popitem(last=False)
        return result

    def _deliver_cancel_once(self, request: TTSCancelRequest) -> bool:
        """Deliver once without holding a lock across the output adapter."""
        owner = threading.get_ident()
        with self._cancel_lock:
            self._cancelled_tts_ids[request.tts_request_id] = 'requested'
            self._cancelled_tts_ids.move_to_end(request.tts_request_id)
            stop_event = self._tts_stop_events.get(request.tts_request_id)
            if stop_event is not None:
                stop_event.set()
            existing = self._cancel_delivery_states.get(request.request_id)
            if existing is not None:
                status, existing_owner = existing
                self._cancel_delivery_states.move_to_end(request.request_id)
                if status == 'delivered':
                    return True
                if status == 'failed':
                    return False
                return existing_owner == owner
            self._cancel_delivery_states[request.request_id] = (
                'delivering',
                owner,
            )

        delivered = True
        try:
            self.speech_output.cancel(request)
        except Exception:
            delivered = False

        with self._cancel_lock:
            status = 'delivered' if delivered else 'failed'
            self._cancel_delivery_states[request.request_id] = (status, None)
            self._cancel_delivery_states.move_to_end(request.request_id)
            self._cancelled_tts_ids[request.tts_request_id] = status
            self._cancelled_tts_ids.move_to_end(request.tts_request_id)
            self._prune_cancel_state_locked()
        return delivered

    def _tts_cancel_status(self, tts_request_id: str) -> Optional[str]:
        with self._cancel_lock:
            return self._cancelled_tts_ids.get(tts_request_id)

    def _finish_output_tracking(self, tts_request_id: str) -> None:
        with self._cancel_lock:
            self._tts_stop_events.pop(tts_request_id, None)
            self._prune_cancel_state_locked()

    def _prune_cancel_state_locked(self) -> None:
        while len(self._cancel_delivery_states) > self.wake_cache_size:
            removable = next(
                (
                    request_id
                    for request_id, (status, _owner)
                    in self._cancel_delivery_states.items()
                    if status != 'delivering'
                ),
                None,
            )
            if removable is None:
                break
            del self._cancel_delivery_states[removable]
        while len(self._cancelled_tts_ids) > self.wake_cache_size:
            self._cancelled_tts_ids.popitem(last=False)

    def _utterance_id(self, wake_event_id: str) -> str:
        return self._stable_id(
            'continuous-voice-utterance',
            self.binding.speech_session_id,
            wake_event_id,
        )

    @staticmethod
    def _stable_id(prefix: str, *parts: str) -> str:
        digest = hashlib.sha256(
            ('continuous-voice-v1\0' + '\0'.join(parts)).encode('utf-8')
        ).hexdigest()[:40]
        return f'{prefix}-{digest}'

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


__all__ = [
    'AWAITING_CONFIRMATION',
    'AWAITING_WAKE',
    'CLOSED',
    'ContinuousVoiceError',
    'ContinuousVoiceSession',
    'LISTENING',
    'MISSION_WAIT',
    'PROCESSING',
    'SPEAKING',
    'SpeechOutput',
    'SpeechOutputResult',
    'ToolConfirmationRequest',
    'TranscriptSource',
    'VoiceCycleResult',
    'WakeWordEvent',
    'WakeWordSource',
]
