"""Deterministic offline safety degradation when cloud LLM providers fail.

When internet or OpenAI is unavailable, this module allows high-confidence
local safety and control commands (stop/cancel, navigation to registered active
map destinations such as the charging dock, and manual recovery) to degrade
safely through the exact same deterministic SafetyPolicy and SpeechMissions
guards. Normal LLM-based interpretation remains primary whenever the provider
is reachable.
"""

from contextlib import contextmanager
from contextvars import ContextVar
import re
from typing import Any, Dict, Iterator, List, Optional, Sequence, Set, Tuple
import unicodedata

from malbut_agent_server.safety import NAVIGATION_LOCATION_ALIASES
from malbut_agent_server.schemas import (
    AgentDecision,
    AgentRequest,
    ProviderResult,
    SpeechAgentRequest,
)


OFFLINE_PROVIDER_NAME = 'malbut-offline-safety'
OFFLINE_MODEL_NAME = 'deterministic-v1'
DEFAULT_OFFLINE_STOP_TIMEOUT_SECONDS = 3.0

_QUOTES = frozenset('"\'“”‘’«»「」『』`')
_QUESTION_MARKS = frozenset('?？')

_META_MARKERS = (
    '만약', '만일', '가정', '예를들', '상상', '번역', '인용', '설명',
    '문장', '말했', '말하', '말해', '뜻', '가능', '할수있', '할줄',
    '갈수있', '될까', '해도돼', '되나요', '수있어', '수있니', '수있나요',
    '기억', '저장', '나중', '내일', '다음에', '예약', '연습', '테스트',
    '아까', '어제', '전에', '왜', '어떻게',
    'hypothetical', 'imagine', 'example', 'quote', 'translate', 'explain',
    'whatif', 'canyou', 'ifyou', 'later', 'tomorrow', 'pretend',
)
_MULTI_CLAUSE_MARKERS = (
    '그리고', '동시에', '이어서', '그다음', '하고나서', '한뒤', '한후',
    '끝나면', '다음으로', '먼저', '말고', '대신', '하고',
    'andthen', 'afterthat', 'instead',
)
_NEGATION_MARKERS = (
    '하지마', '하지말', '하지않', '하지못', '안해', '안하', '안가', '말아',
    '싫', '원치않', '원하지않', '원하지마', '금지', '아니',
    '가지마', '가지말', '가면안', '이동하지', '복구하지',
    'donot', 'dont', 'never', 'without', 'not',
)
_NEGATED_STOP_MARKERS = (
    '멈추지마', '멈추지말', '취소하지마', '취소하지말',
    '중지하지마', '중지하지말', '중단하지마', '중단하지말',
    '정지하지마', '정지하지말', '그만하지마', '그만두지마',
    'dontstop', 'donotstop', 'dontcancel', 'donotcancel',
)

_DO = r'(?:해(?:줘|주세요|줄래|주실래|주겠니|주겠어)?|하자|하세요)(?:요)?'
_WAKE_PREFIX = re.compile(
    r'^(?:제이크야|제이크|말벗아|말벗|지금|바로|당장|빨리|어서|좀|이제|부탁인데|please)'
)

_STOP_TARGET = (
    r'(?:(?:현재|지금)?(?:진행중인|실행중인)?'
    r'(?:작업|이동|주행|따라가기|따라오기|추적|순찰|지도만들기|지도작성'
    r'|위치찾기|위치보정|복구)(?:을|를)?)?'
)
_STOP_VERB = (
    r'(?:(?:비상|긴급)?정지(?:' + _DO + r')?'
    r'|(?:취소|중지|중단)(?:' + _DO + r')?'
    r'|멈춰(?:줘|주세요|줄래|주실래)?(?:요)?|멈추세요|멈추자|멈춰라'
    r'|그만(?:해(?:줘|주세요|줄래)?|둬(?:줘|주세요)?|두세요)?(?:요)?'
    r'|스톱|스탑|서(?:라)?)'
)
_STOP_RE = re.compile(
    r'^(?:'
    + _STOP_TARGET + _STOP_VERB
    + r'|따라오지마(?:요)?|그만따라와(?:요)?|순찰하지마(?:요)?'
    + r'|가지마(?:요)?|움직이지마(?:요)?'
    + r'|(?:emergency)?(?:stop|halt|abort|cancel)(?:the)?(?:current)?'
    + r'(?:mission|movement|navigation|following|patrol)?(?:please)?'
    + r')$'
)

