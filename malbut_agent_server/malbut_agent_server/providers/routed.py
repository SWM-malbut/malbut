"""Select one bounded Agent provider after a non-authorizing route."""

from __future__ import annotations

from dataclasses import replace
from typing import List, Optional, Sequence

from malbut_agent_server.application.front_routing import (
    FrontRoutingError,
    FrontRoutingService,
)
from malbut_agent_server.conversation import (
    ConversationSummary,
    ConversationTurn,
)
from malbut_agent_server.domain.front_route import (
    MAX_FRONT_HISTORY_CHARS,
    MAX_FRONT_HISTORY_MESSAGES,
    MAX_FRONT_HISTORY_MESSAGE_CHARS,
    FrontMessage,
    FrontMessageRole,
    FrontRoute,
    FrontRouteRequest,
)
from malbut_agent_server.memory import MemoryRecord
from malbut_agent_server.providers.base import (
    AgentProvider,
    ProviderError,
)
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ProviderResult,
    ValidationError,
)
from malbut_agent_server.tools import ToolSpec


DEFAULT_ROUTING_POLICY_REVISION = 'malbut-front-provider-routing-v1'

_CLARIFICATION_MESSAGE = (
    '요청을 이해하려면 원하는 내용을 조금 더 구체적으로 말해 주세요.'
)
_ROBOT_STATUS_UNAVAILABLE_MESSAGE = (
    '현재 이 경로에서는 로봇 상태를 조회할 수 없습니다.'
)
_CURRENT_ACTION_UNAVAILABLE_MESSAGE = (
    '현재 이 경로에서는 진행 중인 로봇 작업을 조회할 수 없습니다.'
)
_ROUTER_UNAVAILABLE_MESSAGE = (
    '현재 요청 분류 기능을 사용할 수 없습니다. 잠시 후 다시 말해 주세요.'
)
_GENERAL_TOOL_FORBIDDEN_MESSAGE = (
    '요청을 안전하게 처리할 수 없어 로봇 행동을 실행하지 않습니다.'
)


