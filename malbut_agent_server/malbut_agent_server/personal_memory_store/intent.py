"""Stateless personal-memory intent detection and source-evidence matching."""

import re
import unicodedata
from typing import Any

from malbut_agent_server.conversation_recall import (
    is_self_name_question,
    speech_recall_prohibited,
)
from malbut_agent_server.schemas import AgentDecision

_PET_ALIAS = {
    '강아지': '반려견',
    '반려견': '강아지',
    '고양이': '반려묘',
    '반려묘': '고양이',
}
_PET_ORDINALS = (
    '첫째', '둘째', '셋째', '넷째',
    '첫번째', '두번째', '세번째', '첫 번째', '두 번째', '세 번째',
)
_PET = r'(?:강아지|반려견|고양이|반려묘|토끼|햄스터|앵무새|거북이)'
_PET_OWNER = r'(?:우리(?:집)?|저희(?:집)?|내|제|나의|저의)'
_PET_LABEL = _PET_OWNER + '(?P<pet>' + _PET + r')(?:의)?이름'


def _missing_memory_followup(text: str) -> AgentDecision | None:
    """Ask about a clear command, never infer one from memory vocabulary.

    This only selects user-facing wording when no valid proposal exists;
    retrieval, consent, source validation and mutation authority are unchanged.
    """
    if not direct_source(text) or negated_management(text):
        return None
    # This is a receipt/follow-up check, not a new intent classifier. Bare
    # "기억해" may describe recollection; generic editing is not memory work.
    text = re.sub(r'(기억|저장)\s*해(?:요)?\s*[?？]', '', text)
    ending = r'(?:해\s*(?:줘요?|주세요|둬|두자|줄래|주라)|하자)'
    memory_target = bool(re.search(r'기억|개인화', text))
    # Saving a map/file is not saving a personal fact. A missing proposal must
    # not replace the model's robot request or its question about a map name.
    personal_target = bool(re.search(
        r'(?:내|제|나의|저의)\s*(?:이름|호칭|별명|취향)', text,
    ))
    bare_save = bool(re.fullmatch(
        r'\s*(?:(?:이름|호칭|별명|취향)[을를]?\s*)?저장\s*'
        + ending + r'\s*[.!?。？]?\s*', text,
    ))
    commands = (
        (r'삭제\s*' + ending + r'|지워\s*(?:줘요?|주세요)|잊어\s*줘',
         '어떤 내용을 지울지 다시 알려줄래요?', memory_target),
        (r'(?:정정|수정|변경)\s*' + ending + r'|바꿔\s*(?:줘요?|주세요)',
         '어떤 내용을 어떻게 바꿀지 다시 알려줄래요?', memory_target),
        (r'기억\s*' + ending,
         '기억해 둘 내용을 한 가지만 다시 알려줄래요?', True),
        (r'저장\s*' + ending,
         '기억해 둘 내용을 한 가지만 다시 알려줄래요?',
         memory_target or personal_target or bare_save),
    )
    for pattern, message, eligible in commands:
        if eligible and re.search(
            r'(?:' + pattern + r')(?=\s*(?:$|[.!?。？]))', text,
        ):
            return reply(message, True)
    return None


def compact(text: str) -> str:
    """Normalize comparison text without modifying the stored source."""
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', text)).casefold()


def selection(text: str) -> int | None:
    """Resolve only an exact answer to a numbered memory question."""
    answer = compact(text).rstrip('.!?。')
    ordinal = {
        '첫번째': 0,
        '첫째': 0,
        '두번째': 1,
        '둘째': 1,
        '세번째': 2,
        '셋째': 2,
    }.get(answer)
    if re.fullmatch(r'\d+번?', answer):
        ordinal = int(answer.rstrip('번')) - 1
    return ordinal


