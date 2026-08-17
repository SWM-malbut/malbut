"""Deterministic safety gate for model-proposed robot actions."""

import math
import unicodedata
from dataclasses import dataclass
from typing import Any, Dict, Iterable, Optional, Set

from malbut_agent_server.schemas import AgentDecision, AgentRequest
from malbut_agent_server.tools import TOOL_SPECS


DEFAULT_LOCATIONS = {
    '거실',
    '주방',
    '침실',
    '현관',
    '충전소',
    'living_room',
    'kitchen',
    'bedroom',
    'entrance',
    'dock',
}

ROBOT_STATE_PROFILE_PHYSICAL = 'physical'
ROBOT_STATE_PROFILE_GAZEBO_SIMULATION = 'gazebo_simulation'
ROBOT_STATE_PROFILES = frozenset({
    ROBOT_STATE_PROFILE_PHYSICAL,
    ROBOT_STATE_PROFILE_GAZEBO_SIMULATION,
})


def _monitor_room_location_key(value: Any) -> str:
    """Return one canonical key for room allow/deny comparisons."""
    if not isinstance(value, str):
        return ''
    normalized = unicodedata.normalize('NFKC', value)
    normalized = ' '.join(normalized.split()).casefold()
    if (
        not normalized
        or any(
            unicodedata.category(character).startswith('C')
            for character in normalized
        )
    ):
        return ''
    return normalized


NAVIGATION_LOCATION_ALIASES = {
    '거실': ('거실',),
    '주방': ('주방', '부엌'),
    '침실': ('침실',),
    '현관': ('현관',),
    '충전소': ('충전소', '도크'),
    'living_room': ('livingroom', '거실'),
    'kitchen': ('kitchen', '주방', '부엌'),
    'bedroom': ('bedroom', '침실'),
    'entrance': ('entrance', '현관'),
    'dock': ('dock', '도크', '충전소'),
}

META_REQUEST_MARKERS = (
    '기억속',
    '뭐라고말',
    '말한문장',
    '말했던문장',
    '문장을인용',
    '예시문장',
    '번역해',
    '할수있',
    '할수있는',
    '갈수있',
    '가능한기능',
    '가능해',
)

GENERIC_CANCEL_MARKERS = (
    '취소해',
    '취소할게',
    '필요없',
    'cancel',
    'nevermind',
)

GLOBAL_REJECTION_MARKERS = (
    '싫',
    '원치않',
    '원하지않',
    '원하지마',
    '하지않았으면',
    '하지않으면',
    '하지않길',
    '하지말아줬으면',
    '하지말았으면',
    '하지말길',
    '말아줬으면',
    '말아줘',
    '안했으면',
    '안하면',
    '안해줘',
    '안할래',
    '안하고싶',
    '거부해',
    '거절해',
    'dontwant',
    'donotwant',
    'dontlike',
    'donotlike',
    'wouldrathernot',
    'wouldnotlike',
    'wouldntlike',
    'prefernot',
    'donotwish',
    'dislike',
)

KOREAN_PROHIBITION_SUFFIXES = (
    '지마',
    '지말',
    '지않',
    '지못',
    '주지마',
    '주지말',
    '주지않',
    '어주지마',
    '어주지말',
    '어주지않',
    '아주지마',
    '아주지말',
    '아주지않',
    '해주지마',
    '해주지말',
    '해주지않',
    '면안',
    '으면안',
    '서는안',
    '어서는안',
    '아서는안',
    '해서는안',
    '어선안',
    '해선안',
    '는건안',
    '금지',
)

ENGLISH_PROHIBITION_PREFIXES = (
    'dont',
    'donot',
    'never',
    'mustnot',
    'shouldnot',
    'cannot',
    'cant',
    'avoid',
    'refrainfrom',
    'stop',
    'without',
)

