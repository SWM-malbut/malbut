"""Conversation, memory, provider, and deterministic safety orchestration."""

import copy
import hashlib
import json
import threading
import time
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    ContextManager,
    Dict,
    Iterator,
    List,
    Optional,
    Sequence,
    Tuple,
    Union,
)

from malbut_agent_server.conversation import (
    BeginTurnToken,
    ConfirmationIntentDraft,
    ConversationSummary,
    ConversationTurn,
    SQLiteConversationStore,
)
from malbut_agent_server.gateway import (
    CapabilityRegistry,
    SIMULATION,
    production_registry,
)
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.providers.base import (
    AgentProvider,
    ProviderError,
)
from malbut_agent_server.robot_state import (
    GAZEBO_SIMULATION_ADMISSION_PROFILE,
    GazeboSimulationAdmissionEvidence,
    ServerGazeboSimulationAdmissionSource,
    TrustedRobotStateError,
    TrustedRobotStateEvidence,
    TrustedRobotStateSource,
    trusted_boottime_ns,
)
from malbut_agent_server.safety import (
    ROBOT_STATE_PROFILE_GAZEBO_SIMULATION,
    SafetyPolicy,
    SafetyResult,
)
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ContextMetrics,
    ProviderResult,
    ProviderUsage,
    ValidationError,
    validate_user_id,
)
from malbut_agent_server.trusted_results import TrustedToolResult


class ExpiredDecisionError(ValidationError):
    """Raised when an action request ID refers to an expired decision."""


class MemoryChangedError(ValidationError):
    """Raised when memory changes while a model request is in flight."""


class OrchestrationCancelledError(ValidationError):
    """Raised when a trusted caller cancels before durable completion."""


ROBOT_STATE_EVIDENCE_SCOPE_MONITOR_ROOM = 'monitor_room'
ROBOT_STATE_EVIDENCE_SCHEMA_VERSION = 1
MAX_STATE_EVIDENCE_SEQUENCE = (1 << 64) - 1


def _evidence_identifier(value: Any, field_name: str) -> str:
    """Validate one bounded persisted state-evidence identifier."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(
            ord(character) < 33 or ord(character) > 126
            for character in value
        )
    ):
        raise ValueError(f'{field_name} is invalid')
    return value


def _evidence_digest(value: Any) -> str:
    """Validate one lowercase SHA-256 evidence digest."""
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(character not in '0123456789abcdef' for character in value)
    ):
        raise ValueError('evidence_digest is invalid')
    return value


def _evidence_integer(
    value: Any,
    field_name: str,
    minimum: int = 0,
) -> int:
    """Validate one bounded exact integer from persisted metadata."""
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value < minimum
        or value > MAX_STATE_EVIDENCE_SEQUENCE
    ):
        raise ValueError(f'{field_name} is invalid')
    return value


def _state_error_code(value: Any) -> str:
    """Keep source failures machine-readable without leaking raw details."""
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or not value.isascii()
        or any(
            not (
                character.islower()
                or character.isdigit()
                or character == '_'
            )
            for character in value
        )
    ):
        return 'robot_state_source_failure'
    return value


@dataclass(frozen=True)
class RobotStateEvidenceBinding:
    """Content-minimized binding to one verified ROS state snapshot."""

    evidence_digest: str
    device_id: str
    map_id: str
    map_revision: str
    host_boot_id: str
    instance_id: str
    sequence: int
    assembled_boottime_ns: int
    valid_until_boottime_ns: int
    scope: str = ROBOT_STATE_EVIDENCE_SCOPE_MONITOR_ROOM
    schema_version: int = ROBOT_STATE_EVIDENCE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        """Reject malformed or internally inconsistent metadata."""
        if (
            type(self.schema_version) is not int
            or self.schema_version
            != ROBOT_STATE_EVIDENCE_SCHEMA_VERSION
        ):
            raise ValueError('state evidence schema is unsupported')
        if (
            type(self.scope) is not str
            or self.scope != ROBOT_STATE_EVIDENCE_SCOPE_MONITOR_ROOM
        ):
            raise ValueError('state evidence scope is unsupported')
        object.__setattr__(
            self,
            'evidence_digest',
            _evidence_digest(self.evidence_digest),
        )
        for name in (
            'device_id',
            'map_id',
            'map_revision',
            'host_boot_id',
            'instance_id',
        ):
            object.__setattr__(
                self,
                name,
                _evidence_identifier(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            'sequence',
            _evidence_integer(self.sequence, 'sequence', 0),
        )
        assembled = _evidence_integer(
            self.assembled_boottime_ns,
            'assembled_boottime_ns',
        )
        valid_until = _evidence_integer(
            self.valid_until_boottime_ns,
            'valid_until_boottime_ns',
        )
        if valid_until <= assembled:
            raise ValueError('state evidence validity interval is invalid')
        object.__setattr__(self, 'assembled_boottime_ns', assembled)
        object.__setattr__(self, 'valid_until_boottime_ns', valid_until)

    @classmethod
    def from_evidence(
        cls,
        evidence: TrustedRobotStateEvidence,
    ) -> 'RobotStateEvidenceBinding':
        """Copy only audit metadata from one validated state object."""
        if not isinstance(evidence, TrustedRobotStateEvidence):
            raise TypeError(
                'robot state source returned an invalid evidence type'
            )
        return cls(
            evidence_digest=evidence.evidence_digest,
            device_id=evidence.device_id,
            map_id=evidence.map_id,
            map_revision=evidence.map_revision,
            host_boot_id=evidence.host_boot_id,
            instance_id=evidence.instance_id,
            sequence=evidence.sequence,
            assembled_boottime_ns=evidence.assembled_boottime_ns,
            valid_until_boottime_ns=evidence.valid_until_boottime_ns,
        )

    @classmethod
    def from_dict(cls, value: Any) -> 'RobotStateEvidenceBinding':
        """Reconstruct one exact persisted evidence binding."""
        if not isinstance(value, dict):
            raise ValueError('state evidence binding must be an object')
        fields = {
            'schema_version',
            'scope',
            'evidence_digest',
            'device_id',
            'map_id',
            'map_revision',
            'host_boot_id',
            'instance_id',
            'sequence',
            'assembled_boottime_ns',
            'valid_until_boottime_ns',
        }
        if set(value) != fields:
            raise ValueError('state evidence binding fields are invalid')
        return cls(**value)

    def is_current(self, now_boottime_ns: Optional[int] = None) -> bool:
        """Return whether the bounded state snapshot is still current."""
        try:
            now = (
                trusted_boottime_ns()
                if now_boottime_ns is None
                else now_boottime_ns
            )
        except (
            OSError,
            OverflowError,
            RuntimeError,
            TrustedRobotStateError,
            ValueError,
        ):
            return False
        if isinstance(now, bool) or not isinstance(now, int):
            return False
        return self.assembled_boottime_ns <= now < (
            self.valid_until_boottime_ns
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the exact content-minimized persistence value."""
        return {
            'schema_version': self.schema_version,
            'scope': self.scope,
            'evidence_digest': self.evidence_digest,
            'device_id': self.device_id,
            'map_id': self.map_id,
            'map_revision': self.map_revision,
            'host_boot_id': self.host_boot_id,
            'instance_id': self.instance_id,
            'sequence': self.sequence,
            'assembled_boottime_ns': self.assembled_boottime_ns,
            'valid_until_boottime_ns': self.valid_until_boottime_ns,
        }


