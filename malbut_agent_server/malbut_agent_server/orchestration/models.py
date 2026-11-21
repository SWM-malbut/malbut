"""Immutable orchestration result models and validation errors."""

import math
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

from malbut_agent_server.safety import SafetyResult
from malbut_agent_server.schemas import (
    AgentDecision,
    ContextMetrics,
    ProviderResult,
    ProviderUsage,
    ValidationError,
)
from malbut_agent_server.tools import validate_tool_arguments


class ExpiredDecisionError(ValidationError):
    """Raised when an action request ID refers to an expired decision."""


class MemoryChangedError(ValidationError):
    """Raised when memory changes while a model request is in flight."""


@dataclass(frozen=True)
class StoryReplyBinding:
    """Immutable, content-free provenance for a persisted conversational reply."""

    policy_revision: int
    readset: tuple[tuple[str, tuple[str, ...]], ...]

    @classmethod
    def from_dict(cls, value: Any) -> 'StoryReplyBinding':
        if (type(value) is not dict or set(value) != {'policy_revision', 'readset'}
                or type(value['policy_revision']) is not int or value['policy_revision'] < 0
                or type(value['readset']) is not list or len(value['readset']) > 20):
            raise ValueError('invalid story binding')
        entries: list[tuple[str, tuple[str, ...]]] = []
        seen: set[str] = set()
        for item in value['readset']:
            if type(item) is not dict or set(item) != {'story_id', 'entry_hashes'}:
                raise ValueError('invalid story readset')
            identifier, hashes = item['story_id'], item['entry_hashes']
            if (type(identifier) is not str or not identifier or len(identifier) > 256
                    or identifier != identifier.strip() or identifier in seen
                    or any(ord(char) < 32 or ord(char) == 127 for char in identifier)
                    or type(hashes) is not list or not 1 <= len(hashes) <= 64
                    or any(type(digest) is not str or re.fullmatch(r'[0-9a-f]{64}', digest) is None
                           for digest in hashes) or len(set(hashes)) != len(hashes)):
                raise ValueError('invalid story readset')
            seen.add(identifier)
            entries.append((identifier, tuple(hashes)))
        return cls(value['policy_revision'], tuple(entries))

    def to_dict(self) -> Dict[str, Any]:
        return {'policy_revision': self.policy_revision,
                'readset': [{'story_id': identifier, 'entry_hashes': list(hashes)}
                            for identifier, hashes in self.readset]}


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
    story_binding: StoryReplyBinding | None = field(default=None, repr=False)
    state_evidence_id: str | None = None
    state_observed_at: float | None = None
    safety_policy_revision: str | None = None
    clock: Callable[[], float] = field(
        default=time.time,
        repr=False,
        compare=False,
    )
    speech_published: bool = field(default=False, repr=False, compare=False)

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
        result: Dict[str, Any] = {
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
        value: Dict[str, Any] = {
            'schema_version': 2,
            'public': self.to_dict(include_raw_decision=False),
            'memory_revision': self.memory_revision,
        }
        if self.story_binding is not None:
            value['story_binding'] = self.story_binding.to_dict()
        if (self.raw_decision.type == 'tool_call'
                and self.raw_decision.tool_name in {'get_weather', 'set_weather_location'}
                and self.safety.allowed and self.decision.type != 'tool_call'):
            value['weather_tool_decision'] = self.raw_decision.to_dict()
        if (
            self.state_evidence_id is None
            and self.state_observed_at is None
            and self.safety_policy_revision is None
        ):
            return value
        if (
            self.state_evidence_id is None
            or self.state_observed_at is None
            or self.safety_policy_revision is None
        ):
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
            state_evidence_id: str | None = None
            state_observed_at: float | None = None
            safety_policy_revision: str | None = None
            story_binding = (StoryReplyBinding.from_dict(value['story_binding'])
                             if 'story_binding' in value else None)
            if schema_version == 3:
                if frozenset(value) - {'story_binding'} != frozenset({
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
                        or raw_decision.tool_name is None
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
                or state_observed_at is None
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
                story_binding=story_binding,
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
            clarification_for=value.get('clarification_for'),
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