def direct_source(text: str) -> bool:
    """Conservatively exclude quotation, reported speech and hypotheticals."""
    return not (
        any(
            char in text
            for char in ('"', "'", '`', '“', '”', '‘', '’', '「', '」')
        )
        or re.search(
            r'예시|예를\s*들|만약|가정|라고\s*(했|말|적|써)|친구가|친구는',
            text,
        )
    )


def management_request(text: str) -> bool:
    """Identify explicit memory management, not ordinary personal facts."""
    return direct_source(text) and bool(
        re.search(
            r'기억|개인화|잊어|잊으|삭제|정정|수정|바꿔|아니라|정확히는',
            text,
        )
    )


def explicit_memory_management(text: str) -> bool:
    """Distinguish a memory conflict from ordinary robot self-correction.

    management_request deliberately retrieves broadly for corrections. That
    retrieval hint alone is not evidence of a second, memory-management task.
    Structured memory proposals still independently block mixed robot calls.
    """
    return management_request(text) and bool(re.search(
        r'기억|개인화|(?:내|제|나의|저의)\s*(?:이름|호칭|별명|취향)', text,
    ))


def local_intent(text: str) -> str | None:
    """Recognize clear consent controls without relying on model claims."""
    if not direct_source(text) or negated_management(text):
        return None
    value = compact(text).rstrip('.!?。')
    if value in {'기억사용하지않기', '사용하지않기', '기억사용중단', '기억사용중단해줘'}:
        return 'disable'
    if re.fullmatch(
        r'(개인화|자동저장)(를|는|을)?(중단|꺼|끄|그만|멈춰)'
        r'(해줘|해주세요|해|줘|주세요|할래|하자|해요)?',
        value,
    ):
        return 'disable'
    if re.fullmatch(
        r'(개인화|기억)(를|는|을)?(시작|켜|활성화)'
        r'(해줘|해주세요|해|줘|주세요|할래|하자|해요)?',
        value,
    ):
        return 'enable'
    return None


def negated_management(text: str) -> bool:
    """Do not turn a restriction or negated request into a mutation."""
    return bool(
        re.search(
            r'(기억|저장|삭제|수정|정정|변경|중단|지우|지워|바꾸|바꿔|잊).{0,8}'
            r'(하지|말아|말고|말라|않|금지)|안\s*(지워|바꿔|기억|저장|삭제)',
            text,
        )
    )


def requested_operation(text: str, operation: str) -> bool:
    """Require an operation-specific current instruction, not a model label."""
    if not direct_source(text):
        return False
    if negated_management(text):
        # A read prohibition may be followed by a separate affirmative edit.
        clauses = re.split(r'지\s*말고', text, maxsplit=1)
        if (not speech_recall_prohibited(text) or len(clauses) != 2
                or negated_management(clauses[1])):
            return False
        text = clauses[1]
    patterns = {
        'forget': r'삭제해|삭제하|잊어|잊으|지워|지우자|없애',
        'correct': r'정정해|정정하|수정해|수정하|변경해|바꿔|바꾸자|아니라|정확히는',
        'remember': r'기억해|기억하|기억해\s*둬|저장해|저장하',
    }
    return bool(re.search(patterns[operation], text))


def self_name_correction(text: str) -> re.Match[str] | None:
    """Match a standalone first-person edit, preserving name and word boundaries."""
    match = re.fullmatch(
        r'\s*(?:저장(?:해\s*둔|한|된)\s*)?(?:내|제|나의|저의)\s*(?:본명|이름)(?:을|를)?\s*'
        r'(?:(?P<old>[가-힣]{2,6}?)(?:에서|을|를)\s*)?'
        r'(?P<new>[가-힣]{2,6}?)(?:으로|로)\s*'
        r'(?:(?:정정|수정|변경)해|바꿔)(?:\s*(?:줘요?|주세요))?[.!。]?\s*', text,
    )
    if match and not any(word in match['new'] for word in (
        '이름', '본명', '별명', '호칭', '친구', '예전', '이전', '원래', '그대로',
    )):
        return match
    return None


