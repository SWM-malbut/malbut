"""Non-authorizing confirmation request contracts for proposed Tools."""

import json
import hashlib
import math
import unicodedata
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Dict, Mapping, Optional

from malbut_agent_server.conversation import ConfirmationIntentDraft
from malbut_agent_server.monitor_room_target import (
    GAZEBO_SIMULATION_EFFECTS_SCHEMA_VERSION,
    TargetBinding,
)
from malbut_agent_server.orchestrator import OrchestrationResult
from malbut_agent_server.schemas import (
    MAX_ID_LENGTH,
    ValidationError,
    validate_conversation_id,
    validate_turn_id,
    validate_user_id,
)
from malbut_agent_server.tools import validate_tool_arguments


CONFIRMATION_REQUEST_SCHEMA_VERSION = 3
CONFIRMATION_RESPONSE_SCHEMA_VERSION = 2
CONFIRMATION_RESOLUTION_SCHEMA_VERSION = 2
MAX_CONFIRMATION_MESSAGE_LENGTH = 1000
MAX_CONFIRMATION_ARGUMENT_BYTES = 16384
MAX_CONFIRMATION_SEQUENCE = (1 << 63) - 1
CONFIRMATION_DISPOSITIONS = frozenset({'approve', 'deny', 'cancel'})
CONFIRMATION_TERMINAL_DISPOSITIONS = frozenset(
    {'approve', 'deny', 'cancel', 'expired'}
)

_APPROVE_RESPONSES = frozenset(
    {
        '응',
        '네',
        '예',
        '좋아',
        '승인',
        '승인해',
        '해줘',
        '시작해줘',
        'yes',
        'approve',
    }
)
_DENY_RESPONSES = frozenset(
    {
        '아니',
        '아니요',
        '싫어',
        '거절',
        '거절해',
        'no',
        'deny',
    }
)
_CANCEL_RESPONSES = frozenset(
    {
        '취소',
        '취소해',
        '그만',
        '그만해',
        '하지마',
        'cancel',
    }
)
_TERMINAL_PUNCTUATION = frozenset('.,!?~\u3002\uff01\uff1f')


def _identifier(value: Any, field_name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f'{field_name} must be a string')
    result = value.strip()
    if not result or len(result) > MAX_ID_LENGTH:
        raise ValidationError(f'{field_name} is invalid')
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in result
    ):
        raise ValidationError(
            f'{field_name} must not contain control characters'
        )
    return result


def _message(value: Any) -> str:
    if not isinstance(value, str):
        raise ValidationError('confirmation message must be a string')
    result = value.strip()
    if not result or len(result) > MAX_CONFIRMATION_MESSAGE_LENGTH:
        raise ValidationError('confirmation message is invalid')
    if any(
        ord(character) < 32 and character not in '\n\t'
        for character in result
    ):
        raise ValidationError(
            'confirmation message contains control characters'
        )
    return result


def _timestamp(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f'{field_name} must be a number')
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ValidationError(f'{field_name} is invalid')
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
        raise ValidationError(f'{field_name} is invalid')
    return value


def _digest(value: Any, field_name: str) -> str:
    result = _identifier(value, field_name)
    if len(result) != 64 or any(
        character not in '0123456789abcdef'
        for character in result
    ):
        raise ValidationError(f'{field_name} is invalid')
    return result


