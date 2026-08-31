"""Conversation, memory, provider, and deterministic safety orchestration."""

import copy
import hashlib
import json
import math
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Sequence

from malbut_agent_server.conversation import (
    BeginTurnToken,
    ConversationSummary,
    ConversationTurn,
    SQLiteConversationStore,
)
from malbut_agent_server.gateway import (
    CapabilityRegistry,
    production_registry,
)
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.providers.base import (
    AgentProvider,
    ProviderError,
)
from malbut_agent_server.robot_state_source import RobotStateSource
from malbut_agent_server.safety import SafetyPolicy, SafetyResult
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ContextMetrics,
    ProviderResult,
    ProviderUsage,
    RobotState,
    ValidationError,
)


class ExpiredDecisionError(ValidationError):
    """Raised when an action request ID refers to an expired decision."""


class MemoryChangedError(ValidationError):
    """Raised when memory changes while a model request is in flight."""


@dataclass
class OrchestrationResult:
    """Auditable provider proposal and final locally checked decision."""

    request_id: str
    conversation_id: str
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
    state_evidence_id: str | None = None
    state_observed_at: float | None = None
    safety_policy_revision: str | None = None
    clock: Callable[[], float] = field(
        default=time.time,
        repr=False,
        compare=False,
    )

    def to_dict(
        self,
        include_raw_decision: bool = False,
    ) -> Dict[str, Any]:
        """Return the stable HTTP response contract."""
        now = float(self.clock())
        if not math.isfinite(now):
            raise RuntimeError('orchestration clock is invalid')
        decision_is_fresh = now < self.expires_at
        proposal_authorized = (
            self.state_trusted
            and self.safety.allowed
            and self.decision.type == 'tool_call'
            and decision_is_fresh
        )
        result = {
            'request_id': self.request_id,
            'conversation': {
                'conversation_id': self.conversation_id,
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
                'state_trusted': self.state_trusted,
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
        value = {
            'schema_version': 2,
            'public': self.to_dict(include_raw_decision=False),
            'memory_revision': self.memory_revision,
        }
        provenance = (
            self.state_evidence_id,
            self.state_observed_at,
            self.safety_policy_revision,
        )
        if all(item is None for item in provenance):
            return value
        if any(item is None for item in provenance):
            raise RuntimeError('safety provenance is incomplete')
        if (
            not math.isfinite(float(self.issued_at))
            or not math.isfinite(float(self.expires_at))
            or not math.isfinite(float(self.state_observed_at))
            or self.expires_at <= self.issued_at
            or self.state_observed_at > self.issued_at
        ):
            raise RuntimeError('safety provenance timing is invalid')
        value['schema_version'] = 3
        value['safety_binding'] = {
            'state_evidence_id': self.state_evidence_id,
            'state_observed_at': self.state_observed_at,
            'safety_policy_revision': self.safety_policy_revision,
        }
        return value

    @classmethod
    def from_persisted_dict(
        cls,
        value: Dict[str, Any],
    ) -> 'OrchestrationResult':
        """Reconstruct an idempotent response without another model call."""
        try:
            schema_version = value.get('schema_version')
            if schema_version not in {1, 2, 3}:
                raise ValueError('unsupported persisted response schema')
            state_evidence_id = None
            state_observed_at = None
            safety_policy_revision = None
            if schema_version == 3:
                if frozenset(value) != frozenset({
                    'schema_version',
                    'public',
                    'memory_revision',
                    'safety_binding',
                }):
                    raise ValueError('invalid persisted response shape')
                binding = value['safety_binding']
                if type(binding) is not dict or frozenset(binding) != (
                    frozenset({
                        'state_evidence_id',
                        'state_observed_at',
                        'safety_policy_revision',
                    })
                ):
                    raise ValueError('invalid safety binding shape')
                state_evidence_id = cls._private_identifier(
                    binding['state_evidence_id'],
                    'state_evidence_id',
                )
                if type(binding['state_observed_at']) not in {int, float}:
                    raise ValueError('invalid state_observed_at')
                state_observed_at = float(
                    binding['state_observed_at']
                )
                if (
                    not math.isfinite(state_observed_at)
                    or state_observed_at < 0
                ):
                    raise ValueError('invalid state_observed_at')
                safety_policy_revision = cls._private_identifier(
                    binding['safety_policy_revision'],
                    'safety_policy_revision',
                )
            public = value['public']
            conversation = public['conversation']
            decision = cls._decision_from_dict(public['decision'])
            safety_value = public['safety']
            provider_value = public['provider']
            usage_value = provider_value['usage']
            execution = public['execution']
            memory = public['memory']
            issued_at = float(execution['issued_at'])
            expires_at = float(execution['expires_at'])
            if schema_version == 3 and (
                not math.isfinite(issued_at)
                or not math.isfinite(expires_at)
                or expires_at <= issued_at
                or state_observed_at > issued_at
            ):
                raise ValueError('invalid persisted execution timing')
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
            return cls(
                request_id=str(public['request_id']),
                conversation_id=str(
                    conversation['conversation_id']
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
                issued_at=issued_at,
                expires_at=expires_at,
                state_trusted=bool(execution['state_trusted']),
                memory_revision=int(value['memory_revision']),
                state_evidence_id=state_evidence_id,
                state_observed_at=state_observed_at,
                safety_policy_revision=safety_policy_revision,
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

    @staticmethod
    def _private_identifier(value: Any, field_name: str) -> str:
        if (
            type(value) is not str
            or not value.strip()
            or len(value.strip()) > 128
            or any(
                ord(character) < 32 or ord(character) == 127
                for character in value.strip()
            )
        ):
            raise ValueError(f'invalid {field_name}')
        return value.strip()


class AgentOrchestrator:
    """Keep model selection separate from conversation and authorization."""

    def __init__(
        self,
        provider: AgentProvider,
        memory_store: SQLiteMemoryStore,
        conversation_store: SQLiteConversationStore,
        safety_policy: SafetyPolicy,
        memory_limit: int = 5,
        trusted_robot_state: bool = False,
        capability_registry: CapabilityRegistry | None = None,
        robot_state_source: RobotStateSource | None = None,
        robot_state_max_age_seconds: float = 2.0,
        state_clock: Callable[[], float] = time.time,
    ) -> None:
        """Initialize provider, memory, session, and safety services."""
        if memory_limit < 1 or memory_limit > 10:
            raise ValueError('memory_limit must be between 1 and 10')
        self.provider = provider
        self.memory_store = memory_store
        self.conversation_store = conversation_store
        self.safety_policy = safety_policy
        self.memory_limit = memory_limit
        self.trusted_robot_state = trusted_robot_state
        if (
            isinstance(robot_state_max_age_seconds, bool)
            or not isinstance(robot_state_max_age_seconds, (int, float))
            or robot_state_max_age_seconds <= 0
            or robot_state_max_age_seconds > 60
        ):
            raise ValueError(
                'robot_state_max_age_seconds must be from 0 to 60'
            )
        self.robot_state_source = robot_state_source
        self.robot_state_max_age_seconds = float(
            robot_state_max_age_seconds
        )
        if not callable(state_clock):
            raise TypeError('state_clock must be callable')
        self._state_clock = state_clock
        self.capability_registry = (
            capability_registry or production_registry()
        )
        self._handle_lock = threading.RLock()

    def handle(
        self,
        request: AgentRequest,
        *,
        proposal_verifier: Callable[
            [AgentDecision], SafetyResult | None
        ] | None = None,
        confirmation_factory: Callable[
            [OrchestrationResult, BeginTurnToken], Any
        ] | None = None,
    ) -> OrchestrationResult:
        """Process one turn and optionally bind a non-authorizing intent."""
        if (
            confirmation_factory is not None
            and not callable(confirmation_factory)
        ):
            raise TypeError('confirmation_factory must be callable')
        if proposal_verifier is not None and not callable(proposal_verifier):
            raise TypeError('proposal_verifier must be callable')
        fingerprint = self._request_fingerprint(request)
        with self._handle_lock:
            begin = self.conversation_store.begin_turn(
                user_id=request.user_id,
                conversation_id=request.conversation_id,
                turn_id=request.turn_id,
                request_id=request.request_id,
                request_fingerprint=fingerprint,
                user_content=request.utterance,
            )
            if begin.cached_response is not None:
                result = OrchestrationResult.from_persisted_dict(
                    begin.cached_response
                )
                result.clock = self._state_clock
                return result
            token = begin.token
            if token is None:
                raise RuntimeError(
                    'conversation begin returned no token'
                )
            try:
                result = self._handle_uncached(
                    request,
                    begin.history,
                    begin.summary,
                    token,
                    proposal_verifier,
                )
                completion_arguments = {}
                if confirmation_factory is not None:
                    safety_provenance = (
                        result.state_evidence_id,
                        result.state_observed_at,
                        result.safety_policy_revision,
                    )
                    completion_arguments['confirmation_draft'] = (
                        confirmation_factory(result, token)
                    )
                    if safety_provenance != (
                        result.state_evidence_id,
                        result.state_observed_at,
                        result.safety_policy_revision,
                    ):
                        raise RuntimeError(
                            'confirmation factory modified safety provenance'
                        )
                session, _turn = self.conversation_store.complete_turn(
                    token,
                    assistant_content=result.decision.message,
                    response=result.to_persisted_dict(),
                    **completion_arguments,
                )
                if (
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
                self.conversation_store.fail_turn(token)
                raise

    def _handle_uncached(
        self,
        request: AgentRequest,
        conversation_turns: Sequence[ConversationTurn],
        conversation_summary: ConversationSummary | None,
        token: BeginTurnToken,
        proposal_verifier: Callable[
            [AgentDecision], SafetyResult | None
        ] | None,
    ) -> OrchestrationResult:
        """Call one provider without holding a SQLite transaction."""
        effective_value = request.to_dict()
        effective_value['available_tools'] = (
            self.capability_registry.effective_names(
                request.available_tools
            )
        )
        safety_request = AgentRequest.from_dict(effective_value)
        model_request = AgentRequest.from_dict(effective_value)
        memories, memory_revision = (
            self.memory_store.search_with_revision(
                request.user_id,
                request.utterance,
                limit=self.memory_limit,
            )
        )
        tool_specs = self.capability_registry.select_specs(
            model_request.available_tools
        )
        provider_result = self.provider.complete(
            model_request,
            memories,
            copy.deepcopy(list(conversation_turns)),
            tool_specs,
            conversation_summary=copy.deepcopy(
                conversation_summary
            ),
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
        if self.memory_store.revision != memory_revision:
            raise MemoryChangedError(
                'memory changed during model inference; retry the request'
            )
        try:
            raw_decision.validate()
        except (ValidationError, TypeError) as error:
            raise ProviderError(
                'provider returned an invalid decision'
            ) from error
        early_rejection = (
            proposal_verifier(copy.deepcopy(raw_decision))
            if proposal_verifier is not None
            else None
        )
        if early_rejection is not None:
            if not isinstance(early_rejection, SafetyResult):
                raise TypeError(
                    'proposal_verifier must return SafetyResult or None'
                )
            if early_rejection.allowed is not False:
                raise ValueError(
                    'proposal_verifier may only return a rejection'
                )
            state_trusted = False
            state_evidence_id = None
            state_observed_at = None
            safety = early_rejection
        else:
            (
                safety_request,
                state_trusted,
                state_evidence_id,
                state_observed_at,
            ) = self._fresh_safety_request(safety_request)
            safety = self.safety_policy.evaluate(
                safety_request,
                raw_decision,
                state_trusted=state_trusted,
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
        issued_at = float(self._state_clock())
        expires_at = (
            issued_at + decision.expires_in_ms / 1000.0
        )
        memory_expirations = [
            memory.expires_at
            for memory in memories
            if memory.expires_at is not None
        ]
        if memory_expirations:
            expires_at = min(
                expires_at,
                min(memory_expirations),
            )
        return OrchestrationResult(
            request_id=request.request_id,
            conversation_id=request.conversation_id,
            turn_id=request.turn_id,
            conversation_generation=token.generation,
            conversation_revision=token.revision + 1,
            conversation_ordinal=token.ordinal,
            raw_decision=raw_decision,
            decision=decision,
            safety=safety,
            provider_result=provider_result,
            memory_ids=[memory.id for memory in memories],
            decision_id=str(uuid.uuid4()),
            issued_at=issued_at,
            expires_at=expires_at,
            state_trusted=state_trusted,
            memory_revision=memory_revision,
            state_evidence_id=state_evidence_id,
            state_observed_at=state_observed_at,
            safety_policy_revision=(
                self.safety_policy.policy_revision
                if state_evidence_id is not None
                else None
            ),
            clock=self._state_clock,
        )

    def _fresh_safety_request(
        self,
        request: AgentRequest,
    ) -> tuple[AgentRequest, bool, str | None, float | None]:
        """Read server-owned state after the model, or preserve legacy mode."""
        source = self.robot_state_source
        if source is None:
            return request, self.trusted_robot_state, None, None
        try:
            evidence = source.read()
            now = float(self._state_clock())
            age = now - float(evidence.observed_at)
            trusted = (
                evidence.trusted
                and age >= 0
                and age <= self.robot_state_max_age_seconds
            )
            state = evidence.state
            if age < 0:
                evidence_id = None
                observed_at = None
            else:
                evidence_id = evidence.evidence_id
                observed_at = float(evidence.observed_at)
        except Exception:
            trusted = False
            state = RobotState()
            evidence_id = None
            observed_at = None
        value = request.to_dict()
        value['robot_state'] = state.to_dict()
        return (
            AgentRequest.from_dict(value),
            trusted,
            evidence_id,
            observed_at,
        )

    @staticmethod
    def _request_fingerprint(request: AgentRequest) -> str:
        encoded = json.dumps(
            request.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()
