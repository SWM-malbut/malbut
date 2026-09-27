"""Validate current voice intent for delegation to the public Manager Action.

This policy authorizes a bounded request to Manager, never physical readiness.
It does not turn an unknown RobotState into trusted sensor evidence. Existing
non-speech action policy and HTTP proposal behavior remain separate.
"""

import re
import unicodedata

from malbut_agent_server.gateway import (
    CapabilityRegistry, PROPOSAL_ONLY, TOOL_TIMEOUT_SECONDS, ToolCapability,
)
from malbut_agent_server.safety import (
    NAVIGATION_LOCATION_ALIASES, SafetyResult,
)
from malbut_agent_server.schemas import ValidationError
from malbut_agent_server.tools import (
    SPEECH_MISSION_TOOLS, TOOL_SPECS, validate_tool_arguments,
)


POLICY_REVISION = 'speech-manager-intent-v1'
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
    r'|이동(?:' + _DO + r'|' + _START + r'))'
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


def has_current_mission_intent(utterance, tool_name, arguments):
    """Recognize one explicit current request, without using remembered intent."""
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


class SpeechMissionPolicy:
    """Validate intent to request Manager while retaining legacy action gates."""

    policy_revision = POLICY_REVISION

    def __init__(self, base_policy, enabled_tools):
        """Wrap the existing policy with explicitly enabled Manager requests."""
        self._base = base_policy
        self.enabled_tools = frozenset(enabled_tools)
        if not self.enabled_tools.issubset(SPEECH_MISSION_TOOLS):
            raise ValueError('unsupported speech mission tool')

    def __getattr__(self, name):
        """Retain the underlying policy's unrelated validation interfaces."""
        return getattr(self._base, name)

    def evaluate(self, request, decision, state_trusted=False):
        """Validate delegation intent without fabricating physical readiness."""
        if decision.type != 'tool_call' or decision.tool_name not in SPEECH_MISSION_TOOLS:
            return self._base.evaluate(request, decision, state_trusted=state_trusted)
        if (decision.tool_name not in self.enabled_tools
                or decision.tool_name not in request.available_tools):
            return SafetyResult(False, 'tool_unavailable', '이 음성 실행 기능이 연결되지 않았어요.')
        try:
            validate_tool_arguments(decision.tool_name, decision.arguments)
        except (ValidationError, TypeError):
            return SafetyResult(False, 'invalid_arguments', '실행 요청의 인자가 올바르지 않아요.')
        if (type(decision.expires_in_ms) is not int or decision.expires_in_ms <= 0
                or decision.expires_in_ms > self._base.maximum_action_ttl_ms):
            return SafetyResult(False, 'ttl_too_long', '실행 요청의 유효 시간이 올바르지 않아요.')
        if not has_current_mission_intent(
            request.utterance, decision.tool_name, decision.arguments,
        ):
            return SafetyResult(
                False, 'current_turn_intent_missing',
                '실행할 작업 하나를 직접 말씀해 주세요.',
            )
        return SafetyResult(
            True, 'manager_request',
            'Manager에 요청할 수 있어요. 실제 실행 가능 여부는 Manager가 판단해요.',
        )


def configure_speech_missions(runtime, *, navigation_enabled=False):
    """Explicitly enable Manager proposals on this speech runtime only.

    The caller must supply an actual named-target resolver before enabling
    navigation. No ROS object, hardware state, or execution adapter is created.
    ``runtime.speech_mission_tools`` is the allowed subset for voice requests.
    """
    if type(navigation_enabled) is not bool:
        raise TypeError('navigation_enabled must be a boolean')
    enabled = tuple(name for name in SPEECH_MISSION_TOOLS
                    if navigation_enabled or name != 'request_navigation')
    registry = runtime.capability_registry
    entries = []
    for name in TOOL_SPECS:
        entry = registry.get(name)
        if name in SPEECH_MISSION_TOOLS:
            entry = ToolCapability(
                name=name, mode=PROPOSAL_ONLY, available=name in enabled,
                timeout_seconds=TOOL_TIMEOUT_SECONDS[name],
            )
        if entry is not None:
            entries.append(entry)
    runtime.capability_registry = CapabilityRegistry(
        entries, runtime_mode=registry.runtime_mode,
        revision=POLICY_REVISION,
    )
    base = runtime.safety_policy
    if isinstance(base, SpeechMissionPolicy):
        base = base._base
    runtime.safety_policy = SpeechMissionPolicy(base, enabled)
    runtime.speech_mission_tools = enabled
    return runtime