def _canonical_arguments(tool_name: str, value: Any) -> str:
    try:
        validated = validate_tool_arguments(tool_name, value)
    except (TypeError, ValidationError):
        raise ValidationError(
            'confirmation arguments are invalid'
        ) from None
    try:
        encoded = json.dumps(
            validated,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
    except (TypeError, ValueError):
        raise ValidationError('confirmation arguments are invalid') from None
    if len(encoded) > MAX_CONFIRMATION_ARGUMENT_BYTES:
        raise ValidationError('confirmation arguments are too large')
    return encoded.decode('utf-8')


def classify_confirmation_response(text: Any) -> Optional[str]:
    """Classify one exact local response without consulting a model."""
    if not isinstance(text, str):
        raise TypeError('confirmation response text must be a string')
    normalized = unicodedata.normalize('NFKC', text).casefold().strip()
    if any(
        ord(character) < 32 and not character.isspace()
        for character in normalized
    ):
        return None
    compact = ''.join(
        character for character in normalized if not character.isspace()
    )
    while compact and compact[-1] in _TERMINAL_PUNCTUATION:
        compact = compact[:-1]
    if compact in _APPROVE_RESPONSES:
        return 'approve'
    if compact in _DENY_RESPONSES:
        return 'deny'
    if compact in _CANCEL_RESPONSES:
        return 'cancel'
    return None


@dataclass(frozen=True)
class ToolConfirmationRequest:
    """Immutable UX request that grants no execution authority."""

    confirmation_request_id: str
    agent_request_id: str
    user_id: str
    speech_session_id: str
    source_utterance_id: str
    conversation_id: str
    conversation_session_instance_id: str
    conversation_generation: int
    conversation_revision: int
    conversation_ordinal: int
    turn_id: str
    decision_id: str
    tool_name: str
    arguments: Mapping[str, Any]
    message: str
    issued_at: float
    expires_at: float
    risk_level: str
    target: TargetBinding
    execution_authorized: bool = False
    schema_version: int = CONFIRMATION_REQUEST_SCHEMA_VERSION
    _arguments_json: str = field(init=False, repr=False)
    _proposal_fingerprint: str = field(init=False, repr=False)

    def __post_init__(self) -> None:
        """Bind one request to a validated immutable proposal snapshot."""
        if self.schema_version != CONFIRMATION_REQUEST_SCHEMA_VERSION:
            raise ValidationError(
                'confirmation schema_version is unsupported'
            )
        object.__setattr__(
            self,
            'confirmation_request_id',
            _identifier(
                self.confirmation_request_id,
                'confirmation_request_id',
            ),
        )
        object.__setattr__(
            self,
            'agent_request_id',
            _identifier(self.agent_request_id, 'agent_request_id'),
        )
        object.__setattr__(
            self,
            'user_id',
            validate_user_id(self.user_id),
        )
        object.__setattr__(
            self,
            'speech_session_id',
            _identifier(self.speech_session_id, 'speech_session_id'),
        )
        object.__setattr__(
            self,
            'source_utterance_id',
            _identifier(self.source_utterance_id, 'source_utterance_id'),
        )
        object.__setattr__(
            self,
            'conversation_id',
            validate_conversation_id(self.conversation_id),
        )
        object.__setattr__(
            self,
            'conversation_session_instance_id',
            _identifier(
                self.conversation_session_instance_id,
                'conversation_session_instance_id',
            ),
        )
        object.__setattr__(
            self,
            'conversation_generation',
            _integer(
                self.conversation_generation,
                'conversation_generation',
                1,
                MAX_CONFIRMATION_SEQUENCE,
            ),
        )
        object.__setattr__(
            self,
            'conversation_revision',
            _integer(
                self.conversation_revision,
                'conversation_revision',
                1,
                MAX_CONFIRMATION_SEQUENCE,
            ),
        )
        object.__setattr__(
            self,
            'conversation_ordinal',
            _integer(
                self.conversation_ordinal,
                'conversation_ordinal',
                1,
                MAX_CONFIRMATION_SEQUENCE,
            ),
        )
        object.__setattr__(
            self,
            'turn_id',
            validate_turn_id(self.turn_id),
        )
        object.__setattr__(
            self,
            'decision_id',
            _identifier(self.decision_id, 'decision_id'),
        )
        tool_name = _identifier(self.tool_name, 'tool_name')
        object.__setattr__(self, 'tool_name', tool_name)
        try:
            arguments = dict(self.arguments)
        except (TypeError, ValueError):
            raise ValidationError(
                'confirmation arguments are invalid'
            ) from None
        arguments_json = _canonical_arguments(tool_name, arguments)
        object.__setattr__(self, '_arguments_json', arguments_json)
        object.__setattr__(
            self,
            'arguments',
            MappingProxyType(json.loads(arguments_json)),
        )
        object.__setattr__(self, 'message', _message(self.message))
        issued_at = _timestamp(self.issued_at, 'issued_at')
        expires_at = _timestamp(self.expires_at, 'expires_at')
        if expires_at <= issued_at:
            raise ValidationError('confirmation request is not current')
        object.__setattr__(self, 'issued_at', issued_at)
        object.__setattr__(self, 'expires_at', expires_at)
        if self.risk_level != 'L3':
            raise ValidationError('confirmation risk_level is unsupported')
        if self.execution_authorized is not False:
            raise ValidationError(
                'confirmation requests cannot authorize execution'
            )
        if not isinstance(self.target, TargetBinding):
            raise ValidationError(
                'confirmation target must be a trusted binding'
            )
        arguments_digest = hashlib.sha256(
            arguments_json.encode('utf-8')
        ).hexdigest()
        if self.target.source_arguments_digest != arguments_digest:
            raise ValidationError(
                'confirmation target does not match Tool arguments'
            )
        fingerprint_body = {
            'schema_version': self.schema_version,
            'agent_request_id': self.agent_request_id,
            'user_id': self.user_id,
            'speech_session_id': self.speech_session_id,
            'source_utterance_id': self.source_utterance_id,
            'conversation_id': self.conversation_id,
            'conversation_session_instance_id': (
                self.conversation_session_instance_id
            ),
            'conversation_generation': self.conversation_generation,
            'conversation_revision': self.conversation_revision,
            'conversation_ordinal': self.conversation_ordinal,
            'turn_id': self.turn_id,
            'decision_id': self.decision_id,
            'tool_name': self.tool_name,
            'arguments': json.loads(arguments_json),
            'issued_at': self.issued_at,
            'expires_at': self.expires_at,
            'risk_level': self.risk_level,
            'message': self.message,
            'target': self.target.to_private_dict(),
        }
        encoded = json.dumps(
            fingerprint_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
            allow_nan=False,
        ).encode('utf-8')
        object.__setattr__(
            self,
            '_proposal_fingerprint',
            hashlib.sha256(encoded).hexdigest(),
        )

    @property
    def proposal_fingerprint(self) -> str:
        """Return a content-bound identifier that grants no authority."""
        return self._proposal_fingerprint

    def arguments_dict(self) -> Dict[str, Any]:
        """Return a detached JSON object for transport only."""
        return json.loads(self._arguments_json)

    def to_intent_draft(self) -> ConfirmationIntentDraft:
        """Return the content-minimized row committed with its turn."""
        return ConfirmationIntentDraft(
            schema_version=self.schema_version,
            confirmation_request_id=self.confirmation_request_id,
            agent_request_id=self.agent_request_id,
            user_id=self.user_id,
            speech_session_id=self.speech_session_id,
            source_utterance_id=self.source_utterance_id,
            conversation_id=self.conversation_id,
            session_instance_id=(
                self.conversation_session_instance_id
            ),
            generation=self.conversation_generation,
            revision=self.conversation_revision,
            ordinal=self.conversation_ordinal,
            turn_id=self.turn_id,
            decision_id=self.decision_id,
            tool_name=self.tool_name,
            arguments_digest=hashlib.sha256(
                self._arguments_json.encode('utf-8')
            ).hexdigest(),
            proposal_fingerprint=self.proposal_fingerprint,
            issued_at=self.issued_at,
            expires_at=self.expires_at,
            risk_level=self.risk_level,
            confirmation_message=self.message,
            target_binding_schema_version=self.target.schema_version,
            target_device_id=self.target.device_id,
            target_device_binding_revision=(
                self.target.device_binding_revision
            ),
            target_source_revision=self.target.source_revision,
            target_map_id=self.target.map_id,
            target_map_revision=self.target.map_revision,
            target_semantic_revision=self.target.semantic_revision,
            target_frame_id=self.target.frame_id,
            target_room_id=self.target.room_id,
            target_room_name=self.target.room_name,
            target_room_category=self.target.room_category,
            target_geometry_json=self.target.geometry_json,
            target_geometry_digest=self.target.geometry_digest,
            target_representative_x=self.target.representative_point[0],
            target_representative_y=self.target.representative_point[1],
            target_clearance_m=self.target.clearance_m,
            target_area_m2=self.target.area_m2,
            target_source_arguments_digest=(
                self.target.source_arguments_digest
            ),
            target_binding_digest=self.target.binding_digest,
            effects_schema_version=self.target.effects.schema_version,
            effect_physical_navigation=(
                self.target.effects.physical_navigation
            ),
            effect_camera_capture=self.target.effects.camera_capture,
            effect_external_video_stream=(
                self.target.effects.external_video_stream
            ),
            effect_video_recording=self.target.effects.video_recording,
            effect_audio_capture=self.target.effects.audio_capture,
            effect_coverage_mode=self.target.effects.coverage_mode,
            effect_viewer_scope=self.target.effects.viewer_scope,
            effect_talkback_allowed=(
                self.target.effects.talkback_allowed
            ),
            effect_max_duration_seconds=(
                self.target.effects.max_duration_seconds
            ),
            effects_digest=self.target.effects_digest,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a public request with an explicit non-authorizing flag."""
        return {
            'schema_version': self.schema_version,
            'confirmation_request_id': self.confirmation_request_id,
            'agent_request_id': self.agent_request_id,
            'user_id': self.user_id,
            'speech_session_id': self.speech_session_id,
            'source_utterance_id': self.source_utterance_id,
            'conversation_id': self.conversation_id,
            'conversation_session_instance_id': (
                self.conversation_session_instance_id
            ),
            'conversation_generation': self.conversation_generation,
            'conversation_revision': self.conversation_revision,
            'conversation_ordinal': self.conversation_ordinal,
            'turn_id': self.turn_id,
            'decision_id': self.decision_id,
            'tool_name': self.tool_name,
            'arguments': self.arguments_dict(),
            'message': self.message,
            'issued_at': self.issued_at,
            'expires_at': self.expires_at,
            'risk_level': self.risk_level,
            'target': self.target.to_dict(),
            'proposal_fingerprint': self.proposal_fingerprint,
            'execution_authorized': False,
        }


@dataclass(frozen=True)
class ToolConfirmationResponseEvent:
    """Internal response normalized from one trusted input channel."""

    response_id: str
    speech_session_id: str
    conversation_id: str
    confirmation_request_id: str
    decision_id: str
    proposal_fingerprint: str
    capture_epoch: int
    disposition: str
    schema_version: int = CONFIRMATION_RESPONSE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Validate direct construction at the local trust boundary."""
        if self.schema_version != CONFIRMATION_RESPONSE_SCHEMA_VERSION:
            raise ValidationError(
                'confirmation response schema_version is unsupported'
            )
        for name in (
            'response_id',
            'speech_session_id',
            'confirmation_request_id',
            'decision_id',
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
            'proposal_fingerprint',
            _digest(self.proposal_fingerprint, 'proposal_fingerprint'),
        )
        object.__setattr__(
            self,
            'capture_epoch',
            _integer(
                self.capture_epoch,
                'capture_epoch',
                1,
                MAX_CONFIRMATION_SEQUENCE,
            ),
        )
        if self.disposition not in CONFIRMATION_DISPOSITIONS:
            raise ValidationError(
                'confirmation disposition is unsupported'
            )

    def to_dict(self) -> Dict[str, Any]:
        """Return the bounded response without user-controlled arguments."""
        return {
            'schema_version': self.schema_version,
            'response_id': self.response_id,
            'speech_session_id': self.speech_session_id,
            'conversation_id': self.conversation_id,
            'confirmation_request_id': self.confirmation_request_id,
            'decision_id': self.decision_id,
            'proposal_fingerprint': self.proposal_fingerprint,
            'capture_epoch': self.capture_epoch,
            'disposition': self.disposition,
        }


@dataclass(frozen=True)
class ToolConfirmationUIResponseEvent:
    """Minimal UI DTO that deliberately carries no audio epoch or actor."""

    response_id: str
    speech_session_id: str
    confirmation_request_id: str
    disposition: str
    schema_version: int = CONFIRMATION_RESPONSE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Validate untrusted UI fields before server-side binding."""
        if self.schema_version != CONFIRMATION_RESPONSE_SCHEMA_VERSION:
            raise ValidationError(
                'confirmation UI response schema_version is unsupported'
            )
        for name in (
            'response_id',
            'speech_session_id',
            'confirmation_request_id',
        ):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), name),
            )
        if self.disposition not in CONFIRMATION_DISPOSITIONS:
            raise ValidationError(
                'confirmation disposition is unsupported'
            )

    @classmethod
    def from_dict(cls, value: Any) -> 'ToolConfirmationUIResponseEvent':
        """Parse a strict UI body without accepting authority fields."""
        if not isinstance(value, dict):
            raise ValidationError(
                'confirmation UI response must be an object'
            )
        allowed = {
            'schema_version',
            'response_id',
            'speech_session_id',
            'confirmation_request_id',
            'disposition',
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValidationError(
                'confirmation UI response contains unknown fields'
            )
        return cls(
            schema_version=value.get('schema_version'),
            response_id=value.get('response_id'),
            speech_session_id=value.get('speech_session_id'),
            confirmation_request_id=value.get(
                'confirmation_request_id'
            ),
            disposition=value.get('disposition'),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return only client-supplied, non-authorizing UI data."""
        return {
            'schema_version': self.schema_version,
            'response_id': self.response_id,
            'speech_session_id': self.speech_session_id,
            'confirmation_request_id': self.confirmation_request_id,
            'disposition': self.disposition,
        }


@dataclass(frozen=True)
class AuthenticatedUIActor:
    """Server-owned UI identity context; the type itself grants no power."""

    user_id: str
    auth_session_id: str
    authentication_method: str

    def __post_init__(self) -> None:
        """Validate one identity supplied by an authenticated adapter."""
        object.__setattr__(self, 'user_id', validate_user_id(self.user_id))
        object.__setattr__(
            self,
            'auth_session_id',
            _identifier(self.auth_session_id, 'auth_session_id'),
        )
        object.__setattr__(
            self,
            'authentication_method',
            _identifier(
                self.authentication_method,
                'authentication_method',
            ),
        )


@dataclass(frozen=True)
class ToolConfirmationResolution:
    """Terminal user-intent record that never grants execution authority."""

    confirmation_result_id: str
    response_id: str
    confirmation_request_id: str
    decision_id: str
    proposal_fingerprint: str
    disposition: str
    code: str
    resolved_at: float
    execution_authorized: bool = False
    consume_once: bool = False
    tool_call_id: None = None
    mission_id: None = None
    schema_version: int = CONFIRMATION_RESOLUTION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Reject any attempt to turn a UX resolution into authority."""
        if self.schema_version != CONFIRMATION_RESOLUTION_SCHEMA_VERSION:
            raise ValidationError(
                'confirmation resolution schema_version is unsupported'
            )
        for name in (
            'confirmation_result_id',
            'response_id',
            'confirmation_request_id',
            'decision_id',
        ):
            object.__setattr__(
                self,
                name,
                _identifier(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            'proposal_fingerprint',
            _digest(self.proposal_fingerprint, 'proposal_fingerprint'),
        )
        if self.disposition not in CONFIRMATION_TERMINAL_DISPOSITIONS:
            raise ValidationError(
                'confirmation resolution disposition is unsupported'
            )
        expected_code = {
            'approve': 'confirmation_approval_recorded_no_execution',
            'deny': 'confirmation_denial_recorded',
            'cancel': 'confirmation_cancelled',
            'expired': 'confirmation_expired',
        }[self.disposition]
        if self.code != expected_code:
            raise ValidationError('confirmation resolution code is invalid')
        object.__setattr__(
            self,
            'resolved_at',
            _timestamp(self.resolved_at, 'resolved_at'),
        )
        if (
            self.execution_authorized is not False
            or self.consume_once is not False
            or self.tool_call_id is not None
            or self.mission_id is not None
        ):
            raise ValidationError(
                'confirmation resolutions cannot authorize execution'
            )

    def to_dict(self) -> Dict[str, Any]:
        """Return the terminal record with explicit non-authority fields."""
        return {
            'schema_version': self.schema_version,
            'confirmation_result_id': self.confirmation_result_id,
            'response_id': self.response_id,
            'confirmation_request_id': self.confirmation_request_id,
            'decision_id': self.decision_id,
            'proposal_fingerprint': self.proposal_fingerprint,
            'disposition': self.disposition,
            'code': self.code,
            'resolved_at': self.resolved_at,
            'execution_authorized': False,
            'consume_once': False,
            'tool_call_id': None,
            'mission_id': None,
        }


def build_confirmation_resolution(
    request: ToolConfirmationRequest,
    response: ToolConfirmationResponseEvent,
    resolved_at: Any,
) -> ToolConfirmationResolution:
    """Record one response, letting server time make expiry dominant."""
    if not isinstance(request, ToolConfirmationRequest):
        raise TypeError('request must be a ToolConfirmationRequest')
    if not isinstance(response, ToolConfirmationResponseEvent):
        raise TypeError('response must be a ToolConfirmationResponseEvent')
    if (
        response.speech_session_id != request.speech_session_id
        or response.conversation_id != request.conversation_id
        or response.confirmation_request_id
        != request.confirmation_request_id
        or response.decision_id != request.decision_id
        or response.proposal_fingerprint
        != request.proposal_fingerprint
    ):
        raise ValidationError(
            'confirmation response does not match request'
        )
    normalized_time = _timestamp(resolved_at, 'resolved_at')
    if normalized_time < request.issued_at:
        raise ValidationError(
            'confirmation response time is invalid'
        )
    disposition = (
        'expired'
        if normalized_time >= request.expires_at
        else response.disposition
    )
    code = {
        'approve': 'confirmation_approval_recorded_no_execution',
        'deny': 'confirmation_denial_recorded',
        'cancel': 'confirmation_cancelled',
        'expired': 'confirmation_expired',
    }[disposition]
    digest = hashlib.sha256(
        (
            'confirmation-result-v1\0'
            f'{request.confirmation_request_id}\0'
            f'{response.response_id}'
        ).encode('utf-8')
    ).hexdigest()[:40]
    return ToolConfirmationResolution(
        confirmation_result_id=f'confirmation-result-{digest}',
        response_id=response.response_id,
        confirmation_request_id=request.confirmation_request_id,
        decision_id=request.decision_id,
        proposal_fingerprint=request.proposal_fingerprint,
        disposition=disposition,
        code=code,
        resolved_at=normalized_time,
    )


def build_monitor_room_confirmation(
    user_id: str,
    speech_session_id: str,
    source_utterance_id: str,
    result: OrchestrationResult,
    target: TargetBinding,
) -> ToolConfirmationRequest:
    """Build a request only from one locally approved proposal."""
    if not isinstance(result, OrchestrationResult):
        raise TypeError('result must be an OrchestrationResult')
    decision = result.decision
    if (
        decision.type != 'tool_call'
        or decision.tool_name != 'monitor_room'
        or not result.safety.allowed
        or not result.state_trusted
    ):
        raise ValidationError(
            'monitor_room confirmation requires an approved proposal'
        )
    if result.conversation_session_instance_id is None:
        raise ValidationError(
            'monitor_room confirmation requires a current conversation'
        )
    if not isinstance(target, TargetBinding):
        raise ValidationError(
            'monitor_room confirmation requires a trusted target'
        )
    message = _monitor_room_confirmation_message(target)
    return ToolConfirmationRequest(
        confirmation_request_id=f'confirm-{result.decision_id}',
        agent_request_id=result.request_id,
        user_id=user_id,
        speech_session_id=speech_session_id,
        source_utterance_id=source_utterance_id,
        conversation_id=result.conversation_id,
        conversation_session_instance_id=(
            result.conversation_session_instance_id
        ),
        conversation_generation=result.conversation_generation,
        conversation_revision=result.conversation_revision,
        conversation_ordinal=result.conversation_ordinal,
        turn_id=result.turn_id,
        decision_id=result.decision_id,
        tool_name=decision.tool_name,
        arguments=decision.arguments,
        message=message,
        issued_at=result.issued_at,
        expires_at=result.expires_at,
        risk_level='L3',
        target=target,
    )


def _monitor_room_confirmation_message(target: TargetBinding) -> str:
    """Render exact server-owned effects for informed consent."""
    effects = target.effects
    if effects.gazebo_simulation_navigation:
        if (
            effects.schema_version
            != GAZEBO_SIMULATION_EFFECTS_SCHEMA_VERSION
        ):
            raise ValidationError(
                'Gazebo simulation effects profile is invalid'
            )
        return (
            f'{target.room_name}에서 Gazebo 시뮬레이션 로봇이 '
            '이동해 방의 주행 지점을 순회합니다. 기존 홈캠 '
            '스트리밍은 계속 실행되며 이 요청으로 시작·중지·'
            '재설정하지 않습니다. 실제 로봇 이동, 카메라 촬영·'
            '제어, 새 영상 전송은 승인하지 않습니다. 최대 '
            f'{effects.max_duration_seconds}초 동안 진행할까요?'
        )
    navigation = (
        '로봇이 이동하고 '
        if effects.physical_navigation
        else '로봇은 이동하지 않고 '
    )
    camera = (
        '카메라 영상을 실시간 전송'
        if effects.external_video_stream
        else '카메라 영상을 외부로 전송하지 않음'
    )
    recording = '사용' if effects.video_recording else '사용 안 함'
    audio = '사용' if effects.audio_capture else '사용 안 함'
    talkback = '사용' if effects.talkback_allowed else '사용 안 함'
    return (
        f'{target.room_name}에서 {navigation}방 전체를 확인하며 '
        f'{camera}할까요? 최대 {effects.max_duration_seconds}초, '
        f'녹화 {recording}, 마이크 {audio}, 말하기 {talkback}입니다.'
    )
