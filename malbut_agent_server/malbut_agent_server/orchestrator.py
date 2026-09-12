"""Conversation, memory, provider, and deterministic safety orchestration."""

import copy
import hashlib
import json
import math
import re
import threading
import time
import uuid
from concurrent.futures import CancelledError
from dataclasses import dataclass, field, replace
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
from malbut_agent_server.automatic_memory_jobs import AutomaticMemoryJobs
from malbut_agent_server.automatic_memory_policy import (
    automatic_candidate, automatic_deferred, automatic_request,
)
from malbut_agent_server.automatic_memory_worker import AutomaticMemoryWorker
from malbut_agent_server.personal_memory import (
    PersonalMemory, direct_source, local_intent, management_request,
    negated_management, requested_operation, reply as memory_reply,
)
from malbut_agent_server.providers.base import (
    AgentProvider,
    ProviderError,
    accepts_memory_context,
    accepts_weather_context,
)
from malbut_agent_server.prompting import bounded_weather_context
from malbut_agent_server.robot_state_source import RobotStateSource
from malbut_agent_server.safety import SafetyPolicy, SafetyResult
from malbut_agent_server.tools import validate_tool_arguments
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


@dataclass(frozen=True)
class ServerClarification:
    """One non-action clarification produced by trusted server policy."""

    message: str
    code: str
    policy_revision: str

    def __post_init__(self) -> None:
        """Keep this DTO incapable of carrying Tool or execution fields."""
        for name, maximum in (
            ('message', 2000),
            ('code', 128),
            ('policy_revision', 128),
        ):
            value = getattr(self, name)
            if (
                type(value) is not str
                or not value
                or value != value.strip()
                or len(value) > maximum
                or any(
                    ord(character) < 32 or ord(character) == 127
                    for character in value
                )
            ):
                raise ValueError(f'{name} is invalid')
        for name in ('code', 'policy_revision'):
            value = getattr(self, name)
            if any(
                not (
                    character.isascii()
                    and (
                        character.isalnum()
                        or character in {'_', '-', '.'}
                    )
                )
                for character in value
            ):
                raise ValueError(f'{name} is invalid')

    def to_decision(self) -> AgentDecision:
        """Project the policy result into the existing non-action wire type."""
        return AgentDecision(
            type='clarification',
            message=self.message,
            reason=f'server:{self.code}',
            confidence=1.0,
        )


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
    memory_validator: Callable[[], None] | None = field(
        default=None, repr=False, compare=False,
    )
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
        if self.memory_validator is not None:
            self.memory_validator()
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
        if (self.raw_decision.type == 'tool_call'
                and self.raw_decision.tool_name in {'get_weather', 'set_weather_location'}
                and self.safety.allowed and self.decision.type != 'tool_call'):
            value['weather_tool_decision'] = self.raw_decision.to_dict()
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
            raw_decision = decision
            if 'weather_tool_decision' in value:
                raw_decision = cls._decision_from_dict(value['weather_tool_decision'])
                if (raw_decision.type != 'tool_call'
                        or raw_decision.tool_name not in {'get_weather', 'set_weather_location'}
                        or decision.type == 'tool_call'):
                    raise ValueError('invalid persisted weather Tool decision')
                validate_tool_arguments(raw_decision.tool_name, raw_decision.arguments)
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
                raw_decision=raw_decision,
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
        memory_source_reviewer=None,
        background_memory: bool = False,
        automatic_memory_extractor=None,
        weather_executor: Callable[[str], dict] | None = None,
        weather_location_executor: Callable[[str, str], dict] | None = None,
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
        self.personal_memory = PersonalMemory(memory_store, conversation_store)
        self.memory_source_reviewer = memory_source_reviewer
        self.automatic_memory_extractor = automatic_memory_extractor
        self.weather_executor = weather_executor
        self.weather_location_executor = weather_location_executor
        if type(background_memory) is not bool:
            raise TypeError('background_memory must be a boolean')
        self.automatic_memory_jobs = AutomaticMemoryJobs(conversation_store)
        self.automatic_memory_worker = (
            AutomaticMemoryWorker(self, self.automatic_memory_jobs)
            if background_memory else None
        )
        self._closed = False

    def start_background_memory(self):
        """Recover queued work when serving, not during construction."""
        if self.automatic_memory_worker is not None and not self._closed:
            self.automatic_memory_worker.start()

    def stop_background_memory(self):
        """Drain the owned worker before callers close the shared stores."""
        if self.automatic_memory_worker is not None:
            self.automatic_memory_worker.close()

    def close(self):
        """Reject new turns, join the worker, then close SQLite handles."""
        with self._handle_lock:
            if self._closed:
                return
            self._closed = True
        self.stop_background_memory()
        try:
            self.conversation_store.close()
        finally:
            self.memory_store.close()

    def _memory_guard(self, user_id, request_id):
        """Bind a response to its durable user-specific memory version."""
        def validate():
            try:
                self.personal_memory.assert_fresh(user_id, request_id)
            except ValidationError as error:
                raise MemoryChangedError(
                    'memory changed; submit a new turn'
                ) from error
        return validate

    def _admit_memory_turn(self, connection, request):
        """Ordinary turns preserve jobs; explicit memory intent fences them.

        This runs after request-cache/conflict checks in the turn reservation
        transaction. Bump even for no-match deletion so an older in-flight
        provider cannot enqueue its stale source after this request finishes.
        """
        text = request.utterance
        # Existing extraction eligibility is deliberately broad. Its
        # "기억하" stem also matches read-only questions like "기억하니?";
        # those must not become cancellation merely by mentioning memory.
        remember_instruction = re.sub(
            r'(기억|저장)(하고\s*있|하니|하나요|하는지|했니|했나요|했는지'
            r'|해(?:요)?\s*(?=[?？]))',
            '', text,
        )
        if direct_source(text) and (
            local_intent(text) is not None
            or (negated_management(text) and re.search(
                r'(기억|저장)(을|은|는)?\s*(하지|말아|말고|말라|않|금지)'
                r'|안\s*(기억|저장)', text,
            ))
            or any(requested_operation(text, operation)
                   for operation in ('correct', 'forget'))
            or requested_operation(remember_instruction, 'remember')
        ):
            self.automatic_memory_jobs.invalidate_user(
                connection, request.user_id, reason='memory_control',
            )
            self.memory_store.invalidate_answers(
                request.user_id, connection=connection,
            )

    def handle(
        self,
        request: AgentRequest,
        *,
        utterance_resolver: Callable[
            [AgentRequest, Sequence[ConversationTurn], BeginTurnToken],
            str | None,
        ] | None = None,
        proposal_verifier: Callable[
            [AgentDecision], SafetyResult | None
        ] | None = None,
        confirmation_factory: Callable[
            [OrchestrationResult, BeginTurnToken], Any
        ] | None = None,
        server_clarification: ServerClarification | None = None,
    ) -> OrchestrationResult:
        """Process one turn and optionally bind a non-authorizing intent."""
        if (
            confirmation_factory is not None
            and not callable(confirmation_factory)
        ):
            raise TypeError('confirmation_factory must be callable')
        if proposal_verifier is not None and not callable(proposal_verifier):
            raise TypeError('proposal_verifier must be callable')
        if utterance_resolver is not None and not callable(
            utterance_resolver
        ):
            raise TypeError('utterance_resolver must be callable')
        if (
            server_clarification is not None
            and not isinstance(server_clarification, ServerClarification)
        ):
            raise TypeError(
                'server_clarification must be a ServerClarification'
            )
        fingerprint = self._request_fingerprint(request)
        with self._handle_lock:
            if self._closed:
                raise RuntimeError('orchestrator is closed')
            begin = self.conversation_store.begin_turn(
                user_id=request.user_id,
                conversation_id=request.conversation_id,
                turn_id=request.turn_id,
                request_id=request.request_id,
                request_fingerprint=fingerprint,
                user_content=request.utterance,
                before_new_turn=lambda conn: self._admit_memory_turn(
                    conn, request,
                ),
            )
            if begin.cached_response is not None:
                guard = self._memory_guard(request.user_id, request.request_id)
                guard()
                result = OrchestrationResult.from_persisted_dict(
                    begin.cached_response
                )
                result.clock = self._state_clock
                result.memory_validator = guard
                self.start_background_memory()
                return result
            token = begin.token
            if token is None:
                raise RuntimeError(
                    'conversation begin returned no token'
                )
            try:
                memory_snapshot = self.personal_memory.snapshot(
                    request, token, begin.history, begin.summary,
                    memory_limit=self.memory_limit,
                )
                effective_request = self._effective_request(
                    request,
                    memory_snapshot.history,
                    token,
                    utterance_resolver,
                )
                result = self._handle_uncached(
                    request,
                    effective_request,
                    memory_snapshot.history,
                    memory_snapshot.summary,
                    token,
                    proposal_verifier,
                    server_clarification,
                    memory_snapshot,
                )
                worker = self.automatic_memory_worker
                extract = (
                    self._separate_memory_extraction(request, memory_snapshot)
                    and server_clarification is None
                    and automatic_deferred(request, memory_snapshot, result)
                )
                deferred = (
                    worker is not None and not worker.closed
                    and (extract or automatic_candidate(
                        request, memory_snapshot, result,
                    ))
                )
                if not deferred:
                    self.personal_memory.prepare_source_review(
                        request, memory_snapshot, result,
                        self.memory_source_reviewer,
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

                def commit_memory(connection):
                    if deferred and (extract or automatic_candidate(
                        request, memory_snapshot, result,
                    )):
                        # The response and private reservation commit together.
                        # Saturation skips autosave, not the conversation.
                        self.automatic_memory_jobs.enqueue(
                            connection, request, token, memory_snapshot,
                            result, extract=extract,
                        )
                        result.provider_result.memory_proposal = None
                    return self.personal_memory.commit(
                        request, token, memory_snapshot, result, connection,
                    )

                session, _turn = self.conversation_store.complete_turn(
                    token,
                    assistant_content=result.decision.message,
                    response=result.to_persisted_dict(),
                    commit_callback=commit_memory,
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
                result.memory_validator = self._memory_guard(
                    request.user_id, request.request_id,
                )
                self.start_background_memory()
                return result
            except Exception as error:
                self.conversation_store.fail_turn(token)
                if (
                    isinstance(error, ValidationError)
                    and str(error) == 'memory_changed'
                ):
                    raise MemoryChangedError(
                        'memory changed; submit a new turn'
                    ) from error
                raise

    def _separate_memory_extraction(self, request, snapshot):
        worker = self.automatic_memory_worker
        return (
            self.automatic_memory_extractor is not None
            and worker is not None and not worker.closed
            and accepts_memory_context(self.provider)
            and automatic_request(request, snapshot)
        )

    def _handle_uncached(
        self,
        request: AgentRequest,
        effective_request: AgentRequest,
        conversation_turns: Sequence[ConversationTurn],
        conversation_summary: ConversationSummary | None,
        token: BeginTurnToken,
        proposal_verifier: Callable[
            [AgentDecision], SafetyResult | None
        ] | None,
        server_clarification: ServerClarification | None,
        memory_snapshot,
    ) -> OrchestrationResult:
        """Call one provider without holding a SQLite transaction."""
        effective_value = effective_request.to_dict()
        effective_value['available_tools'] = (
            self.capability_registry.effective_names(
                effective_request.available_tools
            )
        )
        if self.weather_executor is None:
            effective_value['available_tools'] = [
                name for name in effective_value['available_tools'] if name != 'get_weather'
            ]
        if self.weather_location_executor is None:
            effective_value['available_tools'] = [
                name for name in effective_value['available_tools']
                if name != 'set_weather_location'
            ]
        safety_request = AgentRequest.from_dict(effective_value)
        model_request = AgentRequest.from_dict(effective_value)
        local_memory = self.personal_memory.local_decision(
            request, memory_snapshot,
        )
        memories = memory_snapshot.memories[:self.memory_limit]
        memory_revision = memory_snapshot.state['revision']
        if local_memory is not None:
            provider_result = ProviderResult(
                decision=local_memory, provider='malbut-memory-policy',
                model='consent-v1', latency_ms=0.0, memory_supported=True,
            )
        elif server_clarification is None:
            tool_specs = self.capability_registry.select_specs(
                model_request.available_tools
            )
            memory_arguments = {}
            if accepts_memory_context(self.provider):
                memory_context = copy.deepcopy(memory_snapshot.context)
                if self._separate_memory_extraction(request, memory_snapshot):
                    memory_context['mode'] = 'answer_only'
                memory_arguments['memory_context'] = memory_context
            provider_result = self.provider.complete(
                model_request,
                memories,
                copy.deepcopy(list(conversation_turns)),
                tool_specs,
                conversation_summary=copy.deepcopy(
                    conversation_summary
                ),
                **memory_arguments,
            )
        else:
            memories = []
            provider_result = ProviderResult(
                decision=server_clarification.to_decision(),
                provider='malbut-server-policy',
                model=server_clarification.policy_revision,
                latency_ms=0.0,
                input_chars=0,
            )
        try:
            provider_result.validate()
        except (ValidationError, TypeError) as error:
            raise ProviderError(
                'provider returned invalid metadata'
            ) from error
        if self._separate_memory_extraction(request, memory_snapshot):
            # No foreground proposal can bypass the C extraction path,
            # even when an injected/older provider ignores the mode hint.
            provider_result.memory_proposal = None
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
        if raw_decision.type == 'tool_call' and (
            provider_result.memory_proposal is not None
            or (management_request(request.utterance)
                and raw_decision.tool_name != 'set_weather_location')
        ):
            raw_decision = memory_reply(
                '로봇 실행과 기억 관리 중 먼저 처리할 요청을 말씀해 주세요.',
                clarification=True,
            )
            provider_result.memory_proposal = None
        provider_result.decision = raw_decision
        if (
            self.memory_store.policy_state(request.user_id)
            != memory_snapshot.state
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
            if server_clarification is None and raw_decision.tool_name not in {
                'get_weather', 'set_weather_location',
            }:
                (
                    safety_request,
                    state_trusted,
                    state_evidence_id,
                    state_observed_at,
                ) = self._fresh_safety_request(safety_request)
            else:
                state_trusted = False
                state_evidence_id = None
                state_observed_at = None
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
        elif raw_decision.type == 'tool_call' and raw_decision.tool_name in {
            'get_weather', 'set_weather_location',
        }:
            provider_result = self._answer_weather(
                model_request, memories, conversation_turns, conversation_summary, provider_result,
            )
            decision = provider_result.decision
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

    def _answer_weather(
        self, request, memories, conversation_turns, conversation_summary, first_result,
    ) -> ProviderResult:
        """Execute one Manager read after the model's Tool choice, then answer without Tools."""
        setting_location = first_result.decision.tool_name == 'set_weather_location'
        executor = self.weather_location_executor if setting_location else self.weather_executor
        if executor is None or not accepts_weather_context(self.provider):
            return replace(first_result, decision=AgentDecision(
                type='message', message='지금 날씨 조회 기능을 사용할 수 없어요.',
                reason='weather_unavailable', confidence=1.0,
            ))
        try:
            if setting_location:
                weather = executor(request.request_id, first_result.decision.arguments['location'])
            else:
                weather = executor(request.request_id)
            weather = bounded_weather_context(weather)
        except CancelledError:
            raise
        except Exception:
            weather = {'status': 'unavailable'}
        if setting_location:
            # A committed setting needs a receipt even if a second model call would fail.
            status = weather['status']
            location = weather.get('location')
            if status == 'location_set' and isinstance(location, str) and location.strip():
                decision = AgentDecision(
                    'message', f'날씨 조회 위치를 저장했어요. 앞으로 {location} 날씨를 알려드릴게요.',
                    reason='weather_location_saved', confidence=1.0,
                )
            elif status == 'location_ambiguous':
                decision = AgentDecision(
                    'clarification', '어느 지역인가요? ' + ', '.join(weather['candidates'])
                    + ' 중에서 시·구·동을 알려주세요.', reason='weather_location_ambiguous',
                )
            elif status == 'location_not_found':
                decision = AgentDecision(
                    'clarification', '지역을 찾지 못했어요. 시·구·동을 더 자세히 알려주세요.',
                    reason='weather_location_not_found',
                )
            else:
                decision = AgentDecision(
                    'message', '날씨 조회 위치를 저장하지 못했어요. 잠시 후 다시 알려주세요.',
                    reason='weather_location_unavailable',
                )
            return replace(first_result, decision=decision, memory_proposal=None)
        value = request.to_dict()
        value['available_tools'] = []
        answer = self.provider.complete(
            AgentRequest.from_dict(value), list(memories),
            copy.deepcopy(list(conversation_turns)), [],
            conversation_summary=copy.deepcopy(conversation_summary),
            weather_context=weather,
        )
        answer.validate()
        if answer.decision.type == 'tool_call' or answer.memory_proposal is not None:
            answer = replace(answer, decision=AgentDecision(
                type='refusal', message='날씨 조회 결과로 답변을 만들지 못했어요.',
                reason='weather_followup_tool_forbidden', confidence=1.0,
            ), memory_proposal=None)

        def combined(name):
            first = getattr(first_result.usage, name)
            second = getattr(answer.usage, name)
            return first + second if first is not None and second is not None else None

        return replace(
            answer, latency_ms=first_result.latency_ms + answer.latency_ms,
            usage=ProviderUsage(**{name: combined(name) for name in (
                'input_tokens', 'output_tokens', 'total_tokens',
            )}),
            input_chars=(first_result.input_chars + answer.input_chars
                         if first_result.input_chars is not None and answer.input_chars is not None
                         else None),
        )

    @staticmethod
    def _effective_request(
        request: AgentRequest,
        conversation_turns: Sequence[ConversationTurn],
        token: BeginTurnToken,
        utterance_resolver: Callable[
            [AgentRequest, Sequence[ConversationTurn], BeginTurnToken],
            str | None,
        ] | None,
    ) -> AgentRequest:
        """Resolve one server-owned utterance without changing authority."""
        if utterance_resolver is None:
            return request
        resolved = utterance_resolver(
            copy.deepcopy(request),
            copy.deepcopy(tuple(conversation_turns)),
            copy.deepcopy(token),
        )
        if resolved is None:
            return request
        if type(resolved) is not str:
            raise TypeError('utterance_resolver must return str or None')
        value = request.to_dict()
        value['utterance'] = resolved
        return AgentRequest.from_dict(value)

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