_GO_VERB = (
    r'(?:돌아가(?:줘|주세요|줄래|주실래|자)?(?:요)?|돌아가세요'
    r'|가(?:줘|주세요|줄래|주실래|자)?(?:요)?|가세요|가보자|가볼까(?:요)?'
    r'|이동(?:' + _DO + r'|시작(?:' + _DO + r')?)'
    r'|복귀(?:' + _DO + r')?)'
)
_DOCK_DIRECT_RE = re.compile(
    r'^(?:충전하러(?:돌아)?' + _GO_VERB
    + r'|충전(?:으로|하러)?복귀(?:' + _DO + r')?'
    + r'|(?:go|return)to(?:charging)?dock(?:please)?'
    + r'|gotothecharger(?:please)?'
    + r'|charge(?:now)?(?:please)?)$'
)
_DOCK_LOCATION_KEYS = frozenset({'충전소', '충전독', '충전기', '도크', 'dock', 'charger'})

_CONFIRM_RE = re.compile(
    r'^(?:응|네|예|어|맞아(?:요)?|그래(?:요)?|좋아(?:요)?|알겠어(?:요)?'
    r'|가(?:줘|주세요|자)?(?:요)?|거기로' + _GO_VERB
    + r'|yes|yeah|yep|ok|okay|confirm|goahead)$'
)

_RECOVERY_RE = re.compile(
    r'^(?:(?:로봇)?(?:복구|리커버리)(?:를|을)?(?:' + _DO + r')?'
    + r'|(?:robot)?recover(?:y)?(?:please)?)$'
)

_timeout_cap_seconds: ContextVar[Optional[float]] = ContextVar(
    'malbut_offline_timeout_cap_seconds',
    default=None,
)
_suppress_retries: ContextVar[bool] = ContextVar(
    'malbut_offline_suppress_retries',
    default=False,
)


@contextmanager
def offline_stop_budget_scope(
    timeout_seconds: float,
    *,
    suppress_retries: bool = True,
) -> Iterator[None]:
    """Bound provider wait time for urgent stop turns so offline fallback fires fast."""
    cap = float(timeout_seconds) if timeout_seconds > 0 else None
    t_token = _timeout_cap_seconds.set(cap)
    r_token = _suppress_retries.set(bool(suppress_retries))
    try:
        yield
    finally:
        _timeout_cap_seconds.reset(t_token)
        _suppress_retries.reset(r_token)


def current_timeout_cap() -> Optional[float]:
    """Return active request-local provider timeout cap in seconds, or None."""
    return _timeout_cap_seconds.get()


def retries_suppressed() -> bool:
    """Return True when urgent local safety fallback suppresses provider retries."""
    return _suppress_retries.get()


def _compact(text: str) -> str:
    return ''.join(
        char
        for char in unicodedata.normalize('NFKC', text).casefold()
        if not char.isspace() and char not in '.,!。！~·'
    )


def _strip_prefixes(value: str) -> str:
    current = value
    for _ in range(4):
        updated = _WAKE_PREFIX.sub('', current)
        if updated == current:
            break
        current = updated
    return current


def _is_disqualified_utterance(utterance: str) -> bool:
    if not isinstance(utterance, str) or not utterance.strip():
        return True
    if any(ch in _QUOTES or ch in _QUESTION_MARKS for ch in utterance):
        return True
    compact = _compact(utterance)
    if not compact:
        return True
    if any(marker in compact for marker in _META_MARKERS):
        return True
    if any(marker in compact for marker in _MULTI_CLAUSE_MARKERS):
        return True
    return False