def _self_introduction(text: str) -> str | None:
    """Read one name from an unbound first-person declaration."""
    if (not direct_source(text) or re.search(
            r'아니|아냐|몰라|비밀|뭐|뭔|기억|모르|말할|알려|바뀌|개명|'
            r'(?:일까|일걸|이래|같아|됐어|없어)[.!。]?\s*$', text)):
        return None
    match = re.fullmatch(
        r'\s*(?:내|제|나의|저의|사용자(?:의)?)\s*(?:본명|이름)(?:은|는|이|가|:)?\s*'
        r'(?P<name>[가-힣]{2,6}?)(?:입니다|이에요|예요|이야|야)?[.!。]?\s*', text,
    )
    return match['name'] if match else None


def _stored_identity_question(text: str) -> tuple[str, ...] | None:
    owner = r'(?:내|제|나의|저의)'
    saved = r'(?:장기기억에)?(?:저장(?:된|한|해둔)?|기억해둔)'
    name, nickname = r'(?:본명|이름)', r'(?:호칭|별명)'
    join = r'(?:과|와|이?랑|하고)'
    both = rf'(?:{name}{join}{owner}?{nickname}|{nickname}{join}{owner}?{name})'
    # The saved qualifier must cover the nickname or both items. Room-only
    # and otherwise unqualified nicknames still need the conversation.
    if re.fullmatch(
        r'(?:혹시)?(?:지금|현재)?(?:(?:내가|제가)(?:전에)?(?:알려준|말해준))?'
        rf'(?:(?:{owner}{saved}|{saved}{owner}|(?:내가|제가){saved}){both}|'
        rf'{owner}{name}{join}{owner}?{saved}{owner}?{nickname}|{saved}{nickname}{join}{owner}{name})'
        r'(?:을|를|은|는|이|가)?(?:목록으로|각각|둘다|모두|함께|같이)?'
        r'(?:(?:말해|알려)(?:줘요?|주세요|줄래요?)|뭐(?:야|예요|였지)|무엇인가요)[?？.!。]?', compact(text),
    ):
        return ('name', 'nickname')
    if is_self_name_question(text) and '저장' in text:
        return ('name',)
    match = re.fullmatch(
        r'\s*(?:새\s*대화야[.,!。]?\s*)?(?:(?:지금|현재)\s*)?(?:장기\s*기억에\s*)?'
        r'(?:저장된|저장해\s*둔|저장한)\s*(?:내|제|나의|저의)\s*'
        r'(?:호칭|별명)(?:이|가|은|는)?\s*(?:뭐야|뭐예요|뭔가요|무엇인가요)[?？.]?\s*',
        text,
    )
    return ('nickname',) if match else None


def _pet_name_question(text: str) -> str | None:
    """Identify a single owned pet's name without widening a compound request."""
    value = compact(text)
    if not value.endswith(('?', '？')) and re.search(r'기억해(?:요)?[.!。]?$', value):
        return None
    match = re.fullmatch(
        r'(?:혹시)?(?:지금|현재)?(?:전에(?:말한|알려준)|저장된|저장한)?' + _PET_LABEL
        + r'(?:이|은|을)?(?:좀)?(?:뭐(?:야|예요|였어|였지|였더라|지|죠)|(?:알려|말해)(?:줘요?|주세요|줄래요?)'
        r'|기억(?:해요?|나(?:요)?|하고있(?:어|니|나요|어요)))' r'[?？.!。]?', value)
    return match['pet'] if match else None