@dataclass(frozen=True)
class GazeboSimulationEvidenceBinding:
    """Persist only the fixed Gazebo admission identity and freshness."""

    evidence_digest: str
    user_id: str
    device_id: str
    map_id: str
    map_revision: str
    host_boot_id: str
    instance_id: str
    sequence: int
    assembled_boottime_ns: int
    valid_until_boottime_ns: int
    semantic_content_sha256: str
    zones_digest: str
    semantic_map_generation: int
    semantic_authorization_generation: int
    semantic_expires_at_ms: int
    room_id: str
    geometry_digest: str
    source_arguments_digest: str
    target_binding_digest: str
    effects_digest: str
    scope: str = ROBOT_STATE_EVIDENCE_SCOPE_MONITOR_ROOM
    profile: str = GAZEBO_SIMULATION_ADMISSION_PROFILE
    schema_version: int = 1

    def __post_init__(self) -> None:
        """Reject a partial or stronger persisted simulation claim."""
        if (
            type(self.schema_version) is not int
            or self.schema_version != 1
            or self.scope != ROBOT_STATE_EVIDENCE_SCOPE_MONITOR_ROOM
            or self.profile != GAZEBO_SIMULATION_ADMISSION_PROFILE
        ):
            raise ValueError('Gazebo state evidence profile is unsupported')
        object.__setattr__(self, 'user_id', validate_user_id(self.user_id))
        for name in (
            'device_id',
            'map_id',
            'map_revision',
            'host_boot_id',
            'instance_id',
            'room_id',
        ):
            object.__setattr__(
                self,
                name,
                _evidence_identifier(getattr(self, name), name),
            )
        for name in (
            'evidence_digest',
            'semantic_content_sha256',
            'zones_digest',
            'geometry_digest',
            'source_arguments_digest',
            'target_binding_digest',
            'effects_digest',
        ):
            object.__setattr__(
                self,
                name,
                _evidence_digest(getattr(self, name)),
            )
        for name, minimum in (
            ('sequence', 0),
            ('assembled_boottime_ns', 1),
            ('valid_until_boottime_ns', 1),
            ('semantic_map_generation', 1),
            ('semantic_authorization_generation', 1),
            ('semantic_expires_at_ms', 1),
        ):
            object.__setattr__(
                self,
                name,
                _evidence_integer(getattr(self, name), name, minimum),
            )
        if self.valid_until_boottime_ns <= self.assembled_boottime_ns:
            raise ValueError('Gazebo evidence validity interval is invalid')

    @classmethod
    def from_evidence(
        cls,
        evidence: GazeboSimulationAdmissionEvidence,
    ) -> 'GazeboSimulationEvidenceBinding':
        """Copy only content-minimized server-issued evidence fields."""
        if type(evidence) is not GazeboSimulationAdmissionEvidence:
            raise TypeError('Gazebo admission evidence type is invalid')
        return cls(
            evidence_digest=evidence.evidence_digest,
            user_id=evidence.user_id,
            device_id=evidence.device_id,
            map_id=evidence.map_id,
            map_revision=evidence.map_revision,
            host_boot_id=evidence.host_boot_id,
            instance_id=evidence.instance_id,
            sequence=evidence.sequence,
            assembled_boottime_ns=evidence.assembled_boottime_ns,
            valid_until_boottime_ns=evidence.valid_until_boottime_ns,
            semantic_content_sha256=evidence.semantic_content_sha256,
            zones_digest=evidence.zones_digest,
            semantic_map_generation=evidence.semantic_map_generation,
            semantic_authorization_generation=(
                evidence.semantic_authorization_generation
            ),
            semantic_expires_at_ms=evidence.semantic_expires_at_ms,
            room_id=evidence.room_id,
            geometry_digest=evidence.geometry_digest,
            source_arguments_digest=evidence.source_arguments_digest,
            target_binding_digest=evidence.target_binding_digest,
            effects_digest=evidence.effects_digest,
        )

    @classmethod
    def from_dict(cls, value: Any) -> 'GazeboSimulationEvidenceBinding':
        """Reconstruct one exact persisted Gazebo binding."""
        if not isinstance(value, dict):
            raise ValueError('Gazebo evidence binding must be an object')
        fields = {
            'schema_version', 'scope', 'profile', 'evidence_digest',
            'user_id', 'device_id', 'map_id', 'map_revision',
            'host_boot_id', 'instance_id', 'sequence',
            'assembled_boottime_ns', 'valid_until_boottime_ns',
            'semantic_content_sha256', 'zones_digest',
            'semantic_map_generation',
            'semantic_authorization_generation',
            'semantic_expires_at_ms', 'room_id', 'geometry_digest',
            'source_arguments_digest', 'target_binding_digest',
            'effects_digest',
        }
        if set(value) != fields:
            raise ValueError('Gazebo evidence binding fields are invalid')
        return cls(**value)

    def is_current(self, now_boottime_ns: Optional[int] = None) -> bool:
        """Return whether the short-lived admission remains current."""
        try:
            now = (
                trusted_boottime_ns()
                if now_boottime_ns is None
                else now_boottime_ns
            )
        except (
            OSError,
            OverflowError,
            RuntimeError,
            TrustedRobotStateError,
            ValueError,
        ):
            return False
        if isinstance(now, bool) or not isinstance(now, int):
            return False
        return self.assembled_boottime_ns <= now < (
            self.valid_until_boottime_ns
        )

    def to_dict(self) -> Dict[str, Any]:
        """Return the exact private persistence value."""
        return {
            'schema_version': self.schema_version,
            'scope': self.scope,
            'profile': self.profile,
            'evidence_digest': self.evidence_digest,
            'user_id': self.user_id,
            'device_id': self.device_id,
            'map_id': self.map_id,
            'map_revision': self.map_revision,
            'host_boot_id': self.host_boot_id,
            'instance_id': self.instance_id,
            'sequence': self.sequence,
            'assembled_boottime_ns': self.assembled_boottime_ns,
            'valid_until_boottime_ns': self.valid_until_boottime_ns,
            'semantic_content_sha256': self.semantic_content_sha256,
            'zones_digest': self.zones_digest,
            'semantic_map_generation': self.semantic_map_generation,
            'semantic_authorization_generation': (
                self.semantic_authorization_generation
            ),
            'semantic_expires_at_ms': self.semantic_expires_at_ms,
            'room_id': self.room_id,
            'geometry_digest': self.geometry_digest,
            'source_arguments_digest': self.source_arguments_digest,
            'target_binding_digest': self.target_binding_digest,
            'effects_digest': self.effects_digest,
        }