def is_urgent_stop_utterance(utterance: str) -> bool:
    """Return True only for unambiguous standalone stop/cancel speech commands."""
    if _is_disqualified_utterance(utterance):
        return False
    value = _strip_prefixes(_compact(utterance))
    if not value:
        return False
    if any(marker in value for marker in _NEGATED_STOP_MARKERS):
        return False
    return bool(_STOP_RE.fullmatch(value))


def is_provider_outage_result(provider_result: ProviderResult) -> bool:
    """Return True when the provider chain returned an outage fallback refusal."""
    if provider_result.decision.type != 'refusal':
        return False
    if (
        provider_result.provider == 'reliable-fallback'
        and provider_result.decision.reason == 'provider_unavailable'
    ):
        return True
    if (
        provider_result.provider == 'front-router'
        and provider_result.decision.reason == 'front_route:router_unavailable'
    ):
        return True
    return False


def _location_aliases(location: str) -> Tuple[str, ...]:
    normalized = unicodedata.normalize('NFKC', location).casefold().strip()
    candidates: Set[str] = {normalized}
    for alias in NAVIGATION_LOCATION_ALIASES.get(normalized, ()):
        candidates.add(unicodedata.normalize('NFKC', alias).casefold().strip())
    if normalized in _DOCK_LOCATION_KEYS:
        candidates.update({'충전소', '충전독', '충전기', '도크', 'dock'})
    return tuple(sorted(c for c in candidates if c))


def _match_registered_location(
    value: str,
    locations: Sequence[str],
) -> Optional[str]:
    matched: List[str] = []
    dock_targets: List[str] = []
    for location in locations:
        if (
            not isinstance(location, str)
            or not location.strip()
            or location != location.strip()
            or any(ord(ch) < 32 for ch in location)
            or any(ch in _QUOTES for ch in location)
        ):
            continue
        norm_loc = unicodedata.normalize('NFKC', location).casefold().strip()
        if norm_loc in _DOCK_LOCATION_KEYS:
            dock_targets.append(location)
        hit = False
        for alias in _location_aliases(location):
            escaped = re.escape(_compact(alias))
            if not escaped:
                continue
            if re.fullmatch(
                escaped + r'(?:으로|로|에|까지)?(?:좀)?' + _GO_VERB,
                value,
            ):
                hit = True
                break
            if re.fullmatch(
                r'(?:go|move|navigate|return)to' + escaped + r'(?:please)?',
                value,
            ):
                hit = True
                break
        if hit:
            matched.append(location)
    if len(matched) == 1:
        return matched[0]
    if not matched and len(dock_targets) == 1 and _DOCK_DIRECT_RE.fullmatch(value):
        return dock_targets[0]
    return None


def _offline_tool_result(
    tool_name: str,
    arguments: Dict[str, Any],
    message: str,
) -> ProviderResult:
    decision = AgentDecision(
        type='tool_call',
        message=message,
        tool_name=tool_name,
        arguments=arguments,
        reason=f'offline_safety:{tool_name}',
        confidence=1.0,
    )
    decision.validate()
    return ProviderResult(
        decision=decision,
        provider=OFFLINE_PROVIDER_NAME,
        model=OFFLINE_MODEL_NAME,
        latency_ms=0.0,
        memory_supported=False,
    )