KOREAN_LEXEME_SUFFIXES = tuple(
    sorted(
        {
            '였다고',
            '었다고',
            '았다고',
            '한다고',
            '된다고',
            '했다고',
            '습니다',
            '다고',
            '했고',
            '했어',
            '해요',
            '해',
            '았어',
            '었어',
            '였어',
            '아요',
            '어요',
            '네요',
            '군요',
            '구나',
            '이다',
            '였다',
            '었다',
            '았다',
            '였',
            '었',
            '았',
        },
        key=len,
        reverse=True,
    )
)


@dataclass(frozen=True)
class SafetyResult:
    """One policy decision suitable for logs and evaluation."""

    allowed: bool
    code: str
    reason: str

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe policy result."""
        return {
            'allowed': self.allowed,
            'code': self.code,
            'reason': self.reason,
        }


class SafetyPolicy:
    """Validate tool proposals against local, current robot state."""

    def __init__(
        self,
        allowed_locations: Optional[Iterable[str]] = None,
        monitorable_locations: Optional[Iterable[str]] = None,
        minimum_navigation_battery: float = 15.0,
        maximum_action_ttl_ms: int = 10000,
    ) -> None:
        """Initialize local allowlists and action limits."""
        location_source = (
            DEFAULT_LOCATIONS
            if allowed_locations is None
            else allowed_locations
        )
        locations = list(location_source)
        if not all(
            isinstance(location, str) and location.strip()
            for location in locations
        ):
            raise ValueError(
                'allowed_locations must contain non-empty strings'
            )
        monitorable_source = (
            ()
            if monitorable_locations is None
            else monitorable_locations
        )
        monitorable = list(monitorable_source)
        if not all(
            isinstance(location, str) and location.strip()
            for location in monitorable
        ):
            raise ValueError(
                'monitorable_locations must contain non-empty strings'
            )
        if (
            isinstance(minimum_navigation_battery, bool)
            or not isinstance(
                minimum_navigation_battery,
                (int, float),
            )
            or not math.isfinite(
                float(minimum_navigation_battery)
            )
            or minimum_navigation_battery < 0
            or minimum_navigation_battery > 100
        ):
            raise ValueError(
                'minimum_navigation_battery must be from 0 to 100'
            )
        if (
            isinstance(maximum_action_ttl_ms, bool)
            or not isinstance(maximum_action_ttl_ms, int)
            or maximum_action_ttl_ms < 1
            or maximum_action_ttl_ms > 60000
        ):
            raise ValueError(
                'maximum_action_ttl_ms must be from 1 to 60000'
            )
        self.allowed_locations: Set[str] = {
            location.strip()
            for location in locations
        }
        self.monitorable_locations: Set[str] = {
            location.strip()
            for location in monitorable
        }
        self._monitorable_location_keys: Set[str] = {
            _monitor_room_location_key(location)
            for location in self.monitorable_locations
        }
        self.minimum_navigation_battery = float(
            minimum_navigation_battery
        )
        self.maximum_action_ttl_ms = maximum_action_ttl_ms

    def evaluate(
        self,
        request: AgentRequest,
        decision: AgentDecision,
        state_trusted: bool = False,
        state_profile: str = ROBOT_STATE_PROFILE_PHYSICAL,
    ) -> SafetyResult:
        """Return whether a normalized decision may reach an executor."""
        if state_profile not in ROBOT_STATE_PROFILES:
            return SafetyResult(
                False,
                'untrusted_robot_state',
                '지원되지 않는 로봇 상태 신뢰 프로필입니다.',
            )
        if decision.type != 'tool_call':
            return SafetyResult(True, 'not_an_action', '행동 요청이 아닙니다.')

        if not state_trusted:
            return SafetyResult(
                False,
                'untrusted_robot_state',
                '신뢰된 로컬 ROS 상태가 없어 행동을 실행하지 않습니다.',
            )

        if (
            state_profile == ROBOT_STATE_PROFILE_GAZEBO_SIMULATION
            and decision.tool_name != 'monitor_room'
        ):
            return SafetyResult(
                False,
                'untrusted_robot_state',
                'Gazebo 상태는 방 모니터링 제안에만 사용할 수 있습니다.',
            )

        if (
            state_profile == ROBOT_STATE_PROFILE_PHYSICAL
            and request.robot_state.emergency_stop
        ):
            return SafetyResult(
                False,
                'emergency_stop',
                '비상 정지 상태에서는 어떤 로봇 행동도 실행하지 않습니다.',
            )

        if decision.expires_in_ms > self.maximum_action_ttl_ms:
            return SafetyResult(
                False,
                'ttl_too_long',
                '행동 결정의 유효 시간이 안전 한도를 넘었습니다.',
            )

        tool_name = decision.tool_name
        if tool_name not in TOOL_SPECS:
            return SafetyResult(
                False,
                'unknown_tool',
                '등록되지 않은 도구는 실행할 수 없습니다.',
            )
        if tool_name not in request.available_tools:
            return SafetyResult(
                False,
                'tool_unavailable',
                '현재 로봇이 제공하지 않은 도구입니다.',
            )

        validator = getattr(self, f'_validate_{tool_name}', None)
        if validator is None:
            return SafetyResult(
                False,
                'missing_validator',
                '도구별 안전 검증기가 없습니다.',
            )
        if state_profile == ROBOT_STATE_PROFILE_GAZEBO_SIMULATION:
            tool_result = self._validate_monitor_room_gazebo_simulation(
                request,
                decision.arguments,
            )
        else:
            tool_result = validator(request, decision.arguments)
        if not tool_result.allowed:
            return tool_result
        if not self._has_current_turn_intent(request, decision):
            return SafetyResult(
                False,
                'current_turn_intent_missing',
                (
                    '현재 사용자 발화에서 이 행동을 요청한 근거를 '
                    '확인할 수 없어 실행하지 않습니다.'
                ),
            )
        return tool_result

    def _has_current_turn_intent(
        self,
        request: AgentRequest,
        decision: AgentDecision,
    ) -> bool:
        """Require tool intent in the current utterance, never context."""
        compact = self._compact_utterance(request.utterance)
        if any(marker in compact for marker in META_REQUEST_MARKERS):
            return False
        if any(marker in compact for marker in GENERIC_CANCEL_MARKERS):
            return False
        if any(marker in compact for marker in GLOBAL_REJECTION_MARKERS):
            return False

        checker = getattr(
            self,
            f'_has_{decision.tool_name}_intent',
            None,
        )
        if checker is None:
            return False
        if not checker(compact, decision.arguments):
            return False
        if decision.tool_name == 'send_notification':
            return self._notification_arguments_bound(
                request.utterance,
                decision.arguments,
            )
        return True

    def _has_monitor_room_intent(
        self,
        compact: str,
        arguments: Dict[str, Any],
    ) -> bool:
        """Accept only one complete, narrow room-monitoring command."""
        if self._has_action_prohibition(
            compact,
            korean_stems=(
                '보여',
                '모니터링하',
                '모니터링',
                '살펴보',
                '둘러보',
            ),
            english_actions=('show', 'monitor', 'inspect'),
        ):
            return False
        location = arguments.get('location')
        if not isinstance(location, str):
            return False
        normalized_location = unicodedata.normalize(
            'NFKC',
            location,
        ).casefold().strip()
        aliases = NAVIGATION_LOCATION_ALIASES.get(
            normalized_location,
            (self._compact_utterance(normalized_location),),
        )
        exact_commands = set()
        for alias in aliases:
            normalized_alias = self._compact_utterance(alias)
            for coverage in (
                '전체',
                '방전체',
                '의전체',
                '모든부분',
                '의모든부분',
            ):
                for particle in ('', '을', '를'):
                    for action in ('보여줘', '보여주세요'):
                        exact_commands.add(
                            f'{normalized_alias}{coverage}{particle}{action}'
                        )
            for particle in ('', '을', '를'):
                for action in (
                    '모니터링해줘',
                    '모니터링해주세요',
                    '살펴봐줘',
                    '살펴봐주세요',
                    '둘러봐줘',
                    '둘러봐주세요',
                ):
                    exact_commands.add(
                        f'{normalized_alias}{particle}{action}'
                    )

            english_alias = normalized_alias.replace('_', '')
            for prefix in ('', 'please'):
                exact_commands.update(
                    {
                        f'{prefix}showmethewhole{english_alias}',
                        f'{prefix}showmetheentire{english_alias}',
                        f'{prefix}monitorthe{english_alias}',
                        f'{prefix}inspectthe{english_alias}',
                    }
                )
        return compact in exact_commands

    @staticmethod
    def _compact_utterance(value: str) -> str:
        normalized = unicodedata.normalize('NFKC', value).casefold()
        return ''.join(
            character
            for character in normalized
            if character.isalnum() or character == '_'
        )

    @staticmethod
    def _contains_any(value: str, markers: Iterable[str]) -> bool:
        return any(marker in value for marker in markers)

    @staticmethod
    def _ends_with_request(
        compact: str,
        forms: Iterable[str],
    ) -> bool:
        """Match explicit Korean request forms at the utterance tail."""
        polite_tails = ('', '요', '부탁해', '부탁해요')
        return any(
            compact.endswith(f'{form}{tail}')
            for form in forms
            for tail in polite_tails
        )

    @staticmethod
    def _starts_with_english_request(
        compact: str,
        actions: Iterable[str],
    ) -> bool:
        """Match imperative or conventional polite English requests."""
        request_prefixes = (
            '',
            'please',
            'couldyou',
            'wouldyou',
            'canyou',
            'iwantyouto',
        )
        return any(
            compact.startswith(f'{prefix}{action}')
            for prefix in request_prefixes
            for action in actions
        )

    @classmethod
    def _has_action_prohibition(
        cls,
        compact: str,
        *,
        korean_stems: Iterable[str],
        english_actions: Iterable[str],
    ) -> bool:
        """Recognize explicit prohibition scoped to one tool action."""
        for stem in korean_stems:
            if any(
                f'{stem}{suffix}' in compact
                for suffix in KOREAN_PROHIBITION_SUFFIXES
            ):
                return True
            if f'안{stem}' in compact:
                return True

        for action in english_actions:
            if any(
                f'{prefix}{action}' in compact
                for prefix in ENGLISH_PROHIBITION_PREFIXES
            ):
                return True
            if (
                f'{action}notallowed' in compact
                or f'no{action}' in compact
            ):
                return True
            trailing_prohibitions = (
                'notallowed',
                'isntallowed',
                'notpermitted',
                'isntpermitted',
                'forbidden',
                'prohibited',
                'disallowed',
                'banned',
            )
            action_index = compact.find(action)
            if action_index >= 0 and any(
                prohibition
                in compact[action_index + len(action):]
                for prohibition in trailing_prohibitions
            ):
                return True
        return False

    @staticmethod
    def _lexical_tokens(value: str) -> list:
        """Split user-controlled text without requiring an NLP package."""
        normalized = unicodedata.normalize('NFKC', value).casefold()
        tokens = []
        current = []
        for character in normalized:
            if character.isalnum():
                current.append(character)
            elif current:
                tokens.append(''.join(current))
                current = []
        if current:
            tokens.append(''.join(current))
        return tokens

    @classmethod
    def _lexeme_variants(cls, token: str) -> Set[str]:
        """Return conservative inflection variants for lexical binding."""
        variants = {token}
        frontier = {token}
        for _ in range(3):
            next_frontier = set()
            for value in frontier:
                for suffix in KOREAN_LEXEME_SUFFIXES:
                    if (
                        value.endswith(suffix)
                        and len(value) > len(suffix)
                    ):
                        stripped = value[:-len(suffix)]
                        if stripped not in variants:
                            variants.add(stripped)
                            next_frontier.add(stripped)
            if not next_frontier:
                break
            frontier = next_frontier

        if token.isascii():
            if token.endswith("'s") and len(token) > 3:
                variants.add(token[:-2])
            for suffix in ('ing', 'ed', 'es', 's'):
                if token.endswith(suffix) and len(token) > len(suffix) + 2:
                    variants.add(token[:-len(suffix)])
        return variants

    @classmethod
    def _notification_arguments_bound(
        cls,
        utterance: str,
        arguments: Dict[str, Any],
    ) -> bool:
        """Bind every notification payload value to the current turn."""
        if arguments.get('image_id') is not None:
            return False
        message = arguments.get('message')
        if not isinstance(message, str):
            return False

        utterance_variants = [
            cls._lexeme_variants(token)
            for token in cls._lexical_tokens(utterance)
        ]
        message_tokens = cls._lexical_tokens(message)
        if not message_tokens:
            return False
        return all(
            any(
                cls._lexeme_variants(token) & source_variants
                for source_variants in utterance_variants
            )
            for token in message_tokens
        )

    def _has_navigate_intent(
        self,
        compact: str,
        arguments: Dict[str, Any],
    ) -> bool:
        if self._has_action_prohibition(
            compact,
            korean_stems=('가', '이동하', '이동', '오'),
            english_actions=('go', 'move', 'come'),
        ):
            return False

        location = arguments.get('location')
        if not isinstance(location, str):
            return False
        normalized_location = unicodedata.normalize(
            'NFKC',
            location,
        ).casefold().strip()
        aliases = NAVIGATION_LOCATION_ALIASES.get(
            normalized_location,
            (
                self._compact_utterance(normalized_location),
            ),
        )
        if not self._contains_any(compact, aliases):
            return False

        korean_request = self._ends_with_request(
            compact,
            (
                '가줘',
                '가주세요',
                '가자',
                '와줘',
                '와주세요',
                '이동해줘',
                '이동해주세요',
                '이동해',
                '이동시켜',
                '이동하자',
            ),
        )
        bare_destination_command = (
            compact.endswith('로가')
            or compact.endswith('으로가')
        )
        english_request = self._starts_with_english_request(
            compact,
            ('goto', 'moveto', 'cometo'),
        )
        return (
            korean_request
            or bare_destination_command
            or english_request
        )

    def _has_capture_photo_intent(
        self,
        compact: str,
        arguments: Dict[str, Any],
    ) -> bool:
        del arguments
        if self._has_action_prohibition(
            compact,
            korean_stems=('찍', '촬영하', '촬영', '캡처하', '캡처'),
            english_actions=(
                'takeaphoto',
                'takeapicture',
                'capture',
                'photograph',
                'photo',
                'picture',
            ),
        ):
            return False
        subject = self._contains_any(
            compact,
            ('사진', '촬영', '스냅샷', 'photo', 'picture', 'snapshot'),
        )
        action = self._contains_any(
            compact,
            ('찍', '촬영', '캡처', 'capture', 'takeaphoto', 'takeapicture'),
        )
        if not subject or not action:
            return False
        korean_request = self._ends_with_request(
            compact,
            (
                '찍어줘',
                '찍어주세요',
                '찍어',
                '찍자',
                '촬영해줘',
                '촬영해주세요',
                '촬영해',
                '캡처해줘',
                '캡처해주세요',
                '캡처해',
            ),
        )
        english_request = self._starts_with_english_request(
            compact,
            (
                'takeaphoto',
                'takeapicture',
                'captureaphoto',
                'captureapicture',
            ),
        )
        return korean_request or english_request

    def _has_detect_pet_intent(
        self,
        compact: str,
        arguments: Dict[str, Any],
    ) -> bool:
        del arguments
        if self._has_action_prohibition(
            compact,
            korean_stems=('찾', '감지하', '감지', '탐지하', '탐지'),
            english_actions=('find', 'detect', 'lookfor'),
        ):
            return False
        subject = self._contains_any(
            compact,
            (
                '반려동물',
                '강아지',
                '고양이',
                '초코',
                'pet',
                'dog',
                'cat',
            ),
        )
        action = self._contains_any(
            compact,
            (
                '찾아',
                '보이는지',
                '감지',
                '탐지',
                '확인해',
                'find',
                'detect',
                'lookfor',
                'canyousee',
            ),
        )
        if not subject or not action:
            return False
        korean_request = self._ends_with_request(
            compact,
            (
                '찾아봐',
                '찾아줘',
                '찾아주세요',
                '찾아',
                '찾아라',
                '감지해줘',
                '감지해주세요',
                '감지해',
                '탐지해줘',
                '탐지해주세요',
                '탐지해',
                '확인해줘',
                '확인해주세요',
            ),
        )
        english_request = self._starts_with_english_request(
            compact,
            ('find', 'detect', 'lookfor', 'canyousee'),
        )
        return korean_request or english_request

    def _has_send_notification_intent(
        self,
        compact: str,
        arguments: Dict[str, Any],
    ) -> bool:
        if arguments.get('image_id') is not None:
            return False
        if self._has_action_prohibition(
            compact,
            korean_stems=(
                '알림보내',
                '보내',
                '알리',
                '알려주',
                '전하',
                '전달하',
                '통지하',
            ),
            english_actions=(
                'notify',
                'send',
                'sendmessage',
                'tell',
                'notification',
            ),
        ):
            return False
        if self._contains_any(
            compact,
            ('notify', 'sendmessage'),
        ):
            return self._starts_with_english_request(
                compact,
                ('notify', 'sendmessage'),
            )
        recipient = self._contains_any(
            compact,
            (
                '가족',
                '보호자',
                '엄마',
                '아빠',
                'caregiver',
            ),
        )
        action = self._contains_any(
            compact,
            (
                '알려줘',
                '보내줘',
                '전해줘',
                '전달해',
                '통지해',
                'sendmessage',
                'letthemknow',
            ),
        )
        if not recipient or not action:
            return False
        korean_request = self._ends_with_request(
            compact,
            (
                '알려줘',
                '알려주세요',
                '보내줘',
                '보내주세요',
                '전해줘',
                '전해주세요',
                '전달해줘',
                '전달해주세요',
                '통지해줘',
                '통지해주세요',
            ),
        )
        english_request = self._starts_with_english_request(
            compact,
            ('notify', 'sendmessage', 'tellmycaregiver', 'letthemknow'),
        )
        return korean_request or english_request

    def _has_get_robot_status_intent(
        self,
        compact: str,
        arguments: Dict[str, Any],
    ) -> bool:
        del arguments
        if self._has_action_prohibition(
            compact,
            korean_stems=('확인하', '확인', '알려주', '조회하'),
            english_actions=('check', 'checkstatus', 'getstatus'),
        ):
            return False
        subject = self._contains_any(
            compact,
            (
                '배터리',
                '로봇상태',
                '시스템상태',
                '상태확인',
                '상태를확인',
                'battery',
                'robotstatus',
                'systemstatus',
            ),
        )
        if not subject:
            return False
        korean_request = self._ends_with_request(
            compact,
            (
                '확인해줘',
                '확인해주세요',
                '알려줘',
                '알려주세요',
                '조회해줘',
                '조회해주세요',
                '보여줘',
                '보여주세요',
            ),
        )
        english_request = self._starts_with_english_request(
            compact,
            (
                'checkbattery',
                'checkrobotstatus',
                'checksystemstatus',
                'getrobotstatus',
                'getsystemstatus',
            ),
        )
        return korean_request or english_request

    def _validate_navigate(
        self,
        request: AgentRequest,
        arguments: Dict[str, Any],
    ) -> SafetyResult:
        if set(arguments) != {'location'}:
            return SafetyResult(
                False,
                'invalid_arguments',
                'navigate 인자는 location 하나여야 합니다.',
            )
        location = arguments.get('location')
        if not isinstance(location, str) or not location.strip():
            return SafetyResult(
                False,
                'invalid_arguments',
                '이동 목적지가 올바르지 않습니다.',
            )
        location = location.strip()
        if location not in self.allowed_locations:
            return SafetyResult(
                False,
                'location_not_allowed',
                '허용 목록에 없는 목적지입니다.',
            )
        if location in request.robot_state.forbidden_zones:
            return SafetyResult(
                False,
                'forbidden_zone',
                '현재 금지 구역으로 설정된 목적지입니다.',
            )
        if not request.robot_state.navigation_available:
            return SafetyResult(
                False,
                'navigation_unavailable',
                'Nav2 실행 상태가 확인되지 않았습니다.',
            )
        if not request.robot_state.localization_ok:
            return SafetyResult(
                False,
                'localization_unavailable',
                '로봇 위치가 신뢰 가능한 상태가 아닙니다.',
            )
        battery = request.robot_state.battery_percent
        if battery is None:
            return SafetyResult(
                False,
                'battery_unknown',
                '배터리 상태를 확인할 수 없어 이동하지 않습니다.',
            )
        if (
            battery < self.minimum_navigation_battery
            and location not in {'충전소', 'dock'}
        ):
            return SafetyResult(
                False,
                'battery_low',
                '배터리가 부족해 충전소 외 이동을 허용하지 않습니다.',
            )
        return SafetyResult(True, 'allowed', '이동 안전 조건을 통과했습니다.')

    def _validate_monitor_room(
        self,
        request: AgentRequest,
        arguments: Dict[str, Any],
    ) -> SafetyResult:
        """Validate a proposal without authorizing mission execution."""
        if set(arguments) != {'location'}:
            return SafetyResult(
                False,
                'invalid_arguments',
                'monitor_room 인자는 location 하나여야 합니다.',
            )
        location = arguments.get('location')
        if not isinstance(location, str) or not location.strip():
            return SafetyResult(
                False,
                'invalid_arguments',
                '모니터링할 방 이름이 올바르지 않습니다.',
            )
        location = location.strip()
        location_key = _monitor_room_location_key(location)
        if (
            not location_key
            or location_key not in self._monitorable_location_keys
        ):
            return SafetyResult(
                False,
                'room_not_monitorable',
                '검증된 방 모니터링 계획이 없는 장소입니다.',
            )
        forbidden_keys = {
            _monitor_room_location_key(zone)
            for zone in request.robot_state.forbidden_zones
        }
        if location_key in forbidden_keys:
            return SafetyResult(
                False,
                'forbidden_zone',
                '현재 금지 구역으로 설정된 장소입니다.',
            )
        if not request.robot_state.navigation_available:
            return SafetyResult(
                False,
                'navigation_unavailable',
                'Nav2 실행 상태가 확인되지 않았습니다.',
            )
        if not request.robot_state.localization_ok:
            return SafetyResult(
                False,
                'localization_unavailable',
                '로봇 위치가 신뢰 가능한 상태가 아닙니다.',
            )
        battery = request.robot_state.battery_percent
        if battery is None:
            return SafetyResult(
                False,
                'battery_unknown',
                '배터리 상태를 확인할 수 없습니다.',
            )
        if battery < self.minimum_navigation_battery:
            return SafetyResult(
                False,
                'battery_low',
                '배터리가 부족해 방 모니터링을 시작하지 않습니다.',
            )
        if request.robot_state.privacy_mode:
            return SafetyResult(
                False,
                'privacy_mode',
                '프라이버시 모드에서는 카메라를 사용하지 않습니다.',
            )
        if not request.robot_state.camera_available:
            return SafetyResult(
                False,
                'camera_unavailable',
                '카메라 사용 가능 상태가 아닙니다.',
            )
        return SafetyResult(
            True,
            'allowed',
            '방 모니터링 제안의 안전 조건을 통과했습니다.',
        )

    def _validate_monitor_room_gazebo_simulation(
        self,
        request: AgentRequest,
        arguments: Dict[str, Any],
    ) -> SafetyResult:
        """Check only server-issued facts relevant to simulated motion."""
        if set(arguments) != {'location'}:
            return SafetyResult(
                False,
                'invalid_arguments',
                'monitor_room 인자는 location 하나여야 합니다.',
            )
        location = arguments.get('location')
        if not isinstance(location, str) or not location.strip():
            return SafetyResult(
                False,
                'invalid_arguments',
                '모니터링할 방 이름이 올바르지 않습니다.',
            )
        location_key = _monitor_room_location_key(location.strip())
        if (
            not location_key
            or location_key not in self._monitorable_location_keys
        ):
            return SafetyResult(
                False,
                'room_not_monitorable',
                '검증된 방 모니터링 계획이 없는 장소입니다.',
            )
        if not request.robot_state.navigation_available:
            return SafetyResult(
                False,
                'navigation_unavailable',
                'Gazebo Nav2 실행 상태가 확인되지 않았습니다.',
            )
        if not request.robot_state.localization_ok:
            return SafetyResult(
                False,
                'localization_unavailable',
                'Gazebo 로봇 위치가 신뢰 가능한 상태가 아닙니다.',
            )
        return SafetyResult(
            True,
            'allowed',
            'Gazebo 시뮬레이션 주행 제안의 안전 조건을 통과했습니다.',
        )

    def _validate_camera(
        self,
        request: AgentRequest,
        arguments: Dict[str, Any],
    ) -> SafetyResult:
        if arguments:
            return SafetyResult(
                False,
                'invalid_arguments',
                '이 카메라 도구는 인자를 받지 않습니다.',
            )
        if request.robot_state.privacy_mode:
            return SafetyResult(
                False,
                'privacy_mode',
                '프라이버시 모드에서는 카메라를 사용하지 않습니다.',
            )
        if not request.robot_state.camera_available:
            return SafetyResult(
                False,
                'camera_unavailable',
                '카메라 사용 가능 상태가 아닙니다.',
            )
        return SafetyResult(
            True,
            'allowed',
            '카메라 안전 조건을 통과했습니다.',
        )

    def _validate_detect_pet(
        self,
        request: AgentRequest,
        arguments: Dict[str, Any],
    ) -> SafetyResult:
        return self._validate_camera(request, arguments)

    def _validate_capture_photo(
        self,
        request: AgentRequest,
        arguments: Dict[str, Any],
    ) -> SafetyResult:
        return self._validate_camera(request, arguments)

    def _validate_send_notification(
        self,
        request: AgentRequest,
        arguments: Dict[str, Any],
    ) -> SafetyResult:
        if set(arguments) != {'message', 'image_id'}:
            return SafetyResult(
                False,
                'invalid_arguments',
                '알림 인자 구성이 올바르지 않습니다.',
            )
        message = arguments.get('message')
        image_id = arguments.get('image_id')
        if (
            not isinstance(message, str)
            or not message.strip()
            or len(message) > 500
        ):
            return SafetyResult(
                False,
                'invalid_arguments',
                '알림 문구가 올바르지 않습니다.',
            )
        if image_id is not None and not isinstance(image_id, str):
            return SafetyResult(
                False,
                'invalid_arguments',
                'image_id는 문자열 또는 null이어야 합니다.',
            )
        if image_id is not None:
            if request.robot_state.privacy_mode:
                return SafetyResult(
                    False,
                    'privacy_mode',
                    '프라이버시 모드에서는 이미지를 전송하지 않습니다.',
                )
            return SafetyResult(
                False,
                'image_attachment_unverified',
                '검증된 사용자별 미디어 저장소가 아직 연결되지 않았습니다.',
            )
        normalized_message = message.casefold()
        if any(
            marker in normalized_message
            for marker in (
                'openai_api_key',
                'authorization: bearer',
                '비밀번호',
                'password=',
                'sk-',
            )
        ):
            return SafetyResult(
                False,
                'sensitive_notification',
                '민감정보로 보이는 문구는 알림으로 전송하지 않습니다.',
            )
        return SafetyResult(True, 'allowed', '알림 검증을 통과했습니다.')

    def _validate_get_robot_status(
        self,
        request: AgentRequest,
        arguments: Dict[str, Any],
    ) -> SafetyResult:
        del request
        if arguments:
            return SafetyResult(
                False,
                'invalid_arguments',
                '상태 조회 도구는 인자를 받지 않습니다.',
            )
        return SafetyResult(True, 'allowed', '읽기 전용 상태 조회입니다.')
