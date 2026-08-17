"""Validated request and response types for the Malbut agent boundary."""

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


MAX_ID_LENGTH = 128
MAX_UTTERANCE_LENGTH = 2000
DECISION_TYPES = {
    'message',
    'tool_call',
    'clarification',
    'refusal',
}


class ValidationError(ValueError):
    """Raised when untrusted input violates an agent boundary schema."""


def _required_string(
    value: Any,
    field_name: str,
    max_length: int,
) -> str:
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


def _identifier_string(value: Any, field_name: str) -> str:
    result = _required_string(value, field_name, MAX_ID_LENGTH)
    if any(
        ord(character) < 32 or ord(character) == 127
        for character in result
    ):
        raise ValidationError(
            f'{field_name} must not contain control characters'
        )
    return result


def validate_user_id(value: Any) -> str:
    """Validate and normalize a user identifier."""
    return _required_string(value, 'user_id', MAX_ID_LENGTH)


def validate_conversation_id(value: Any) -> str:
    """Validate and normalize a conversation identifier."""
    return _identifier_string(value, 'conversation_id')


def validate_turn_id(value: Any) -> str:
    """Validate and normalize a turn identifier."""
    return _identifier_string(value, 'turn_id')


@dataclass(frozen=True)
class RobotState:
    """Small, explicit state snapshot used by the safety policy."""

    battery_percent: Optional[float] = None
    navigation_available: bool = False
    localization_ok: bool = False
    emergency_stop: bool = False
    camera_available: bool = False
    privacy_mode: bool = False
    docked: bool = False
    forbidden_zones: Tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_dict(cls, value: Any) -> 'RobotState':
        """Build state from an untrusted JSON object."""
        if value is None:
            return cls()
        if not isinstance(value, dict):
            raise ValidationError('robot_state must be an object')

        allowed = {
            'battery_percent',
            'navigation_available',
            'localization_ok',
            'emergency_stop',
            'camera_available',
            'privacy_mode',
            'docked',
            'forbidden_zones',
        }
        unknown = set(value) - allowed
        if unknown:
            names = ', '.join(sorted(unknown))
            raise ValidationError(
                f'robot_state contains unknown fields: {names}'
            )

        battery = value.get('battery_percent')
        if battery is not None:
            if isinstance(battery, bool) or not isinstance(
                battery, (int, float)
            ):
                raise ValidationError(
                    'battery_percent must be a number or null'
                )
            battery = float(battery)
            if (
                not math.isfinite(battery)
                or battery < 0
                or battery > 100
            ):
                raise ValidationError(
                    'battery_percent must be between 0 and 100'
                )

        bool_fields = (
            'navigation_available',
            'localization_ok',
            'emergency_stop',
            'camera_available',
            'privacy_mode',
            'docked',
        )
        bool_values: Dict[str, bool] = {}
        for name in bool_fields:
            raw = value.get(name, False)
            if not isinstance(raw, bool):
                raise ValidationError(f'{name} must be a boolean')
            bool_values[name] = raw

        zones = value.get('forbidden_zones', [])
        if not isinstance(zones, list) or not all(
            isinstance(item, str) and item.strip()
            for item in zones
        ):
            raise ValidationError(
                'forbidden_zones must be a list of non-empty strings'
            )
        if len(zones) > 50:
            raise ValidationError('forbidden_zones has too many items')

        return cls(
            battery_percent=battery,
            forbidden_zones=tuple(item.strip() for item in zones),
            **bool_values,
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe state snapshot."""
        return {
            'battery_percent': self.battery_percent,
            'navigation_available': self.navigation_available,
            'localization_ok': self.localization_ok,
            'emergency_stop': self.emergency_stop,
            'camera_available': self.camera_available,
            'privacy_mode': self.privacy_mode,
            'docked': self.docked,
            'forbidden_zones': list(self.forbidden_zones),
        }


@dataclass(frozen=True)
class AgentRequest:
    """One user request plus optional untrusted robot-state context."""

    request_id: str
    user_id: str
    conversation_id: str
    turn_id: str
    utterance: str
    robot_state: RobotState
    available_tools: Tuple[str, ...]
    robot_state_provided: bool = True

    def __post_init__(self) -> None:
        """Keep state presence explicit without treating defaults as facts."""
        if type(self.robot_state_provided) is not bool:
            raise ValidationError('robot_state_provided must be a boolean')

    @classmethod
    def from_dict(cls, value: Any) -> 'AgentRequest':
        """Validate one JSON request object."""
        if not isinstance(value, dict):
            raise ValidationError('request body must be an object')
        allowed = {
            'request_id',
            'user_id',
            'conversation_id',
            'turn_id',
            'utterance',
            'robot_state',
            'available_tools',
        }
        unknown = set(value) - allowed
        if unknown:
            names = ', '.join(sorted(unknown))
            raise ValidationError(f'unknown request fields: {names}')

        request_id = _required_string(
            value.get('request_id'),
            'request_id',
            MAX_ID_LENGTH,
        )
        user_id = validate_user_id(value.get('user_id'))
        conversation_id = validate_conversation_id(
            value.get('conversation_id')
        )
        turn_id = validate_turn_id(value.get('turn_id'))
        utterance = _required_string(
            value.get('utterance'),
            'utterance',
            MAX_UTTERANCE_LENGTH,
        )
        tools = value.get('available_tools', [])
        if not isinstance(tools, list):
            raise ValidationError('available_tools must be a list')
        if len(tools) > 32:
            raise ValidationError('available_tools has too many items')
        normalized_tools: List[str] = []
        for item in tools:
            tool_name = _required_string(
                item,
                'available_tools item',
                64,
            )
            if tool_name not in normalized_tools:
                normalized_tools.append(tool_name)

        robot_state_value = value.get('robot_state')
        return cls(
            request_id=request_id,
            user_id=user_id,
            conversation_id=conversation_id,
            turn_id=turn_id,
            utterance=utterance,
            robot_state=RobotState.from_dict(
                robot_state_value
            ),
            available_tools=tuple(normalized_tools),
            robot_state_provided=(
                'robot_state' in value
                and robot_state_value is not None
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe request."""
        return {
            'request_id': self.request_id,
            'user_id': self.user_id,
            'conversation_id': self.conversation_id,
            'turn_id': self.turn_id,
            'utterance': self.utterance,
            'robot_state': (
                self.robot_state.to_dict()
                if self.robot_state_provided
                else None
            ),
            'available_tools': list(self.available_tools),
        }


@dataclass
class AgentDecision:
    """A high-level, non-actuating decision returned by a model."""

    type: str
    message: str
    tool_name: Optional[str] = None
    arguments: Dict[str, Any] = field(default_factory=dict)
    reason: str = ''
    confidence: Optional[float] = None
    expires_in_ms: int = 5000

    def validate(self) -> None:
        """Validate provider output before any downstream consumer sees it."""
        if not isinstance(self.type, str):
            raise ValidationError('decision type must be a string')
        if self.type not in DECISION_TYPES:
            raise ValidationError(f'unknown decision type: {self.type}')
        if not isinstance(self.message, str):
            raise ValidationError('decision message must be a string')
        if not self.message.strip():
            raise ValidationError('decision message must not be empty')
        if len(self.message) > MAX_UTTERANCE_LENGTH:
            raise ValidationError('decision message is too long')
        if not isinstance(self.arguments, dict):
            raise ValidationError('decision arguments must be an object')
        if not isinstance(self.reason, str) or len(self.reason) > 1000:
            raise ValidationError('decision reason is invalid')
        if self.confidence is not None:
            if isinstance(self.confidence, bool) or not isinstance(
                self.confidence, (int, float)
            ):
                raise ValidationError(
                    'decision confidence must be a number or null'
                )
            if self.confidence < 0 or self.confidence > 1:
                raise ValidationError(
                    'decision confidence must be between 0 and 1'
                )
            if not math.isfinite(float(self.confidence)):
                raise ValidationError(
                    'decision confidence must be finite'
                )
        if isinstance(self.expires_in_ms, bool) or not isinstance(
            self.expires_in_ms, int
        ):
            raise ValidationError('expires_in_ms must be an integer')
        if self.expires_in_ms < 1 or self.expires_in_ms > 60000:
            raise ValidationError(
                'expires_in_ms must be between 1 and 60000'
            )
        if self.type == 'tool_call':
            if not isinstance(self.tool_name, str) or not self.tool_name:
                raise ValidationError(
                    'tool_call decisions require tool_name'
                )
        elif self.tool_name is not None:
            raise ValidationError(
                'only tool_call decisions may contain tool_name'
            )
        if self.type != 'tool_call' and self.arguments:
            raise ValidationError(
                'only tool_call decisions may contain arguments'
            )

    def to_dict(self) -> Dict[str, Any]:
        """Return the public decision fields."""
        self.validate()
        return {
            'type': self.type,
            'message': self.message,
            'tool_name': self.tool_name,
            'arguments': dict(self.arguments),
            'reason': self.reason,
            'confidence': self.confidence,
            'expires_in_ms': self.expires_in_ms,
        }


@dataclass(frozen=True)
class ProviderUsage:
    """Token usage returned by a provider, when available."""

    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None

    def validate(self) -> None:
        """Reject negative, boolean, or inconsistent token counters."""
        for field_name in (
            'input_tokens',
            'output_tokens',
            'total_tokens',
        ):
            value = getattr(self, field_name)
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                raise ValidationError(
                    f'{field_name} must be a non-negative integer or null'
                )
        if (
            self.total_tokens is not None
            and self.input_tokens is not None
            and self.output_tokens is not None
            and self.total_tokens
            < self.input_tokens + self.output_tokens
        ):
            raise ValidationError(
                'total_tokens is smaller than input plus output tokens'
            )

    def to_dict(self) -> Dict[str, Optional[int]]:
        """Return JSON-safe token counters."""
        self.validate()
        return {
            'input_tokens': self.input_tokens,
            'output_tokens': self.output_tokens,
            'total_tokens': self.total_tokens,
        }


@dataclass(frozen=True)
class ContextMetrics:
    """Content-free measurements for one prepared model context."""

    recent_turn_count: int = 0
    recent_included_turn_count: int = 0
    recent_source_chars: int = 0
    recent_included_chars: int = 0
    summary_id: Optional[str] = None
    summary_source_turn_count: int = 0
    summary_source_chars: int = 0
    summary_included_chars: int = 0
    memory_count: int = 0
    memory_included_count: int = 0
    memory_source_chars: int = 0
    memory_included_chars: int = 0
    current_utterance_source_chars: int = 0
    current_utterance_included_chars: int = 0
    model_input_chars: int = 0
    max_model_input_chars: int = 0
    truncated_sections: Tuple[str, ...] = field(
        default_factory=tuple
    )
    overflow_fallback: bool = False

    def to_dict(self) -> Dict[str, Any]:
        """Return section sizes without returning conversation content."""
        return {
            'recent_conversation': {
                'turn_count': self.recent_turn_count,
                'included_turn_count': (
                    self.recent_included_turn_count
                ),
                'source_chars': self.recent_source_chars,
                'included_chars': self.recent_included_chars,
            },
            'conversation_summary': {
                'summary_id': self.summary_id,
                'source_turn_count': (
                    self.summary_source_turn_count
                ),
                'source_chars': self.summary_source_chars,
                'included_chars': self.summary_included_chars,
            },
            'long_term_memory': {
                'record_count': self.memory_count,
                'included_record_count': (
                    self.memory_included_count
                ),
                'source_chars': self.memory_source_chars,
                'included_chars': self.memory_included_chars,
            },
            'current_utterance': {
                'source_chars': (
                    self.current_utterance_source_chars
                ),
                'included_chars': (
                    self.current_utterance_included_chars
                ),
            },
            'model_input': {
                'chars': self.model_input_chars,
                'max_chars': self.max_model_input_chars,
                'truncated': bool(self.truncated_sections),
                'truncated_sections': list(
                    self.truncated_sections
                ),
                'overflow_fallback': self.overflow_fallback,
            },
        }

    @classmethod
    def from_dict(cls, value: Any) -> 'ContextMetrics':
        """Reconstruct persisted metrics from the public nested form."""
        if not isinstance(value, dict):
            raise ValidationError(
                'context metrics must be an object'
            )
        recent = value.get('recent_conversation', {})
        summary = value.get('conversation_summary', {})
        memory = value.get('long_term_memory', {})
        utterance = value.get('current_utterance', {})
        model_input = value.get('model_input', {})
        named_sections = {
            'recent_conversation': recent,
            'conversation_summary': summary,
            'long_term_memory': memory,
            'current_utterance': utterance,
            'model_input': model_input,
        }
        for name, section in named_sections.items():
            if not isinstance(section, dict):
                raise ValidationError(
                    f'{name} context metric must be an object'
                )
        sections = model_input.get('truncated_sections', [])
        if not isinstance(sections, list):
            raise ValidationError(
                'truncated_sections must be a list'
            )
        if not all(
            isinstance(section, str)
            for section in sections
        ):
            raise ValidationError(
                'truncated_sections must contain strings'
            )
        summary_id = summary.get('summary_id')
        if summary_id is not None and not isinstance(
            summary_id,
            str,
        ):
            raise ValidationError(
                'summary_id must be a string or null'
            )
        overflow_fallback = model_input.get(
            'overflow_fallback',
            False,
        )
        if not isinstance(overflow_fallback, bool):
            raise ValidationError(
                'overflow_fallback must be a boolean'
            )

        def count(section: Dict[str, Any], key: str) -> int:
            raw = section.get(key, 0)
            if (
                isinstance(raw, bool)
                or not isinstance(raw, int)
                or raw < 0
            ):
                raise ValidationError(
                    f'{key} context metric must be non-negative'
                )
            return raw

        return cls(
            recent_turn_count=count(recent, 'turn_count'),
            recent_included_turn_count=count(
                recent,
                'included_turn_count',
            ),
            recent_source_chars=count(
                recent,
                'source_chars',
            ),
            recent_included_chars=count(
                recent,
                'included_chars',
            ),
            summary_id=summary_id,
            summary_source_turn_count=count(
                summary,
                'source_turn_count',
            ),
            summary_source_chars=count(
                summary,
                'source_chars',
            ),
            summary_included_chars=count(
                summary,
                'included_chars',
            ),
            memory_count=count(memory, 'record_count'),
            memory_included_count=count(
                memory,
                'included_record_count',
            ),
            memory_source_chars=count(
                memory,
                'source_chars',
            ),
            memory_included_chars=count(
                memory,
                'included_chars',
            ),
            current_utterance_source_chars=count(
                utterance,
                'source_chars',
            ),
            current_utterance_included_chars=count(
                utterance,
                'included_chars',
            ),
            model_input_chars=count(model_input, 'chars'),
            max_model_input_chars=count(
                model_input,
                'max_chars',
            ),
            truncated_sections=tuple(sections),
            overflow_fallback=overflow_fallback,
        )


@dataclass
class ProviderResult:
    """Normalized output from any local or remote provider."""

    decision: AgentDecision
    provider: str
    model: str
    latency_ms: float
    usage: ProviderUsage = field(default_factory=ProviderUsage)
    response_id: Optional[str] = None
    input_chars: Optional[int] = None
    context_metrics: Optional[ContextMetrics] = None

    def validate(self) -> None:
        """Validate content-free provider metadata and its decision."""
        self.decision.validate()
        for field_name in ('provider', 'model'):
            value = getattr(self, field_name)
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 128
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in value
                )
            ):
                raise ValidationError(
                    f'provider result {field_name} is invalid'
                )
        if (
            isinstance(self.latency_ms, bool)
            or not isinstance(self.latency_ms, (int, float))
            or not math.isfinite(float(self.latency_ms))
            or self.latency_ms < 0
        ):
            raise ValidationError(
                'provider result latency_ms is invalid'
            )
        self.usage.validate()
        if self.response_id is not None:
            if (
                not isinstance(self.response_id, str)
                or not self.response_id
                or len(self.response_id) > 256
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in self.response_id
                )
            ):
                raise ValidationError(
                    'provider result response_id is invalid'
                )
        if self.input_chars is not None:
            if (
                isinstance(self.input_chars, bool)
                or not isinstance(self.input_chars, int)
                or self.input_chars < 0
            ):
                raise ValidationError(
                    'provider result input_chars is invalid'
                )

    def to_dict(self) -> Dict[str, Any]:
        """Return provider metadata without credentials or raw prompts."""
        self.validate()
        return {
            'provider': self.provider,
            'model': self.model,
            'latency_ms': round(self.latency_ms, 3),
            'usage': self.usage.to_dict(),
            'response_id': self.response_id,
            'input_chars': self.input_chars,
            'context': (
                self.context_metrics.to_dict()
                if self.context_metrics is not None
                else None
            ),
        }
