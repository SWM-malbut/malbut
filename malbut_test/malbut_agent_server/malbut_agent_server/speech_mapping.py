"""Literal field entry is only available after a published map-name question."""

from malbut_agent_server.schemas import SpeechAgentRequest, ValidationError
from malbut_agent_server.tools import normalize_mapping_name


MAPPING_NAME_INSTRUCTIONS = (
    'mapping_context.awaiting_name은 서버가 실제 전달한 지도 이름 질문의 현재 대기 상태다. '
    '이 값이 true이면 짧은 이름 답변은 request_mapping의 map_name을 채운다. '
    '작업실·거실·주방처럼 장소와 같은 이름도 이동 목적지가 아니다. '
    '따옴표로 감싼 이름은 내부 문자를 그대로 쓰고, 따옴표 없는 작업실로 또는 '
    '작업실로 해 같은 이름 지정 답변에서는 이름 작업실을 추출한다. '
    '거실로 가·이동해 같은 명시적 이동, 순찰 요청, 번역, 설명, 거절은 현재 발화대로 '
    '처리하고 이전 지도 요청을 이어 실행하지 않는다. 응·네만으로 이름을 만들지 않는다. '
    '새 지도 실행에 필요한 이름이 없어 묻거나 잘못된 이름을 다시 물을 때 반드시 '
    'type=clarification, clarification_for=mapping_name으로 응답한다. '
    '그 외 모든 텍스트 응답은 clarification_for=null이다. '
    'awaiting_name이 false이면 과거 기록에 이름 질문이 남아 있어도 이름만으로 '
    '지도 실행을 재개하지 않는다. 현재 발화의 명시적인 지도 실행 요청은 처리할 수 있다.'
)


def quoted_mapping_name(request):
    """Return an exact valid quoted field, never a substring of a command."""
    if not isinstance(request, SpeechAgentRequest) or not request.mapping_name_requested:
        return None
    text = request.utterance.strip()
    pairs = {'"': '"', "'": "'", '“': '”', '‘': '’'}
    if len(text) < 3 or pairs.get(text[0]) != text[-1]:
        return None
    value = text[1:-1]
    try:
        normalized = normalize_mapping_name(value)
    except (ValidationError, ValueError, TypeError):
        return None
    return value if value == normalized else None


def mapping_name_binding(request):
    """Bind literal input and a whole registered place plus naming particle.

    In a map-name field, '주방으로' supplies 주방; it contains no movement verb.
    Match the entire answer, never a place substring in a new command. Explicit
    quotation takes precedence, including a filename that itself ends in 로.
    Unregistered names and other phrasing remain semantic model decisions.
    """
    literal = quoted_mapping_name(request)
    if literal is not None:
        return literal
    if not isinstance(request, SpeechAgentRequest) or not request.mapping_name_requested:
        return None
    text = request.utterance.strip()
    names = request.navigation_locations or ()
    candidates = {name for name in names if text in {name + '로', name + '으로'}}
    if len(candidates) != 1 or text in names:
        return None
    name = candidates.pop()
    try:
        return name if normalize_mapping_name(name) == name else None
    except (ValidationError, ValueError, TypeError):
        return None
