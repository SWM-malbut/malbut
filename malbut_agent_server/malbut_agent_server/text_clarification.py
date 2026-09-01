"""Fail-closed one-hop clarification resolution for authenticated text turns."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Sequence

from malbut_agent_server.conversation import (
    BeginTurnToken,
    ConversationTurn,
)
from malbut_agent_server.named_target import (
    BoundNamedTarget,
    NamedTargetResolver,
)
from malbut_agent_server.schemas import AgentRequest


_DEICTIC_DESTINATIONS = (
    '여기',
    '저기',
    '거기',
    '이곳',
    '저곳',
    '그곳',
    '이쪽',
    '저쪽',
    '그쪽',
    '이방',
    '저방',
    '그방',
    '이쪽방',
    '저쪽방',
    '그쪽방',
)
_DESTINATION_PARTICLES = ('', '로', '으로')
_NAVIGATION_REQUESTS = (
    '가',
    '가줘',
    '가주세요',
    '가자',
    '와',
    '와줘',
    '와주세요',
    '이동해',
    '이동해줘',
    '이동해주세요',
)
_DEICTIC_NAVIGATION_FORMS = frozenset(
    f'{destination}{particle}{request}'
    for destination in _DEICTIC_DESTINATIONS
    for particle in _DESTINATION_PARTICLES
    for request in _NAVIGATION_REQUESTS
)
NAVIGATION_DESTINATION_QUESTION = (
    '어느 목적지로 이동할지 등록된 공간 이름 하나를 말해 주세요.'
)
NAVIGATION_CLARIFICATION_POLICY_REVISION = (
    'navigation-clarification-v1'
)
_SHA256 = re.compile(r'^[0-9a-f]{64}$')
_UNSAFE_TARGET_MARKERS = (
    '가지마',
    '가지말',
    '이동하지',
    '가면안',
    '말고',
    '취소',
    '무시',
    '명령',
    '규칙',
    '시스템',
    '프롬프트',
    '실행',
    '이동해',
    '가줘',
    '가주세요',
    'cancel',
    'nevermind',
    'ignore',
    'system',
    'developer',
    'assistant',
    'prompt',
    'instruction',
    'tool',
)
_NON_TARGET_REPLIES = frozenset({
    '네',
    '예',
    '아니',
    '아니오',
    '아니요',
    '취소',
    'yes',
    'no',
})
_PERSISTED_KEYS = frozenset({
    'schema_version',
    'public',
    'memory_revision',
})
_PERSISTED_V3_KEYS = _PERSISTED_KEYS | {'safety_binding'}
_PUBLIC_KEYS = frozenset({
    'request_id',
    'conversation',
    'decision',
    'safety',
    'provider',
    'memory',
    'execution',
})
_CONVERSATION_KEYS = frozenset({
    'conversation_id',
    'turn_id',
    'generation',
    'revision',
    'ordinal',
})
_DECISION_KEYS = frozenset({
    'type',
    'message',
    'tool_name',
    'arguments',
    'reason',
    'confidence',
    'expires_in_ms',
})
_SAFETY_KEYS = frozenset({'allowed', 'code', 'reason'})
_EXECUTION_KEYS = frozenset({
    'decision_id',
    'issued_at',
    'expires_at',
    'authorized',
    'proposal_authorized',
    'state_trusted',
    'fresh',
    'consume_once',
    'tool_call_id',
})


def is_deictic_navigation_request(value: str) -> bool:
    """Recognize only the bounded missing-destination forms we own."""
    return (
        type(value) is str
        and _compact(value) in _DEICTIC_NAVIGATION_FORMS
    )


@dataclass(frozen=True)
class TextClarificationResolution:
    """One server-derived missing destination, without execution authority."""

    source_request_id: str
    source_turn_id: str
    location: str
    canonical_utterance: str

    def __post_init__(self) -> None:
        """Reject malformed internal values before they reach a provider."""
        for name, maximum in (
            ('source_request_id', 128),
            ('source_turn_id', 128),
            ('location', 128),
            ('canonical_utterance', 2000),
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


class NavigationClarificationResolver:
    """Resolve only an immediately preceding deictic navigation question."""

    def __init__(self, target_resolver: NamedTargetResolver) -> None:
        """Bind a read-only authoritative named-target resolver."""
        if not callable(getattr(target_resolver, 'resolve', None)):
            raise TypeError('target_resolver must implement resolve')
        self.target_resolver = target_resolver

    def resolve(
        self,
        request: AgentRequest,
        turns: Sequence[ConversationTurn],
        token: BeginTurnToken,
    ) -> TextClarificationResolution | None:
        """Return one bounded resolution, or ``None`` for every ambiguity."""
        if not self.has_pending_navigation_clarification(
            request,
            turns,
            token,
        ):
            return None

        history = tuple(turns)
        previous = history[-1]
        answer = request.utterance
        if not self._is_plain_target(answer):
            return None
        try:
            target = self.target_resolver.resolve(answer)
        except Exception:
            return None
        if not isinstance(target, BoundNamedTarget):
            return None
        if not self._is_plain_target(target.room_name):
            return None
        return TextClarificationResolution(
            source_request_id=previous.request_id,
            source_turn_id=previous.turn_id,
            location=target.room_name,
            canonical_utterance=(
                f'{target.room_name}'
                f'{_directional_particle(target.room_name)} 이동해줘'
            ),
        )

    def has_pending_navigation_clarification(
        self,
        request: AgentRequest,
        turns: Sequence[ConversationTurn],
        token: BeginTurnToken,
    ) -> bool:
        """Return whether the immediate prior turn is an eligible question."""
        if not isinstance(request, AgentRequest):
            raise TypeError('request must be an AgentRequest')
        if not isinstance(token, BeginTurnToken):
            raise TypeError('token must be a BeginTurnToken')
        if isinstance(turns, (str, bytes)):
            raise TypeError('turns must be a sequence of ConversationTurn')
        try:
            history = tuple(turns)
        except TypeError as error:
            raise TypeError(
                'turns must be a sequence of ConversationTurn'
            ) from error
        if not history or any(
            not isinstance(turn, ConversationTurn) for turn in history
        ):
            return False
        if not self._current_metadata_matches(request, token):
            return False

        previous = history[-1]
        if not self._previous_metadata_matches(previous, request, token):
            return False
        restored = self._restore_turn(previous)
        if restored is None or not self._matches_previous_result(
            previous,
            token,
            restored,
        ):
            return False
        if not self._is_non_action_clarification(restored):
            return False
        if not self._is_deictic_navigation(previous.user_content):
            return False
        if 'navigate' not in request.available_tools:
            return False
        return True

    @staticmethod
    def _current_metadata_matches(
        request: AgentRequest,
        token: BeginTurnToken,
    ) -> bool:
        if (
            type(token.generation) is not int
            or type(token.revision) is not int
            or type(token.ordinal) is not int
            or token.generation < 0
            or token.revision < 0
            or token.ordinal < 1
            or request.user_id != token.user_id
            or request.conversation_id != token.conversation_id
            or request.turn_id != token.turn_id
            or request.request_id != token.request_id
        ):
            return False
        encoded = json.dumps(
            request.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest() == (
            token.request_fingerprint
        )

    @staticmethod
    def _previous_metadata_matches(
        previous: ConversationTurn,
        request: AgentRequest,
        token: BeginTurnToken,
    ) -> bool:
        return (
            type(previous.generation) is int
            and type(previous.ordinal) is int
            and previous.ordinal >= 1
            and previous.user_id == request.user_id == token.user_id
            and previous.conversation_id
            == request.conversation_id
            == token.conversation_id
            and previous.session_instance_id == token.session_instance_id
            and previous.generation == token.generation
            and previous.ordinal + 1 == token.ordinal
            and type(previous.request_fingerprint) is str
            and _SHA256.fullmatch(previous.request_fingerprint) is not None
        )

    @classmethod
    def _restore_turn(cls, turn: ConversationTurn) -> Any | None:
        if not cls._has_strict_persisted_shape(turn.response):
            return None
        try:
            # Local import avoids coupling the orchestrator's module load to
            # this optional text-only policy.
            from malbut_agent_server.orchestrator import OrchestrationResult

            return OrchestrationResult.from_persisted_dict(
                copy.deepcopy(turn.response)
            )
        except Exception:
            return None

    @staticmethod
    def _has_strict_persisted_shape(response: Any) -> bool:
        if type(response) is not dict:
            return False
        schema_version = response.get('schema_version')
        if type(schema_version) is not int:
            return False
        expected = (
            _PERSISTED_V3_KEYS if schema_version == 3 else _PERSISTED_KEYS
        )
        if schema_version not in {1, 2, 3} or frozenset(response) != expected:
            return False
        public = response.get('public')
        if type(public) is not dict or frozenset(public) != _PUBLIC_KEYS:
            return False
        conversation = public.get('conversation')
        decision = public.get('decision')
        safety = public.get('safety')
        execution = public.get('execution')
        return (
            type(conversation) is dict
            and frozenset(conversation) == _CONVERSATION_KEYS
            and type(decision) is dict
            and frozenset(decision) == _DECISION_KEYS
            and decision['type'] == 'clarification'
            and decision['tool_name'] is None
            and decision['arguments'] == {}
            and type(safety) is dict
            and frozenset(safety) == _SAFETY_KEYS
            and safety['allowed'] is True
            and safety['code'] == 'not_an_action'
            and type(execution) is dict
            and frozenset(execution) == _EXECUTION_KEYS
            and execution['authorized'] is False
            and execution['proposal_authorized'] is False
            and execution['consume_once'] is False
            and execution['tool_call_id'] is None
        )

    @staticmethod
    def _matches_previous_result(
        previous: ConversationTurn,
        token: BeginTurnToken,
        result: Any,
    ) -> bool:
        public = previous.response['public']
        conversation = public['conversation']
        if (
            type(public['request_id']) is not str
            or type(conversation['conversation_id']) is not str
            or type(conversation['turn_id']) is not str
            or type(conversation['generation']) is not int
            or type(conversation['revision']) is not int
            or type(conversation['ordinal']) is not int
        ):
            return False
        return (
            result.request_id == previous.request_id
            and public['request_id'] == previous.request_id
            and result.conversation_id == previous.conversation_id
            and conversation['conversation_id'] == previous.conversation_id
            and result.turn_id == previous.turn_id
            and conversation['turn_id'] == previous.turn_id
            and result.conversation_generation == previous.generation
            and conversation['generation'] == previous.generation
            and result.conversation_ordinal == previous.ordinal
            and conversation['ordinal'] == previous.ordinal
            and result.conversation_revision == token.revision
            and conversation['revision'] == token.revision
            and result.decision.message == previous.assistant_content
        )

    @staticmethod
    def _is_non_action_clarification(result: Any) -> bool:
        decision = result.decision
        return (
            decision.type == 'clarification'
            and decision.tool_name is None
            and decision.arguments == {}
            and result.safety.allowed is True
            and result.safety.code == 'not_an_action'
        )

    @staticmethod
    def _is_deictic_navigation(value: str) -> bool:
        return is_deictic_navigation_request(value)

    @staticmethod
    def _is_plain_target(value: str) -> bool:
        if type(value) is not str or not value or value != value.strip():
            return False
        if unicodedata.normalize('NFKC', value) != value:
            return False
        if any(
            ord(character) < 32 or ord(character) == 127
            for character in value
        ):
            return False
        if '  ' in value or any(
            not (
                character.isalnum()
                or character in {' ', '_', '-'}
            )
            for character in value
        ):
            return False
        compact = _compact(value)
        return (
            compact not in _NON_TARGET_REPLIES
            and not any(marker in compact for marker in _UNSAFE_TARGET_MARKERS)
        )


def _compact(value: str) -> str:
    normalized = unicodedata.normalize('NFKC', value).casefold()
    return ''.join(character for character in normalized if character.isalnum())


def _directional_particle(value: str) -> str:
    """Return the Korean directional particle for one validated name."""
    syllable_offset = ord(value[-1]) - 0xAC00
    if 0 <= syllable_offset < 11172:
        final_consonant = syllable_offset % 28
        if final_consonant not in {0, 8}:
            return '으로'
    return '로'
