"""Fixed phrase fixtures for the offline MockProvider only.

These deterministic matches exercise transport scenarios without a model call.
Production speech policy must not use them to interpret or restrict user intent.
"""

import re
import unicodedata

from malbut_agent_server.safety import NAVIGATION_LOCATION_ALIASES


_QUOTES = frozenset('\"\'“”‘’«»「」『』`')
_META = (
    '만약', '만일', '가정', '예를들', '상상', '번역', '인용', '설명',
    '문장', '말했', '말하', '말해', '뜻', '가능', '할수있', '할줄',
    '한다면', '라면', '하면', '따르면', '갈수있', '따라올수있',
    '기억', '저장', '나중', '내일', '다음에', '예약', '연습', '테스트',
    'hypothetical', 'imagine', 'example', 'quote', 'translate', 'explain',
    'whatif', 'canyoufollow', 'ifyou', 'later', 'tomorrow', 'pretend',
)
_NEGATION = (
    '하지마', '하지말', '하지않', '하지못', '안해', '안하', '말아',
    '싫', '원치않', '원하지않', '원하지마', '금지', '아니',
    '따라오지', '따라가지', '가지마', '가지말', '가면안',
    '이동하지', '순찰하지', 'donot', 'dont', 'never', 'without',
)
_MULTI = (
    '그리고', '동시에', '이어서', '그다음', '하고나서', '한뒤', '한후',
    '끝나면', '다음으로', '먼저', 'andthen', 'afterthat', 'then',
)
_DO = r'(?:해(?:줘|주세요|줄래|주실래|주겠니|주겠어)?|하자|하세요)(?:요)?'
_START = r'(?:시작(?:' + _DO + r')?)'
_FOLLOW = (
    r'(?:(?:나|저)(?:를)?|(?:현재)?보이는사람(?:을)?|사람(?:을)?'
    r'|앞에있는사람(?:을)?|눈앞의사람(?:을)?)?(?:좀)?'
    r'(?:따라(?:와(?:줘|주세요|줄래|주실래|주겠니)?|오세요|와라)(?:요)?'
    r'|따라가기(?:를)?' + _START + r'|추적(?:을)?' + _DO + r')'
)
_GO = (
    r'(?:가(?:줘|주세요|줄래|주실래|주겠니|자)?(?:요)?|가세요'
    r'|(?:갈까|가볼까|가볼래)(?:요)?|가보자'
    r'|이동(?:' + _DO + r'|' + _START + r'|해볼(?:까|래)(?:요)?|해보자))'
)
_CANCEL = (
    r'(?:(?:현재|지금)?(?:진행중인|실행중인)?'
    r'(?:작업|이동|따라가기|추적|순찰)(?:을|를)?)?'
    r'(?:(?:취소|중지|중단|정지)(?:' + _DO + r')?'
    r'|멈춰(?:줘|주세요|줄래)?(?:요)?|멈추세요|그만(?:해|해줘|해주세요)?(?:요)?)'
)


def _compact(text):
    return ''.join(char for char in unicodedata.normalize('NFKC', text).casefold()
                   if not char.isspace() and char not in '.,!?。？！')


def _command(text):
    value = _compact(text)
    # Addressing the robot or adding polite urgency does not add a new task.
    for _ in range(4):
        previous = value
        value = re.sub(
            r'^(?:제이크야|제이크|말벗아|말벗|지금|바로|좀|어서|부탁인데|please)',
            '', value,
        )
        if value == previous:
            break
    return value


def _navigation_intent(value, location):
    if (location != location.strip() or any(ord(c) < 32 for c in location)
            or any(c in _QUOTES for c in location)):
        return False
    normalized = unicodedata.normalize('NFKC', location).casefold()
    aliases = NAVIGATION_LOCATION_ALIASES.get(normalized, (normalized,))
    for alias in {*aliases, normalized}:
        target = re.escape(_compact(alias))
        if re.fullmatch(target + r'(?:으로|로|에|까지)?(?:좀)?' + _GO, value):
            return True
        if re.fullmatch(r'(?:go|move|navigate)to' + target + r'(?:please)?', value):
            return True
    return False


def _patrol_intent(value, thoroughness):
    coverage = {
        'light': r'(?:가볍게|간단히|간단하게)',
        'normal': r'(?:보통으로|평소처럼)?',
        'thorough': r'(?:꼼꼼하게|꼼꼼히|자세히|철저하게|철저히)',
    }[thoroughness]
    area = r'(?:(?:집안|집)(?:을)?|한바퀴|한번)?'
    if re.fullmatch(area + coverage + r'순찰(?:을)?(?:' + _DO + r'|' + _START + r')', value):
        return True
    english_coverage = {
        'light': r'light', 'normal': r'(?:normal)?', 'thorough': r'thorough',
    }[thoroughness]
    return bool(re.fullmatch(
        r'(?:start)?(?:a)?' + english_coverage + r'patrol(?:please)?', value,
    ))


def matches_speech_fixture(utterance, tool_name, arguments):
    """Match fixed offline test phrases; this does not validate live LLM meaning."""
    if not isinstance(utterance, str) or any(c in _QUOTES for c in utterance):
        return False
    value = _command(utterance)
    if any(marker in value for marker in (*_META, *_MULTI)):
        return False
    if tool_name == 'cancel_voice_mission':
        return bool(
            re.fullmatch(_CANCEL, value)
            or re.fullmatch(r'(?:그만따라와|따라오지마|순찰하지마)(?:요)?', value)
            or re.fullmatch(r'(?:stop|cancel)(?:the)?(?:current)?'
                            r'(?:mission|movement|navigation|following|patrol)?'
                            r'(?:please)?', value)
        )
    if any(marker in value for marker in _NEGATION):
        return False
    if tool_name == 'request_navigation':
        return _navigation_intent(value, arguments['location'])
    if tool_name == 'request_follow_person':
        return bool(re.fullmatch(_FOLLOW, value) or re.fullmatch(
            r'(?:followme|followthevisibleperson|startfollowingme)(?:please)?', value,
        ))
    if tool_name == 'request_patrol':
        return _patrol_intent(value, arguments['thoroughness'])
    return False
