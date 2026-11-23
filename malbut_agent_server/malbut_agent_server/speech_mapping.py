"""Literal field entry is only available after a published map-name question."""

from malbut_agent_server.schemas import SpeechAgentRequest, ValidationError
from malbut_agent_server.tools import normalize_mapping_name


MAPPING_NAME_INSTRUCTIONS = (
    'mapping_context.awaiting_name이 true이면 짧은 이름 답은 request_mapping의 map_name을 채우며 '
    '장소와 같은 이름도 이동 목적지가 아니다. 따옴표 안은 그대로 쓰고, 따옴표 없는 "작업실로 해"에서는 '
    '이름 "작업실"을 추출한다. 명시적 이동·순찰·번역·설명·거절은 현재 발화대로 처리하고 응·네만으로 이름을 만들지 않는다. '
    '지도 이름이 없거나 잘못돼 묻는 경우에만 type=clarification, clarification_for=mapping_name을 쓰고 그 외엔 null이다. '
    'awaiting_name이 false이면 이름만으로 이전 지도 요청을 재개하지 않는다.'
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