def resolve_offline_speech_fallback(
    request: AgentRequest,
    available_tools: Sequence[str],
) -> Optional[ProviderResult]:
    """Resolve a strictly bounded offline safety/control proposal for speech.

    Every returned tool proposal still flows through SafetyPolicy /
    SpeechMissionPolicy and SpeechMissions dispatch guards.
    """
    if not isinstance(request, SpeechAgentRequest):
        return None
    if request.mapping_name_requested:
        return None
    if _is_disqualified_utterance(request.utterance):
        return None

    tool_set = frozenset(available_tools)
    value = _strip_prefixes(_compact(request.utterance))
    if not value:
        return None

    # Priority 1: Emergency stop / mission cancel (only outside pending confirmation).
    if (
        'cancel_voice_mission' in tool_set
        and not request.navigation_confirmation
        and is_urgent_stop_utterance(request.utterance)
    ):
        return _offline_tool_result(
            'cancel_voice_mission',
            {},
            '전면 작업의 취소를 요청할게요.',
        )

    # Reject negations before considering positive motion or recovery actions.
    if any(marker in value for marker in _NEGATION_MARKERS):
        return None

    # Priority 2: Navigation confirmation or exact registered destination on active map.
    if 'request_navigation' in tool_set and request.navigation_locations:
        locations = tuple(request.navigation_locations)
        confirmation = request.navigation_confirmation
        if (
            confirmation
            and confirmation in locations
            and _CONFIRM_RE.fullmatch(value)
        ):
            return _offline_tool_result(
                'request_navigation',
                {'location': confirmation},
                'Manager에 작업 실행을 요청할게요.',
            )
        matched_location = _match_registered_location(value, locations)
        if matched_location is not None:
            return _offline_tool_result(
                'request_navigation',
                {'location': matched_location},
                'Manager에 작업 실행을 요청할게요.',
            )

    # Priority 3: Manual recovery.
    if (
        'request_recovery' in tool_set
        and not request.navigation_confirmation
        and _RECOVERY_RE.fullmatch(value)
    ):
        return _offline_tool_result(
            'request_recovery',
            {},
            'Manager에 작업 실행을 요청할게요.',
        )

    return None


def provider_circuit_summary(provider: Any) -> Dict[str, Any]:
    """Return a content-free health summary of provider circuits without I/O."""
    circuits: List[Dict[str, Any]] = []
    degraded = False
    visited: Set[int] = set()

    def _collect(candidate: Any, role: str) -> None:
        nonlocal degraded
        if candidate is None or id(candidate) in visited:
            return
        visited.add(id(candidate))

        # Unwrap StoryMemoryProvider or similar single-provider wrappers.
        inner = getattr(candidate, 'provider', None)
        if inner is not None and inner is not candidate:
            _collect(inner, role)
            return

        # Inspect RoutedAgentProvider role providers.
        if all(
            hasattr(candidate, attr)
            for attr in ('general_provider', 'robot_planner_provider', 'fallback_provider')
        ):
            _collect(candidate.general_provider, 'general')
            _collect(candidate.robot_planner_provider, 'robot_planner')
            _collect(candidate.fallback_provider, 'fallback')
            return

        # Inspect ReliableProvider circuits safely under its lock.
        sub_providers = getattr(candidate, '_providers', None)
        sub_circuits = getattr(candidate, '_circuits', None)
        lock = getattr(candidate, '_circuit_lock', None)
        if sub_providers is not None and sub_circuits is not None:
            if lock is not None:
                with lock:
                    states = [
                        (c.state.value, int(c.consecutive_failures))
                        for c in sub_circuits
                    ]
            else:
                states = [
                    (c.state.value, int(c.consecutive_failures))
                    for c in sub_circuits
                ]
            if states and all(state == 'open' for state, _ in states):
                degraded = True
            for sub, (state, failures) in zip(sub_providers, states):
                circuits.append({
                    'role': role,
                    'provider': str(getattr(sub, 'name', type(sub).__name__)),
                    'model': (
                        str(sub.model)
                        if isinstance(getattr(sub, 'model', None), str)
                        else None
                    ),
                    'state': state,
                    'consecutive_failures': failures,
                })

    _collect(provider, 'default')
    return {
        'degraded': degraded,
        'circuits': circuits,
    }
