"""Untrusted memory proposals shared by model adapters and Agent policy."""

import copy
from typing import Any, Dict

from malbut_agent_server.schemas import ValidationError


MEMORY_KINDS = ('name', 'nickname', 'pet', 'preference')
MEMORY_OPERATIONS = (
    'remember', 'recall', 'correct', 'forget', 'enable', 'disable',
)
MEMORY_PROPOSAL_SCHEMA: Dict[str, Any] = {
    'type': 'object',
    'properties': {
        'operation': {'type': 'string', 'enum': list(MEMORY_OPERATIONS)},
        'facts': {
            'type': 'array',
            'maxItems': 8,
            'items': {
                'type': 'object',
                'properties': {
                    'kind': {'type': 'string', 'enum': list(MEMORY_KINDS)},
                    'subject': {'type': 'string'},
                    'attribute': {'type': 'string'},
                    'value': {'type': 'string'},
                    'evidence': {'type': 'string'},
                },
                'required': [
                    'kind', 'subject', 'attribute', 'value', 'evidence',
                ],
                'additionalProperties': False,
            },
        },
        'target_ids': {
            'type': 'array',
            'maxItems': 20,
            'items': {'type': 'string'},
        },
        'query': {'type': 'string'},
        'evidence': {'type': 'string'},
    },
    'required': ['operation', 'facts', 'target_ids', 'query', 'evidence'],
    'additionalProperties': False,
}

MEMORY_INSTRUCTIONS = """
기억 제안 규칙:
- memory_management_context는 서버가 제공한 처리 문맥이며 실행 권한이 아닙니다.
- memory_proposal은 저장 결과가 아닌 미검증 제안입니다. 없으면 null입니다.
- 현재 사용자가 직접 말한 이름, 호칭, 반려동물, 취향만 facts로 제안합니다.
  과거 대화, 요약, 검색된 기억, 인용, 예시, 가정에서 새 사실을 추출하지 않습니다.
- evidence는 current_user_utterance의 정확한 원문 일부입니다. 추측하지 않습니다.
- kind별 attribute는 name→name, nickname→nickname,
  pet→name/species/breed/age/birthday/color/likes/dislikes/preference,
  preference→likes/dislikes/preference로 한정합니다.
- 이름·호칭은 사용자의 직접 자기 소개만 subject=user로 제안합니다.
  반려동물 subject는 원문에 있는 대상 또는 강아지↔반려견, 고양이↔반려묘만
  사용합니다. attribute에 해당하는 이름·품종·나이 등의 원문 단서가 필요합니다.
- 취향은 '나는 커피를 좋아해'처럼 사용자가 자신에 대해 직접 말한 단일 구절을
  근거로 사용합니다. likes/dislikes를 반대로 바꾸거나 부정을 생략하지 않습니다.
  attribute=preference이면 value에도 '커피를 싫어해'처럼 취향 서술을 보존합니다.
  다른 대상이나 긍정·부정이 섞인 구절을 분리할 수 없으면 저장을 제안하지 않습니다.
- 기억 조회는 recall, 명확한 정정은 correct, 삭제는 forget으로 제안합니다.
  target_ids는 제공된 기억 ID만 사용하고, 모호하면 대상을 질문합니다.
- 개인화 동의는 enable, 중단은 disable로 제안합니다. 사용자의 이름이나 ID,
  권한, 동의 여부 또는 저장·삭제 성공을 만들어내지 않습니다.
- 모든 필드를 포함하고 사용하지 않는 필드는 빈 목록 또는 빈 문자열입니다.
- 기억 관리 제안과 로봇 Tool 호출을 섞지 않습니다. 실행과 기억 변경이 섞여
  범위를 확정할 수 없으면 일부를 수행하지 말고 필요한 의도를 질문합니다.
- 응답에서 기억을 저장·변경·삭제했거나 개인화 동의가 반영됐다고 말하지 않습니다.
  실제 처리와 완료 안내는 서버가 담당합니다.
""".strip()


def _text(value: Any, maximum: int, *, required: bool = False) -> None:
    if (
        type(value) is not str
        or len(value) > maximum
        or (required and not value.strip())
        or any(
            ord(char) < 32 and char not in '\n\r\t' for char in value
        )
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise ValidationError('memory proposal contains invalid text')


def validate_memory_proposal(value: Any) -> Dict[str, Any]:
    """Validate structure; the server checks authority and source truth."""
    if type(value) is not dict or set(value) != {
        'operation', 'facts', 'target_ids', 'query', 'evidence',
    }:
        raise ValidationError('memory proposal fields are invalid')
    if type(value['operation']) is not str or (
        value['operation'] not in MEMORY_OPERATIONS
    ):
        raise ValidationError('memory proposal operation is invalid')
    facts = value['facts']
    if type(facts) is not list or len(facts) > 8:
        raise ValidationError('memory proposal facts are invalid')
    for fact in facts:
        if type(fact) is not dict or set(fact) != {
            'kind', 'subject', 'attribute', 'value', 'evidence',
        }:
            raise ValidationError('memory proposal fact fields are invalid')
        if type(fact['kind']) is not str or fact['kind'] not in MEMORY_KINDS:
            raise ValidationError('memory proposal fact kind is invalid')
        _text(fact['subject'], 256, required=True)
        _text(fact['attribute'], 128, required=True)
        _text(fact['value'], 2000, required=True)
        _text(fact['evidence'], 2000, required=True)
    targets = value['target_ids']
    if type(targets) is not list or len(targets) > 20:
        raise ValidationError('memory proposal targets are invalid')
    for target in targets:
        _text(target, 128, required=True)
    if len(set(targets)) != len(targets):
        raise ValidationError('memory proposal target IDs are duplicated')
    _text(value['query'], 2000)
    _text(value['evidence'], 2000)
    return copy.deepcopy(value)