StateEvidenceBinding = Union[
    RobotStateEvidenceBinding,
    GazeboSimulationEvidenceBinding,
]


@dataclass
class _ConversationLockEntry:
    """One process-local conversation lock with waiter accounting."""

    lock: threading.RLock
    references: int = 0


@dataclass
class OrchestrationResult:
    """Auditable provider proposal and final locally checked decision."""

    request_id: str
    conversation_id: str
    conversation_session_instance_id: Optional[str]
    turn_id: str
    conversation_generation: int
    conversation_revision: int
    conversation_ordinal: int
    raw_decision: AgentDecision
    decision: AgentDecision
    safety: SafetyResult
    provider_result: ProviderResult
    memory_ids: List[str]
    decision_id: str
    issued_at: float
    expires_at: float
    state_trusted: bool
    memory_revision: int
    state_evidence: Optional[StateEvidenceBinding] = None
    state_evidence_runtime_verified: bool = field(
        default=False,
        repr=False,
    )
    test_only_request_state_trusted: bool = field(
        default=False,
        repr=False,
    )

    def current_state_trusted(self) -> bool:
        """Return current request-scoped trust, never historical trust."""
        if not self.state_trusted:
            return False
        if self.test_only_request_state_trusted:
            return True
        return (
            self.state_evidence_runtime_verified
            and self.state_evidence is not None
            and self.state_evidence.is_current()
        )

    def current_monitor_room_evidence(
        self,
    ) -> Optional[StateEvidenceBinding]:
        """Return only fresh evidence scoped to monitor_room."""
        evidence = self.state_evidence
        if (
            not self.current_state_trusted()
            or evidence is None
            or evidence.scope != ROBOT_STATE_EVIDENCE_SCOPE_MONITOR_ROOM
        ):
            return None
        return evidence

    def to_dict(
        self,
        include_raw_decision: bool = False,
    ) -> Dict[str, Any]:
        """Return the stable HTTP response contract."""
        decision_is_fresh = time.time() < self.expires_at
        state_trusted = self.current_state_trusted()
        scope_matches = (
            self.test_only_request_state_trusted
            or (
                self.state_evidence is not None
                and self.state_evidence.scope == self.decision.tool_name
            )
        )
        proposal_authorized = (
            state_trusted
            and scope_matches
            and self.safety.allowed
            and self.decision.type == 'tool_call'
            and decision_is_fresh
        )
        result = {
            'request_id': self.request_id,
            'conversation': {
                'conversation_id': self.conversation_id,
                'session_instance_id': (
                    self.conversation_session_instance_id
                ),
                'turn_id': self.turn_id,
                'generation': self.conversation_generation,
                'revision': self.conversation_revision,
                'ordinal': self.conversation_ordinal,
            },
            'decision': self.decision.to_dict(),
            'safety': self.safety.to_dict(),
            'provider': self.provider_result.to_dict(),
            'memory': {
                'retrieved_count': len(self.memory_ids),
                'ids': list(self.memory_ids),
            },
            'execution': {
                'decision_id': self.decision_id,
                'issued_at': self.issued_at,
                'expires_at': self.expires_at,
                # A policy-approved model proposal is still not an
                # executable SWM25-74 authorization.
                'authorized': False,
                'proposal_authorized': proposal_authorized,
                'state_trusted': state_trusted,
                'state_evidence_scope': (
                    self.state_evidence.scope
                    if self.state_evidence is not None
                    else (
                        'test_only_request'
                        if self.test_only_request_state_trusted
                        else None
                    )
                ),
                'state_evidence': (
                    {
                        'scope': self.state_evidence.scope,
                        'evidence_digest': (
                            self.state_evidence.evidence_digest
                        ),
                        'current': state_trusted,
                    }
                    if self.state_evidence is not None
                    else None
                ),
                'fresh': decision_is_fresh,
                'consume_once': False,
                'tool_call_id': None,
            },
        }
        if include_raw_decision:
            result['raw_decision'] = self.raw_decision.to_dict()
        return result

    def to_persisted_dict(self) -> Dict[str, Any]:
        """Persist the final safe response and required metadata."""
        return {
            'schema_version': (
                4
                if isinstance(
                    self.state_evidence,
                    GazeboSimulationEvidenceBinding,
                )
                else 3
            ),
            'public': self.to_dict(include_raw_decision=False),
            'memory_revision': self.memory_revision,
            'state_evidence': (
                self.state_evidence.to_dict()
                if self.state_evidence is not None
                else None
            ),
        }

    @classmethod
    def from_persisted_dict(
        cls,
        value: Dict[str, Any],
    ) -> 'OrchestrationResult':
        """Reconstruct an idempotent response without another model call."""
        try:
            schema_version = value.get('schema_version')
            if schema_version not in {1, 2, 3, 4}:
                raise ValueError('unsupported persisted response schema')
            public = value['public']
            conversation = public['conversation']
            decision = cls._decision_from_dict(public['decision'])
            safety_value = public['safety']
            provider_value = public['provider']
            usage_value = provider_value['usage']
            execution = public['execution']
            memory = public['memory']
            provider_result = ProviderResult(
                decision=decision,
                provider=str(provider_value['provider']),
                model=str(provider_value['model']),
                latency_ms=float(provider_value['latency_ms']),
                usage=ProviderUsage(
                    input_tokens=usage_value.get('input_tokens'),
                    output_tokens=usage_value.get('output_tokens'),
                    total_tokens=usage_value.get('total_tokens'),
                ),
                response_id=provider_value.get('response_id'),
                input_chars=provider_value.get('input_chars'),
                context_metrics=(
                    ContextMetrics.from_dict(
                        provider_value['context']
                    )
                    if provider_value.get('context') is not None
                    else None
                ),
            )
            state_evidence_value = value.get('state_evidence')
            if state_evidence_value is None:
                state_evidence = None
            elif schema_version == 4:
                state_evidence = GazeboSimulationEvidenceBinding.from_dict(
                    state_evidence_value
                )
            else:
                state_evidence = RobotStateEvidenceBinding.from_dict(
                    state_evidence_value
                )
            if schema_version == 4 and not isinstance(
                state_evidence,
                GazeboSimulationEvidenceBinding,
            ):
                raise ValueError(
                    'Gazebo persisted response requires simulation evidence'
                )
            stored_state_trusted = execution['state_trusted']
            if type(stored_state_trusted) is not bool:
                raise ValueError('stored state trust must be a boolean')
            public_evidence = execution.get('state_evidence')
            public_scope = execution.get('state_evidence_scope')
            if state_evidence is None:
                if public_evidence is not None or public_scope not in {
                    None,
                    'test_only_request',
                }:
                    raise ValueError('stored state evidence is inconsistent')
            else:
                if not isinstance(public_evidence, dict):
                    raise ValueError(
                        'stored public state evidence is invalid'
                    )
                if set(public_evidence) != {
                    'scope',
                    'evidence_digest',
                    'current',
                }:
                    raise ValueError(
                        'stored public state evidence is invalid'
                    )
                if (
                    public_evidence['scope'] != state_evidence.scope
                    or public_evidence['evidence_digest']
                    != state_evidence.evidence_digest
                    or type(public_evidence['current']) is not bool
                    or public_scope != state_evidence.scope
                ):
                    raise ValueError(
                        'stored state evidence is inconsistent'
                    )
            return cls(
                request_id=str(public['request_id']),
                conversation_id=str(
                    conversation['conversation_id']
                ),
                conversation_session_instance_id=(
                    str(conversation['session_instance_id'])
                    if schema_version in {3, 4}
                    else None
                ),
                turn_id=str(conversation['turn_id']),
                conversation_generation=int(
                    conversation['generation']
                ),
                conversation_revision=int(
                    conversation['revision']
                ),
                conversation_ordinal=int(
                    conversation['ordinal']
                ),
                raw_decision=decision,
                decision=decision,
                safety=SafetyResult(
                    allowed=bool(safety_value['allowed']),
                    code=str(safety_value['code']),
                    reason=str(safety_value['reason']),
                ),
                provider_result=provider_result,
                memory_ids=[
                    str(memory_id)
                    for memory_id in memory['ids']
                ],
                decision_id=str(execution['decision_id']),
                issued_at=float(execution['issued_at']),
                expires_at=float(execution['expires_at']),
                state_trusted=stored_state_trusted,
                memory_revision=int(value['memory_revision']),
                state_evidence=state_evidence,
                # A persisted snapshot is historical until the configured
                # source is read again in this process.
                state_evidence_runtime_verified=False,
                test_only_request_state_trusted=False,
            )
        except (
            KeyError,
            TypeError,
            ValueError,
        ) as error:
            raise RuntimeError(
                'stored orchestration response is invalid'
            ) from error

    @staticmethod
    def _decision_from_dict(value: Dict[str, Any]) -> AgentDecision:
        decision = AgentDecision(
            type=value['type'],
            message=value['message'],
            tool_name=value.get('tool_name'),
            arguments=dict(value.get('arguments', {})),
            reason=value.get('reason', ''),
            confidence=value.get('confidence'),
            expires_in_ms=value.get('expires_in_ms', 5000),
        )
        decision.validate()
        return decision


