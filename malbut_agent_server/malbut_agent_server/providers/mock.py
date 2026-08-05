"""Deterministic Korean provider used for safe integration tests."""

import re
import time
import unicodedata
from typing import List, Optional

from malbut_agent_server.conversation import (
    ConversationSummary,
    ConversationTurn,
)
from malbut_agent_server.memory import MemoryRecord
from malbut_agent_server.prompting import (
    MAX_CONVERSATION_TURNS,
    MAX_MODEL_INPUT_CHARS,
    prepare_model_input,
)
from malbut_agent_server.providers.base import AgentProvider
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ProviderResult,
)
from malbut_agent_server.tools import ToolSpec


def _normalized(value: str) -> str:
    return unicodedata.normalize('NFKC', value).casefold()


class MockProvider(AgentProvider):
    """Small rule-based provider that never calls a network service."""

    name = 'mock'
    model = 'malbut-korean-rules-v1'

    def __init__(
        self,
        max_model_input_chars: int = MAX_MODEL_INPUT_CHARS,
    ) -> None:
        """Set the same bounded context policy used by live providers."""
        self.max_model_input_chars = max_model_input_chars

    def complete(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
    ) -> ProviderResult:
        """Return a predictable response for regression and safety tests."""
        del tools
        started = time.perf_counter()
        decision = self._decide(
            request,
            memories,
            conversation_turns,
        )
        decision.validate()
        elapsed = (time.perf_counter() - started) * 1000
        prepared = prepare_model_input(
            request,
            memories,
            conversation_turns,
            conversation_summary,
            self.max_model_input_chars,
            MAX_CONVERSATION_TURNS,
        )
        return ProviderResult(
            decision=decision,
            provider=self.name,
            model=self.model,
            latency_ms=elapsed,
            input_chars=prepared.metrics.model_input_chars,
            context_metrics=prepared.metrics,
        )

    def _decide(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
    ) -> AgentDecision:
        text = _normalized(request.utterance)
        compact = re.sub(r'\s+', '', text)

        if any(
            phrase in compact
            for phrase in (
                'api키',
                'apikey',
                '환경변수',
                'openai_api_key',
                '비밀번호',
            )
        ):
            return self._refusal(
                '비밀정보는 읽거나 전송할 수 없어.',
                'secret_exfiltration',
            )
        if any(
            phrase in compact
            for phrase in (
                'cmd_vel',
                '모터pwm',
                '안전규칙은무시',
                '안전장치해제',
                '벽에부딪',
                '최고속도로달',
            )
        ):
            return self._refusal(
                '저수준 제어나 위험한 이동 요청은 실행할 수 없어.',
                'unsafe_motion_request',
            )
        if '현관문' in compact and any(
            word in compact for word in ('열어', '잠금해제')
        ):
            return self._refusal(
                'Malbut에는 문을 여는 기능이 없어.',
                'unsupported_capability',
            )

        follow_up = self._history_follow_up(
            compact,
            conversation_turns,
        )
        if follow_up is not None:
            return follow_up

        if any(
            phrase in compact
            for phrase in (
                '기억해둔대로움직',
                '아까말한데로가',
                '저쪽방으로가',
                '저기로가',
            )
        ):
            return self._clarification(
                '어느 목적지로 갈지 이름을 말해줘.',
                'ambiguous_destination',
            )

        locations = self._extract_locations(compact)
        if locations and any(
            phrase in compact
            for phrase in (
                '가지마',
                '가지말',
                '이동하지마',
                '이동하지말',
                '가면안',
            )
        ):
            return AgentDecision(
                type='message',
                message='알겠어. 이동하지 않을게.',
                reason='negated_navigation_request',
                confidence=1.0,
            )
        movement_requested = any(
            word in compact
            for word in (
                '가줘',
                '이동',
                '와줘',
                '가자',
                '로가',
            )
        )
        if movement_requested and len(locations) > 1:
            return self._clarification(
                '한 번에 한 목적지만 말해줘.',
                'multiple_actions',
            )
        if (
            '찾' in compact
            and any(word in compact for word in ('사진', '찍'))
        ):
            return self._clarification(
                '찾기와 촬영 중 무엇을 먼저 할지 말해줘.',
                'multiple_actions',
            )

        if any(word in compact for word in ('할수있는', '기능')):
            return AgentDecision(
                type='message',
                message=(
                    '이동, 반려동물 감지, 사진 촬영, 상태 확인과 '
                    '알림 요청을 도와줄 수 있어.'
                ),
                reason='capability_question',
                confidence=1.0,
            )

        if (
            any(word in compact for word in ('알림', '전해줘'))
            or ('가족' in compact and '알려줘' in compact)
        ):
            subject = '요청한 내용을 확인했어.'
            if '초코' in compact and '찾' in compact:
                subject = '초코를 찾았어.'
            return AgentDecision(
                type='tool_call',
                message='가족에게 알림을 보낼게.',
                tool_name='send_notification',
                arguments={
                    'message': subject,
                    'image_id': None,
                },
                reason='notification_request',
                confidence=0.95,
            )

        if movement_requested and locations:
            return AgentDecision(
                type='tool_call',
                message=f'{locations[0]} 이동을 요청할게.',
                tool_name='navigate',
                arguments={'location': locations[0]},
                reason='named_navigation_request',
                confidence=0.98,
            )

        if any(word in compact for word in ('사진', '촬영', '찍어')):
            return AgentDecision(
                type='tool_call',
                message='현재 장면 촬영을 요청할게.',
                tool_name='capture_photo',
                arguments={},
                reason='photo_request',
                confidence=0.98,
            )

        if (
            any(word in compact for word in ('반려동물', '강아지', '고양이'))
            or '초코' in compact
        ) and any(
            word in compact
            for word in ('찾아', '보이는지', '감지', '확인')
        ):
            return AgentDecision(
                type='tool_call',
                message='카메라에서 반려동물을 찾아볼게.',
                tool_name='detect_pet',
                arguments={},
                reason='pet_detection_request',
                confidence=0.96,
            )

        if '배터리' in compact or '상태확인' in compact:
            return AgentDecision(
                type='tool_call',
                message='로봇 상태를 확인할게.',
                tool_name='get_robot_status',
                arguments={},
                reason='status_request',
                confidence=0.98,
            )

        if self._looks_like_memory_question(compact):
            memory = self._best_safe_memory(memories)
            if memory is None:
                return self._clarification(
                    '확인할 수 있는 기억이 없어. 먼저 알려줄래?',
                    'memory_not_found',
                )
            return AgentDecision(
                type='message',
                message=f'기억해 둔 내용은 “{memory.content}”이야.',
                reason='retrieved_verified_memory',
                confidence=min(1.0, memory.confidence),
            )

        if any(word in compact for word in ('안녕', '반가워')):
            return AgentDecision(
                type='message',
                message='안녕! 오늘도 안전하게 같이 해보자.',
                reason='greeting',
                confidence=1.0,
            )
        if '고마워' in compact or '잘했어' in compact:
            return AgentDecision(
                type='message',
                message='고마워! 필요하면 언제든 불러줘.',
                reason='thanks',
                confidence=1.0,
            )
        return self._clarification(
            '요청을 정확히 이해하지 못했어. 한 가지 작업으로 말해줘.',
            'intent_unclear',
        )

    @staticmethod
    def _history_follow_up(
        compact: str,
        conversation_turns: List[ConversationTurn],
    ) -> Optional[AgentDecision]:
        if not conversation_turns:
            return None
        previous = conversation_turns[-1]
        if any(
            phrase in compact
            for phrase in (
                '아까뭐라고',
                '방금뭐라고',
                '아까말한것',
                '내가뭐라고했',
            )
        ):
            return AgentDecision(
                type='message',
                message=(
                    '아까 “'
                    f'{previous.user_content}'
                    '”라고 말했어.'
                ),
                reason='conversation_history_user_reference',
                confidence=1.0,
            )
        if any(
            phrase in compact
            for phrase in (
                '그거다시',
                '그게뭐였',
                '그거뭐였',
            )
        ):
            return AgentDecision(
                type='message',
                message=(
                    '내가 앞서 “'
                    f'{previous.assistant_content}'
                    '”라고 답했어.'
                ),
                reason='conversation_history_assistant_reference',
                confidence=1.0,
            )
        if '그사람' in compact:
            candidates = [
                turn.user_content
                for turn in reversed(conversation_turns)
                if any(
                    marker in turn.user_content
                    for marker in (
                        '사람',
                        '님',
                        '민수',
                        '영희',
                        '엄마',
                        '아빠',
                    )
                )
            ]
            if len(candidates) == 1:
                return AgentDecision(
                    type='message',
                    message=(
                        '앞선 대화에서는 “'
                        f'{candidates[0]}'
                        '”라고 했어.'
                    ),
                    reason='conversation_history_person_reference',
                    confidence=0.95,
                )
            return AgentDecision(
                type='clarification',
                message='어느 사람을 말하는지 이름을 알려줘.',
                reason='ambiguous_person_reference',
                confidence=0.9,
            )
        return None

    @staticmethod
    def _extract_locations(compact: str) -> List[str]:
        aliases = (
            ('거실', ('거실',)),
            ('주방', ('주방', '부엌')),
            ('침실', ('침실',)),
            ('현관', ('현관',)),
            ('충전소', ('충전소', '도크')),
            ('베란다', ('베란다',)),
        )
        return [
            location
            for location, patterns in aliases
            if any(pattern in compact for pattern in patterns)
        ]

    @staticmethod
    def _looks_like_memory_question(compact: str) -> bool:
        return (
            any(
                word in compact
                for word in ('이름', '좋아하는곳', '좋아하는', '뭐였지')
            )
            and any(
                word in compact
                for word in (
                    '뭐',
                    '어디',
                    '곳은',
                    '기억',
                    '였지',
                    '알려',
                )
            )
        )

    @staticmethod
    def _best_safe_memory(
        memories: List[MemoryRecord],
    ) -> Optional[MemoryRecord]:
        for memory in memories:
            content = _normalized(memory.content)
            if any(
                marker in content
                for marker in (
                    'system:',
                    '규칙을 무시',
                    '이동하라',
                    '명령',
                )
            ):
                continue
            return memory
        return None

    @staticmethod
    def _refusal(message: str, reason: str) -> AgentDecision:
        return AgentDecision(
            type='refusal',
            message=message,
            reason=reason,
            confidence=1.0,
        )

    @staticmethod
    def _clarification(message: str, reason: str) -> AgentDecision:
        return AgentDecision(
            type='clarification',
            message=message,
            reason=reason,
            confidence=0.9,
        )