def _pet_name_declaration(text: str) -> re.Match[str] | None:
    """Read a single owned pet name predicate without joining words or clauses."""
    match = re.fullmatch(
        r'\s*(?:우리|저희|내|제|나의|저의)\s*(?:집\s*)?(?P<pet>' + _PET + r')\s*(?:의\s*)?'
        r'이름(?:은|는|이|가)\s*(?P<stem>[가-힣]{1,24}?)(?P<ending>입니다|이에요|예요|이야|야)[.!。]?'
        r'(?:\s*(?:' + _PET + r'\s*이름으로\s*)?(?:기억|저장)해\s*(?:줘요?|주세요))?[.!。]?\s*', text)
    if match and not re.search(r'아니|몰라|예시|가명|뭐|비밀|였', match['stem']):
        return match
    return None


def _pet_name_correction(text: str) -> re.Match[str] | None:
    """A single owned pet and both literal names must be stated in the edit."""
    match = re.fullmatch(
        r'\s*(?:우리|저희|내|제|나의|저의)\s*(?:집\s*)?(?P<pet>' + _PET + r')\s*(?:의\s*)?'
        r'이름(?:을|를)?\s*(?P<old>[가-힣]{1,24}?)에서\s*(?P<new>[가-힣]{1,24}?)(?:으로|로)\s*'
        r'(?:(?:정정|수정|변경)해|바꿔)(?:\s*(?:줘요?|주세요))?[.!。]?\s*', text)
    if match and not re.search(r'에서.*(?:이름|그대로|원래대로|예전대로|이전대로|에서)', text):
        return match
    return None


def _correct_name_fact(
    fact: dict[str, Any],
    correction: re.Match[str],
    text: str,
) -> bool:
    # A model may strip a final 이 as a spoken suffix. Here 로 is already the
    # particle, so the preceding 이 is part of the value supplied by the user.
    new = correction['new']
    return (fact['kind'] == fact['attribute'] == 'name'
            and fact['subject'] in {'user', 'self', '사용자', '나', '저'}
            and (fact['value'] == new or (new.endswith('이') and fact['value'] == new[:-1]))
            and fact['evidence'] in text and new in fact['evidence'])


def fact_matches_source(fact: dict[str, Any], source: str) -> bool:
    """Check the declared subject and attribute against one direct phrase."""
    evidence = fact['evidence']
    value = compact(fact['value'])
    raw = compact(evidence)
    subject = compact(fact['subject'])
    attribute = compact(fact['attribute'])
    kind = fact['kind']
    if evidence not in source or not value or value not in raw:
        return False
    remaining_source = compact(source).replace(raw, '', 1)
    if re.search(r'정정|취소|아니|않|싫|실은|사실은|예전|지금|하지만|그런데|반면',
                 remaining_source):
        # A clipped quote can hide a later correction or a different polarity.
        # Resolve the full source semantically instead of approving the clip.
        return False
    self_subject = subject in {'user', 'self', '사용자', '나', '저'}
    self_name = bool(re.search(r'(내|제|나의|저의)이름', raw))
    self_nickname = bool(re.search(
        r'(내|제|나의|저의)(호칭|별명)|(나를|저를).+불러', raw,
    ))
    if kind == 'name':
        return (self_subject and attribute == 'name' and self_name
                and _label_value('이름', value, raw))
    if kind == 'nickname':
        named = _label_value('(호칭|별명)', value, raw) or bool(re.search(
            r'(나를|저를)' + re.escape(value) + r'(이?라고)불러', raw,
        ))
        return (self_subject and attribute == 'nickname'
                and self_nickname and named)
    if kind == 'preference':
        if not self_subject or not re.match(
            r'^(나는|저는|난|전|내가|제가|내취향|제취향|내선호|제선호)', raw,
        ):
            return False
        return preference_matches_source(attribute, value, raw)
    if kind != 'pet' or self_subject:
        return False
    if not pet_owner_is_unambiguous(fact, source):
        return False
    if not re.search(
        r'반려동물|강아지|반려견|고양이|반려묘|토끼|햄스터|앵무새|거북이', raw,
    ):
        return False
    if not any(name in raw for name in (subject, _PET_ALIAS.get(subject, subject))):
        return False
    if any(word in raw for word in ('강아지', '반려견')) and any(
        word in raw for word in ('고양이', '반려묘')
    ):
        return False
    labels = {
        'name': '이름', 'species': '(종류|종)',
        'breed': '(품종|견종|묘종)', 'age': '나이',
        'birthday': '(생일|태어난날짜|태어난날)',
        'color': '(털색|색깔|색)',
    }
    if attribute in labels:
        return _label_value(labels[attribute], value, raw)
    return preference_matches_source(attribute, value, raw)