class AgentOrchestrator:
    """Keep model selection separate from conversation and authorization."""

    def __init__(
        self,
        provider: AgentProvider,
        memory_store: SQLiteMemoryStore,
        conversation_store: SQLiteConversationStore,
        safety_policy: SafetyPolicy,
        memory_limit: int = 5,
        trusted_robot_state_source: Optional[
            TrustedRobotStateSource
        ] = None,
        test_only_trusted_robot_state: bool = False,
        capability_registry: CapabilityRegistry | None = None,
        trusted_robot_state: Optional[bool] = None,
        gazebo_simulation_admission_source: Optional[
            ServerGazeboSimulationAdmissionSource
        ] = None,
    ) -> None:
        """Initialize provider, memory, session, and safety services."""
        if memory_limit < 1 or memory_limit > 10:
            raise ValueError('memory_limit must be between 1 and 10')
        chosen_registry = capability_registry or production_registry()
        if not isinstance(chosen_registry, CapabilityRegistry):
            raise TypeError('capability_registry is invalid')
        if (
            trusted_robot_state_source is not None
            and not callable(
                getattr(trusted_robot_state_source, 'read', None)
            )
        ):
            raise TypeError(
                'trusted_robot_state_source must provide read()'
            )
        if trusted_robot_state is not None:
            if not isinstance(trusted_robot_state, bool):
                raise TypeError('trusted_robot_state must be a boolean')
            if (
                test_only_trusted_robot_state
                and not trusted_robot_state
            ):
                raise ValueError(
                    'legacy and explicit test-only trust disagree'
                )
            # Backward compatibility for offline evaluators only.  HTTP
            # construction rejects this mode and production factory code
            # never sets it.
            test_only_trusted_robot_state = trusted_robot_state
        if not isinstance(test_only_trusted_robot_state, bool):
            raise TypeError(
                'test_only_trusted_robot_state must be a boolean'
            )
        if (
            trusted_robot_state_source is not None
            and test_only_trusted_robot_state
        ):
            raise ValueError(
                'trusted state source and test-only request trust are '
                'mutually exclusive'
            )
        if (
            gazebo_simulation_admission_source is not None
            and type(gazebo_simulation_admission_source)
            is not ServerGazeboSimulationAdmissionSource
        ):
            raise TypeError(
                'gazebo_simulation_admission_source is invalid'
            )
        if (
            gazebo_simulation_admission_source is not None
            and chosen_registry.runtime_mode != SIMULATION
        ):
            raise ValueError(
                'Gazebo admission requires simulation Tool runtime mode'
            )
        if gazebo_simulation_admission_source is not None and (
            trusted_robot_state_source is not None
            or test_only_trusted_robot_state
        ):
            raise ValueError(
                'Gazebo and physical/test RobotState trust are mutually '
                'exclusive'
            )
        self.provider = provider
        self.memory_store = memory_store
        self.conversation_store = conversation_store
        self.safety_policy = safety_policy
        self.memory_limit = memory_limit
        self.trusted_robot_state_source = trusted_robot_state_source
        self.gazebo_simulation_admission_source = (
            gazebo_simulation_admission_source
        )
        self.test_only_trusted_robot_state = (
            test_only_trusted_robot_state
        )
        self.capability_registry = chosen_registry
        self._conversation_locks_guard = threading.Lock()
        self._conversation_locks: Dict[
            Tuple[str, str],
            _ConversationLockEntry,
        ] = {}

    def handle(
        self,
        request: AgentRequest,
        *,
        expected_session_instance_id: Optional[str] = None,
        completion_guard: Optional[
            Callable[[], ContextManager[None]]
        ] = None,
        result_completion_guard: Optional[
            Callable[
                [OrchestrationResult],
                ContextManager[Optional[ConfirmationIntentDraft]],
            ]
        ] = None,
    ) -> OrchestrationResult:
        """
        Process one ordered turn with durable idempotency.

        ``expected_session_instance_id`` is trusted adapter context, not a
        client request field.  It fences a caller that was bound to an older
        lifecycle before either a cached response or provider call is used.

        The legacy guard wraps only a new durable commit.  The result-aware
        guard receives both new and cached results so a caller can validate
        delivery state before entry and finalize it before releasing its
        own synchronization fence.  A guard must not raise after its yield:
        once ``complete_turn`` returns, the conversation commit is durable
        and cannot be rolled back by this API.
        """
        if completion_guard is not None and not callable(
            completion_guard
        ):
            raise TypeError('completion_guard must be callable')
        if result_completion_guard is not None and not callable(
            result_completion_guard
        ):
            raise TypeError(
                'result_completion_guard must be callable'
            )
        if (
            completion_guard is not None
            and result_completion_guard is not None
        ):
            raise TypeError(
                'completion guards are mutually exclusive'
            )
        fingerprint = self._request_fingerprint(request)
        with self._conversation_lock(
            request.user_id,
            request.conversation_id,
        ):
            begin = self.conversation_store.begin_turn(
                user_id=request.user_id,
                conversation_id=request.conversation_id,
                turn_id=request.turn_id,
                request_id=request.request_id,
                request_fingerprint=fingerprint,
                user_content=request.utterance,
                expected_session_instance_id=(
                    expected_session_instance_id
                ),
            )
            if begin.cached_response is not None:
                result = OrchestrationResult.from_persisted_dict(
                    begin.cached_response
                )
                if result.conversation_session_instance_id is None:
                    if (
                        begin.session.generation
                        != result.conversation_generation
                        or begin.session.revision
                        != result.conversation_revision
                    ):
                        raise RuntimeError(
                            'stored orchestration response is stale'
                        )
                    result.conversation_session_instance_id = (
                        begin.session.session_instance_id
                    )
                elif (
                    result.conversation_session_instance_id
                    != begin.session.session_instance_id
                ):
                    raise RuntimeError(
                        'stored orchestration response has wrong session'
                    )
                result = self._revalidate_cached_tool_proposal(
                    request,
                    result,
                )
                if result_completion_guard is not None:
                    with result_completion_guard(result) as intent:
                        if intent is not None:
                            register = (
                                self.conversation_store
                                .register_confirmation_intent
                            )
                            register(intent)
                return result
            token = begin.token
            if token is None:
                raise RuntimeError(
                    'conversation begin returned no token'
                )
            committed = False
            try:
                result = self._handle_uncached(
                    request,
                    begin.history,
                    begin.summary,
                    begin.trusted_results,
                    token,
                )
                guard = (
                    result_completion_guard(result)
                    if result_completion_guard is not None
                    else (
                        completion_guard()
                        if completion_guard is not None
                        else nullcontext()
                    )
                )
                with guard as confirmation_intent:
                    session, _turn = (
                        self.conversation_store.complete_turn(
                            token,
                            assistant_content=result.decision.message,
                            response=result.to_persisted_dict(),
                            confirmation_intent=confirmation_intent,
                        )
                    )
                    committed = True
                if (
                    session.session_instance_id
                    != result.conversation_session_instance_id
                    or
                    session.generation
                    != result.conversation_generation
                    or session.revision
                    != result.conversation_revision
                ):
                    raise RuntimeError(
                        'conversation commit metadata did not match'
                    )
                return result
            except Exception:
                if not committed:
                    self.conversation_store.fail_turn(token)
                raise

    @contextmanager
    def _conversation_lock(
        self,
        user_id: str,
        conversation_id: str,
    ) -> Iterator[None]:
        """Serialize one session while allowing independent sessions."""
        key = (user_id, conversation_id)
        with self._conversation_locks_guard:
            entry = self._conversation_locks.get(key)
            if entry is None:
                entry = _ConversationLockEntry(
                    lock=threading.RLock(),
                )
                self._conversation_locks[key] = entry
            entry.references += 1
        acquired = False
        try:
            entry.lock.acquire()
            acquired = True
            yield
        finally:
            if acquired:
                entry.lock.release()
            with self._conversation_locks_guard:
                entry.references -= 1
                if (
                    entry.references == 0
                    and self._conversation_locks.get(key) is entry
                ):
                    del self._conversation_locks[key]

    def _handle_uncached(
        self,
        request: AgentRequest,
        conversation_turns: Sequence[ConversationTurn],
        conversation_summary: ConversationSummary | None,
        trusted_server_tool_results: Sequence[TrustedToolResult],
        token: BeginTurnToken,
    ) -> OrchestrationResult:
        """Call one provider without holding a SQLite transaction."""
        effective_value = request.to_dict()
        effective_value['available_tools'] = (
            self.capability_registry.effective_names(
                request.available_tools
            )
        )
        # Client request state is neither sent to the model nor reused by
        # Safety.  A production Tool proposal must obtain fresh evidence
        # after inference from the fixed local state source below.
        effective_value['robot_state'] = None
        model_request = AgentRequest.from_dict(effective_value)
        memories, memory_revision = (
            self.memory_store.search_with_owner_revision(
                request.user_id,
                request.utterance,
                limit=self.memory_limit,
            )
        )
        memory_snapshot = copy.deepcopy(memories)
        tool_specs = self.capability_registry.select_specs(
            model_request.available_tools
        )
        provider_memories = copy.deepcopy(memory_snapshot)
        provider_turns = copy.deepcopy(list(conversation_turns))
        provider_summary = copy.deepcopy(conversation_summary)
        provider_trusted_results = copy.deepcopy(
            list(trusted_server_tool_results)
        )
        complete_with_context = getattr(
            self.provider,
            'complete_with_context',
            None,
        )
        provider_type = type(self.provider)
        legacy_subclass_override = (
            'complete' in provider_type.__dict__
            and 'complete_with_context' not in provider_type.__dict__
            and getattr(
                provider_type,
                'complete_with_context',
                None,
            ) is not AgentProvider.complete_with_context
        )
        if callable(complete_with_context) and not legacy_subclass_override:
            provider_result = complete_with_context(
                model_request,
                provider_memories,
                provider_turns,
                tool_specs,
                conversation_summary=provider_summary,
                trusted_server_tool_results=provider_trusted_results,
            )
        else:
            provider_result = self.provider.complete(
                model_request,
                provider_memories,
                provider_turns,
                tool_specs,
                conversation_summary=provider_summary,
            )
        try:
            provider_result.validate()
        except (ValidationError, TypeError) as error:
            raise ProviderError(
                'provider returned invalid metadata'
            ) from error
        provider_decision = provider_result.decision
        raw_decision = AgentDecision(
            type=provider_decision.type,
            message=provider_decision.message,
            tool_name=provider_decision.tool_name,
            arguments=copy.deepcopy(provider_decision.arguments),
            reason=provider_decision.reason,
            confidence=provider_decision.confidence,
            expires_in_ms=provider_decision.expires_in_ms,
        )
        provider_result.decision = raw_decision
        if not self.memory_store.owner_snapshot_is_current(
            request.user_id,
            memory_revision,
            memory_snapshot,
        ):
            raise MemoryChangedError(
                'memory changed during model inference; retry the request'
            )
        try:
            raw_decision.validate()
        except (ValidationError, TypeError) as error:
            raise ProviderError(
                'provider returned an invalid decision'
            ) from error
        (
            safety,
            state_evidence,
            state_trusted,
            test_only_state_trusted,
        ) = self._evaluate_with_current_robot_state(
            request,
            effective_value,
            raw_decision,
        )
        decision = raw_decision
        if not safety.allowed:
            decision = AgentDecision(
                type='refusal',
                message=safety.reason,
                reason=f'safety:{safety.code}',
                confidence=1.0,
                expires_in_ms=raw_decision.expires_in_ms,
            )
        issued_at = time.time()
        expires_at = (
            issued_at + decision.expires_in_ms / 1000.0
        )
        memory_expirations = [
            memory.expires_at
            for memory in memory_snapshot
            if memory.expires_at is not None
        ]
        if memory_expirations:
            expires_at = min(
                expires_at,
                min(memory_expirations),
            )
        state_evidence_runtime_verified = state_evidence is not None
        if state_evidence is not None:
            try:
                remaining_state_seconds = max(
                    0.0,
                    (
                        state_evidence.valid_until_boottime_ns
                        - trusted_boottime_ns()
                    )
                    / 1_000_000_000.0,
                )
            except (
                OSError,
                OverflowError,
                RuntimeError,
                TrustedRobotStateError,
                ValueError,
            ):
                safety = SafetyResult(
                    False,
                    'robot_state_clock_unavailable',
                    '신뢰된 로봇 상태의 유효 시간을 확인할 수 없어 '
                    '방 모니터링을 시작하지 않습니다.',
                )
                decision = AgentDecision(
                    type='refusal',
                    message=safety.reason,
                    reason=f'safety:{safety.code}',
                    confidence=1.0,
                    expires_in_ms=raw_decision.expires_in_ms,
                )
                state_trusted = False
                state_evidence_runtime_verified = False
            else:
                expires_at = min(
                    expires_at,
                    issued_at + remaining_state_seconds,
                )
        return OrchestrationResult(
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            conversation_session_instance_id=(
                token.session_instance_id
            ),
            turn_id=request.turn_id,
            conversation_generation=token.generation,
            conversation_revision=token.revision + 1,
            conversation_ordinal=token.ordinal,
            raw_decision=raw_decision,
            decision=decision,
            safety=safety,
            provider_result=provider_result,
            memory_ids=[memory.id for memory in memory_snapshot],
            decision_id=str(uuid.uuid4()),
            issued_at=issued_at,
            expires_at=expires_at,
            state_trusted=state_trusted,
            memory_revision=memory_revision,
            state_evidence=state_evidence,
            state_evidence_runtime_verified=(
                state_evidence_runtime_verified
            ),
            test_only_request_state_trusted=(
                test_only_state_trusted
            ),
        )

    def _evaluate_with_current_robot_state(
        self,
        original_request: AgentRequest,
        effective_value: Dict[str, Any],
        decision: AgentDecision,
    ) -> Tuple[
        SafetyResult,
        Optional[StateEvidenceBinding],
        bool,
        bool,
    ]:
        """Evaluate one proposal with only request-scoped local evidence."""
        if decision.type != 'tool_call':
            neutral_request = AgentRequest.from_dict(effective_value)
            return (
                self.safety_policy.evaluate(
                    neutral_request,
                    decision,
                    state_trusted=False,
                ),
                None,
                False,
                False,
            )

        if self.test_only_trusted_robot_state:
            test_value = dict(effective_value)
            test_value['robot_state'] = original_request.robot_state.to_dict()
            test_request = AgentRequest.from_dict(test_value)
            return (
                self.safety_policy.evaluate(
                    test_request,
                    decision,
                    state_trusted=True,
                ),
                None,
                True,
                True,
            )

        # The first physical scenario is intentionally tool-scoped.  This
        # evidence must never authorize legacy navigate/camera Tools.
        if decision.tool_name != ROBOT_STATE_EVIDENCE_SCOPE_MONITOR_ROOM:
            neutral_request = AgentRequest.from_dict(effective_value)
            return (
                self.safety_policy.evaluate(
                    neutral_request,
                    decision,
                    state_trusted=False,
                ),
                None,
                False,
                False,
            )

        simulation_source = self.gazebo_simulation_admission_source
        if simulation_source is not None:
            error_code = None
            admission = None
            readiness = None
            try:
                admission = simulation_source.issue(
                    user_id=original_request.user_id,
                    location=decision.arguments.get('location'),
                )
                if type(admission) is not GazeboSimulationAdmissionEvidence:
                    raise TypeError('invalid Gazebo admission evidence type')
                state_now = trusted_boottime_ns()
                readiness = admission.require_ready(state_now)
                if not admission.is_current(state_now):
                    raise TrustedRobotStateError('robot_state_stale')
                binding = GazeboSimulationEvidenceBinding.from_evidence(
                    admission
                )
            except TrustedRobotStateError as error:
                error_code = _state_error_code(error.code)
            except Exception:
                error_code = 'robot_state_source_failure'
            if error_code is not None:
                return (
                    SafetyResult(
                        False,
                        error_code,
                        '신뢰된 최신 Gazebo 상태와 방 메타데이터를 '
                        '확인할 수 없어 방 모니터링을 시작하지 않습니다.',
                    ),
                    None,
                    False,
                    False,
                )
            if admission is None or readiness is None:
                raise AssertionError(
                    'Gazebo admission evaluation is incomplete'
                )
            state_value = dict(effective_value)
            # Only these two simulation-readiness facts are admitted.  The
            # other RobotState defaults are not evidence and the dedicated
            # Safety profile is prohibited from consulting them.
            state_value['robot_state'] = {
                'navigation_available': readiness.navigation_available,
                'localization_ok': readiness.localization_ok,
            }
            safety_request = AgentRequest.from_dict(state_value)
            return (
                self.safety_policy.evaluate(
                    safety_request,
                    decision,
                    state_trusted=True,
                    state_profile=(
                        ROBOT_STATE_PROFILE_GAZEBO_SIMULATION
                    ),
                ),
                binding,
                True,
                False,
            )

        source = self.trusted_robot_state_source
        if source is None:
            neutral_request = AgentRequest.from_dict(effective_value)
            return (
                self.safety_policy.evaluate(
                    neutral_request,
                    decision,
                    state_trusted=False,
                ),
                None,
                False,
                False,
            )

        error_code = None
        evidence = None
        trusted_state = None
        try:
            evidence = source.read()
            if not isinstance(evidence, TrustedRobotStateEvidence):
                raise TypeError('invalid trusted state evidence type')
            state_now = trusted_boottime_ns()
            trusted_state = evidence.require_complete_for_monitor_room(
                now_boottime_ns=state_now,
            )
            if not evidence.is_current(now_boottime_ns=state_now):
                raise TrustedRobotStateError(
                    'robot_state_stale',
                )
            binding = RobotStateEvidenceBinding.from_evidence(evidence)
        except TrustedRobotStateError as error:
            error_code = _state_error_code(error.code)
        except Exception:
            error_code = 'robot_state_source_failure'
        if error_code is not None:
            return (
                SafetyResult(
                    False,
                    error_code,
                    '신뢰된 최신 로봇 상태를 확인할 수 없어 '
                    '방 모니터링을 시작하지 않습니다.',
                ),
                None,
                False,
                False,
            )
        if evidence is None or trusted_state is None:
            raise AssertionError('trusted state evaluation is incomplete')
        state_value = dict(effective_value)
        state_value['robot_state'] = trusted_state.to_dict()
        safety_request = AgentRequest.from_dict(state_value)
        return (
            self.safety_policy.evaluate(
                safety_request,
                decision,
                state_trusted=True,
            ),
            binding,
            True,
            False,
        )

    def _revalidate_cached_tool_proposal(
        self,
        request: AgentRequest,
        result: OrchestrationResult,
    ) -> OrchestrationResult:
        """Never replay historical state trust as current authority."""
        original_evidence = result.state_evidence
        result.state_evidence_runtime_verified = False
        result.test_only_request_state_trusted = False
        if (
            result.decision.type == 'tool_call'
            and time.time() >= result.expires_at
        ):
            result.state_trusted = False
            result.safety = SafetyResult(
                False,
                'expired_decision',
                '행동 제안의 유효 시간이 지나 다시 요청해야 합니다.',
            )
            result.decision = AgentDecision(
                type='refusal',
                message=result.safety.reason,
                reason=f'safety:{result.safety.code}',
                confidence=1.0,
                expires_in_ms=result.decision.expires_in_ms,
            )
            return result
        if (
            result.decision.type != 'tool_call'
            or result.decision.tool_name
            != ROBOT_STATE_EVIDENCE_SCOPE_MONITOR_ROOM
        ):
            result.state_trusted = False
            if result.decision.type == 'tool_call':
                result.safety = SafetyResult(
                    False,
                    'untrusted_robot_state',
                    '신뢰된 로컬 ROS 상태가 없어 행동을 실행하지 않습니다.',
                )
                result.decision = AgentDecision(
                    type='refusal',
                    message=result.safety.reason,
                    reason=f'safety:{result.safety.code}',
                    confidence=1.0,
                    expires_in_ms=result.decision.expires_in_ms,
                )
            return result
        effective_value = request.to_dict()
        effective_value['available_tools'] = (
            self.capability_registry.effective_names(
                request.available_tools
            )
        )
        effective_value['robot_state'] = None
        (
            safety,
            evidence,
            state_trusted,
            _test_only,
        ) = self._evaluate_with_current_robot_state(
            request,
            effective_value,
            result.decision,
        )
        result.safety = safety
        if (
            original_evidence is None
            or evidence is None
            or evidence != original_evidence
        ):
            # Idempotent replay may re-verify the exact same short-lived
            # snapshot, but it must not silently migrate an old decision to
            # another sequence/map/state.  Retaining the persisted binding
            # also prevents A -> B -> A resurrection via a new sequence.
            result.state_evidence = original_evidence
            result.state_trusted = False
            result.state_evidence_runtime_verified = False
            if safety.allowed:
                result.safety = SafetyResult(
                    False,
                    'robot_state_evidence_changed',
                    '로봇 상태가 달라져 행동을 다시 요청해야 합니다.',
                )
        else:
            result.state_evidence = original_evidence
            result.state_trusted = state_trusted
            result.state_evidence_runtime_verified = state_trusted
        if not safety.allowed:
            result.decision = AgentDecision(
                type='refusal',
                message=result.safety.reason,
                reason=f'safety:{result.safety.code}',
                confidence=1.0,
                expires_in_ms=result.decision.expires_in_ms,
            )
        elif not result.safety.allowed:
            result.decision = AgentDecision(
                type='refusal',
                message=result.safety.reason,
                reason=f'safety:{result.safety.code}',
                confidence=1.0,
                expires_in_ms=result.decision.expires_in_ms,
            )
        return result

    @staticmethod
    def _request_fingerprint(request: AgentRequest) -> str:
        encoded = json.dumps(
            request.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()
