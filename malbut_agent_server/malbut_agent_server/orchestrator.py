"""Conversation, memory, provider, and deterministic safety orchestration."""

import copy
import hashlib
import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence

from malbut_agent_server.conversation import (
    BeginTurnToken,
    ConversationSummary,
    ConversationTurn,
    SQLiteConversationStore,
)
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.providers.base import (
    AgentProvider,
    ProviderError,
)
from malbut_agent_server.safety import SafetyPolicy, SafetyResult
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ContextMetrics,
    ProviderResult,
    ProviderUsage,
    ValidationError,
)
from malbut_agent_server.tools import select_tool_specs


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

    def to_dict(
        self,
        include_raw_decision: bool = False,
    ) -> Dict[str, Any]:
        """Return the stable HTTP response contract."""
        decision_is_fresh = time.time() < self.expires_at
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
                'authorized': (
                    self.state_trusted
                    and self.safety.allowed
                    and self.decision.type == 'tool_call'
                    and decision_is_fresh
                ),
                'state_trusted': self.state_trusted,
                'fresh': decision_is_fresh,
                'consume_once': True,
            },
        }
        if include_raw_decision:
            result['raw_decision'] = self.raw_decision.to_dict()
        return result

    def to_persisted_dict(self) -> Dict[str, Any]:
        """Persist the final safe response and required metadata."""
        return {
            'schema_version': 1,
            'public': self.to_dict(include_raw_decision=False),
            'memory_revision': self.memory_revision,
        }

    @classmethod
    def from_persisted_dict(
        cls,
        value: Dict[str, Any],
    ) -> 'OrchestrationResult':
        """Reconstruct an idempotent response without another model call."""
        try:
            if value.get('schema_version') != 1:
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
                issued_at=float(execution['issued_at']),
                expires_at=float(execution['expires_at']),
                state_trusted=bool(execution['state_trusted']),
                memory_revision=int(value['memory_revision']),
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
        trusted_robot_state: bool = False,
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
        self._handle_lock = threading.RLock()

    def handle(self, request: AgentRequest) -> OrchestrationResult:
        """Process one ordered turn with durable idempotency."""
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
                return OrchestrationResult.from_persisted_dict(
                    begin.cached_response
                )
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
                )
                session, _turn = (
                    self.conversation_store.complete_turn(
                        token,
                        assistant_content=result.decision.message,
                        response=result.to_persisted_dict(),
                    )
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
    ) -> OrchestrationResult:
        """Call one provider without holding a SQLite transaction."""
        safety_request = AgentRequest.from_dict(request.to_dict())
        model_request = AgentRequest.from_dict(request.to_dict())
        memories, memory_revision = (
            self.memory_store.search_with_revision(
                request.user_id,
                request.utterance,
                limit=self.memory_limit,
            )
        )
        tool_specs = select_tool_specs(request.available_tools)
        provider_result = self.provider.complete(
            model_request,
            memories,
            copy.deepcopy(list(conversation_turns)),
            tool_specs,
            conversation_summary=copy.deepcopy(
                conversation_summary
            ),
        )
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
        safety = self.safety_policy.evaluate(
            safety_request,
            raw_decision,
            state_trusted=self.trusted_robot_state,
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
            state_trusted=self.trusted_robot_state,
            memory_revision=memory_revision,
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