def pet_owner_is_unambiguous(fact: dict[str, Any], source: str) -> bool:
    """Only fast-approve explicit personal ownership; otherwise ask the reviewer.

    Check the full source as well as the selected evidence, so trimming 'the
    neighbor's' from a quote cannot turn a third-party pet into the user's pet.
    Ambiguity routes to semantic review rather than rejecting a valid candidate.
    """
    raw = compact(fact['evidence'])
    ownership = r'(?:우리(?:집)?|저희(?:집)?|내|제|나의|저의)'
    animal = r'(?:반려동물|강아지|반려견|고양이|반려묘|토끼|햄스터|앵무새|거북이)'
    ordinal = '(?:' + '|'.join(_PET_ORDINALS) + ')?'
    if not (re.search(ownership + ordinal + animal, raw)
            or (requested_operation(source, 'correct')
                and re.match(animal + r'(?:의)?(?:이름|품종|나이|생일|털색)', raw))):
        return False
    return not re.search(
        r'옆집|이웃|다른집|친구(?:의|네)|동료(?:의|네)|'
        r'(?:동생|형|누나|언니|오빠|부모님)(?:의|네)|주인|소유|입양',
        compact(source),
    )


def _label_value(label: str, value: str, text: str) -> bool:
    correction = r'(?:[가-힣A-Za-z0-9]+(?:가|이)아니라)?'
    matched = re.search(
        label + r'(?:은|는|이|가|을|를|:)?' + correction
        + re.escape(value), text,
    )
    # A value mentioned before "아니야/아니라" is not an affirmed fact.
    return matched is not None and not re.match(
        r'(?:이|가)?아니', text[matched.end():],
    )


def preference_matches_source(attribute: str, value: str, evidence: str) -> bool:
    """Preserve preference polarity and reject ambiguous combined clauses."""
    if attribute not in {'likes', 'dislikes', 'preference'}:
        return False
    text = evidence.rstrip('.!?。')
    if re.search(
        r'그리고|하지만|반면|그런데|아니|[,.!?;\n]|안싫|싫어하지않|할지도|일수',
        text,
    ):
        return False
    negative = bool(re.search(r'싫어|안좋아|안선호|좋아하지않|선호하지않', text))
    positive = bool(re.search(r'좋아|선호', text)) and not negative
    # Multiple predicates can associate the right sentiment with a wrong noun.
    if len(re.findall(r'좋아|싫어|선호', text)) != 1:
        return False
    if attribute == 'likes':
        return positive and _preference_object(value, text)
    if attribute == 'dislikes':
        return negative and _preference_object(value, text)
    value_negative = bool(re.search(
        r'싫어|안좋아|안선호|좋아하지않|선호하지않', value,
    ))
    value_positive = bool(re.search(r'좋아|선호', value)) and not value_negative
    return (positive and value_positive) or (negative and value_negative)


def _preference_object(value: str, text: str) -> bool:
    return bool(re.search(
        re.escape(value) + r'(?:을|를|은|는|이|가)?'
        r'(?:정말|아주|많이|너무|별로|안)?(?:좋아|싫어|선호)', text,
    ))


def reply(message: str, clarification: bool = False) -> AgentDecision:
    """Build a server-owned non-actuating outcome."""
    return AgentDecision(
        type='clarification' if clarification else 'message',
        message=message,
        reason='personal_memory',
        confidence=1.0,
    )
