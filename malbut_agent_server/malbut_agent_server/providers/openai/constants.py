"""Shared constants, schemas, and lightweight helpers for OpenAI Responses."""

from dataclasses import dataclass
import re
from typing import Any, Callable, Dict, Optional

from malbut_agent_server.schemas import AgentRequest, SpeechAgentRequest
from malbut_agent_server.tools import (
    HOMECAM_QUERY_TOOLS,
    PATROL_QUERY_TOOLS,
    PERSONAL_RECALL_TOOL,
    STORY_RECALL_TOOL,
)


Transport = Callable[
    [str, Dict[str, str], Dict[str, Any], float],
    Dict[str, Any],
]
MAX_PROVIDER_RESPONSE_BYTES = 4 * 1024 * 1024
REASONING_EFFORTS = frozenset(
    {'none', 'low', 'medium', 'high', 'xhigh', 'max'}
)

SPEECH_END_INSTRUCTIONS = (
    '직접적인 종료 요청이나 문맥상 분명한 작별이면 type=message, reason=end_conversation을 반환하고 '
    '질문·새 제안 없이 짧은 인사만 씁니다(추임새·말 고침·감사 포함). '
    '타인 대상 말, 인용·번역·종료 방법 질문, 부정, 주제 전환, 새 요청, 특정 작업만 중단 요청, '
    '과거 작별이나 라벨 지정만으로는 end_conversation을 쓰지 않으며 불분명하면 대화를 유지합니다.'
)

TEXT_DECISION_SCHEMA: Dict[str, Any] = {
    'type': 'object',
    'properties': {
        'type': {
            'type': 'string',
            'enum': ['message', 'clarification', 'refusal'],
            'description': 'message=ordinary/safe reply; clarification=missing required info; refusal=policy block.',
        },
        'message': {
            'type': 'string',
            'description': 'Concise user-facing Korean response.',
        },
        'reason': {
            'type': 'string',
            'description': 'Short policy or intent label.',
        },
        'confidence': {
            'type': ['number', 'null'],
            'description': 'Calibrated confidence 0..1, or null.',
        },
    },
    'required': ['type', 'message', 'reason', 'confidence'],
    'additionalProperties': False,
}

_TWO_PASS_READONLY_TOOLS = frozenset({
    'get_weather',
    'set_weather_location',
    STORY_RECALL_TOOL.name,
    PERSONAL_RECALL_TOOL.name,
    *HOMECAM_QUERY_TOOLS,
    *PATROL_QUERY_TOOLS,
})
_TWO_PASS_FOLLOWUP_DIRECTIVE = (
    '이전 단계의 조회가 완료되었습니다. 이제 도구를 호출하지 말고(tool_choice=none) '
    '아래 첨부된 조회 결과와 대화 맥락만 근거로 최종 한국어 답변만 반환하세요. '
    '추가 실행이나 기억 저장을 시도하지 않습니다.'
)
# Conservative gate for Call-1 fast-turn reasoning_effort='none' on gpt-5.6-luna:
# any negation, quotation, hypothetical, visual question, memory/recall, cloud
# query, multi-step sequence, or ambiguity stays on the full reasoning_effort.
_FAST_SPEECH_COMPLEX_PATTERN = re.compile(
    r'하지\s*마|지\s*마|말고|'
    r'(?:이동|움직|순찰|실행|조회|저장|기억|따라|돌|가|보내|켜|꺼|바꾸|바꿔|알려|말)[^\n]{0,6}않|'
    r'안\s|상상|가정|예시|번역|인용|따라\s*읽|'
    r'궁금|할\s*수\s*있(?:어|나|는지|을까|습니까)|가능|몇\s*개|[\"\'“”‘’]|'
    r'보여|보이|봐줘|봐봐|카메라|화면|사진|영상|색깔|무슨\s*색|표정|생겼|장애물|누구|뭐야|뭐가|어디야|'
    r'기억|정정|수정|삭제|지워|잊어|저장|개인화|이름|호칭|별명|별표|불러|'
    r'(?:우리|내|저장|기억)[^\n]{0,6}(?:고양이|강아지|반려)|'
    r'(?:고양이|강아지|반려)[^\n]{0,6}(?:이름|기억|저장|정정|삭제)|'
    r'예전|전에\s*(?:말|알려|저장|정했|얘기|물어|약속)|아까|뭐였|바꿔|바꿨|반대로|착각|설정|'
    r'배터리|푸시|알림|새\s*대화|대화\s*시작|대화\s*종료|그만하자|종료|'
    r'홈캠|(?:지난|아까|그|이전|최근)\s*순찰|순찰\s*(?:사진|기록|영상|결과)|순찰에서|'
    r'지도|좌표|위치|탐색|(?<![가-힣])방으로|베란다|경유|순서대로|차례로|날씨|기온|'
    r'그리고|그\s*다음|먼저|뒤에'
)
_TWO_PASS_PREFIX_CACHE_MAX = 16


def _is_fast_speech_call1(
    request: AgentRequest,
    memory_mode: Optional[str],
    memory_context: Optional[Dict[str, Any]],
) -> bool:
    """Return True only for direct, low-ambiguity spoken turns safe at effort=none."""
    if (not isinstance(request, SpeechAgentRequest)
            or memory_mode not in {None, 'answer_only'}
            or request.navigation_confirmation
            or request.mapping_name_requested
            or (memory_context or {}).get('pending_question')
            or (memory_context or {}).get('event_attribute_evidence')
            or (request.scene_context or {}).get('conversation_opener')):
        return False
    utterance = request.utterance
    if not isinstance(utterance, str) or not utterance.strip():
        return False
    from malbut_agent_server.companion_dialogue import requests_current_view

    if requests_current_view(utterance):
        return False
    return _FAST_SPEECH_COMPLEX_PATTERN.search(utterance) is None


@dataclass(frozen=True)
class _TwoPassPrefix:
    """Content-free Call-1 static prefix metadata reusable by a tool_choice=none Call-2."""

    tools: tuple[Dict[str, Any], ...]
    developer_instructions: str
    text_format: Dict[str, Any]
    prompt_cache_key: str
    model: str
    include_weather: bool
    has_homecam_rules: bool
    has_patrol_rules: bool