class RoutedAgentProvider(AgentProvider):
    """Delegate one uncached turn to exactly one selected provider."""

    def __init__(
        self,
        routing_service: FrontRoutingService,
        *,
        general_provider: AgentProvider,
        robot_planner_provider: AgentProvider,
        fallback_provider: AgentProvider,
        policy_revision: str = DEFAULT_ROUTING_POLICY_REVISION,
    ) -> None:
        """Bind one Router and three explicit, non-cascading providers."""
        if not isinstance(routing_service, FrontRoutingService):
            raise TypeError(
                'routing_service must be a FrontRoutingService'
            )
        for name, provider in (
            ('general_provider', general_provider),
            ('robot_planner_provider', robot_planner_provider),
            ('fallback_provider', fallback_provider),
        ):
            if not isinstance(provider, AgentProvider):
                raise TypeError(f'{name} must be an AgentProvider')
        self._validate_policy_revision(policy_revision)
        self.routing_service = routing_service
        self.general_provider = general_provider
        self.robot_planner_provider = robot_planner_provider
        self.fallback_provider = fallback_provider
        self.policy_revision = policy_revision

    def complete(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
    ) -> ProviderResult:
        """Route once and return without trying a second provider."""
        front_request = self._front_request(
            request,
            conversation_turns,
        )
        try:
            match = self.routing_service.try_route(front_request)
        except FrontRoutingError:
            return self._local_result(
                decision_type='refusal',
                message=_ROUTER_UNAVAILABLE_MESSAGE,
                reason='front_route:router_unavailable',
            )

        if match is None:
            return self._delegate(
                self.fallback_provider,
                request,
                memories,
                conversation_turns,
                tools,
                conversation_summary,
            )
        if match.route is FrontRoute.GENERAL_CONVERSATION:
            general_request = self._without_tools(request)
            result = self._delegate(
                self.general_provider,
                general_request,
                memories,
                conversation_turns,
                [],
                conversation_summary,
            )
            if result.decision.type == 'tool_call':
                sanitized = replace(
                    result,
                    decision=self._local_decision(
                        decision_type='refusal',
                        message=_GENERAL_TOOL_FORBIDDEN_MESSAGE,
                        reason=(
                            'front_route:general_tool_forbidden'
                        ),
                    ),
                )
                sanitized.validate()
                return sanitized
            return result
        if match.route is FrontRoute.CLARIFICATION_REQUIRED:
            return self._local_result(
                decision_type='clarification',
                message=_CLARIFICATION_MESSAGE,
                reason='front_route:clarification_required',
            )
        if match.route is FrontRoute.ROBOT_STATUS_QUERY:
            return self._local_result(
                decision_type='refusal',
                message=_ROBOT_STATUS_UNAVAILABLE_MESSAGE,
                reason='front_route:robot_status_unavailable',
            )
        if match.route is FrontRoute.CURRENT_ACTION_QUERY:
            return self._local_result(
                decision_type='refusal',
                message=_CURRENT_ACTION_UNAVAILABLE_MESSAGE,
                reason='front_route:current_action_unavailable',
            )
        if match.route is FrontRoute.ROBOT_ACTION_REQUEST:
            return self._delegate(
                self.robot_planner_provider,
                request,
                memories,
                conversation_turns,
                tools,
                conversation_summary,
            )
        raise ProviderError('front route is unsupported')

    def _front_request(
        self,
        request: AgentRequest,
        conversation_turns: Sequence[ConversationTurn],
    ) -> FrontRouteRequest:
        if not isinstance(request, AgentRequest):
            raise TypeError('request must be an AgentRequest')
        try:
            recent_messages = self._recent_messages(
                conversation_turns
            )
            return FrontRouteRequest(
                request_id=request.request_id,
                user_message=request.utterance,
                recent_messages=recent_messages,
            )
        except (TypeError, ValueError):
            raise ProviderError(
                'front route request projection failed'
            ) from None

    @staticmethod
    def _recent_messages(
        conversation_turns: Sequence[ConversationTurn],
    ) -> tuple[FrontMessage, ...]:
        if isinstance(conversation_turns, (str, bytes)):
            raise TypeError('conversation_turns must be a sequence')
        candidates: list[FrontMessage] = []
        for turn in conversation_turns:
            if not isinstance(turn, ConversationTurn):
                raise TypeError(
                    'conversation_turns contains an invalid turn'
                )
            for role, content in (
                (FrontMessageRole.USER, turn.user_content),
                (FrontMessageRole.ASSISTANT, turn.assistant_content),
            ):
                if type(content) is not str:
                    raise TypeError(
                        'conversation turn content must be a string'
                    )
                normalized = content.strip()
                if not normalized:
                    continue
                candidates.append(FrontMessage(
                    role=role,
                    content=normalized[
                        :MAX_FRONT_HISTORY_MESSAGE_CHARS
                    ],
                ))

        selected: list[FrontMessage] = []
        included_chars = 0
        for message in reversed(candidates):
            if len(selected) >= MAX_FRONT_HISTORY_MESSAGES:
                break
            next_size = included_chars + len(message.content)
            if next_size > MAX_FRONT_HISTORY_CHARS:
                break
            selected.append(message)
            included_chars = next_size
        selected.reverse()
        return tuple(selected)

    @staticmethod
    def _without_tools(request: AgentRequest) -> AgentRequest:
        value = request.to_dict()
        value['available_tools'] = []
        return AgentRequest.from_dict(value)

    @staticmethod
    def _delegate(
        provider: AgentProvider,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary],
    ) -> ProviderResult:
        result = provider.complete(
            request,
            memories,
            conversation_turns,
            tools,
            conversation_summary=conversation_summary,
        )
        if not isinstance(result, ProviderResult):
            raise ProviderError(
                'selected provider returned an invalid result'
            )
        try:
            result.validate()
        except (TypeError, ValidationError):
            raise ProviderError(
                'selected provider returned an invalid result'
            ) from None
        return result

    def _local_result(
        self,
        *,
        decision_type: str,
        message: str,
        reason: str,
    ) -> ProviderResult:
        result = ProviderResult(
            decision=self._local_decision(
                decision_type=decision_type,
                message=message,
                reason=reason,
            ),
            provider='malbut-front-policy',
            model=self.policy_revision,
            latency_ms=0.0,
            input_chars=0,
        )
        result.validate()
        return result

    @staticmethod
    def _local_decision(
        *,
        decision_type: str,
        message: str,
        reason: str,
    ) -> AgentDecision:
        decision = AgentDecision(
            type=decision_type,
            message=message,
            reason=reason,
            confidence=1.0,
        )
        decision.validate()
        return decision

    @staticmethod
    def _validate_policy_revision(value: str) -> None:
        if (
            type(value) is not str
            or not value
            or value != value.strip()
            or len(value) > 128
            or any(
                not (
                    character.isascii()
                    and (
                        character.isalnum()
                        or character in {'_', '-', '.'}
                    )
                )
                for character in value
            )
        ):
            raise ValueError('policy_revision is invalid')
